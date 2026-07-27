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
import sys
import threading
from dataclasses import dataclass
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


def _requiere_libracommerce(repo_root: Path) -> bool:
    """True si requirements.txt del producto depende de libracommerce —
    mismo grep ("^libracommerce") que usa el Dockerfile para separar el
    paso de instalación con su propia deploy key."""
    req_file = repo_root / "requirements.txt"
    if not req_file.exists():
        return False
    return any(
        line.startswith("libracommerce")
        for line in req_file.read_text(encoding="utf-8").splitlines()
    )


def docker_build_ssh_args(repo_root: Path) -> list[str]:
    """Arma los `--ssh id=key` para `docker build`, compartido entre
    `panel_admin.cmd_actualizar` y `nuevo_cliente.build_image` (mismo
    Dockerfile, mismo requisito de mounts SSH en ambos flujos de build).
    Se pasan `default` y `libracore` apuntando a la misma key (compat con
    Dockerfile viejos de un solo mount id y con los nuevos que ya usan
    `id=libracore` explícito); `libracommerce` solo si el producto
    depende de ese paquete (si no, la key puede ni existir en esta
    máquina). Pasar un `--ssh` de más para un id que el Dockerfile no
    monta es inofensivo (BuildKit lo ignora), pero pasar uno con una key
    inexistente rompe el build."""
    args = [
        "--ssh", f"default={LIBRACORE_SSH_KEY}",
        "--ssh", f"libracore={LIBRACORE_SSH_KEY}",
    ]
    if _requiere_libracommerce(repo_root):
        args += ["--ssh", f"libracommerce={LIBRACOMMERCE_SSH_KEY}"]
    return args


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
