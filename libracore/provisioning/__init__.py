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
import sys
import threading
from dataclasses import dataclass
from pathlib import Path


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
