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
from collections.abc import Callable
from datetime import datetime as _datetime
from datetime import timedelta as _timedelta
from datetime import timezone as _timezone
from typing import TYPE_CHECKING, TypeAlias

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


#: Una conexión de esta familia: `sqlite3.Connection` **o** el
#: `ConnectionWrapper` de PostgreSQL.
#:
#: 🔴 **Existe porque el nombre anterior mentía a medias.** Estas funciones
#: estaban anotadas `sqlite3.Connection`, y contra PostgreSQL reciben el wrapper
#: de `db/_postgres.py`. La anotación no era falsa —el wrapper emula la API de
#: `sqlite3` a propósito: el `Row`, los `?` como placeholders, y hasta las
#: excepciones traducidas por nombre— pero se lee como si dijera *"esto corre
#: sobre SQLite"*, que es otra cosa.
#:
#: La diferencia importa cuando se escribe SQL. Pasó el 2026-08-25 en
#: [[ventalibra]]: leer `sqlite3.Connection` en un servicio llevó a escribir un
#: arreglo con dialecto de PostgreSQL, que habría roto la corrida que entonces
#: iba contra SQLite. **Lo que el wrapper garantiza es la forma de la API, no el
#: dialecto de la base.**
#:
#: Y hay una diferencia que ni siquiera la API tapa: en PostgreSQL un error
#: **aborta la transacción** y en SQLite no. Ver `_errores_como_sqlite3` en
#: `db/_postgres.py`.
if TYPE_CHECKING:
    from ._postgres import ConnectionWrapper

    Conexion: TypeAlias = "sqlite3.Connection | ConnectionWrapper"
else:  # pragma: no cover - en runtime alcanza el tipo base
    Conexion = sqlite3.Connection


def es_url_postgres(destino: str) -> bool:
    """Si ese string es una URL PostgreSQL y no una ruta de archivo.

    Mismo criterio en un solo lugar. Los productos y los scripts de
    provisioning reciben "la base" como un string que puede ser cualquiera de
    las dos cosas, y cada uno decidiendo por su cuenta es como aparecieron los
    defectos del backup (que trataba el nombre de la base como una ruta) y del
    plan de modulos.
    """
    return destino.startswith(("postgresql://", "postgresql+psycopg://"))


def conectar(destino: str, *, timeout: int = 5, extra_pragmas: tuple[str, ...] = ()):
    """Una conexion contra `destino`, sea una ruta SQLite o una URL PostgreSQL.

    **Sin estado global**: no lee ni escribe la configuracion del proceso. Es
    lo que necesita cualquier codigo que trabaje sobre una instancia que no es
    la suya —el provisioning, el panel de admin, los scripts de backup—, que
    hasta el 2026-08-09 abria `sqlite3.connect()` por su cuenta y por lo tanto
    **no funcionaba contra una instancia PostgreSQL**.

    `get_connection()` delega aca: la logica de abrir es una sola.
    """
    if es_url_postgres(destino):
        from psycopg import connect

        from ._postgres import ConnectionWrapper

        url = destino.replace("postgresql+psycopg://", "postgresql://", 1)
        return ConnectionWrapper(connect(url, connect_timeout=timeout))

    if "://" in destino:
        raise ValueError(f"URL de base no soportada: {destino!r}")

    conn = sqlite3.connect(destino, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    for pragma in extra_pragmas:
        conn.execute(pragma)
    return conn


def get_connection():
    if _db_path is None:
        raise RuntimeError(
            "libracore.db.core no está configurado — llamar "
            "libracore.db.core.configure(db_path=...) al arrancar la app."
        )
    return conectar(_db_path, timeout=_timeout, extra_pragmas=_extra_pragmas)


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
