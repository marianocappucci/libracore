"""Los errores de PostgreSQL llegan con el nombre que los productos atrapan.

Por qué existe: los seis productos y los dos motores tienen **23 lugares** que
hacen `except sqlite3.IntegrityError` (20) o `sqlite3.OperationalError` (1).
Contra PostgreSQL psycopg tira su propia jerarquía, así que **ninguno de esos
`except` atrapaba nada** y el error subía crudo hasta el usuario.

Medido en [[ventalibra]] el 2026-08-10: 12 de sus 16 rojos contra PostgreSQL
eran un `UniqueViolation` que el producto creía estar manejando —su test se
llama `test_create_second_default_price_list_fails` y esperaba un 4xx—.
"""
import os
import sqlite3

import pytest

from libracore.db import core


def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("LIBRACORE_POSTGRES_URL no está definida en CI")
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


@pytest.fixture
def conn():
    core.configure(_url())
    c = core.get_connection()
    c.execute("DROP SCHEMA IF EXISTS public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    yield c
    c.close()
    core._db_path = None
    core._database_url = None


def test_una_clave_repetida_llega_como_integrityerror(conn):
    """El caso exacto que rompía: un UNIQUE violado."""
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, k TEXT UNIQUE)")
    conn.execute("INSERT INTO t (k) VALUES (?)", ("una",))
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO t (k) VALUES (?)", ("una",))


def test_un_not_null_violado_tambien(conn):
    conn.execute("CREATE TABLE t2 (id INTEGER PRIMARY KEY AUTOINCREMENT, obligatorio TEXT NOT NULL)")
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO t2 (obligatorio) VALUES (NULL)")


def test_una_fk_violada_tambien(conn):
    conn.execute("CREATE TABLE padre (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    conn.execute(
        "CREATE TABLE hijo (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "padre_id INTEGER REFERENCES padre(id))"
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO hijo (padre_id) VALUES (9999)")


def test_una_tabla_que_no_existe_no_llega_como_integrityerror(conn):
    """Contraprueba: la traducción no aplasta todo a `IntegrityError`.

    Si lo hiciera, un `except IntegrityError` se comería un error de programación
    y el defecto se vería mucho después y en otro lado.
    """
    with pytest.raises(sqlite3.DatabaseError) as capturado:
        conn.execute("SELECT * FROM tabla_que_no_existe")
    assert not isinstance(capturado.value, sqlite3.IntegrityError)


def test_el_mensaje_de_postgres_no_se_pierde(conn):
    """Traducir el tipo no puede costar el diagnóstico: el texto original y la
    excepción de psycopg quedan encadenados."""
    conn.execute("CREATE TABLE t3 (k TEXT UNIQUE)")
    conn.execute("INSERT INTO t3 (k) VALUES ('x')")
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError) as capturado:
        conn.execute("INSERT INTO t3 (k) VALUES ('x')")

    assert "t3_k_key" in str(capturado.value)
    assert capturado.value.__cause__ is not None


def test_en_sqlite_el_comportamiento_no_cambia(tmp_path):
    """La traducción es sólo del backend PostgreSQL: en SQLite el error sale de
    la biblioteca directamente, como siempre."""
    core.configure(str(tmp_path / "t.db"))
    c = core.get_connection()
    try:
        c.execute("CREATE TABLE t (k TEXT UNIQUE)")
        c.execute("INSERT INTO t (k) VALUES ('x')")
        c.commit()
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO t (k) VALUES ('x')")
    finally:
        c.close()
        core._db_path = None
        core._database_url = None
