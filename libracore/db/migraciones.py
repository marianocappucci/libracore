"""Puente entre Alembic y la capa de conexión de LibraCore.

El problema que resuelve, que es el que trabó este trabajo dos veces:
`init_core_schema()` **no habla SQLAlchemy**. Espera una conexión de
`libracore.db.core` — DB-API con parámetros `?`, `executescript()`, `PRAGMA
table_info(...)` — y contra PostgreSQL espera además el adaptador que traduce
todo eso. Una revisión de Alembic, en cambio, trabaja sobre su propio bind.

La salida no es abrir una segunda conexión: es **envolver la misma**. Del bind
se saca la conexión DB-API cruda y se la presenta como lo que LibraCore espera.
Así el DDL y el `alembic_version` viajan en la misma transacción, que es lo que
hace que una migración interrumpida no deje la base a medio camino con la
versión ya escrita.

> ⚠️ En SQLite eso vale para todo menos el `executescript()` del bloque grande:
> `sqlite3` hace un COMMIT implícito antes de correrlo. No es un problema acá
> porque `init_core_schema()` es idempotente de punta a punta —correrla de nuevo
> sobre lo ya aplicado es un no-op—, pero conviene saberlo antes de escribir una
> revisión que no lo sea.
"""
from __future__ import annotations
from libracore.db.core import Conexion


def conexion_libracore(bind):
    """La conexión DB-API del bind de Alembic, presentada como la espera
    `libracore.db`.

    Contra PostgreSQL devuelve el adaptador de LibraCore sobre la conexión
    psycopg del propio bind; contra SQLite, la `Conexion` cruda, que
    ya es exactamente lo que la capa usa.
    """
    from . import core

    cruda = bind.connection.driver_connection

    if core.is_postgres():
        from ._postgres import ConnectionWrapper

        return ConnectionWrapper(cruda)
    return cruda
