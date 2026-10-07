"""`init_core_schema()` siembra el depósito por defecto sólo en SU tabla `depositos`.

Existe por un rojo real: `libracore-migrar upgrade --prefijo libradesk` sobre una base
**vacía** (alta de un cliente, reset nocturno de la demo, restaurar un backup) moría con

    DatatypeMismatch: column "es_default" is of type boolean but expression is of type integer

LibraDesk tiene su propia tabla `depositos` (su migración `0005_depositos`, con `activo` y
`es_default` BOOLEAN), la cadena del producto corre primero y la `0001` del motor llama a
`init_core_schema()`, que veía la tabla vacía y le insertaba `es_default = 1`. PostgreSQL no
convierte un entero en booleano. Sobre una base con depósitos nunca pasó, porque la siembra
sólo corre con la tabla vacía.

Lo que fijan, en SQLite y en PostgreSQL:

1. 🔴 Que sobre una `depositos` ajena y vacía la función **no muerda ni siembre**: una base
   nueva de LibraDesk nace sin depósitos y su pantalla los crea; sembrar uno cambiaría el
   producto.
2. Que sobre la tabla del motor la siembra siga igual (`Depósito Principal`, `es_default`
   en 1), que es lo que esperan los ocho productos.
3. Que la función siga siendo idempotente en las dos situaciones.
"""
import os

import pytest

from libracore.db import core
from libracore.db.schema import init_core_schema

#: La `depositos` de LibraDesk, tal como la deja su migración `0005`: lo que importa
#: para este defecto son `activo` y `es_default`, que son BOOLEAN.
DEPOSITOS_AJENA_PG = """
    CREATE TABLE depositos (
        id          SERIAL PRIMARY KEY,
        cliente_id  INTEGER,
        nombre      VARCHAR(100) NOT NULL,
        descripcion VARCHAR(500),
        activo      BOOLEAN NOT NULL DEFAULT TRUE,
        es_default  BOOLEAN NOT NULL DEFAULT FALSE,
        created_at  TIMESTAMP DEFAULT now()
    )
"""

#: SQLite no distingue el tipo al insertar, pero lo declara: la tabla ajena se reconoce igual.
DEPOSITOS_AJENA_SQLITE = """
    CREATE TABLE depositos (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id  INTEGER,
        nombre      VARCHAR(100) NOT NULL,
        descripcion VARCHAR(500),
        activo      BOOLEAN NOT NULL DEFAULT 1,
        es_default  BOOLEAN NOT NULL DEFAULT 0,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""


def _liberar():
    core._db_path = None
    core._database_url = None


def _depositos(conn):
    return conn.execute("SELECT nombre, es_default FROM depositos ORDER BY id").fetchall()


def _url_postgres() -> str:
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("LIBRACORE_POSTGRES_URL no está definida en CI")
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


# --- SQLite -----------------------------------------------------------------------------


@pytest.fixture
def sqlite_vacia(tmp_path):
    core.configure(db_path=str(tmp_path / "siembra.db"))
    conn = core.get_connection()
    try:
        yield conn
    finally:
        conn.close()
        _liberar()


def test_sqlite_la_tabla_del_motor_se_siembra_como_siempre(sqlite_vacia):
    conn = sqlite_vacia
    init_core_schema(conn)
    conn.commit()

    assert [tuple(f) for f in _depositos(conn)] == [("Depósito Principal", 1)]


def test_sqlite_una_depositos_ajena_vacia_no_se_siembra(sqlite_vacia):
    conn = sqlite_vacia
    conn.executescript(DEPOSITOS_AJENA_SQLITE)
    conn.commit()

    init_core_schema(conn)
    conn.commit()

    assert _depositos(conn) == []
    # El resto del schema y de las siembras del motor sigue su curso.
    assert conn.execute("SELECT nombre FROM cajas WHERE es_default=1").fetchone()[0] == "Caja Principal"


def test_sqlite_es_idempotente_en_las_dos_situaciones(tmp_path):
    for nombre, ajena in (("motor", False), ("ajena", True)):
        core.configure(db_path=str(tmp_path / f"{nombre}.db"))
        conn = core.get_connection()
        try:
            if ajena:
                conn.executescript(DEPOSITOS_AJENA_SQLITE)
            init_core_schema(conn)
            primera = [tuple(f) for f in _depositos(conn)]
            init_core_schema(conn)
            conn.commit()
            assert [tuple(f) for f in _depositos(conn)] == primera
        finally:
            conn.close()
            _liberar()


# --- PostgreSQL -------------------------------------------------------------------------


@pytest.fixture
def _servidor_pg():
    """Antes que `bases`: ésta se saltea sin la variable, y en CI eso sería un falso verde."""
    return _url_postgres()


@pytest.fixture
def pg_vacia(_servidor_pg, bases):
    """Una base PostgreSQL recién creada, sin ninguna tabla, propia del test."""
    core.configure(bases.nueva())
    conn = core.get_connection()
    try:
        yield conn
    finally:
        conn.close()
        _liberar()


def test_postgres_la_tabla_del_motor_se_siembra_como_siempre(pg_vacia):
    conn = pg_vacia
    init_core_schema(conn)
    conn.commit()

    assert [tuple(f) for f in _depositos(conn)] == [("Depósito Principal", 1)]


def test_postgres_una_depositos_booleana_ajena_y_vacia_migra_sin_error_y_no_se_siembra(pg_vacia):
    """El defecto: antes moría con `DatatypeMismatch` en `es_default`."""
    conn = pg_vacia
    conn.execute(DEPOSITOS_AJENA_PG)
    conn.commit()

    init_core_schema(conn)
    conn.commit()

    assert _depositos(conn) == []
    assert conn.execute("SELECT nombre FROM cajas WHERE es_default=1").fetchone()[0] == "Caja Principal"


def test_postgres_una_depositos_ajena_con_filas_no_se_toca(pg_vacia):
    conn = pg_vacia
    conn.execute(DEPOSITOS_AJENA_PG)
    conn.execute("INSERT INTO depositos (nombre, es_default) VALUES ('Central', TRUE)")
    conn.commit()

    init_core_schema(conn)
    conn.commit()

    assert [tuple(f) for f in _depositos(conn)] == [("Central", True)]


def test_postgres_es_idempotente_sobre_la_ajena(pg_vacia):
    conn = pg_vacia
    conn.execute(DEPOSITOS_AJENA_PG)
    conn.commit()

    init_core_schema(conn)
    init_core_schema(conn)
    conn.commit()

    assert _depositos(conn) == []
