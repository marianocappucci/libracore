"""Backup y restore de una instancia completa, para que el cliente pueda
bajarse una copia de SUS datos desde la pantalla de Configuracion.

## Por que "instancia" y no "la base"

[[contalibra]] resuelve esto bajando **un archivo `.db`**, y eso alcanza ahi
porque tiene una sola base. Copiar ese endpoint a los otros cuatro productos
habria dado un backup que **pierde datos en silencio**, que es peor que no
tener backup: el cliente se baja el archivo, cree que tiene todo, y no lo
tiene.

Lo que hay en cada producto, medido el 2026-08-05:

| Producto | Bases | Otros archivos |
|---|---|---|
| Contalibra / Restolibra | 1 | logos, certificados ARCA |
| LibraDesk | 1 | logos |
| Gestiolibra | **2** (dominio + libracore) | logos |
| MedLibra | **2** | logos + **documentos clinicos** |
| VentaLibra | **2** | logos |

En tres de ellos `usuarios` vive en una base SEPARADA de la del dominio. Un
backup de una sola de las dos no se puede restaurar: o volves el dominio y te
quedan usuarios de otro momento, o al reves. Y en MedLibra los estudios y las
interconsultas subidas son archivos en disco — un backup "de la base" los deja
afuera enteros.

Por eso la unidad es la **instancia**: todas sus bases y todos sus directorios
de datos, en un ZIP.

## Por que no `shutil.copy2` despues de un checkpoint

Es lo que hace Contalibra hoy y tiene una carrera real: entre el
`wal_checkpoint` y la copia puede entrar una escritura, y el archivo copiado
queda inconsistente. No falla ruidosamente — da un `.db` que abre y al que le
falta la ultima transaccion, o peor.

Aca se usa la **API de backup online de SQLite** (`Connection.backup()`), que
existe justamente para copiar una base en uso: toma un snapshot coherente sin
bloquear a los que escriben. Es una linea menos de codigo y no tiene carrera.

## Instancias sobre PostgreSQL

Desde el 2026-08-09 una instancia puede correr sobre PostgreSQL en vez de
SQLite (ver la migracion de la familia). Ahi no hay archivo que copiar, asi que
la base va al ZIP como un **dump en formato custom de `pg_dump`**, y el restore
la repone con `pg_restore`.

El resto no cambia: mismo ZIP, mismos directorios de datos, misma rotacion, el
mismo backup previo obligatorio antes de restaurar.

🔴 **Antes de esto, una instancia sobre PostgreSQL producia un backup VACIO y
no se quejaba.** El producto pasaba `make_url(url).database` como si fuera una
ruta —en PostgreSQL eso es el NOMBRE de la base, no un archivo— y
`_copiar_base` tiene un `if not origen.exists(): return` pensado para
instancias recien creadas. El cliente se bajaba un ZIP con los logos y sin
datos, y recien se enteraba al intentar restaurar. Por eso ahora, cuando la
instancia declara `postgres_url`, que el dump falle es un **error ruidoso**.
"""
import datetime
import io
import os
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# Cuantos backups automaticos se conservan antes de empezar a borrar los mas
# viejos. Diez cubre varios dias de una instancia que hace uno por dia y
# alguno manual; mas que eso es llenar el disco del VPS, que ya viene ajustado.
MAX_BACKUPS = 10

#: Directorios que NUNCA entran al ZIP, esten donde esten dentro de los
#: `directorios` de la instancia.
#:
#: 🔴 `.resguardo` guarda el token de la nube del cliente
#: (`libracore.resguardo_enlace`). Si entrara, cada backup descargado llevaria
#: el acceso a su Drive o su Dropbox — y un backup se manda por mail. No se
#: confia en que el directorio de backups quede afuera de lo que declara cada
#: producto: se poda por nombre, sin importar donde aparezca.
NUNCA_EN_EL_ZIP = frozenset({".resguardo"})

_MAGIC_SQLITE = b"SQLite format 3\x00"
# Los dumps de `pg_dump -Fc` empiezan con esta firma. Sirve para el mismo
# chequeo que `_MAGIC_SQLITE`: que el archivo del ZIP sea lo que dice ser antes
# de dejarlo tocar la base.
_MAGIC_PGDUMP = b"PGDMP"


@dataclass
class Instancia:
    """Que compone una instancia, para respaldarla entera.

    `bases` son rutas a archivos SQLite; `directorios` son carpetas de datos
    (logos, certificados, documentos). Los dos son listas porque **la mitad de
    la familia tiene mas de uno de cada** — ver el docstring del modulo.

    `nombre` sale en el nombre del archivo que baja el cliente.
    """

    nombre: str
    bases: list[Path] = field(default_factory=list)
    directorios: list[Path] = field(default_factory=list)
    postgres_url: str | None = None
    #: Bases PostgreSQL **adicionales**. Media familia tiene mas de una: en
    #: [[gestiolibra]] y [[medlibra]] el dominio y LibraCore no pueden compartir
    #: schema —los dos declaran una tabla `clients` con `id` de tipos
    #: incompatibles— asi que al cortar quedan como dos bases en el mismo
    #: servidor, igual que eran dos archivos. Un backup que traiga una sola no
    #: se puede restaurar: o volves el dominio y te quedan usuarios de otro
    #: momento, o al reves.
    postgres_extra: list[str] = field(default_factory=list)

    def __post_init__(self):
        # 🔴 Antes de convertir a `Path`, que se come la doble barra: una URL en
        # `bases` es el cableado roto que dejo a VentaLibra con backups de 0
        # entradas (medido el 2026-09-17 en `ventalibra-dev`). `Path` la
        # aceptaba, `_copiar_base` no encontraba el "archivo" y el ZIP salia
        # vacio sin que nada fallara.
        for b in self.bases:
            if "://" in str(b):
                raise ValueError(
                    f"`bases` son rutas a archivos SQLite y recibio una URL "
                    f"({str(b).split('://', 1)[0]}://...): una base PostgreSQL va en "
                    "`postgres_url` (la principal) o en `postgres_extra`"
                )
        self.bases = [Path(b) for b in self.bases]
        self.directorios = [Path(d) for d in self.directorios]
        # Antes que la de "sin ninguna base": las dos aplican a una instancia
        # que solo trae `postgres_extra`, y este mensaje dice que arreglar.
        if self.postgres_extra and not self.postgres_url:
            raise ValueError(
                "postgres_extra sin postgres_url: la principal es la que da el "
                "nombre del dump y la que se restaura primero"
            )
        if not self.bases and not self.postgres_url:
            raise ValueError("una instancia sin ninguna base no se puede respaldar")
        if self.postgres_url:
            # 🔴 Dos bases que producen el MISMO nombre dentro del ZIP.
            #
            # Pasa cuando la principal no es la que se cree: `dumps` nombra a la
            # principal por `nombre` y a las extra por su base, asi que si la
            # principal fuera `gestiolibra_core` y la extra `gestiolibra`, las
            # dos saldrian como `gestiolibra.dump`. Una pisaria a la otra en el
            # ZIP y `nombres_en_zip` —que es un set— tendria un solo elemento,
            # asi que **la verificacion pasaria igual**: un backup con una sola
            # mitad, dado por bueno.
            #
            # El orden depende de en que orden aparecen las variables en el
            # contenedor cuando lo arma `provisioning.panel_admin`, o sea del
            # compose. Es exactamente la clase de cosa que anda hasta el dia que
            # alguien reordena dos lineas.
            nombres = [n for _, n in self.dumps]
            repetidos = sorted({n for n in nombres if nombres.count(n) > 1})
            if repetidos:
                raise ValueError(
                    f"dos bases de esta instancia caerian en el mismo archivo "
                    f"del backup ({repetidos}): la principal tiene que ser la "
                    f"base del dominio, no una de las adicionales"
                )
        if self.bases and self.postgres_url:
            # No es una limitacion tecnica, es que no existe el caso y dejarlo
            # pasar esconde un error de cableado: un producto que pasa las dos
            # cosas casi seguro esta pasando la ruta SQLite vieja ademas de la
            # URL nueva, y el ZIP saldria con una base de cada momento.
            raise ValueError(
                "una instancia es SQLite o PostgreSQL, no las dos: "
                f"bases={self.bases} y postgres_url tambien esta puesta"
            )

    @property
    def nombre_dump(self) -> str:
        """Como se llama la base principal dentro del ZIP."""
        return f"{self.nombre}.dump"

    @property
    def dumps(self) -> list[tuple[str, str]]:
        """`(url, nombre en el ZIP)` por cada base PostgreSQL de la instancia.

        La principal conserva `{nombre}.dump` **a proposito**: es como se llaman
        las bases dentro de los backups que ya existen, y cambiarlo dejaria sin
        restaurar los ZIP que ya bajaron los clientes. Las adicionales se
        nombran por su base, que es lo unico que las distingue.
        """
        if not self.postgres_url:
            return []
        salida = [(self.postgres_url, self.nombre_dump)]
        for url in self.postgres_extra:
            # Por el nombre de la BASE y no `{nombre}_{base}`: los nombres de
            # base son unicos en el servidor, y `medlibra_medlibra_core.dump`
            # no le dice nada a nadie.
            base = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            salida.append((url, f"{base}.dump"))
        return salida

    @property
    def nombres_en_zip(self) -> set[str]:
        """Los nombres que `bases/` tiene que traer para ESTA instancia."""
        if self.postgres_url:
            return {nombre for _, nombre in self.dumps}
        return {b.name for b in self.bases}


def _copiar_base(origen: Path, destino: Path) -> None:
    """Snapshot coherente de una base en uso, via la API de backup de SQLite.

    🔴 **Una base declarada que no existe es un error, no un caso a saltear.**
    Hasta el 2026-09-17 volvia en silencio, pensado para "una instancia recien
    creada que todavia no arranco". Ese `return` es el que produjo backups
    vacios dos veces: el 2026-08-09 (un producto pasaba el nombre de la base
    PostgreSQL como ruta) y el 2026-09-17 (VentaLibra pasaba las URLs en
    `bases`, y su pantalla bajaba ZIPs de 0 entradas). Un backup que falla se
    ve; uno que sale sin la base, no.
    """
    if not origen.exists():
        raise BackupInvalido(
            f"No se puede hacer el backup: la base {origen} no existe. Si la "
            f"instancia corre sobre PostgreSQL, la base va en `postgres_url`, no en `bases`."
        )
    src = sqlite3.connect(f"file:{origen}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(destino))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _conninfo(url: str) -> tuple[str, dict]:
    """Parte una URL de SQLAlchemy en lo que entienden `pg_dump`/`pg_restore`.

    Devuelve `(url_sin_password, entorno)`. La contraseña sale de la URL y va
    por `PGPASSWORD`: en la linea de comandos quedaria visible en `ps` para
    cualquier proceso del host, que es exactamente la clase de filtracion que
    ya nos costo una rotacion de credenciales.

    Tambien saca el `+psycopg` del esquema: eso lo entiende SQLAlchemy, libpq
    no.
    """
    from urllib.parse import urlsplit, urlunsplit

    partes = urlsplit(url.replace("postgresql+psycopg://", "postgresql://", 1))
    entorno = dict(os.environ)
    if partes.password:
        entorno["PGPASSWORD"] = partes.password
    usuario = f"{partes.username}@" if partes.username else ""
    puerto = f":{partes.port}" if partes.port else ""
    limpia = urlunsplit((
        partes.scheme,
        f"{usuario}{partes.hostname or ''}{puerto}",
        partes.path,
        "",
        "",
    ))
    return limpia, entorno


def _correr_pg(binario: str, argumentos: list[str], url: str, que: str) -> None:
    """Corre `pg_dump`/`pg_restore` y **falla ruidosamente** si algo sale mal.

    Los dos modos de fallar que importan tienen mensaje propio, porque los dos
    se leen mal por defecto: el binario que no esta instalado (se veria como un
    `FileNotFoundError` crudo en medio de un backup) y el que existe pero
    devuelve error (cuya causa esta en stderr, no en el codigo de salida).
    """
    conninfo, entorno = _conninfo(url)
    try:
        r = subprocess.run(
            [binario, "-d", conninfo, *argumentos],
            env=entorno, capture_output=True, text=True,
        )
    except FileNotFoundError:
        raise BackupInvalido(
            f"No se puede {que}: falta `{binario}` en esta imagen. Una instancia "
            f"sobre PostgreSQL lo necesita (paquete postgresql-client)."
        )
    if r.returncode != 0:
        # Todas las lineas de error y no la ultima: ver `errores_de_stderr`.
        from .respaldo_postgres import errores_de_stderr

        raise BackupInvalido(
            f"No se pudo {que}: {binario} termino con codigo {r.returncode}. "
            f"{errores_de_stderr(r.stderr)}"
        )


def _dump_postgres(url: str, destino: Path) -> None:
    """Snapshot de la base entera, en el formato custom de `pg_dump`.

    Es el equivalente de `Connection.backup()` de SQLite: `pg_dump` corre en
    una transaccion y da una foto coherente sin bloquear a los que escriben.

    `-Fc` (custom) y no `-Fp` (SQL plano) porque es lo que `pg_restore` sabe
    leer selectivamente, viene comprimido, y **se puede validar sin ejecutarlo**
    — eso es lo que reemplaza al `PRAGMA integrity_check` del camino SQLite.
    """
    _correr_pg(
        "pg_dump",
        ["--format=custom", "--no-owner", "--no-privileges", "--file", str(destino)],
        url,
        "hacer el backup de la base PostgreSQL",
    )


def crear_backup(
    instancia: Instancia, destino_dir, motivo: str = "manual", dump_fn=None,
) -> Path:
    """Arma el ZIP y devuelve su ruta. Rota los viejos.

    El nombre lleva el motivo para que en el listado se distinga un backup que
    pidio el cliente de uno que se hizo solo antes de un restore — que es
    justo el que se busca cuando algo salio mal.

    `dump_fn(url, destino)` existe para que **el cron del host arme el mismo
    ZIP** que arma la app. Los dos necesitan un `pg_dump`, pero no pueden
    correr el mismo:

    - Desde adentro del contenedor (la app) alcanza con `_dump_postgres`, que
      llama al binario contra la URL.
    - Desde el host (`provisioning.panel_admin`) eso **no funciona**: el sidecar
      no publica puerto —a proposito, publicar 5432 en un VPS es publicarlo a
      Internet— y su nombre es un alias de la red de Docker que afuera no
      resuelve. Ahi hay que correr `pg_dump` DENTRO del sidecar y traerse el
      archivo con `docker cp`.

    Inyectarlo es lo que evita tener dos armadores de backup distintos, que es
    exactamente el problema que este parametro vino a cerrar: hasta el
    2026-08-12 el cron armaba un `tar.gz` propio que la pantalla no listaba y el
    cliente no podia restaurar.
    """
    dump = dump_fn or _dump_postgres
    destino_dir = Path(destino_dir)
    destino_dir.mkdir(parents=True, exist_ok=True)
    _rotar(destino_dir)

    destino = _nombre_libre(destino_dir, motivo)
    try:
        _escribir_zip(instancia, destino, dump)
    except BaseException:
        # El ZIP se abre antes de copiar las bases: si una falla, queda en
        # disco un archivo con nombre de backup y adentro lo que alcanzo a
        # entrar. La rotacion lo contaria como uno de los diez.
        destino.unlink(missing_ok=True)
        raise
    return destino


def _escribir_zip(instancia: Instancia, destino: Path, dump) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
            # Sin `if copia.exists()`: en PostgreSQL no hay caso legitimo de
            # "todavia no existe". Si un dump no salio, el backup falla en vez
            # de producir un ZIP al que le falta una base.
            for url, nombre_en_zip in instancia.dumps:
                copia = tmp / nombre_en_zip
                dump(url, copia)
                z.write(copia, f"bases/{nombre_en_zip}")
            for base in instancia.bases:
                copia = tmp / base.name
                _copiar_base(base, copia)
                if copia.exists():
                    z.write(copia, f"bases/{base.name}")
            for carpeta in instancia.directorios:
                if not carpeta.is_dir():
                    continue
                for raiz, subdirs, archivos in os.walk(carpeta):
                    # Poda en el lugar: `os.walk` no entra a lo que se saca de
                    # `subdirs`. Ver `NUNCA_EN_EL_ZIP`.
                    subdirs[:] = [s for s in subdirs if s not in NUNCA_EN_EL_ZIP]
                    for a in archivos:
                        completo = Path(raiz) / a
                        relativo = completo.relative_to(carpeta.parent)
                        z.write(completo, f"datos/{relativo}")


def verificar_backup(destino, instancia: Instancia) -> dict:
    """Abre el ZIP recien hecho y confirma que trae las bases, con contenido.

    **No alcanza con que `crear_backup` no haya tirado excepcion.** El historial
    de este modulo es una lista de backups que salieron "bien" y estaban vacios:
    el `if not origen.exists()` que se saltaba la base en silencio, el
    `if db_src.exists()` del cron, el dump de 0 bytes que quedaba con nombre de
    backup. Todos devolvian exito.

    Por eso el chequeo mira el **producto**, no el proceso: que en `bases/`
    esten exactamente las que esta instancia declara, y que ninguna pese cero.
    Devuelve `{"archivo", "tamano_mb", "bases": {nombre: bytes}}` y levanta
    `BackupInvalido` si algo no cierra.
    """
    destino = Path(destino)
    esperadas = instancia.nombres_en_zip
    with zipfile.ZipFile(destino) as z:
        info = {
            Path(i.filename).name: i.file_size
            for i in z.infolist()
            if i.filename.startswith("bases/") and not i.is_dir()
        }
    faltan = esperadas - set(info)
    if faltan:
        raise BackupInvalido(
            f"El backup quedo sin {sorted(faltan)}: trae {sorted(info)} y esta "
            f"instancia declara {sorted(esperadas)}."
        )
    vacias = sorted(n for n, tam in info.items() if tam == 0)
    if vacias:
        raise BackupInvalido(
            f"El backup trae {vacias} con 0 bytes. Un archivo con nombre de "
            f"backup y nada adentro es peor que ninguno: la rotacion lo cuenta."
        )
    return {
        "archivo": destino.name,
        "tamano_mb": round(destino.stat().st_size / 1_048_576, 2),
        "bases": info,
    }


def _nombre_libre(destino_dir: Path, motivo: str) -> Path:
    """Nombre que no pise uno existente.

    El timestamp tiene resolucion de **segundo**, asi que dos backups del mismo
    segundo colisionan — y sin este desempate el segundo sobreescribe al
    primero **en silencio**. Pasa de verdad en dos casos: el cliente que
    aprieta "Backup rapido" dos veces, y el backup automatico previo a un
    restore cayendo en el mismo segundo que uno manual. Justo ese es el que no
    se puede perder.
    """
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    destino = destino_dir / f"backup_{motivo}_{ts}.zip"
    n = 2
    while destino.exists():
        destino = destino_dir / f"backup_{motivo}_{ts}_{n}.zip"
        n += 1
    return destino


def _rotar(destino_dir: Path) -> None:
    backups = sorted(
        (f for f in os.listdir(destino_dir) if f.endswith(".zip")), reverse=True,
    )
    for viejo in backups[MAX_BACKUPS - 1:]:
        try:
            (destino_dir / viejo).unlink()
        except OSError:
            pass


def listar_backups(destino_dir) -> list[dict]:
    """Del mas reciente al mas viejo. Si no hay carpeta, no hay backups —
    no es un error, es una instancia que todavia no hizo ninguno."""
    destino_dir = Path(destino_dir)
    if not destino_dir.is_dir():
        return []
    filas = []
    for nombre in sorted(os.listdir(destino_dir), reverse=True):
        if not nombre.endswith(".zip"):
            continue
        st = (destino_dir / nombre).stat()
        filas.append({
            "filename": nombre,
            "size_mb": round(st.st_size / 1_048_576, 2),
            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return filas


class BackupInvalido(Exception):
    """El archivo que subieron no sirve para restaurar. El mensaje va tal cual
    a la pantalla, asi que dice **que** esta mal y no solo que fallo."""


def _validar(contenido: bytes, instancia: Instancia) -> zipfile.ZipFile:
    try:
        z = zipfile.ZipFile(io.BytesIO(contenido))
    except zipfile.BadZipFile:
        raise BackupInvalido(
            "El archivo no es un backup de esta aplicacion (se esperaba un .zip)."
        )
    if z.testzip() is not None:
        raise BackupInvalido("El archivo esta dañado.")

    bases_en_zip = {Path(n).name for n in z.namelist() if n.startswith("bases/")}
    if not bases_en_zip:
        raise BackupInvalido("El backup no contiene ninguna base de datos.")

    # Que sea un backup DE ESTE PRODUCTO. Sin este chequeo, restaurar el
    # backup de otro producto de la familia deja la instancia con las bases de
    # otro sistema — y como los nombres de archivo se parecen, es un error
    # facil de cometer y dificil de deshacer.
    esperadas = instancia.nombres_en_zip
    ajenas = bases_en_zip - esperadas
    if ajenas:
        raise BackupInvalido(
            f"El backup es de otro sistema: contiene {sorted(ajenas)} y "
            f"esta instancia usa {sorted(esperadas)}."
        )

    for nombre in z.namelist():
        # Zip slip: una entrada con `..` o ruta absoluta escribiria fuera del
        # destino al extraer. El archivo lo sube un admin, pero un admin
        # tambien puede estar restaurando algo que le mandaron.
        p = Path(nombre)
        if p.is_absolute() or ".." in p.parts:
            raise BackupInvalido(f"El backup tiene una ruta invalida: {nombre}")
    return z


def restaurar(
    instancia: Instancia,
    origen,
    backups_dir,
    *,
    migraciones=None,
    cerrar_conexiones=None,
    reabrir_conexiones=None,
) -> dict:
    """**El** restore de una instancia. La unica puerta.

    La llaman, sin logica propia, la pantalla de Configuracion de los productos
    (`config_router`, via `restaurar_backup`) y `panel_admin.py restore-db`
    (via `python -m libracore.respaldo restaurar`, dentro del contenedor de la
    app). Hasta el 2026-09-17 eran dos caminos que no compartian una linea, y
    el segundo ni siquiera sabia restaurar PostgreSQL.

    `origen` es el ZIP: sus bytes (lo que sube la pantalla) o su ruta (lo que
    deja el cron en `data/backups/`, o lo que se bajo de Drive/Dropbox).

    **Antes de tocar nada hace un backup del estado actual**, y no en un
    `try/except` que se lo trague: si esa copia falla, el restore no arranca.

    En una instancia PostgreSQL:

    - `migraciones` son las cadenas declaradas en el `configure()` del
      producto. Con `None` se leen de la imagen (`migraciones_de_la_imagen`); si
      no se pueden leer, **aborta antes de tocar**. Restaurar sin migrar deja
      una base que la app en curso no sabe leer —o que no la deja arrancar—.
    - El dump se restaura en bases temporales, las migraciones corren contra
      ellas, y recien al final se intercambian por nombre. Ver
      `libracore.respaldo_postgres`.

    🔴 **`cerrar_conexiones` / `reabrir_conexiones` no son opcionales en la
    practica, aunque la firma los deje pasar.** En SQLite reemplazar el archivo
    con el proceso abierto no hace nada visible: el descriptor sigue en el
    inodo viejo y la app sirve la base ANTERIOR. En PostgreSQL el pool queda
    con conexiones a una base que cambio de nombre. El producto pasa lo que
    corresponda a su capa de datos — `engine.dispose` en los que usan
    SQLAlchemy.
    """
    contenido = origen if isinstance(origen, (bytes, bytearray)) else Path(origen).read_bytes()
    z = _validar(bytes(contenido), instancia)
    if instancia.postgres_url:
        return _restaurar_postgres(
            instancia, z, backups_dir, migraciones, cerrar_conexiones, reabrir_conexiones,
        )
    return _restaurar_sqlite(instancia, z, backups_dir, cerrar_conexiones, reabrir_conexiones)


def restaurar_backup(
    instancia: Instancia,
    contenido: bytes,
    backups_dir,
    cerrar_conexiones=None,
    reabrir_conexiones=None,
    migraciones=None,
) -> dict:
    """La firma que usa `config_router` desde v1.11.0. Es `restaurar`, nada mas."""
    return restaurar(
        instancia, contenido, backups_dir, migraciones=migraciones,
        cerrar_conexiones=cerrar_conexiones, reabrir_conexiones=reabrir_conexiones,
    )


def _restaurar_postgres(instancia, z, backups_dir, migraciones, cerrar_conexiones, reabrir_conexiones) -> dict:
    from . import respaldo_postgres as rp

    # Las migraciones se resuelven PRIMERO: si la imagen no las declara, no hay
    # restore posible y no tiene sentido ni hacer el backup previo.
    if migraciones is None:
        migraciones = migraciones_de_la_imagen()

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        z.extractall(tmp)

        # Validar TODAS las bases antes de tocar la primera. `pg_restore --list`
        # lee el indice del dump sin ejecutar una sola sentencia: si el archivo
        # esta cortado o no es un dump, se sabe ahora y no a mitad del restore.
        for url, nombre_en_zip in instancia.dumps:
            entrante = tmp / "bases" / nombre_en_zip
            if not entrante.exists():
                raise BackupInvalido(
                    f"El backup no trae {nombre_en_zip}: esta instancia necesita "
                    f"{sorted(instancia.nombres_en_zip)} para restaurarse entera."
                )
            with open(entrante, "rb") as f:
                if f.read(len(_MAGIC_PGDUMP)) != _MAGIC_PGDUMP:
                    raise BackupInvalido(f"{nombre_en_zip} no es un dump de PostgreSQL valido.")
            _correr_pg("pg_restore", ["--list", str(entrante)], url, "leer el dump del backup")

        try:
            cliente = rp.version_pg_restore()
            for url, _ in instancia.dumps:
                servidor = rp.version_servidor(url)
                if cliente > servidor:
                    raise BackupInvalido(
                        f"pg_restore {cliente} no puede restaurar contra un servidor PostgreSQL "
                        f"{servidor}: la imagen tiene que traer el cliente de la misma major "
                        f"que el servidor."
                    )
        except rp.ErrorDeRestore as exc:
            raise BackupInvalido(f"No se puede restaurar: {exc}") from exc

        # Cada base tiene que estar nombrada por alguna variable de entorno, o
        # la migracion no ve su temporal. Ver `rp.bases_sin_variable`.
        if migraciones:
            sin_variable = rp.bases_sin_variable([url for url, _ in instancia.dumps])
            if sin_variable:
                raise BackupInvalido(
                    "No se puede restaurar: ninguna variable de entorno apunta a "
                    f"{', '.join(sin_variable)}, asi que las migraciones no correrian "
                    "contra la base restaurada. La URL de la base tiene que estar en el "
                    "entorno del proceso, no solo en la configuracion de la app."
                )

        rp.vigilar_pools()
        previo = crear_backup(instancia, backups_dir, motivo="antes_restore")

        pares: list[tuple[str, str]] = []
        cerradas = False
        try:
            for url, nombre_en_zip in instancia.dumps:
                temporal = rp.crear_temporal(url)
                pares.append((url, temporal))
                # Contra una base vacia recien creada: sin `--clean`. Con
                # `--exit-on-error` y `--single-transaction`, cualquier error
                # —una FK que los datos no cumplen, un dump de otra version—
                # corta en la primera sentencia y no deja nada a medias.
                _correr_pg(
                    "pg_restore",
                    ["--no-owner", "--no-privileges", "--single-transaction", "--exit-on-error",
                     str(tmp / "bases" / nombre_en_zip)],
                    temporal,
                    f"restaurar {nombre_en_zip}",
                )
            rp.correr_migraciones(migraciones, pares)

            if cerrar_conexiones is not None:
                cerrar_conexiones()
                cerradas = True
            try:
                anteriores = rp.intercambiar(pares)
            finally:
                # Salga bien o mal, el intercambio ya termino conexiones: los
                # pools del proceso tienen que abrir otras. Ver `rp.vigilar_pools`.
                rp.descartar_conexiones_anteriores()
        except Exception as exc:
            for _, temporal in pares:
                try:
                    rp.borrar_temporal(temporal)
                except Exception:  # noqa: BLE001 - limpiar no debe tapar la causa
                    pass
            if cerradas and reabrir_conexiones is not None:
                reabrir_conexiones()
            if isinstance(exc, BackupInvalido):
                raise
            if isinstance(exc, rp.ErrorDeRestore):
                raise BackupInvalido(f"No se restauro nada, la base sigue como estaba: {exc}") from exc
            raise

        _reponer_directorios(instancia, tmp)

    if reabrir_conexiones is not None:
        reabrir_conexiones()

    return {
        "ok": True,
        "bases_restauradas": [nombre for _, nombre in instancia.dumps],
        "backup_previo": Path(previo).name,
        "antes_restore": anteriores,
        "como_volver": rp.como_volver(anteriores),
    }


def _restaurar_sqlite(instancia, z, backups_dir, cerrar_conexiones, reabrir_conexiones) -> dict:
    """El camino de las instancias SQLite. Se conserva hasta que se decida
    retirarlo en su propia tanda: ningun producto corre hoy sobre SQLite."""
    previo = crear_backup(instancia, backups_dir, motivo="antes_restore")

    # Antes de mover: suelta los descriptores. En Linux evita el inodo
    # huerfano; en Windows es directamente la unica forma de reemplazar el
    # archivo.
    if cerrar_conexiones is not None:
        cerrar_conexiones()

    restauradas = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        z.extractall(tmp)

        # Se validan TODAS las bases antes de pisar la primera: a mitad de
        # camino la instancia queda mezclada y no hay vuelta atras automatica.
        for base in instancia.bases:
            entrante = tmp / "bases" / base.name
            if not entrante.exists():
                continue
            with open(entrante, "rb") as f:
                if f.read(len(_MAGIC_SQLITE)) != _MAGIC_SQLITE:
                    raise BackupInvalido(f"{base.name} no es una base SQLite valida.")
            conn = sqlite3.connect(str(entrante))
            try:
                estado = conn.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                conn.close()
            if estado != "ok":
                raise BackupInvalido(f"{base.name} tiene errores de integridad: {estado}")

        for base in instancia.bases:
            entrante = tmp / "bases" / base.name
            if not entrante.exists():
                continue
            base.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entrante), str(base))
            # Los sidecar del WAL describen transacciones de la base VIEJA. Si
            # quedan, SQLite los aplica sobre la nueva y la corrompe.
            for ext in ("-wal", "-shm"):
                sidecar = Path(str(base) + ext)
                if sidecar.exists():
                    try:
                        sidecar.unlink()
                    except OSError:
                        pass
            restauradas.append(base.name)

        _reponer_directorios(instancia, tmp)

    # Despues de mover: el pool vuelve a abrir contra el archivo nuevo. Si esto
    # no corre, el restore no tuvo efecto para el proceso en curso.
    if reabrir_conexiones is not None:
        reabrir_conexiones()

    return {
        "ok": True,
        "bases_restauradas": restauradas,
        "backup_previo": Path(previo).name,
    }


def _reponer_directorios(instancia: Instancia, extraido: Path) -> None:
    """Los directorios de datos se reemplazan enteros, sin merge."""
    for carpeta in instancia.directorios:
        entrante = extraido / "datos" / carpeta.name
        if not entrante.is_dir():
            continue
        if carpeta.exists():
            shutil.rmtree(carpeta)
        shutil.move(str(entrante), str(carpeta))


# ── Lo que necesita el restore cuando corre DENTRO del contenedor ────────────

def migraciones_de_la_imagen(raiz=".") -> tuple:
    """Las cadenas de migracion que declara el producto de ESTA imagen.

    Salen de `get_config().migraciones`, despues de importar el
    `scripts/panel_admin.py` que viaja en la imagen: es la misma declaracion
    que lee `panel_admin.py actualizar` para desplegar.

    🔴 **Se lee de la imagen y no del checkout del host.** El checkout del VPS
    esta en `develop`; la imagen, en lo que se desplego. El 2026-09-16 esa
    diferencia hizo que un deploy de `main` corriera una migracion que solo
    estaba en `develop`. Aca el restore corre con el codigo de la imagen, asi
    que las migraciones tienen que ser las de la imagen.

    Si no se pueden leer, **no se restaura**: medido el 2026-09-17, las
    imagenes de LibraCargo y LibraClub no traen `scripts/`.
    """
    import importlib
    import sys

    carpeta = str(Path(raiz).resolve())
    if carpeta not in sys.path:
        sys.path.insert(0, carpeta)
    try:
        importlib.import_module("scripts.panel_admin")
    except Exception as exc:  # noqa: BLE001 - cualquier falla es "no se sabe como migrar"
        raise BackupInvalido(
            "No se puede restaurar: no se pudieron leer las migraciones que declara el "
            f"producto (scripts/panel_admin.py en {carpeta}): {exc}. Restaurar sin "
            "migrar dejaria una base que esta version de la app no sabe leer."
        ) from exc
    from .provisioning import get_config

    migraciones = tuple(tuple(c) for c in get_config().migraciones)
    if not migraciones:
        raise BackupInvalido(
            "No se puede restaurar: el producto no declara migraciones en su configure()."
        )
    return migraciones


def principal_primero(urls: list[str], nombre: str) -> list[str]:
    """Pone adelante la base del DOMINIO, que es la que el producto respalda
    como principal.

    🔴 **No alcanza con respetar el orden en que vinieron.** Las URLs salen de
    las variables del contenedor, o sea del orden en que estan escritas en el
    compose. Si alguien reordena dos lineas, la principal pasaria a ser la de
    LibraCore y las dos bases caerian en el mismo archivo del ZIP.

    El criterio es el nombre: la base del dominio se llama igual que el
    producto (`gestiolibra`), la de LibraCore lleva sufijo (`gestiolibra_core`).
    Si ninguna coincide, se deja el orden como vino — no es un caso conocido, y
    reordenar a ciegas seria peor que no tocar nada.
    """
    def _base(url: str) -> str:
        return url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]

    exactas = [u for u in urls if _base(u) == nombre]
    if not exactas:
        return list(urls)
    return exactas + [u for u in urls if _base(u) != nombre]


def directorios_de_datos(data_dir: Path) -> list[Path]:
    """Las carpetas de `data/` que entran al ZIP: todas menos `backups/`.

    Todas y no una lista fija porque cada producto guarda cosas distintas ahi.
    `backups/` afuera porque es donde queda el propio ZIP: incluirla haria que
    cada backup se llevara adentro a los anteriores.
    """
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return []
    return sorted(d for d in data_dir.iterdir() if d.is_dir() and d.name != "backups")


def instancia_desde_entorno(nombre: str, datos, entorno: dict | None = None) -> Instancia:
    """La `Instancia` que arma `panel_admin._instancia_del_cliente` desde el
    host, armada desde adentro del contenedor.

    Las bases salen de las variables de entorno **por su valor** —toda la que
    sea una URL de PostgreSQL—, igual que `_urls_postgres_del_contenedor`: el
    nombre de la variable cambia de producto a producto. Tienen que dar los
    mismos nombres dentro del ZIP que el backup del cron, o `_validar` lo
    rechazaria como "de otro sistema".
    """
    from .respaldo_postgres import es_url_postgres

    urls: list[str] = []
    for valor in (os.environ if entorno is None else entorno).values():
        if es_url_postgres(valor):
            normal = valor.replace("postgresql+psycopg://", "postgresql://", 1)
            if normal not in urls:
                urls.append(normal)
    if not urls:
        raise BackupInvalido(
            "No se puede restaurar: el contenedor no declara ninguna URL de PostgreSQL."
        )
    principal, *extra = principal_primero(urls, nombre)
    return Instancia(
        nombre=nombre,
        postgres_url=principal,
        postgres_extra=extra,
        directorios=directorios_de_datos(datos),
    )


def main(argv=None) -> int:
    """`python -m libracore.respaldo restaurar --nombre N --zip RUTA --datos DIR`

    Es lo que corre `panel_admin.py restore-db` dentro de un contenedor efimero
    de la app, con la app parada. No tiene logica propia: arma la instancia
    desde el entorno y llama a `restaurar`.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m libracore.respaldo")
    sub = parser.add_subparsers(dest="comando", required=True)
    rest = sub.add_parser("restaurar", help="restaura la instancia desde un ZIP de backup")
    rest.add_argument("--nombre", required=True, help="el container_prefix del producto")
    rest.add_argument("--zip", required=True, help="ruta del ZIP, vista desde el contenedor")
    rest.add_argument("--datos", required=True, help="el directorio data/ de la instancia")
    args = parser.parse_args(argv)

    try:
        instancia = instancia_desde_entorno(args.nombre, args.datos)
        resultado = restaurar(instancia, Path(args.zip), Path(args.datos) / "backups")
    except BackupInvalido as exc:
        print(f"[ERROR] {exc}")
        return 1
    print(f"[OK] Restaurado desde {Path(args.zip).name}: {', '.join(resultado['bases_restauradas'])}")
    print(f"     Backup previo: {resultado['backup_previo']}")
    if resultado.get("antes_restore"):
        print(f"     Bases anteriores conservadas: {', '.join(resultado['antes_restore'])}")
        print(f"     Para volver a ellas: {resultado['como_volver']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
