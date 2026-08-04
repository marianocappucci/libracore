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
import subprocess
import sys
import threading
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
    if pyproject.exists() and f"{paquete} @ git+" in pyproject.read_text(encoding="utf-8"):
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


def _pin_declarado(repo_root: Path) -> str | None:
    """Extrae la versión pineada de `libracore @ git+…@vX.Y.Z` del producto,
    mirando **requirements.txt y pyproject.toml** — o None si no está
    declarada en ninguno, o no tiene ese formato.

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
    req_file = repo_root / "requirements.txt"
    if req_file.exists():
        for line in req_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("libracore") and "@v" in line:
                return line.rsplit("@v", 1)[1].strip()

    # En pyproject el pin es un elemento de lista: viene entre comillas y
    # con coma final (`"libracore @ git+https://…@v1.8.0",`), así que no
    # sirve el `rsplit` de arriba. Exigir `@ git+` es lo que evita agarrar
    # los comentarios que nombran al motor — en estos pyproject hay varios,
    # justo encima de la línea del pin.
    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists():
        m = re.search(
            r"""libracore\s*@\s*git\+\S+?@v([0-9][^"'\s,]*)""",
            pyproject.read_text(encoding="utf-8"),
        )
        if m:
            return m.group(1)
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

    pin = _pin_declarado(repo_root)
    if pin is None:
        return None
    installed = libracore.__version__.split("+")[0]  # sin sufijo local de hatch-vcs
    if installed == pin:
        return None
    return (
        f"[ADVERTENCIA] Este venv tiene libracore=={installed} instalado, "
        f"pero requirements.txt de este producto pinea v{pin}. "
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
    """Identificador de la versión de un deploy: `vYYYY.MM.DD-HHMM`.

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
    return (now or datetime.now()).strftime("v%Y.%m.%d-%H%M")


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


def build_image_tagged(version: str, *, log=print) -> bool:
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
    ref = cfg.image_ref(version)
    cmd = [
        "docker", "build", *docker_build_ssh_args(cfg.repo_root),
        "-t", ref,
        "-t", cfg.image_ref("latest"),
        "--label", f"org.libra.version={version}",
        "--label", f"org.libra.built-at={datetime.now().astimezone().isoformat(timespec='seconds')}",
    ]
    commit = _git_commit_corto(cfg.repo_root)
    if commit:
        cmd += ["--label", f"org.libra.commit={commit}"]
    cmd.append(".")

    log(f"[*] Construyendo {ref}" + (f" (commit {commit})" if commit else "") + " ...")
    r = subprocess.run(
        cmd, cwd=str(cfg.repo_root),
        env={**os.environ, "DOCKER_BUILDKIT": "1"},
    )
    return r.returncode == 0


_lock = threading.Lock()
_cfg: ProductConfig | None = None


def configure(*, product_name: str, image_name: str, container_prefix: str,
              db_filename: str, repo_root, base_port: int = 8071,
              docs_auth_secret: str = ""):
    """Configura el producto activo. Llamar una sola vez, al principio de
    `scripts/nuevo_cliente.py`/`scripts/panel_admin.py` de cada producto."""
    global _cfg
    with _lock:
        repo_root = Path(repo_root)
        _cfg = ProductConfig(
            product_name=product_name, image_name=image_name,
            container_prefix=container_prefix, db_filename=db_filename,
            repo_root=repo_root, base_port=base_port,
            docs_auth_secret=docs_auth_secret,
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


def apply_plan_modules(db_path, *, active_modules: set, all_modules: set, plan: str) -> None:
    """Escribe el estado de módulos (habilitado + plan) directo en la tabla
    `modulos` de la base SQLite de un cliente (`clientes/<slug>/data/*.db`).
    Idempotente (INSERT OR IGNORE + UPDATE), funciona igual sobre una base
    recién migrada o una existente. Requiere que la tabla `modulos` ya
    exista (la crea la migración propia de cada vertical). Extraído
    2026-07-26 de Gestiolibra/MedLibra/VentaLibra
    (`plans.py::aplicar_plan_en_db`), donde el cuerpo era idéntico salvo el
    nombre de la variable — ver
    wiki/analyses/auditoria-duplicacion-familia-libra.md. La definición del
    plan en sí (`PLANES`/`PLAN_MODULOS`/precios) sigue viviendo en el
    `plans.py` de cada producto — es catálogo propio, no código."""
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        for m in sorted(all_modules):
            on = 1 if m in active_modules else 0
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
