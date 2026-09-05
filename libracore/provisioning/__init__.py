"""
Configuración compartida para `libracore.provisioning.nuevo_cliente` y
`libracore.provisioning.panel_admin` — las dos CLIs de gestión de clientes
(alta de cliente nuevo, panel de administración de contenedores) que hasta
ahora vivían duplicadas byte-a-byte en `scripts/nuevo_cliente.py` y
`scripts/panel_admin.py` de cada producto, salvo un puñado de constantes
(nombre de imagen Docker, prefijo de contenedor, nombre de archivo de DB —
ver wiki/entities/libracore.md).

Cada producto llama `configure()` una sola vez al principio de su script
(antes de usar cualquier función de estos módulos) — mismo patrón que
`libracore.db.core`/`libracore.admin.services`: estado global de proceso,
válido porque cada script/servicio corre en un proceso separado por
producto, nunca dos productos en el mismo intérprete.

`plans.py` (planes reales de cada producto, no genérico) y `npm_api.py`
(idéntico entre productos pero vive en `scripts/` de cada repo, no en
LibraCore) se resuelven en tiempo de ejecución vía imports diferidos, mismo
patrón que `libracore.admin.services::_plans()/_pa()/_nc()`.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# requirements.txt de cada producto depende de libracore (paquete interno
# privado, ver wiki/entities/libracore.md) vía git+ssh — el build necesita
# BuildKit + --ssh con la deploy key dedicada. Misma ruta/variable de
# entorno en todos los productos, no varía por producto.
LIBRACORE_SSH_KEY = os.environ.get(
    "LIBRACORE_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519_libracore")
)

# Desde que Contalibra/Restolibra (P7/P8) agregaron libracommerce como
# segunda dependencia privada, sus Dockerfile usan DOS mounts SSH con id
# propio (`id=libracore` / `id=libracommerce`, no el `id=default` viejo de
# un solo mount) — GitHub autentica toda la conexión SSH con la primera
# deploy key que el agente le ofrece, así que cada repo privado necesita su
# propio socket/id. Ver Dockerfile de esos productos y wiki/entities/*.
LIBRACOMMERCE_SSH_KEY = os.environ.get(
    "LIBRACOMMERCE_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519_libracommerce")
)

# libra-ui (paquete de frontend compartido, ver wiki/entities/libra-ui.md)
# es una dependencia de NPM (frontend/package.json), no de pip
# (requirements.txt/pyproject.toml) como libracore/libracommerce/libragenda
# — mismo motivo de fondo (repo privado propio, su propia deploy key) pero
# detectado con un grep distinto (ver _requiere_libra_ui).
LIBRA_UI_SSH_KEY = os.environ.get(
    "LIBRA_UI_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519_libra_ui")
)

# Gestiolibra/MedLibra dependen de libragenda (motor de turnos/agenda,
# tercer paquete interno privado de la familia, ver wiki/entities/
# libragenda.md) — misma necesidad de deploy key propia que libracommerce.
LIBRAGENDA_SSH_KEY = os.environ.get(
    "LIBRAGENDA_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519_libragenda")
)

# Los 5 productos dependen de libraauth desde la migracion de auth del
# 2026-07-30 (ver wiki/entities/libraauth.md), que saco `auth.py` y
# `db/usuarios.py` de este paquete. Faltaba aca: `docker_build_ssh_args()` no
# emitia su `--ssh`, asi que `cmd_actualizar` y sobre todo
# `nuevo_cliente.build_image` construian sin ese mount y el paso de pip de
# libraauth fallaba — o sea que **no se podia dar de alta una instancia nueva**
# en ningun producto ya migrado.
LIBRAAUTH_SSH_KEY = os.environ.get(
    "LIBRAAUTH_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519_libraauth")
)


def _como_se_declara(paquete: str) -> str:
    """El nombre del paquete tal como puede aparecer en una dependencia.

    🔴 **Con o sin extras.** `libracore` y `libracore[migrations]` son la misma
    dependencia; el segundo es el que trae alembic, y lo declaran cuatro
    productos —Contalibra, LibraCargo, Restolibra y VentaLibra— porque su deploy
    corre `libracore-migrar`.

    Existe porque los dos lectores de dependencias de este módulo se lo perdían,
    cada uno a su manera, y eso los dejaba **mudos**: `_pin_declarado` devolvía
    `None` para esos cuatro y `check_venv_sync` no avisaba nada. Medido el
    2026-09-02 sobre las ocho instalaciones del VPS: los cuatro que declaran el
    extra son exactamente los cuatro sin pin detectado.

    Es la tercera vez que este módulo se calla por leer la forma equivocada de
    una dependencia —antes fue `requirements.txt` contra `pyproject.toml`, dos
    veces— y por eso ahora la forma vive **acá y en ningún otro lado**. Si
    aparece un cuarto lector, que use esto.
    """
    return rf"{re.escape(paquete)}(?:\[[^\]]*\])?"


def _depende_de(repo_root: Path, paquete: str) -> bool:
    """True si el producto declara una dependencia de `paquete` (paquete
    interno privado) vía git+, ya sea en requirements.txt (Contalibra/
    Restolibra, formato pip clásico) o en pyproject.toml (Gestiolibra/
    MedLibra/VentaLibra, formato PEP 621 — ninguno de los tres tiene
    requirements.txt). Sin este segundo chequeo, `_requiere_libracommerce`
    nunca detectaba la dependencia real de VentaLibra (declarada en su
    pyproject.toml) y `docker_build_ssh_args()` no le pasaba la key de
    libracommerce — bug real, no atacado hasta este fix."""
    req_file = repo_root / "requirements.txt"
    if req_file.exists() and any(
        line.startswith(paquete)
        for line in req_file.read_text(encoding="utf-8").splitlines()
    ):
        return True
    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists() and re.search(
        _como_se_declara(paquete) + r"\s*@\s*git\+",
        pyproject.read_text(encoding="utf-8"),
    ):
        return True
    return False


def _requiere_libracommerce(repo_root: Path) -> bool:
    """True si el producto depende de libracommerce (requirements.txt o
    pyproject.toml) — separa el paso de instalación con su propia deploy
    key."""
    return _depende_de(repo_root, "libracommerce")


def _requiere_libragenda(repo_root: Path) -> bool:
    """True si el producto depende de libragenda (requirements.txt o
    pyproject.toml) — equivalente a _requiere_libracommerce."""
    return _depende_de(repo_root, "libragenda")


def _requiere_libraauth(repo_root: Path) -> bool:
    """True si el producto depende de libraauth (requirements.txt o
    pyproject.toml) — equivalente a _requiere_libracommerce."""
    return _depende_de(repo_root, "libraauth")


def _requiere_libra_ui(repo_root: Path) -> bool:
    """True si frontend/package.json del producto depende de libra-ui —
    equivalente a _requiere_libracommerce pero para la dependencia de NPM
    (no vive en requirements.txt/pyproject.toml)."""
    pkg_file = repo_root / "frontend" / "package.json"
    if not pkg_file.exists():
        return False
    return '"libra-ui"' in pkg_file.read_text(encoding="utf-8")


def docker_build_ssh_args(repo_root: Path) -> list[str]:
    """Arma los `--ssh id=key` para `docker build`, compartido entre
    `panel_admin.cmd_actualizar` y `nuevo_cliente.build_image` (mismo
    Dockerfile, mismo requisito de mounts SSH en ambos flujos de build).
    Se pasan `default` y `libracore` apuntando a la misma key (compat con
    Dockerfile viejos de un solo mount id y con los nuevos que ya usan
    `id=libracore` explícito);
    `libracommerce`/`libragenda`/`libraauth`/`libra-ui` solo
    si el producto depende de ese paquete (si no, la key puede ni existir
    en esta máquina). Pasar un `--ssh` de más para un id que el Dockerfile
    no monta es inofensivo (BuildKit lo ignora), pero pasar uno con una key
    inexistente rompe el build.

    `libraauth` se sumó el 2026-07-30, al cerrarse la migración de auth: sin
    él, los 5 productos (que ya lo declaran en `requirements.txt`) construían
    sin ese mount y `nuevo_cliente.build_image` no podía dar de alta una
    instancia nueva."""
    args = [
        "--ssh", f"default={LIBRACORE_SSH_KEY}",
        "--ssh", f"libracore={LIBRACORE_SSH_KEY}",
    ]
    if _requiere_libracommerce(repo_root):
        args += ["--ssh", f"libracommerce={LIBRACOMMERCE_SSH_KEY}"]
    if _requiere_libragenda(repo_root):
        args += ["--ssh", f"libragenda={LIBRAGENDA_SSH_KEY}"]
    if _requiere_libraauth(repo_root):
        args += ["--ssh", f"libraauth={LIBRAAUTH_SSH_KEY}"]
    if _requiere_libra_ui(repo_root):
        args += ["--ssh", f"libra-ui={LIBRA_UI_SSH_KEY}"]
    return args


def _pin_declarado(repo_root: Path) -> tuple[str, str] | None:
    """Devuelve `(version, archivo)` del pin `libracore @ git+…@vX.Y.Z` del
    producto, mirando **pyproject.toml y requirements.txt** — o None si no
    está declarado en ninguno, o no tiene ese formato.

    Devuelve también el archivo porque el aviso tiene que nombrar el que hay
    que editar de verdad: decir "requirements.txt" a un producto que ya no lo
    tiene manda a corregir un archivo inexistente.

    **`pyproject.toml` primero**, y no al revés, aunque el formato clásico sea
    el más viejo: el `Dockerfile` de estos productos hace `pip install .`, o
    sea que la versión que termina dentro de la imagen es la de `pyproject`.
    Restolibra tiene los dos archivos **con pines distintos** — `pyproject`
    en `v1.4.0` y un `requirements.txt` en `v1.2.0` que ya no consume nadie —
    y su contenedor corre `1.4.0`. Leyendo primero `requirements.txt` la
    guarda comparaba contra un pin muerto: probado el 2026-08-04, daba
    `1.2.0` para un producto que corre `1.4.0`.

    Los dos archivos, no uno: Contalibra, Gestiolibra, MedLibra y VentaLibra
    ya no tienen `requirements.txt` (migraron a PEP 621), y en Restolibra el
    archivo existe pero su única línea con "libracore" es un comentario.
    Mientras esto leyó sólo `requirements.txt` devolvía None en los cinco y
    `check_venv_sync` no avisaba **nunca**: el 2026-08-04 se lo ejecutó a
    mano sobre `/root/contalibra`, con el venv tres versiones desalineado, y
    devolvió None. La guarda no fallaba —miraba un archivo que ya no
    existe—, que es peor: no dice "todo bien", no dice nada, y nada se lee
    igual que todo bien.

    Es el **mismo** bug que `_depende_de()` ya tenía arreglado unas líneas
    más arriba (ver su docstring: leyendo sólo requirements.txt no detectaba
    la dependencia real de VentaLibra). Aquel arreglo no se propagó hasta
    acá. Si aparece un tercer lugar que resuelva dependencias declaradas,
    tiene que mirar los dos archivos también.

    Se renombró de `_pin_de_requirements`: el nombre viejo describía
    justamente la mitad que faltaba.
    """
    # En pyproject el pin es un elemento de lista: viene entre comillas y
    # con coma final (`"libracore @ git+https://…@v1.8.0",`), y puede traer
    # extras (`"libracore[migrations] @ git+…"`) — ver `_como_se_declara`.
    # Exigir `@ git+` es lo que evita agarrar los comentarios que nombran al
    # motor — en estos pyproject hay varios, justo encima de la línea del pin.
    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists():
        m = re.search(
            _como_se_declara("libracore") + r"""\s*@\s*git\+\S+?@v([0-9][^"'\s,]*)""",
            pyproject.read_text(encoding="utf-8"),
        )
        if m:
            return m.group(1), "pyproject.toml"

    req_file = repo_root / "requirements.txt"
    if req_file.exists():
        for line in req_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("libracore") and "@v" in line:
                return line.rsplit("@v", 1)[1].strip(), "requirements.txt"
    return None


def _tiene_alias_ssh(alias: str) -> bool:
    """True si `~/.ssh/config` declara `Host <alias>`."""
    cfg = Path.home() / ".ssh" / "config"
    try:
        contenido = cfg.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in contenido.splitlines():
        partes = line.strip().split()
        if len(partes) >= 2 and partes[0].lower() == "host" and alias in partes[1:]:
            return True
    return False


def url_instalacion_libracore(version: str) -> str:
    """URL `pip`-instalable de libracore, preferindo el alias SSH del repo
    si está declarado.

    En el VPS, GitHub se autentica con una **deploy key por repo** expuesta
    como alias en `~/.ssh/config` (`Host github-libracore`). Con el host
    plano `github.com` se usa la clave por defecto, que no tiene acceso al
    repo privado: el clone falla con `Repository not found`. Fuera del VPS
    (WSL local, donde se usa `gh` como credential helper) el alias no
    existe y el host plano es el correcto."""
    host = "github-libracore" if _tiene_alias_ssh("github-libracore") else "github.com"
    return f"git+ssh://git@{host}/marianocappucci/libracore.git@v{version}"


def check_venv_sync(repo_root: Path) -> str | None:
    """Compara la versión de `libracore` instalada en ESTE venv (el que
    corre `panel_admin.py`/`nuevo_cliente.py` en el host) contra el pin
    real del producto —`requirements.txt` o `pyproject.toml`, ver
    `_pin_declarado`— son dos lugares independientes: el pin fija qué
    versión se instala *dentro* de la imagen Docker en cada build, este
    venv es aparte y solo se actualiza con un `pip install --upgrade`
    manual (ver ONBOARDING_CLIENTES.md). Si un bump del pin no se
    replica acá, `panel_admin.py` sigue corriendo con lógica de
    provisioning vieja sin que nada lo avise — incidente real detectado
    2026-07-27 (ver wiki/entities/libracore.md, sección v0.23.0):
    `.venv-scripts` quedó 8 versiones atrás, enmascarado por caché de
    Docker hasta el primer build en frío real. Y volvió a pasar: el
    2026-08-04 los cinco venv del VPS estaban en 1.5.0 contra pines de
    1.2.0 a 1.8.0, sin que esta función avisara nada — leía sólo
    `requirements.txt`, que cuatro productos ya no tienen.

    Devuelve un mensaje de advertencia si difieren, o None si coinciden
    o no se puede determinar el pin (producto que no declara libracore
    en ninguno de los dos archivos). No aborta nada — es una advertencia
    para que la lea quien corre el comando, no un chequeo bloqueante."""
    import libracore

    declarado = _pin_declarado(repo_root)
    if declarado is None:
        return None
    pin, archivo = declarado
    installed = libracore.__version__.split("+")[0]  # sin sufijo local de hatch-vcs
    if installed == pin:
        return None
    return (
        f"[ADVERTENCIA] Este venv tiene libracore=={installed} instalado, "
        f"pero {archivo} de este producto pinea v{pin}. "
        "panel_admin.py puede estar corriendo logica de provisioning vieja "
        "(el problema real detectado el 2026-07-27, ver wiki/entities/libracore.md). "
        "Actualizar con:\n"
        f"  pip install --upgrade --no-cache-dir 'libracore @ {url_instalacion_libracore(pin)}'"
    )


@dataclass(frozen=True)
class ProductConfig:
    product_name: str        # nombre visible, ej. "CONTALIBRA"
    image_name: str          # ej. "contalibra:latest"
    container_prefix: str    # ej. "contalibra"
    db_filename: str         # ej. "contalibra.db"
    repo_root: Path
    base_port: int = 8071
    docs_auth_secret: str = ""

    # Ruta del endpoint de salud, para el healthcheck del compose que se le
    # genera a cada instancia.
    #
    # **Hoy los seis productos usan el default y ninguno pasa este parámetro.**
    # Existe por LibraDesk, que hasta el 2026-08-12 declaraba su salud sólo en
    # `/api/health`: el generador estampaba `/health` para todos, así que toda
    # instancia suya nacía con el healthcheck apuntado a una ruta que en ese
    # producto no existía. La contestaba el fallback de la SPA con un 200 y el
    # contenedor figuraba `healthy` con la API muerta (medido en
    # `libradesk-demo`). Ese producto normalizó su ruta y el parámetro quedó
    # como escape hatch, sin usuarios.
    #
    # > Si vuelve a aparecer un producto que necesite pasarlo, el que lo pase
    # > tiene que hacerlo en `nuevo_cliente.py` **y** en `panel_admin.py`:
    # > `configure()` pisa un `_cfg` global y `libracore.admin.services` importa
    # > los dos en el mismo proceso, así que gana el último import. Y conviene
    # > que su suite verifique que la ruta configurada sea una que su router
    # > sirva de verdad — con una SPA horneada, apuntar a una ruta inexistente
    # > no se ve como un 404.
    health_path: str = "/health"

    # — PostgreSQL de las instancias NUEVAS —
    #
    # `postgres=True` hace que `crear_cliente()` le genere a la instancia su
    # propio sidecar. **False significa que el producto sigue pariendo
    # instancias SQLite**, que es como estaba todo hasta el 2026-08-11, así que
    # los seis se migran de a uno sin romper a los demás.
    postgres: bool = False

    # `True` sólo donde LibraCore necesita su PROPIA base (Gestiolibra y
    # MedLibra). No es preferencia: LibraCore y LibraGenda declaran los dos una
    # tabla `clients` con `id` de tipos incompatibles, así que en un solo schema
    # el segundo `CREATE TABLE IF NOT EXISTS` no hace nada y después PostgreSQL
    # rechaza las nueve FK que apuntan a `clients(id)`. Dos bases es la
    # traducción fiel de los dos archivos SQLite que había antes.
    #
    # Donde es `False` las dos variables apuntan a la MISMA base — que es lo que
    # pasa en VentaLibra, donde LibraCommerce y LibraCore sí conviven.
    base_core_separada: bool = False

    # `True` hace que el backup del cron (`panel_admin.cmd_backup`) arme **el
    # mismo ZIP que arma la app** en vez de su propio `tar.gz`.
    #
    # Es un interruptor de transicion, con el mismo criterio que `postgres` acá
    # arriba: sólo pueden prenderlo los productos cuya pantalla de Backups sale
    # de `libracore.respaldo` —Gestiolibra, MedLibra, VentaLibra y LibraDesk—,
    # porque son los que listan y restauran ese ZIP. Contalibra y Restolibra
    # tienen implementación propia (`app/web/routers/config.py`) que filtra por
    # `.db`/`.dump`: prenderlo ahí les dejaría un backup que su pantalla no ve.
    #
    # Se retira cuando esos dos migren al motor — ver
    # wiki/analyses/resguardo-backup-familia-libra.md.
    backup_zip: bool = False

    # La imagen del sidecar. **Alpine no es un detalle**: musl no implementa
    # locales, así que ordena por bytes igual que el `BINARY` de SQLite. Con la
    # imagen Debian, cada pantalla ordenada por texto cambia de orden sin que
    # nadie toque una línea. Ver wiki/analyses/migracion-postgresql-familia-libra.md.
    postgres_image: str = "postgres:16-alpine"

    # Los comandos que aplican las migraciones de esquema del producto,
    # corridos en orden como parte de `panel_admin.py actualizar`. Vacío = **el
    # producto no tiene migraciones en el camino de deploy**, que es como
    # estaba todo hasta el 2026-08-24.
    #
    # 🔑 Es una secuencia de comandos y no UN comando porque **Gestiolibra y
    # MedLibra tienen dos cadenas de Alembic independientes**: la de LibraGenda
    # (`libragenda-migrar upgrade`, con su propia `alembic_version`) y la
    # propia (`alembic upgrade head`, con `alembic_version_<producto>`). Las dos
    # tienen que correr, y en ese orden: las revisiones del producto tienen FK
    # contra tablas de LibraGenda.
    #
    # Podría ser un `("sh", "-c", "a && b")`, pero entonces el `[ERROR]` de acá
    # abajo diría "fallaron las migraciones" sin decir **cuál de las dos**, que
    # es justo el dato que uno necesita a las tres de la mañana.
    #
    # 🔴 Existe porque tener el mecanismo no es tenerlo invocado. LibraClub
    # llegó a `main` con la revisión `0008` adentro de la imagen y **nadie que
    # corriera en el deploy la aplicaba**: los únicos `alembic upgrade` del repo
    # están en `semilla_dev.py`, `reset_demo.sh` y la suite. La instancia se
    # habría reconstruido con código que espera una columna que su base no
    # tiene.
    #
    # Cada comando es una tupla y no un string: se pasa a `subprocess` como
    # argumentos sueltos, sin shell de por medio. La forma plana
    # —`("alembic", "upgrade", "head")`, que es la que uno escribe por
    # reflejo— la **rechaza `configure()`**: sin esa guarda cada string se
    # splatearía carácter por carácter y el deploy correría
    # `compose run --rm app a l e m b i c`.
    #
    # Hoy lo pueden prender los productos con Alembic (LibraClub, Gestiolibra,
    # MedLibra). VentaLibra crea su esquema al conectar (`init_*_schema`) y
    # Contalibra/Restolibra con `init_core_schema()`: ésos no tienen nada que
    # correr acá, y por eso el default es vacío en vez de `alembic upgrade head`.
    migraciones: tuple[tuple[str, ...], ...] = ()

    @property
    def usa_postgres(self) -> bool:
        return bool(self.postgres)

    @property
    def db_urls(self) -> tuple[tuple[str, str], ...]:
        """Pares `(variable de entorno, nombre de base)` de esta instancia.

        **Derivados del prefijo, no declarados por cada producto**: hasta el
        2026-08-11 había cuatro convenciones entre seis productos —dos de ellas
        con `_DB_PATH` en el nombre y una URL adentro— y cada alta nueva elegía
        de cuál copiar. Los nombres salen de `libracore.db.url_de_instancia`,
        que es también quien los lee del lado de la app: **un solo lugar define
        cómo se llaman**, así que no pueden volver a divergir.

        🔴 **La PRIMERA es contra la que se aplica el plan.** Medido, no
        supuesto: en los productos con dos bases la tabla `modulos` existe en
        las DOS y la que tiene filas es la del dominio; la de LibraCore la crea
        su DDL y queda vacía. Aplicar el plan contra la segunda escribiría en
        una tabla que el producto no lee — sin fallar.
        """
        if not self.postgres:
            return ()
        from ..db.url_de_instancia import nombre_normalizado

        p = self.container_prefix
        base_dominio = p
        base_core = f"{p}_core" if self.base_core_separada else p
        return (
            (nombre_normalizado(p), base_dominio),
            (nombre_normalizado(p, core=True), base_core),
        )

    @property
    def bases_postgres(self) -> tuple[str, ...]:
        """Nombres de base distintos, en orden de aparición. La primera es la
        que crea la imagen vía `POSTGRES_DB`; el resto necesitan un
        `CREATE DATABASE` en el init."""
        vistas: list[str] = []
        for _, base in self.db_urls:
            if base not in vistas:
                vistas.append(base)
        return tuple(vistas)

    @property
    def clientes_dir(self) -> Path:
        return self.repo_root / "clientes"

    @property
    def image_repo(self) -> str:
        """`image_name` sin el tag: `contalibra:latest` → `contalibra`.

        Los productos siguen declarando `image_name` con `:latest` en su
        `configure()` (no se les cambia la llamada), pero desde el
        versionado de imagen lo que importa es el repo, porque el tag lo
        pone cada deploy — ver `deploy_version()`.

        El `:` de un registry con puerto (`registry:5000/contalibra`) no es
        un tag: solo se corta la última parte si no contiene `/`.
        """
        repo, sep, tag = self.image_name.rpartition(":")
        if sep and "/" not in tag:
            return repo
        return self.image_name

    def image_ref(self, version: str) -> str:
        """Referencia completa de una versión: `contalibra:v2026.07.30-2110`."""
        return f"{self.image_repo}:{version}"


def deploy_version(now: datetime | None = None) -> str:
    """Identificador de la versión de un deploy: `vYYYY.MM.DD-HHMMSS`.

    Mismo esquema que `deploy.sh` de Farmacia (ver
    wiki/entities/farmacia-python.md), que es el único producto del
    inventario que ya versionaba sus deploys. La diferencia es dónde se
    usa: Farmacia lo escribe en un archivo `VERSION` y crea un tag de git,
    pero su imagen Docker sigue siendo `farmacia-app:latest`; acá además
    **nombra la imagen**, que es lo que permite que el compose de cada
    cliente pinee una versión concreta en vez de un `:latest` mutable.

    Por qué timestamp y no la versión del producto: un deploy puede repetir
    código (rebuild por un bump de dependencia, por ejemplo), y lo que hay
    que poder distinguir es *el artefacto*, no el número de release.
    """
    # Con segundos desde el 2026-09-05: dos `actualizar` de instancias
    # distintas en el mismo minuto acuñaban el MISMO tag, y el segundo build
    # le robaba el nombre al primero --la demo de LibraDesk quedó corriendo
    # una imagen sin tag, indistinguible por versión de la de lagrace--.
    return (now or datetime.now()).strftime("v%Y.%m.%d-%H%M%S")


def _git_commit_corto(repo_root: Path) -> str | None:
    """Hash corto del checkout, o None si no es un repo git (o no hay git).
    Se guarda como label de la imagen: el tag dice *cuándo* se construyó,
    el label dice *qué* se construyó."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
        )
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


@contextmanager
def contexto_de_build(repo_root: Path, ref: str = "main", *,
                      from_checkout: bool = False, log=print):
    """El directorio del que construir, como un `git worktree` limpio del `ref`.

    Rinde `(contexto, commit, origen)` y limpia el worktree al salir, pase lo
    que pase.

    🔴 **Por qué no se construye del checkout.** El checkout es una variable
    global compartida: el mismo directorio alimenta el build de `<producto>-dev`
    y el de la instancia de cada cliente. Con el build atado al checkout, la
    rama que necesita dev decide de rebote qué código se le despliega al
    cliente. Le pasó a LibraDesk el 2026-08-03 — el checkout del VPS pasó a
    `develop` para poder probar algo en dev, y a partir de ahí un deploy de
    cliente habría construido `develop` y se lo habría puesto a un cliente
    real. No fallaba ni preguntaba: imprimía el commit y seguía.

    De ahí el default `main`, que quiere decir **lo que está promovido**, y no
    la rama local que puede haber quedado atrás: si existe `origin/<ref>`, gana
    ese.

    Efecto lateral buscado: el contexto es un árbol limpio, sin los restos
    ignorados que viven en el host y sin los directorios de datos de clientes
    que el `.dockerignore` de cada producto no siempre lista.

    `from_checkout=True` es la salida de emergencia, explícita a propósito:
    construye el working tree tal cual está, con lo que tenga sin commitear.
    """
    repo_root = Path(repo_root)

    if from_checkout:
        commit = _git_commit_corto(repo_root) or "desconocido"
        sucio = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain=v1"],
            capture_output=True, text=True,
        ).stdout.strip()
        n = len(sucio.splitlines()) if sucio else 0
        log("[AVISO] from_checkout: se construye el working tree tal cual está, "
            f"no un ref promovido ({n} archivo/s sin commitear).")
        yield repo_root, commit, f"checkout {repo_root} ({n} sin commitear)"
        return

    if subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--git-dir"],
                      capture_output=True).returncode != 0:
        raise RuntimeError(
            f"{repo_root} no es un repo git; no se puede resolver el ref '{ref}'. "
            "Usá from_checkout=True si de verdad querés construir el directorio."
        )

    # Best-effort: si no hay red o la deploy key falla seguimos con las refs
    # locales, avisando. Un fetch caído no tiene por qué bloquear un deploy.
    if subprocess.run(["git", "-C", str(repo_root), "fetch", "--quiet", "origin"],
                      capture_output=True).returncode != 0:
        log(f"[AVISO] 'git fetch origin' falló. Se resuelve '{ref}' con las refs "
            "locales, que pueden estar viejas.")

    resuelto = ref
    if subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet",
         f"refs/remotes/origin/{ref}^{{commit}}"], capture_output=True
    ).returncode == 0:
        resuelto = f"origin/{ref}"

    r = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet",
         f"{resuelto}^{{commit}}"], capture_output=True, text=True,
    )
    if r.returncode != 0:
        disponibles = subprocess.run(
            ["git", "-C", str(repo_root), "for-each-ref",
             "--format=%(refname:short)", "refs/heads", "refs/remotes/origin", "refs/tags"],
            capture_output=True, text=True,
        ).stdout.split()[:20]
        raise RuntimeError(
            f"El ref '{ref}' no existe en {repo_root} (ni como origin/{ref}). "
            f"Disponibles: {', '.join(disponibles)}"
        )
    commit_full = r.stdout.strip()
    commit = commit_full[:7]

    head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip() or "?"

    padre = tempfile.mkdtemp(prefix="libracore-build-")
    destino = Path(padre) / "src"
    try:
        # 🔴 CLONE, no `worktree add`. En un worktree `.git` es un ARCHIVO que
        # apunta a `<repo>/.git/worktrees/<nombre>`, y ese archivo entra al
        # contexto de build: adentro del contenedor la ruta no existe y
        # cualquier cosa que llame a git muere con
        #
        #     fatal: not a git repository: /root/<producto>/.git/worktrees/src
        #
        # Que es justo lo que le pasa a los productos cuyo Dockerfile hace
        # `pip install .` con la version derivada de git. Medido el 2026-08-17
        # desplegando los seis: fallaron 3 de 6 --contalibra, restolibra y
        # ventalibra-- y los otros tres pasaron sólo porque su build no llama a
        # git. O sea que el defecto no se ve en la mitad de los casos.
        #
        # `git clone --local` deja un `.git` de VERDAD (objetos por hardlink,
        # sin alternates que apunten afuera), asi que el contexto es
        # autocontenido y git funciona adentro del contenedor. `--shared` NO
        # sirve: usa alternates al repo padre y reintroduce el mismo problema.
        subprocess.run(
            ["git", "clone", "--quiet", "--local", "--no-checkout",
             str(repo_root), str(destino)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(destino), "checkout", "--quiet", "--detach", commit_full],
            check=True, capture_output=True,
        )
        yield destino, commit, f"{ref} -> {resuelto} (clon limpio; el checkout sigue en {head})"
    finally:
        shutil.rmtree(padre, ignore_errors=True)


def build_image_tagged(version: str, *, ref: str = "main",
                       from_checkout: bool = False, log=print) -> bool:
    """Construye la imagen del producto activo etiquetándola **con la
    versión y además con `latest`**, y devuelve si el build salió bien.

    Compartido por `panel_admin.cmd_actualizar` y
    `nuevo_cliente.build_image`, que hasta el versionado tenían el mismo
    `docker build` duplicado con distinto mensaje.

    `latest` se sigue moviendo a propósito, por compatibilidad con lo que
    haya quedado apuntando ahí (un compose viejo, un script suelto). Lo que
    hace segura la convivencia es que los clientes dejan de usarlo: cada
    uno queda pineado a su versión, así que un `up -d` inocente ya no puede
    saltarlos a código que nadie probó para ellos.
    """
    cfg = get_config()
    image = cfg.image_ref(version)

    with contexto_de_build(cfg.repo_root, ref, from_checkout=from_checkout,
                           log=log) as (contexto, commit, origen):
        cmd = [
            "docker", "build", *docker_build_ssh_args(cfg.repo_root),
            "-t", image,
            "-t", cfg.image_ref("latest"),
            "--label", f"org.libra.version={version}",
            "--label", f"org.libra.built-at={datetime.now().astimezone().isoformat(timespec='seconds')}",
            # `org.libra.commit` es verdadera POR CONSTRUCCIÓN: es el commit
            # del que salió el worktree, no lo que el checkout tuviera puesto.
            "--label", f"org.libra.commit={commit}",
            "--label", f"org.libra.ref={origen}",
            str(contexto),
        ]
        log(f"[*] Construyendo {image} (commit {commit}) desde {origen} ...")
        r = subprocess.run(
            cmd, cwd=str(cfg.repo_root),
            env={**os.environ, "DOCKER_BUILDKIT": "1"},
        )
        return r.returncode == 0


_lock = threading.Lock()
_cfg: ProductConfig | None = None


def _migraciones_normalizadas(valor) -> tuple:
    """Una secuencia de comandos, con la forma plana rechazada a propósito.

    🔴 `migraciones=("alembic", "upgrade", "head")` es lo que uno escribe por
    reflejo, y sin esta guarda **no falla**: el bucle de `cmd_actualizar`
    iteraría los tres strings y splatearía cada uno carácter por carácter,
    corriendo `compose run --rm app a l e m b i c`. Un `TypeError` acá, al
    importar el `panel_admin.py` del producto, es infinitamente mejor que
    descubrirlo en un deploy.
    """
    comandos = tuple(valor)
    planos = [c for c in comandos if isinstance(c, str)]
    if planos:
        raise TypeError(
            "migraciones es una secuencia de COMANDOS, no un comando: "
            f"llegó {planos[0]!r} suelto. Anidalo — "
            f"migraciones=({tuple(comandos)!r},)"
        )
    normalizados = tuple(tuple(str(a) for a in c) for c in comandos)
    if any(not c for c in normalizados):
        raise ValueError("migraciones tiene un comando vacío")
    return normalizados


def configure(*, product_name: str, image_name: str, container_prefix: str,
              db_filename: str, repo_root, base_port: int = 8071,
              docs_auth_secret: str = "", postgres: bool = False,
              base_core_separada: bool = False,
              postgres_image: str = "postgres:16-alpine",
              backup_zip: bool = False, health_path: str = "/health",
              migraciones: tuple[tuple[str, ...], ...] = ()):
    """Configura el producto activo. Llamar una sola vez, al principio de
    `scripts/nuevo_cliente.py`/`scripts/panel_admin.py` de cada producto.

    `postgres=True` es lo que hace que una instancia nueva nazca sobre
    PostgreSQL. Sin ese argumento el comportamiento es el de siempre (SQLite),
    así que un producto que todavía no lo pase sigue funcionando igual.

    **Los nombres de las variables no se pasan**: salen del prefijo, vía
    `libracore.db.url_de_instancia` — ver `ProductConfig.db_urls`.

    `health_path` no lo pasa nadie: **los seis sirven su salud en `/health`**
    desde el 2026-08-12, que es el default. Quedó como escape hatch para un
    producto que no pudiera. Ver el comentario del campo en `ProductConfig`,
    que dice qué hay que hacer si alguna vez vuelve a hacer falta.

    `migraciones` son los comandos que aplican el esquema —típicamente
    `(("alembic", "upgrade", "head"),)`— y los corre `cmd_actualizar` en orden
    **antes** de mover la instancia a la imagen nueva. Vacío por default: un
    producto que no lo pase se comporta exactamente como antes.

    ⚠️ **Van anidados aunque sea uno solo.** La forma plana se rechaza con un
    `TypeError` — ver `_migraciones_normalizadas`.
    """
    global _cfg
    with _lock:
        repo_root = Path(repo_root)
        _cfg = ProductConfig(
            product_name=product_name, image_name=image_name,
            container_prefix=container_prefix, db_filename=db_filename,
            repo_root=repo_root, base_port=base_port,
            docs_auth_secret=docs_auth_secret,
            postgres=postgres, base_core_separada=base_core_separada,
            postgres_image=postgres_image, backup_zip=backup_zip,
            health_path=health_path,
            migraciones=_migraciones_normalizadas(migraciones),
        )
        for p in (repo_root, repo_root / "scripts"):
            sp = str(p)
            if sp not in sys.path:
                sys.path.insert(0, sp)


def get_config() -> ProductConfig:
    if _cfg is None:
        raise RuntimeError(
            "libracore.provisioning no está configurado — llamar configure("
            "product_name=..., image_name=..., container_prefix=..., "
            "db_filename=..., repo_root=...) al principio del script."
        )
    return _cfg


def _plans():
    get_config()
    import plans
    return plans


def _npm_api():
    """Módulo `npm_api` del producto activo, o None si no está disponible."""
    get_config()
    try:
        import npm_api
        return npm_api
    except Exception:
        return None


def npm_available() -> bool:
    return _npm_api() is not None


def client_from_config():
    npm = _npm_api()
    return npm.client_from_config() if npm else None


def forward_host_from_config():
    npm = _npm_api()
    return npm.forward_host_from_config() if npm else None


def le_email_from_config():
    npm = _npm_api()
    return npm.le_email_from_config() if npm else None


def _como_se_escribe_habilitado(con):
    """Devuelve la función que convierte el booleano al tipo que la columna
    `modulos.habilitado` tiene **en esta base**.

    🔴 No es lo mismo en todos los productos, y en PostgreSQL el tipo del
    parámetro tiene que coincidir: los dos errores son simétricos y los dos
    frenan el alta.

    | Dónde | Tipo | Qué falla |
    |---|---|---|
    | La `modulos` de LibraCore | `BOOLEAN` | pasar `1` → *"column habilitado is of type boolean but expression is of type smallint"* |
    | La `modulos` propia de VentaLibra | `INTEGER` | pasar `True` → *"column habilitado is of type integer but expression is of type boolean"* |

    VentaLibra declara su propia `modulos` (con `modulo TEXT PK`, sin `id`) y
    esa gana sobre la del core — está documentado en la página de la migración.
    En SQLite daba igual, porque el tipado es dinámico: **el defecto aparece
    recién al dar de alta una instancia PostgreSQL**, y se encontró así, con un
    alta real el 2026-08-11.

    En SQLite no hay `information_schema`: se cae al booleano de siempre, que es
    lo que venía andando.
    """
    try:
        fila = con.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='modulos' AND column_name='habilitado'"
        ).fetchone()
    except Exception:
        return bool
    if not fila:
        return bool
    tipo = str(fila[0]).lower()
    return int if ("int" in tipo or "numeric" in tipo) else bool


def apply_plan_modules(db_path, *, active_modules: set, all_modules: set, plan: str) -> None:
    """Escribe el estado de módulos (habilitado + plan) en la tabla `modulos`
    de la base de un cliente (`clientes/<slug>/data/*.db`, o su base
    PostgreSQL).

    Idempotente (INSERT OR IGNORE + UPDATE), funciona igual sobre una base
    recién migrada o una existente. Requiere que la tabla `modulos` ya
    exista (la crea la migración propia de cada vertical). Extraído
    2026-07-26 de Gestiolibra/MedLibra/VentaLibra
    (`plans.py::aplicar_plan_en_db`), donde el cuerpo era idéntico salvo el
    nombre de la variable — ver
    wiki/analyses/auditoria-duplicacion-familia-libra.md. La definición del
    plan en sí (`PLANES`/`PLAN_MODULOS`/precios) sigue viviendo en el
    `plans.py` de cada producto — es catálogo propio, no código.

    🔴 **`db_path` puede ser una ruta SQLite o una URL PostgreSQL** desde el
    2026-08-09. Antes esto hacía `sqlite3.connect(db_path)` a secas: contra una
    instancia PostgreSQL creaba un archivo vacío al lado y moría con `no such
    table: modulos`, así que **el plan no se aplicaba** y la instancia quedaba
    con los módulos como vinieran. Lo encontró la suite de LibraDesk corriendo
    contra PostgreSQL.

    Va por `libracore.db.core.conectar()`, que abre contra cualquiera de los
    dos motores y **no toca el estado global** del proceso — importante porque
    esto lo llama el provisioning sobre la instancia de OTRO cliente, no sobre
    la propia.
    """
    from ..db.core import conectar

    con = conectar(str(db_path))
    try:
        convertir = _como_se_escribe_habilitado(con)
        for m in sorted(all_modules):
            # 🔴 `bool`, no `1`/`0`. `modulos.habilitado` es BOOLEAN y
            # PostgreSQL no acepta un entero ahi: *"column habilitado is of
            # type boolean but expression is of type smallint"*. SQLite se lo
            # traga porque no tiene booleano nativo, asi que el defecto era
            # invisible. Es la misma forma de fallar que el `BOOLEAN DEFAULT 1`
            # de las migraciones de LibraDesk.
            on = convertir(m in active_modules)
            con.execute(
                "INSERT OR IGNORE INTO modulos (modulo, habilitado, plan) VALUES (?,?,?)",
                (m, on, plan),
            )
            con.execute(
                "UPDATE modulos SET habilitado=?, plan=? WHERE modulo=?",
                (on, plan, m),
            )
        con.commit()
    finally:
        con.close()
