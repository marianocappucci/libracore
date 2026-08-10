import os

import pytest

from libracore.db import core
from libracore.db._postgres import _paramstyle


def test_sqlite_configuration_stays_unchanged(tmp_path):
    core.configure(str(tmp_path / "libra.db"))
    with core.get_connection() as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)")
        cursor = conn.execute("INSERT INTO probe (value) VALUES (?)", ("sqlite",))
        assert cursor.lastrowid == 1
        assert conn.execute("SELECT * FROM probe WHERE id=?", (1,)).fetchone()["value"] == "sqlite"


def test_postgres_compatibility_when_configured():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")

    core.configure(url)
    with core.get_connection() as conn:
        conn.execute("CREATE TEMP TABLE probe (id SERIAL PRIMARY KEY, value TEXT)")
        cursor = conn.execute("INSERT INTO probe (value) VALUES (?)", ("postgres",))
        assert cursor.lastrowid == 1
        assert conn.execute("SELECT * FROM probe WHERE id=?", (1,)).fetchone()["value"] == "postgres"


def test_sqlite_dialect_translation():
    # Las tres formas de `datetime('now')` tienen que producir TEXTO con el
    # formato exacto de SQLite ('YYYY-MM-DD HH:MM:SS'), no un timestamp: las 30
    # columnas `created_at` del schema son TEXT y hay codigo que las parsea.
    # `CURRENT_TIMESTAMP` a secas escribia microsegundos y offset de zona.
    assert _paramstyle("SELECT datetime('now')") == (
        "SELECT to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
    )
    assert _paramstyle("SELECT datetime('now', 'localtime')") == (
        "SELECT to_char(LOCALTIMESTAMP, 'YYYY-MM-DD HH24:MI:SS')"
    )
    assert _paramstyle("SELECT datetime('now', 'localtime', ?)") == (
        "SELECT to_char(LOCALTIMESTAMP + %s::interval, 'YYYY-MM-DD HH24:MI:SS')"
    )
    # Contraprueba de lo que se rompio: ninguna de las tres puede dejar un
    # timestamp crudo, que es lo que metia los microsegundos.
    for forma in ("datetime('now')", "datetime('now', 'localtime')"):
        traducida = _paramstyle(f"SELECT {forma}")
        assert "to_char(" in traducida
        assert not traducida.endswith("CURRENT_TIMESTAMP")

    assert "DOUBLE PRECISION" in _paramstyle("SELECT CAST(value AS REAL)")
    ddl = _paramstyle(
        "CREATE TABLE probe (id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL, payload BLOB, created_at TEXT DEFAULT (datetime('now')))"
    )
    assert "BIGSERIAL PRIMARY KEY" in ddl
    assert "DOUBLE PRECISION" in ddl
    assert "BYTEA" in ddl
    assert "to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')" in ddl


def test_round_de_dos_argumentos_castea_cualquier_expresion():
    """🔴 `round(double precision, integer)` no existe en PostgreSQL.

    La traduccion anterior era una regex que exigia literalmente
    `ROUND(SUM(...), n)`. La consulta real del reporte de stock bajo es
    `ROUND(COALESCE(SUM(...), 0), 3)` y no matcheaba: pasaba entera al motor y
    reventaba. Se cubren las dos formas y el anidamiento.
    """
    assert _paramstyle("SELECT ROUND(SUM(x), 2)") == (
        "SELECT CAST(ROUND(CAST(SUM(x) AS NUMERIC), 2) AS DOUBLE PRECISION)"
    )
    assert _paramstyle("SELECT ROUND(COALESCE(SUM(x), 0), 3)") == (
        "SELECT CAST(ROUND(CAST(COALESCE(SUM(x), 0) AS NUMERIC), 3) AS DOUBLE PRECISION)"
    )
    # Un solo argumento sí existe en PostgreSQL: no se toca.
    assert _paramstyle("SELECT ROUND(x)") == "SELECT ROUND(x)"
    # Y no se confunde con un identificador que termina en "round".
    assert _paramstyle("SELECT background(x, 2)") == "SELECT background(x, 2)"


def test_sqlite_reporting_translation():
    sql = _paramstyle(
        "SELECT strftime('%Y-%m', fecha), printf('%04d', punto_venta), "
        "GROUP_CONCAT(medio, '|'), date('now') FROM caja"
    )
    assert "to_char(cast(fecha AS date), 'YYYY-MM')" in sql
    assert "lpad(cast(punto_venta AS text), 4, '0')" in sql
    assert "string_agg(medio, '|')" in sql
    # `date('now')` va a TEXTO, no a `CURRENT_DATE`: se compara contra columnas
    # TEXT ISO y `text < date` no tiene operador en PostgreSQL.
    assert "to_char(CURRENT_DATE, 'YYYY-MM-DD')" in sql


def test_sqlite_json_each_translation():
    sql = _paramstyle(
        "SELECT ji.value->>'$.nombre' FROM ventas v, json_each(v.items) ji"
    )
    assert "ji.value->> 'nombre'" in sql
    assert "jsonb_array_elements(v.items::jsonb) AS ji(value)" in sql


def test_declaracion_fk_fuera_de_orden_se_difiere():
    sql = _paramstyle(
        "CREATE TABLE caja_movimientos (turno_id INTEGER REFERENCES turnos_caja(id) ON DELETE SET NULL)"
    )
    assert "REFERENCES turnos_caja" not in sql
    assert "REFERENCES ventas" not in _paramstyle(
        "CREATE TABLE movimientos_stock (venta_id INTEGER REFERENCES ventas(id) ON DELETE SET NULL)"
    )
    alter = _paramstyle(
        "ALTER TABLE caja_movimientos ADD CONSTRAINT fk FOREIGN KEY (turno_id) "
        "REFERENCES turnos_caja(id) ON DELETE SET NULL"
    )
    assert "REFERENCES turnos_caja" in alter


def test_solo_se_difieren_las_dos_fks_que_van_fuera_de_orden():
    """🔴 Regresión: una FK diferida que nadie vuelve a agregar es una FK perdida.

    El diferimiento se hacía por nombre de tabla referenciada, así que
    `REFERENCES turnos_caja(id) ON DELETE SET NULL` desaparecía de **cualquier**
    `CREATE TABLE`. Tres tablas no lo necesitaban —`turnos_caja` ya existe
    cuando se crean— y a ninguna se la volvía a poner: `ventas` en LibraCore, y
    `venta_links` en Contalibra y en Restolibra quedaban sin integridad
    referencial en PostgreSQL, y con ella en SQLite. Lo encontró el volcado de
    `schema_dump.py` al diffear los dos motores (42 FKs contra 41).

    Este test es la contraparte barata del gate de `test_schema_congelado.py`:
    no necesita un PostgreSQL levantado, así que se pone rojo en cualquier
    corrida.
    """
    ventas = _paramstyle(
        "CREATE TABLE IF NOT EXISTS ventas (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "turno_id INTEGER REFERENCES turnos_caja(id) ON DELETE SET NULL)"
    )
    assert "REFERENCES turnos_caja(id) ON DELETE SET NULL" in ventas

    venta_links = _paramstyle(
        "CREATE TABLE IF NOT EXISTS venta_links (\n"
        "    venta_id INTEGER PRIMARY KEY REFERENCES sales(id) ON DELETE CASCADE,\n"
        "    turno_id INTEGER REFERENCES turnos_caja(id) ON DELETE SET NULL\n)"
    )
    assert "REFERENCES turnos_caja(id) ON DELETE SET NULL" in venta_links

    # Y la que sí va fuera de orden se sigue difiriendo escrita como la escribe
    # el schema —una columna por línea—, no sólo en el renglón único de arriba.
    multilinea = _paramstyle(
        "CREATE TABLE IF NOT EXISTS caja_movimientos (\n"
        "    id       INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    turno_id INTEGER REFERENCES turnos_caja(id) ON DELETE SET NULL\n)"
    )
    assert "REFERENCES turnos_caja" not in multilinea
    assert "turno_id INTEGER" in multilinea


def test_sqlite_round_translation():
    sql = _paramstyle("SELECT ROUND(SUM(total), 2) FROM ventas")
    assert "CAST(ROUND(CAST(SUM(total) AS NUMERIC), 2) AS DOUBLE PRECISION)" in sql


def test_qmark_en_comentario_no_se_convierte_en_placeholder():
    sql = _paramstyle("-- ¿esta consulta?\nCREATE INDEX idx ON tabla(id)")
    assert "¿esta consulta?" in sql
    assert "%s" not in sql
