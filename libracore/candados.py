"""Candados entre procesos: lo que hace falta para que una instancia corra con varios workers de uvicorn.

Hasta ahora el motor suponía **un solo proceso por instancia** (`uvicorn` sin `--workers`), y tres cosas se apoyaban en
eso: la nota de crédito («una a la vez», un `set` en memoria), los hooks de arranque (sembrar el admin, `ensure_seeded`:
un «si no existe, crear» que con dos procesos arrancando juntos crea dos) y el `config.json`. Este módulo es la mitad de
la respuesta —la otra es escribir los archivos de forma atómica—, y tiene tres herramientas con propósitos distintos:

- **`candado_de_base(conn, nombre)`**: un candado **no bloqueante** y **atado a una transacción**. Dice «¿soy yo el que
  está haciendo esto ahora?» y, si no, el que llama decide (la nota de crédito contesta 409). Es para secciones
  críticas de un pedido.
- **`una_vez_al_arrancar(nombre)`**: un candado **bloqueante** con conexión propia, para **serializar** un hook de
  arranque: el segundo worker espera a que el primero termine y recién entonces corre el suyo, que tiene que ser
  idempotente («si no existe, crear») y por lo tanto ve lo que dejó el primero.
- **`esperar_candado_de_transaccion(conn, nombre)`**: un candado **bloqueante** sobre la **conexión de quien llama**, que
  dura hasta el fin de **su** transacción. Es para una transacción larga que no se puede partir y que dos procesos no
  pueden correr a la vez: la creación del esquema (`init_core_schema`, ADR-053). Con otra conexión (como
  `una_vez_al_arrancar`) no sirve: el DDL que importa vive en la transacción de quien llama.

**Cómo se elige el candado, por motor:**

| | PostgreSQL | SQLite / sin conexión |
|---|---|---|
| `candado_de_base` | `pg_try_advisory_xact_lock`, dura hasta el fin de la transacción de `conn` | `flock` sobre un archivo de `DATA_DIR` (con `conn`), o un candado del proceso (sin `conn`); dura lo que el `with` |
| `una_vez_al_arrancar` | `pg_try_advisory_lock` en un bucle, con una conexión propia | `flock` sobre un archivo de `DATA_DIR` |
| `esperar_candado_de_transaccion` | `pg_try_advisory_xact_lock` en un bucle, en la conexión de quien llama | no hace nada (SQLite serializa a sus escritores solo) |

La clave de un candado asesor de PostgreSQL es un entero de 64 bits; se deriva del nombre con SHA-256 (56 bits),
igual que `libracommerce` (`erp/compras.py`, `erp/reposicion_ordenes.py`), y no con `hashtext()`: es la misma en cualquier
driver y no depende de qué función trae la versión del servidor. Dos nombres distintos que choquen serían un falso
«ocupado», nunca un falso «libre».

🔴 **Ninguno de los dos reemplaza a una restricción única en la base** donde se la pueda tener: el candado evita el
trabajo duplicado (una nota ante ARCA, un INSERT); la restricción es la red de abajo.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from typing import Any

from . import bloqueo_de_archivo

logger = logging.getLogger(__name__)

#: Cuánto espera `una_vez_al_arrancar` al candado antes de rendirse. Un hook de arranque (sembrar un admin, las
#: tablas semilla) tarda segundos; si pasaron dos minutos, el que lo tiene está colgado y mejor que el arranque lo diga
#: a quedarse esperando para siempre.
ESPERA_AL_ARRANCAR = 120.0

#: Cada cuánto reintenta mientras espera.
_PASO = 0.1

_PREFIJO = "libracore-candado:"

# Los candados de este proceso (sin conexión, o SQLite). Un `flock` abierto dos veces **dentro del mismo proceso**
# también se excluye, pero este conjunto evita abrir un archivo para lo que se resuelve en memoria.
_DEL_PROCESO: set[str] = set()
_DEL_PROCESO_LOCK = threading.Lock()


def _clave(nombre: str) -> int:
    """El entero de 64 bits (positivo, 56 bits usados) que identifica a `nombre` en los candados asesores."""
    return int.from_bytes(hashlib.sha256((_PREFIJO + nombre).encode()).digest()[:7], "big")


def _ruta_del_archivo(nombre: str) -> str:
    """El archivo del `flock` de `nombre`: en `DATA_DIR` (el volumen de la instancia, compartido entre los procesos)."""
    base = os.environ.get("DATA_DIR", os.getcwd())
    return os.path.join(base, f".libracore-candado-{hashlib.sha256(nombre.encode()).hexdigest()[:16]}.lock")


def _pedir_en_postgres(conn: Any, sentencia: str, clave: int) -> bool | None:
    """Ejecuta `SELECT <sentencia>(<clave>)` en `conn` y devuelve el booleano; `None` si `conn` no es PostgreSQL.

    Acepta las conexiones que circulan en la familia: el `ConnectionWrapper` de `libracore.db`, un `psycopg.Connection`,
    y una `Session` o `Connection` de SQLAlchemy (LibraCargo, LibraClub, LibraDesk). La `sentencia` es un nombre de
    función fijo del código, nunca un dato.
    """
    if isinstance(conn, sqlite3.Connection):
        return None
    if hasattr(conn, "get_bind") or hasattr(conn, "exec_driver_sql"):          # SQLAlchemy: Session / Connection
        from sqlalchemy import text

        motor = conn.get_bind() if hasattr(conn, "get_bind") else conn
        if motor.dialect.name != "postgresql":
            return None
        return bool(conn.execute(text(f"SELECT {sentencia}(:clave)"), {"clave": clave}).scalar())
    from .db._postgres import ConnectionWrapper

    if isinstance(conn, ConnectionWrapper):
        return bool(conn.execute(f"SELECT {sentencia}(?)", (clave,)).fetchone()[0])
    if type(conn).__module__.startswith("psycopg"):
        return bool(conn.execute(f"SELECT {sentencia}(%s)", (clave,)).fetchone()[0])
    raise TypeError(f"candado_de_base: no sé qué conexión es {type(conn).__name__!r} "
                    "(espero la de libracore.db, psycopg, SQLAlchemy o sqlite3)")


def _en_postgres(conn: Any, sentencia: str, nombre: str) -> bool | None:
    return _pedir_en_postgres(conn, sentencia, _clave(nombre))


@contextlib.contextmanager
def _candado_de_proceso(nombre: str) -> Iterator[bool]:
    """El candado cuando no hay PostgreSQL: un conjunto en memoria. Dura lo que el `with`."""
    with _DEL_PROCESO_LOCK:
        obtenido = nombre not in _DEL_PROCESO
        if obtenido:
            _DEL_PROCESO.add(nombre)
    try:
        yield obtenido
    finally:
        if obtenido:
            with _DEL_PROCESO_LOCK:
                _DEL_PROCESO.discard(nombre)


@contextlib.contextmanager
def _candado_de_archivo(nombre: str, *, espera: float = 0.0) -> Iterator[bool]:
    """`flock` exclusivo sobre un archivo de `DATA_DIR`, entre procesos. `espera` = 0 es no bloqueante.

    Si el archivo no se puede abrir (un `DATA_DIR` de sólo lectura) no se inventa una exclusión: se avisa en el log y
    se sigue **sin** candado entre procesos, que es lo que había antes de este módulo.
    """
    try:
        fd = os.open(_ruta_del_archivo(nombre), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        logger.warning("candado %r sin exclusión entre procesos (no se pudo abrir el archivo): %s", nombre, e)
        yield True
        return
    obtenido = False
    try:
        limite = time.monotonic() + espera
        while True:
            if bloqueo_de_archivo.intentar(fd):
                obtenido = True
                break
            if time.monotonic() >= limite:
                break
            time.sleep(_PASO)
        yield obtenido
    finally:
        if obtenido:
            bloqueo_de_archivo.soltar(fd)
        os.close(fd)


@contextlib.contextmanager
def candado_de_base(conn: Any, nombre: str) -> Iterator[bool]:
    """Intenta tomar el candado `nombre` **sin esperar**. Entrega `True` si lo tomó y `False` si lo tiene otro.

    Quien recibe `False` decide qué hacer (la nota de crédito contesta «ya hay una en curso»); este candado nunca
    bloquea el pedido.

    - **PostgreSQL** (`conn` es la conexión del que llama: la de `libracore.db`, `psycopg` o una `Session` /
      `Connection` de SQLAlchemy): `pg_try_advisory_xact_lock`. 🔴 **Dura hasta que termina la transacción de
      `conn`** (`commit` o `rollback`), no hasta el fin del `with`: PostgreSQL no suelta un candado de transacción
      antes. Es lo que se quiere cuando lo protegido se escribe en esa misma transacción —el siguiente pedido ya
      encuentra lo que dejó el primero—, y obliga a una regla: **quien llama no hace `commit` ni `rollback` de `conn`
      mientras dure lo que protege**, o lo suelta antes de tiempo. Tomarlo dos veces con la misma conexión no se
      excluye (los candados asesores son reentrantes por sesión): la exclusión es entre conexiones.
    - **SQLite**: un `flock` sobre un archivo de `DATA_DIR`, que dura lo que el `with`. Sirve entre procesos que
      comparten ese directorio (tests, nodos sueltos).
    - **`conn=None`**: sólo un candado **del proceso**, que dura lo que el `with`. Es el comportamiento de antes de
      los varios workers.
    """
    if conn is None:
        with _candado_de_proceso(nombre) as obtenido:
            yield obtenido
        return
    obtenido = _en_postgres(conn, "pg_try_advisory_xact_lock", nombre)
    if obtenido is not None:
        yield obtenido
        return
    with _candado_de_proceso(nombre) as en_proceso:
        if not en_proceso:
            yield False
            return
        with _candado_de_archivo(nombre) as en_archivo:
            yield en_archivo


def esperar_candado_de_transaccion(conn: Any, nombre: str, *, espera: float = ESPERA_AL_ARRANCAR) -> bool:
    """Toma el candado `nombre` en la conexión `conn`, **esperando** a quien lo tenga, hasta el fin de **su** transacción.

    ```python
    esperar_candado_de_transaccion(conn, "mi-esquema")      # lo primero de la transacción
    ...DDL, seeds...
    conn.commit()                                           # acá se suelta, y entra el que esperaba
    ```

    Es la cara bloqueante de `candado_de_base`: la misma clave y el mismo `pg_try_advisory_xact_lock`, pero reintentado
    cada 0,1 s en lugar de contestar «ocupado». No hay `with` porque PostgreSQL no suelta un candado de transacción antes
    del `commit` o el `rollback` de `conn`. Sirve para **una transacción larga que dos procesos no pueden correr a la
    vez**: `init_core_schema()` mezcla DDL y escrituras en una sola transacción y, corrida por dos workers juntos, da
    `deadlock detected` (cada uno se queda con un `ROW EXCLUSIVE` de la tabla que el otro quiere indexar) o una clave
    duplicada del catálogo (dos `CREATE TABLE IF NOT EXISTS` de la misma tabla).

    - Devuelve `True` si lo tomó (PostgreSQL) y `False` si `conn` no es PostgreSQL: SQLite serializa a sus escritores
      con el bloqueo de la base y no tiene este problema, así que **no se inventa un `flock`**.
    - 🔴 **Va antes de cualquier otra cosa de la transacción**: un candado tomado a mitad de camino no protege lo que ya
      se tocó. Y la conexión tiene que estar en una transacción (no `autocommit`): con `autocommit` PostgreSQL lo suelta
      al terminar el `SELECT` que lo pidió y no excluye nada.
    - Tomarlo dos veces con la misma conexión no espera (los candados asesores son reentrantes por sesión).
    - Si pasan `espera` segundos sin conseguirlo levanta `TimeoutError`: quien lo tiene está colgado, y un arranque que
      se queda esperando en silencio es peor que uno que falla diciendo por qué.
    """
    limite = time.monotonic() + espera
    while True:
        obtenido = _en_postgres(conn, "pg_try_advisory_xact_lock", nombre)
        if obtenido is None:
            return False
        if obtenido:
            return True
        if time.monotonic() >= limite:
            raise TimeoutError(f"{nombre!r}: no se pudo tomar el candado en {espera:.0f} s: "
                               "otra conexión lo tiene y no termina su transacción.")
        time.sleep(_PASO)


@contextlib.contextmanager
def una_vez_al_arrancar(nombre: str, *, destino: str | None = None, espera: float = ESPERA_AL_ARRANCAR) -> Iterator[None]:
    """Serializa un hook de arranque entre los procesos de una instancia: **de a uno por vez, los demás esperan**.

    ```python
    with una_vez_al_arrancar("sembrar-admin"):
        if not hay_admin():
            crear_admin()
    ```

    Con dos workers, cada uno corre el arranque del producto, y un «si no existe, crear» se contesta «no existe» en
    los dos antes de que ninguno haya creado. Con esto el segundo espera al primero y **ve lo que dejó**: el hook
    tiene que seguir siendo idempotente —preguntar adentro del `with`—; el candado no lo saltea, lo ordena.

    - **PostgreSQL**: `pg_try_advisory_lock` reintentado con una conexión **propia** (`destino`, o la de
      `libracore.db.core`), que se cierra al salir; si el proceso muere con el candado puesto, PostgreSQL lo suelta
      solo. No toca ninguna transacción del que llama: puede usarse con la base a medio configurar.
    - **SQLite**: `flock` sobre un archivo de `DATA_DIR`.

    Si pasan `espera` segundos sin conseguirlo levanta `TimeoutError`: un arranque que se queda esperando en
    silencio es peor que uno que falla diciendo por qué.
    """
    from .db import core

    destino = destino or core._db_path
    if destino is None:
        raise RuntimeError("una_vez_al_arrancar: libracore.db.core no está configurado ni se pasó `destino`.")
    if not core.es_url_postgres(destino):
        with _candado_de_archivo(nombre, espera=espera) as obtenido:
            if not obtenido:
                raise TimeoutError(f"El arranque {nombre!r} no pudo tomar su candado en {espera:.0f} s.")
            yield
        return

    conn = core.conectar(destino)
    try:
        limite = time.monotonic() + espera
        while not _en_postgres(conn, "pg_try_advisory_lock", nombre):
            conn.commit()                                   # no dejar la espera dentro de una transacción abierta
            if time.monotonic() >= limite:
                raise TimeoutError(f"El arranque {nombre!r} no pudo tomar su candado en {espera:.0f} s: "
                                   "otro proceso lo tiene y no termina.")
            time.sleep(_PASO)
        conn.commit()
        try:
            yield
        finally:
            with contextlib.suppress(Exception):            # si la conexión murió, PostgreSQL ya lo soltó
                _en_postgres(conn, "pg_advisory_unlock", nombre)
                conn.commit()
    finally:
        with contextlib.suppress(Exception):
            conn.close()
