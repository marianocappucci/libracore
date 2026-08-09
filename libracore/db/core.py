"""
Infraestructura compartida por los módulos `libracore.db.*`: conexión
SQLite configurable por producto y utilidades de fecha/hora.

Cada producto llama `configure()` una vez al arrancar (antes de que
cualquier otro módulo de `libracore.db` abra una conexión) con su propio
`db_path`. Los ~200 call sites existentes en cada producto siguen llamando
`get_connection()` sin argumentos — mismo patrón de mínima huella que el
resto de LibraCore (callback/config inyectado en vez de reescribir call
sites, ver `libracore.auth`).
"""
import sqlite3
import threading
from datetime import datetime as _datetime, timezone as _timezone, timedelta as _timedelta
from typing import Callable

_AR_TZ = _timezone(_timedelta(hours=-3))   # America/Argentina/Buenos_Aires (sin DST)


def _ar_now() -> str:
    """Fecha y hora actual en zona horaria Argentina (UTC-3)."""
    return _datetime.now(_AR_TZ).strftime("%Y-%m-%d %H:%M:%S")


def minutos_desde(ts: str) -> int:
    """Minutos transcurridos (en hora AR) desde un timestamp 'YYYY-MM-DD HH:MM:SS'."""
    if not ts:
        return 0
    try:
        t = _datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
        now = _datetime.strptime(_ar_now(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return max(0, int((now - t).total_seconds() // 60))


_lock = threading.Lock()
_db_path: str | None = None
_timeout: int = 5
_extra_pragmas: tuple[str, ...] = ()
_database_url: str | None = None


def configure(db_path: str, *, timeout: int = 5, extra_pragmas: tuple[str, ...] = ()):
    """Configura la conexión que usará `get_connection()` para todo el
    proceso. Llamar una sola vez, al arrancar la app, antes de cualquier
    otro import de `libracore.db.*` que abra una conexión.

    `timeout`: segundos de espera ante lock de escritura (Restolibra usa 15,
    Contalibra el default de sqlite3 — se preserva la diferencia real que ya
    tenía cada producto, no se unifica). `extra_pragmas`: PRAGMAs adicionales
    que un producto necesite correr en cada conexión nueva."""
    global _db_path, _database_url, _timeout, _extra_pragmas
    with _lock:
        _db_path = db_path
        _database_url = db_path if "://" in db_path else None
        _timeout = timeout
        _extra_pragmas = tuple(extra_pragmas)


def get_connection():
    if _db_path is None:
        raise RuntimeError(
            "libracore.db.core no está configurado — llamar "
            "libracore.db.core.configure(db_path=...) al arrancar la app."
        )
    if _database_url:
        if not _database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError(f"URL de base no soportada: {_database_url!r}")
        from psycopg import connect

        from ._postgres import ConnectionWrapper

        url = _database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        return ConnectionWrapper(connect(url, connect_timeout=_timeout))

    conn = sqlite3.connect(_db_path, timeout=_timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    for pragma in _extra_pragmas:
        conn.execute(pragma)
    return conn


def is_postgres() -> bool:
    """Indica si el backend configurado es PostgreSQL."""
    return _database_url is not None


# Hook opcional para comportamiento receta-aware de `descontar_stock_venta`
# (ver libracore.db.stock). None = comportamiento simple (Contalibra: siempre
# descuenta el producto vendido). Un producto con recetas (Restolibra) inyecta
# un callable `(producto_id: int) -> dict | None` que devuelve la receta con
# sus ingredientes, o None si el producto no tiene receta.
ResolverReceta = Callable[[int], dict | None]
_resolver_receta: ResolverReceta | None = None


def configure_resolver_receta(resolver: ResolverReceta | None):
    global _resolver_receta
    with _lock:
        _resolver_receta = resolver


def get_resolver_receta() -> ResolverReceta | None:
    return _resolver_receta
