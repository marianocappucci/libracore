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
| `esperar_candado_de_transaccion` | `pg_try_advisory_xact_lock` y, si está ocupado, `pg_advisory_xact_lock` **bloqueante** con `lock_timeout`, en la conexión de quien llama | no hace nada (SQLite serializa a sus escritores solo) |

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


def _es_postgres(conn: Any) -> bool:
    """Si `conn` es una conexión a PostgreSQL. Acepta las que circulan en la familia: el `ConnectionWrapper` de
    `libracore.db`, un `psycopg.Connection`, y una `Session` o `Connection` de SQLAlchemy (LibraCargo, LibraClub,
    LibraDesk). `sqlite3` y SQLAlchemy sobre otro motor dan `False`; cualquier otra cosa, `TypeError`."""
    if isinstance(conn, sqlite3.Connection):
        return False
    if hasattr(conn, "get_bind") or hasattr(conn, "exec_driver_sql"):          # SQLAlchemy: Session / Connection
        motor = conn.get_bind() if hasattr(conn, "get_bind") else conn
        return motor.dialect.name == "postgresql"
    from .db._postgres import ConnectionWrapper

    if isinstance(conn, ConnectionWrapper) or type(conn).__module__.startswith("psycopg"):
        return True
    raise TypeError(f"candado_de_base: no sé qué conexión es {type(conn).__name__!r} "
                    "(espero la de libracore.db, psycopg, SQLAlchemy o sqlite3)")


def _ejecutar(conn: Any, sql: str, params: tuple = ()) -> Any:
    """Ejecuta `sql` (con `?` como marcador) en la conexión PostgreSQL `conn` y devuelve la primera fila, o `None` si la
    sentencia no devuelve filas. El `sql` es un texto fijo del código, nunca un dato."""
    if hasattr(conn, "get_bind") or hasattr(conn, "exec_driver_sql"):          # SQLAlchemy
        from sqlalchemy import text

        consulta = sql
        valores = {}
        for i, valor in enumerate(params):
            consulta = consulta.replace("?", f":p{i}", 1)
            valores[f"p{i}"] = valor
        resultado = conn.execute(text(consulta), valores)
        return resultado.first() if resultado.returns_rows else None
    if type(conn).__module__.startswith("psycopg"):
        cur = conn.execute(sql.replace("?", "%s"), params)
    else:                                                                       # ConnectionWrapper (ya traduce `?`)
        cur = conn.execute(sql, params)
    return cur.fetchone() if cur.description is not None else None


def _pedir_en_postgres(conn: Any, sentencia: str, clave: int) -> bool | None:
    """Ejecuta `SELECT <sentencia>(<clave>)` en `conn` y devuelve el booleano; `None` si `conn` no es PostgreSQL.

    La `sentencia` es un nombre de función fijo del código, nunca un dato.
    """
    if not _es_postgres(conn):
        return None
    return bool(_ejecutar(conn, f"SELECT {sentencia}(?)", (clave,))[0])


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

    Es la cara bloqueante de `candado_de_base`: la misma clave, pero esperando en lugar de contestar «ocupado». No hay
    `with` porque PostgreSQL no suelta un candado de transacción antes del `commit` o el `rollback` de `conn`. Sirve para
    **una transacción larga que dos procesos no pueden correr a la vez**: `init_core_schema()` mezcla DDL y escrituras
    en una sola transacción y, corrida por dos workers juntos, da `deadlock detected` (cada uno se queda con un
    `ROW EXCLUSIVE` de la tabla que el otro quiere indexar) o una clave duplicada del catálogo (dos `CREATE TABLE IF NOT
    EXISTS` de la misma tabla).

    - Devuelve `True` si lo tomó (PostgreSQL) y `False` si `conn` no es PostgreSQL: SQLite serializa a sus escritores
      con el bloqueo de la base y no tiene este problema, así que **no se inventa un `flock`**.
    - 🔴 **Va antes de cualquier otra cosa de la transacción**: un candado tomado a mitad de camino no protege lo que ya
      se tocó. Y la conexión tiene que estar en una transacción (no `autocommit`): con `autocommit` PostgreSQL lo suelta
      al terminar el `SELECT` que lo pidió y no excluye nada.
    - Tomarlo dos veces con la misma conexión no espera (los candados asesores son reentrantes por sesión).
    - 🔴 **Si está ocupado espera dentro de PostgreSQL, no en un bucle de Python** (ADR-081): `pg_advisory_xact_lock`
      bloqueante con un `lock_timeout` de lo que le queda a `espera`. Así el que espera está **en la cola de bloqueos**
      y el detector de deadlocks de PostgreSQL lo ve: si el que tiene el candado espera algo que tiene éste (una tabla,
      otro candado), a ~1 s PostgreSQL aborta a uno con `deadlock detected` —un error claro, con los dos procesos y lo
      que pide cada uno— en lugar de dejar a los dos callados hasta `espera`. Con el bucle de `pg_try_advisory_xact_lock`
      que había PostgreSQL no veía nada (el que espera no espera ningún bloqueo, duerme) y un ciclo real duraba los 120 s
      completos (medido el 2026-10-24 en LibraClub: `TimeoutError ... 120 s`).
    - Si pasan `espera` segundos sin conseguirlo levanta `TimeoutError`: quien lo tiene está colgado, y un arranque que
      se queda esperando en silencio es peor que uno que falla diciendo por qué. La transacción de `conn` **sigue sana**
      (la espera corre dentro de un `SAVEPOINT` que se deshace) y el `lock_timeout` de la sesión queda como estaba.
    - Si PostgreSQL detecta un deadlock, el error es el de PostgreSQL (`psycopg.errors.DeadlockDetected`, o el de
      SQLAlchemy que lo envuelve) con una nota que dice qué candado se esperaba. Esa transacción ya está abortada: quien
      llama tiene que hacer `rollback`.
    """
    if not _es_postgres(conn):
        return False
    clave = _clave(nombre)
    if _ejecutar(conn, "SELECT pg_try_advisory_xact_lock(?)", (clave,))[0]:      # el caso normal: libre, una sola ida
        return True
    espera_ms = int(espera * 1000)
    if espera_ms >= 1 and _esperar_bloqueando(conn, nombre, clave, espera_ms):
        return True
    raise TimeoutError(f"{nombre!r}: no se pudo tomar el candado en {espera:.0f} s: "
                       "otra conexión lo tiene y no termina su transacción.")


#: Los `SQLSTATE` de PostgreSQL que distingue `esperar_candado_de_transaccion`.
_LOCK_NOT_AVAILABLE = "55P03"     # `lock_timeout` vencido
_DEADLOCK_DETECTED = "40P01"


def _sqlstate(error: BaseException) -> str | None:
    """El `SQLSTATE` de un error de la base: el de `psycopg`, o el de la excepción que lo envuelve (`orig` en SQLAlchemy;
    `__cause__` en el `ConnectionWrapper` de `libracore.db`, que lo traduce a una excepción de `sqlite3`)."""
    for e in (error, getattr(error, "orig", None), error.__cause__):
        estado = getattr(e, "sqlstate", None)
        if estado:
            return estado
    return None


def _esperar_bloqueando(conn: Any, nombre: str, clave: int, ms: int) -> bool:
    """`pg_advisory_xact_lock` con `lock_timeout` de `ms`: `True` si lo tomó, `False` si venció el tope.

    Un `SAVEPOINT` para que el tope vencido no aborte la transacción de quien llama (en PostgreSQL cualquier error la
    deja inservible hasta el `rollback`) y para que el `SET LOCAL` se deshaga con él. Si lo toma, el `lock_timeout` previo
    se restituye: `set_config(..., true)` dura hasta el fin de la transacción y, sin restituirlo, el DDL que sigue
    heredaría un tope que nunca pidió.
    """
    previo = _ejecutar(conn, "SELECT current_setting('lock_timeout')")[0]
    _ejecutar(conn, "SAVEPOINT libracore_candado")
    try:
        _ejecutar(conn, "SELECT set_config('lock_timeout', ?, true)", (f"{ms}ms",))
        _ejecutar(conn, "SELECT pg_advisory_xact_lock(?)", (clave,))
    except Exception as e:
        estado = _sqlstate(e)
        if estado == _LOCK_NOT_AVAILABLE:
            _ejecutar(conn, "ROLLBACK TO SAVEPOINT libracore_candado")
            _ejecutar(conn, "RELEASE SAVEPOINT libracore_candado")
            return False
        if estado == _DEADLOCK_DETECTED:
            e.add_note(f"Esperaba el candado {nombre!r} (libracore.candados.esperar_candado_de_transaccion): otra conexión "
                       "lo tiene y espera algo que tiene ésta. La transacción quedó abortada: hacé rollback.")
        raise
    _ejecutar(conn, "RELEASE SAVEPOINT libracore_candado")
    _ejecutar(conn, "SELECT set_config('lock_timeout', ?, true)", (previo,))
    return True


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
