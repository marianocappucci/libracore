"""Las ESCRITURAS del adaptador, ejecutadas contra PostgreSQL.

Complementa a `test_postgres_lecturas.py`. Los dos defectos que cubre son de la
misma familia que todos los de esta migración —código que funciona en SQLite
porque SQLite es permisivo— y los encontró la suite de LibraDesk el 2026-08-09,
aplicando un plan de módulos:

1. 🔴 **`RETURNING id` a ciegas.** El wrapper se lo agrega a todo INSERT para
   emular `lastrowid`, pero no todas las tablas tienen `id`: `modulos` usa
   `modulo` como clave. El INSERT moría con *"column id does not exist"*, así
   que aplicar un plan contra PostgreSQL era imposible.
2. 🔴 **Un entero en una columna BOOLEAN.** `1`/`0` es lo natural en SQLite,
   que no tiene booleano; PostgreSQL corta con *"column is of type boolean but
   expression is of type smallint"*.
"""
import os

import pytest

from libracore.db.core import conectar, es_url_postgres


def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    return url


@pytest.fixture
def conn():
    """Una base limpia con las dos formas de tabla que importan: una con `id`
    y una sin `id`, que es la que destapó el defecto."""
    c = conectar(_url())
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.execute("CREATE TABLE con_id (id BIGSERIAL PRIMARY KEY, nombre TEXT)")
    c.execute(
        "CREATE TABLE modulos (modulo TEXT PRIMARY KEY, habilitado BOOLEAN, plan TEXT)"
    )
    c.commit()
    yield c
    c.close()


def test_insert_en_una_tabla_sin_id_no_agrega_returning(conn):
    """🔴 El defecto: `RETURNING id` sobre una tabla que no tiene `id`."""
    conn.execute(
        "INSERT INTO modulos (modulo, habilitado, plan) VALUES (?,?,?)",
        ("agenda", True, "basico"),
    )
    conn.commit()

    fila = conn.execute("SELECT modulo, habilitado FROM modulos").fetchone()
    assert fila["modulo"] == "agenda"
    assert fila["habilitado"] is True


def test_insert_en_una_tabla_con_id_sigue_dando_lastrowid(conn):
    """Contraprueba. Sin esto, "no agregar nunca RETURNING" pasaría el test de
    arriba y rompería `lastrowid` en las 30 tablas que sí tienen `id`."""
    cur = conn.execute("INSERT INTO con_id (nombre) VALUES (?)", ("primero",))
    assert cur.lastrowid == 1
    cur = conn.execute("INSERT INTO con_id (nombre) VALUES (?)", ("segundo",))
    assert cur.lastrowid == 2
    conn.commit()


def test_insert_or_ignore_en_una_tabla_sin_id(conn):
    """Las dos traducciones juntas: `ON CONFLICT DO NOTHING` **y** sin
    `RETURNING`. Es la combinación exacta de `apply_plan_modules`, que hace
    INSERT OR IGNORE + UPDATE por cada módulo."""
    for _ in range(2):
        conn.execute(
            "INSERT OR IGNORE INTO modulos (modulo, habilitado, plan) VALUES (?,?,?)",
            ("agenda", False, "basico"),
        )
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM modulos").fetchone()[0] == 1


def test_un_entero_en_una_columna_boolean_no_pasa(conn):
    """🔴 Que el motor **rechace** el entero es lo correcto, y queda fijado acá.

    No es un test de la defensa: es la razón por la que
    `apply_plan_modules` tuvo que pasar a mandar `bool`. Si algún día
    PostgreSQL o el driver empezaran a aceptarlo, este test se pone rojo y
    avisa que la conversión de allá dejó de ser necesaria — mejor eso que
    descubrirlo por un `habilitado` mal escrito.
    """
    import psycopg

    with pytest.raises(psycopg.errors.DatatypeMismatch):
        conn.execute(
            "INSERT INTO modulos (modulo, habilitado, plan) VALUES (?,?,?)",
            ("agenda", 1, "basico"),
        )
    conn.rollback()


# --- `conectar()`, que es lo que reemplaza a los `sqlite3.connect` sueltos ---

def test_conectar_distingue_ruta_de_url(tmp_path):
    """El criterio de "esto es PostgreSQL" vive en UN lugar.

    Cada call site decidiéndolo por su cuenta es como aparecieron el backup que
    trataba el nombre de la base como una ruta y el plan de módulos que abría
    su propio SQLite.
    """
    assert es_url_postgres("postgresql://u:p@h/db")
    assert es_url_postgres("postgresql+psycopg://u:p@h/db")
    assert not es_url_postgres("/datos/libradesk.db")
    assert not es_url_postgres("libradesk.db")

    # Y una ruta abre SQLite de verdad, sin estado global de por medio.
    import sqlite3

    c = conectar(str(tmp_path / "suelta.db"))
    try:
        assert isinstance(c, sqlite3.Connection)
    finally:
        c.close()


def test_conectar_rechaza_una_url_que_no_es_postgres(tmp_path):
    """Un `mysql://` mal copiado tiene que fallar acá y no crear un archivo
    SQLite llamado `mysql:` en el disco."""
    with pytest.raises(ValueError, match="no soportada"):
        conectar("mysql://u:p@h/db")
