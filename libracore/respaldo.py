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

    def __post_init__(self):
        self.bases = [Path(b) for b in self.bases]
        self.directorios = [Path(d) for d in self.directorios]
        if not self.bases and not self.postgres_url:
            raise ValueError("una instancia sin ninguna base no se puede respaldar")
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
        """Como se llama la base dentro del ZIP cuando la instancia es PostgreSQL."""
        return f"{self.nombre}.dump"

    @property
    def nombres_en_zip(self) -> set[str]:
        """Los nombres que `bases/` tiene que traer para ESTA instancia."""
        if self.postgres_url:
            return {self.nombre_dump}
        return {b.name for b in self.bases}


def _copiar_base(origen: Path, destino: Path) -> None:
    """Snapshot coherente de una base en uso, via la API de backup de SQLite.

    Si la base no existe todavia (una instancia recien creada que no arranco),
    no es un error: no hay nada que copiar y el backup sale sin ella.
    """
    if not origen.exists():
        return
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
        detalle = (r.stderr or "").strip().splitlines()
        raise BackupInvalido(
            f"No se pudo {que}: {binario} termino con codigo {r.returncode}. "
            f"{detalle[-1] if detalle else 'sin detalle en stderr'}"
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


def crear_backup(instancia: Instancia, destino_dir, motivo: str = "manual") -> Path:
    """Arma el ZIP y devuelve su ruta. Rota los viejos.

    El nombre lleva el motivo para que en el listado se distinga un backup que
    pidio el cliente de uno que se hizo solo antes de un restore — que es
    justo el que se busca cuando algo salio mal.
    """
    destino_dir = Path(destino_dir)
    destino_dir.mkdir(parents=True, exist_ok=True)
    _rotar(destino_dir)

    destino = _nombre_libre(destino_dir, motivo)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
            if instancia.postgres_url:
                # Sin `if copia.exists()`: en PostgreSQL no hay caso legitimo
                # de "todavia no existe". Si el dump no salio, el backup falla
                # en vez de producir un ZIP sin base.
                copia = tmp / instancia.nombre_dump
                _dump_postgres(instancia.postgres_url, copia)
                z.write(copia, f"bases/{instancia.nombre_dump}")
            for base in instancia.bases:
                copia = tmp / base.name
                _copiar_base(base, copia)
                if copia.exists():
                    z.write(copia, f"bases/{base.name}")
            for carpeta in instancia.directorios:
                if not carpeta.is_dir():
                    continue
                for raiz, _, archivos in os.walk(carpeta):
                    for a in archivos:
                        completo = Path(raiz) / a
                        relativo = completo.relative_to(carpeta.parent)
                        z.write(completo, f"datos/{relativo}")
    return destino


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


def restaurar_backup(
    instancia: Instancia,
    contenido: bytes,
    backups_dir,
    cerrar_conexiones=None,
    reabrir_conexiones=None,
) -> dict:
    """Reemplaza las bases y los directorios de la instancia por los del ZIP.

    **Antes de tocar nada hace un backup del estado actual**, y no en un
    `try/except` que se lo trague: si esa copia falla, el restore no arranca.
    Restaurar es la operacion mas destructiva que el cliente puede disparar
    solo desde una pantalla, y sin red no se hace.

    🔴 **`cerrar_conexiones` / `reabrir_conexiones` no son opcionales en la
    practica, aunque la firma los deje pasar.** Reemplazar el archivo mientras
    el proceso lo tiene abierto **no hace nada visible**: en Linux el descriptor
    sigue apuntando al inodo viejo, asi que la app sigue sirviendo la base
    ANTERIOR hasta que alguien reinicie el contenedor. El endpoint devuelve
    `ok`, la pantalla dice que salio bien, y los datos son los de antes.

    Lo encontro un test de LibraDesk que restauraba y despues preguntaba por
    los clientes: seguian los de despues del backup.

    El producto pasa lo que corresponda a su capa de datos — `engine.dispose`
    en los que usan SQLAlchemy, cerrar y reabrir la conexion en los que usan
    sqlite3 crudo.
    """
    z = _validar(contenido, instancia)

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

        if instancia.postgres_url:
            entrante = tmp / "bases" / instancia.nombre_dump
            # Mismo orden que en el camino SQLite: **validar antes de tocar
            # nada**. `pg_restore --list` lee el indice del dump sin ejecutar
            # una sola sentencia, asi que es el equivalente exacto del
            # `PRAGMA integrity_check`: si el archivo esta cortado o no es un
            # dump, se sabe ahora y no a mitad del restore.
            with open(entrante, "rb") as f:
                if f.read(len(_MAGIC_PGDUMP)) != _MAGIC_PGDUMP:
                    raise BackupInvalido(
                        f"{instancia.nombre_dump} no es un dump de PostgreSQL valido."
                    )
            _correr_pg(
                "pg_restore", ["--list", str(entrante)], instancia.postgres_url,
                "leer el dump del backup",
            )

            # `--clean --if-exists` borra cada objeto del dump antes de
            # recrearlo. NO es lo mismo que reemplazar el archivo en SQLite:
            # una tabla que exista en la base y no en el dump sobrevive. Se
            # deja asi a proposito —vaciar el schema entero convierte cualquier
            # fallo a mitad de camino en perdida total— y el backup previo
            # obligatorio de arriba es la red para ese caso.
            _correr_pg(
                "pg_restore",
                ["--clean", "--if-exists", "--no-owner", "--no-privileges",
                 "--single-transaction", str(entrante)],
                instancia.postgres_url,
                "restaurar la base PostgreSQL",
            )
            restauradas.append(instancia.nombre_dump)

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

        for carpeta in instancia.directorios:
            entrante = tmp / "datos" / carpeta.name
            if not entrante.is_dir():
                continue
            if carpeta.exists():
                shutil.rmtree(carpeta)
            shutil.move(str(entrante), str(carpeta))

    # Despues de mover: el pool vuelve a abrir contra el archivo nuevo. Va
    # fuera del `with` del temporal pero dentro del flujo normal — si esto no
    # corre, el restore no tuvo efecto para el proceso en curso.
    if reabrir_conexiones is not None:
        reabrir_conexiones()

    return {
        "ok": True,
        "bases_restauradas": restauradas,
        "backup_previo": Path(previo).name,
    }
