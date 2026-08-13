"""
Capa de servicios del backoffice: envuelve los scripts de gestión existentes
de cada producto (`scripts/panel_admin.py`, `scripts/nuevo_cliente.py`) y el
mapeo de planes (`plans.py`) para exponerlos a las rutas web de forma no
interactiva. No reimplementa Docker/NPM/backup: reutiliza las funciones ya
probadas de la CLI.

Migrado a libracore.admin (Fase 4 de LibraCore, backoffice compartido — ver
wiki/entities/libracore.md). `plans.py`/`panel_admin.py`/`nuevo_cliente.py`
siguen viviendo en el repo de cada producto (no son genéricos, tienen la
lógica real de planes/clientes de cada uno) — se resuelven en tiempo de
ejecución vía `configure(repo_root=...)` + imports diferidos dentro de cada
función, mismo patrón que `libracore.db.modulos::apply_plan()`. El nombre
del archivo de base de datos también es configurable por producto
(`contalibra.db` vs `restolibra.db`, antes hardcodeado).
"""
import json
import shutil
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()
_repo_root: Path | None = None
_db_filename: str = ""


def configure(repo_root, db_filename: str):
    global _repo_root, _db_filename
    with _lock:
        _repo_root = Path(repo_root)
        _db_filename = db_filename
        sys.path.insert(0, str(_repo_root))
        sys.path.insert(0, str(_repo_root / "scripts"))


def _require_configured():
    if _repo_root is None:
        raise RuntimeError(
            "libracore.admin.services no está configurado — llamar "
            "configure(repo_root=..., db_filename=...) al arrancar la app."
        )


def _pa():
    _require_configured()
    import panel_admin
    return panel_admin


def _nc():
    _require_configured()
    import nuevo_cliente
    return nuevo_cliente


def _plans():
    _require_configured()
    import plans
    return plans


def _clientes_dir() -> Path:
    return _pa().CLIENTES_DIR


class ServiceError(Exception):
    """Error de operación del backoffice."""


# ── lectura ────────────────────────────────────────────────────────────────────

def _modulos_activos(db_path: Path) -> int | None:
    """Cantidad de módulos habilitados en la DB del cliente (None si no se puede leer)."""
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(*) FROM modulos WHERE habilitado=1").fetchone()[0]
        con.close()
        return n
    except Exception:
        return None


def _servicio(dir_cliente: Path) -> tuple[str, str]:
    """Estado de servicio y mensaje de una instancia, leídos de su `config.json`.

    Es el corte comercial (activo / pausado / suspendido), no el estado del
    contenedor: un contenedor `running` puede estar suspendido y devolver 503 a
    todo. Son dos ejes distintos y la UI los tiene que mostrar por separado.

    Si `config.json` no existe todavía (la instancia nunca arrancó), la app
    levanta con los DEFAULTS de `config_manager`, que traen `activo`. Se
    devuelve eso mismo para no inventar un tercer estado que no existe del lado
    de la instancia.
    """
    config_path = dir_cliente / "data" / "config.json"
    if not config_path.exists():
        return "activo", ""
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return "activo", ""
    return cfg.get("servicio_estado", "activo"), cfg.get("servicio_mensaje", "")


def _enrich(c: dict) -> dict:
    """Agrega estado del contenedor, plan y conteo de módulos a un cliente."""
    pa = _pa()
    info = pa.container_status(c["container"])
    db_path = c["dir"] / "data" / _db_filename
    servicio_estado, servicio_mensaje = _servicio(c["dir"])
    return {
        "nombre":      c.get("nombre", ""),
        "slug":        c["slug"],
        "domain":      c.get("domain", "") or "",
        "port":        c.get("port", ""),
        "container":   c["container"],
        "admin_user":  c.get("admin_user", ""),
        "plan":        c.get("plan", "") or "",
        "estado":      info["status"],
        "iniciado":    info["started"],
        "modulos_activos": _modulos_activos(db_path),
        "servicio_estado":  servicio_estado,
        "servicio_mensaje": servicio_mensaje,
    }


def listar_clientes() -> list[dict]:
    return [_enrich(c) for c in _pa().load_clients()]


def get_cliente(slug: str) -> dict | None:
    c = _pa().find_client(slug)
    if not c:
        return None
    data = _enrich(c)
    data["dir"] = str(c["dir"])
    return data


# ── alta ───────────────────────────────────────────────────────────────────────

def crear_cliente(nombre, slug="", domain="", port=0, admin_user="admin",
                  admin_password="", plan="basico", setup_npm=True) -> dict:
    nc = _nc()
    try:
        return nc.crear_cliente(
            nombre=nombre, slug=slug, domain=domain, port=int(port or 0),
            admin_user=admin_user or "admin", admin_password=admin_password,
            plan=plan, setup_npm=setup_npm,
        )
    except nc.ClienteError as e:
        raise ServiceError(str(e))


# ── edición de metadata ─────────────────────────────────────────────────────────

def editar_cliente(slug: str, nombre: str, domain: str) -> dict:
    pa = _pa()
    c = pa.find_client(slug)
    if not c:
        raise ServiceError(f"Cliente '{slug}' no encontrado.")
    meta_path = c["dir"] / "cliente.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    dominio_prev = meta.get("domain", "") or ""
    meta["nombre"] = (nombre or "").strip() or meta.get("nombre", "")
    meta["domain"] = (domain or "").strip()
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # Si cambió el dominio, (re)crear el proxy NPM al nuevo dominio.
    if meta["domain"] and meta["domain"] != dominio_prev:
        _npm_crear_proxy(meta["domain"], meta.get("port", 0))
    return meta


# ── planes ──────────────────────────────────────────────────────────────────────

def set_plan(slug: str, plan: str) -> None:
    plans = _plans()
    if plan not in plans.PLANES:
        raise ServiceError(f"Plan inválido: {plan!r}.")
    c = _pa().find_client(slug)
    if not c:
        raise ServiceError(f"Cliente '{slug}' no encontrado.")
    db_path = c["dir"] / "data" / _db_filename
    if not db_path.exists():
        raise ServiceError("La instancia todavía no inicializó su base de datos.")
    plans.aplicar_plan_en_db(str(db_path), plan)
    # Persistir el plan en cliente.json (metadato).
    meta_path = c["dir"] / "cliente.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["plan"] = plan
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


# ── estado / ciclo de vida del contenedor ───────────────────────────────────────

ESTADOS_ACCION = {"start", "stop", "restart", "pausar", "suspender", "activar"}


def accion_estado(slug: str, accion: str, mensaje: str = "") -> None:
    """Ciclo de vida del contenedor (start/stop/restart) o corte de servicio.

    `mensaje` es el texto que ve el cliente en el banner de pausa o en la
    pantalla de suspensión, y sólo aplica a `pausar`/`suspender`. `activar` lo
    limpia siempre: un mensaje de "falta de pago" que sobrevive a la
    reactivación se le sigue mostrando al cliente que ya pagó.
    """
    if accion not in ESTADOS_ACCION:
        raise ServiceError(f"Acción inválida: {accion!r}.")
    pa = _pa()
    c = pa.find_client(slug)
    if not c:
        raise ServiceError(f"Cliente '{slug}' no encontrado.")
    if accion == "start":
        pa.compose(slug, "up", "-d")
    elif accion == "restart":
        pa.compose(slug, "restart")
    elif accion == "stop":
        pa.compose(slug, "stop")
    else:
        # pausar / suspender / activar → estado de servicio (banner en la instancia)
        estado = {"pausar": "pausado", "suspender": "suspendido", "activar": "activo"}[accion]
        texto = "" if accion == "activar" else (mensaje or "")
        if not (c["dir"] / "data" / "config.json").exists():
            raise ServiceError(
                "La instancia todavía no creó su configuración: hay que "
                "iniciarla al menos una vez antes de cortarle el servicio."
            )
        if not pa._set_servicio_estado(slug, estado, texto):
            raise ServiceError("No se pudo cambiar el estado del servicio.")


# ── backup ──────────────────────────────────────────────────────────────────────

def backup_cliente(slug: str) -> str:
    """Crea un backup tar.gz del directorio data y devuelve la ruta del archivo."""
    c = _pa().find_client(slug)
    if not c:
        raise ServiceError(f"Cliente '{slug}' no encontrado.")
    data_dir = c["dir"] / "data"
    if not data_dir.exists():
        raise ServiceError("El cliente no tiene datos para respaldar.")
    import tarfile
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = _clientes_dir() / f"{slug}_backup_{ts}.tar.gz"
    with tarfile.open(out_file, "w:gz") as tar:
        tar.add(data_dir, arcname=f"{slug}/data")
    return str(out_file)


# ── eliminación ─────────────────────────────────────────────────────────────────

def eliminar_cliente(slug: str, hacer_backup: bool = True) -> dict:
    """Baja: backup previo (opcional), elimina proxy NPM, baja el contenedor con
    su volumen y borra el directorio del cliente."""
    pa = _pa()
    c = pa.find_client(slug)
    if not c:
        raise ServiceError(f"Cliente '{slug}' no encontrado.")

    resultado = {"slug": slug, "backup": None, "npm": None}
    if hacer_backup:
        resultado["backup"] = backup_cliente(slug)

    # Proxy NPM (best-effort)
    domain = c.get("domain", "") or ""
    if domain:
        resultado["npm"] = _npm_eliminar_proxy(domain)

    # Contenedor + volumen
    pa.compose(slug, "down", "-v")
    # Directorio del cliente
    shutil.rmtree(c["dir"])
    return resultado


# ── NPM helpers (best-effort, no rompen la operación si NPM no está) ─────────────

def _npm():
    pa = _pa()
    if not pa._NPM_AVAILABLE:
        return None
    try:
        return pa.client_from_config()
    except Exception:
        return None


def _npm_crear_proxy(domain: str, port) -> bool | None:
    npm = _npm()
    if not npm:
        return None
    pa = _pa()
    try:
        if npm.get_proxy_host_by_domain(domain):
            return True
        npm.create_proxy_host(
            domain=domain, forward_host=pa.forward_host_from_config(),
            forward_port=int(port or 0), ssl=True, le_email=pa.le_email_from_config(),
        )
        return True
    except Exception:
        return False


def _npm_eliminar_proxy(domain: str) -> bool | None:
    npm = _npm()
    if not npm:
        return None
    try:
        host = npm.get_proxy_host_by_domain(domain)
        if not host:
            return None
        return bool(npm.delete_proxy_host(host["id"]))
    except Exception:
        return False


# ── metadatos de planes para la UI ──────────────────────────────────────────────

def planes_info() -> list[dict]:
    plans = _plans()
    return [
        {
            "key": p,
            "label": plans.PLAN_LABELS.get(p, p.title()),
            "precio": plans.PLAN_PRECIOS.get(p),
            "modulos": sorted(plans.modulos_de_plan(p)),
        }
        for p in plans.PLANES
    ]
