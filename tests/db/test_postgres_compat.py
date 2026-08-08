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
    assert "CURRENT_TIMESTAMP" in _paramstyle("SELECT datetime('now')")
    assert "CURRENT_TIMESTAMP + %s::interval" in _paramstyle(
        "SELECT datetime('now', 'localtime', ?)"
    )
    assert "DOUBLE PRECISION" in _paramstyle("SELECT CAST(value AS REAL)")
    ddl = _paramstyle(
        "CREATE TABLE probe (id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL, payload BLOB, created_at TEXT DEFAULT (datetime('now')))"
    )
    assert "BIGSERIAL PRIMARY KEY" in ddl
    assert "DOUBLE PRECISION" in ddl
    assert "BYTEA" in ddl
    assert "CURRENT_TIMESTAMP" in ddl


def test_sqlite_reporting_translation():
    sql = _paramstyle(
        "SELECT strftime('%Y-%m', fecha), printf('%04d', punto_venta), "
        "GROUP_CONCAT(medio, '|'), date('now') FROM caja"
    )
    assert "to_char(cast(fecha AS date), 'YYYY-MM')" in sql
    assert "lpad(cast(punto_venta AS text), 4, '0')" in sql
    assert "string_agg(medio, '|')" in sql
    assert "CURRENT_DATE" in sql


def test_sqlite_json_each_translation():
    sql = _paramstyle(
        "SELECT ji.value->>'$.nombre' FROM ventas v, json_each(v.items) ji"
    )
    assert "ji.value->> 'nombre'" in sql
    assert "jsonb_array_elements(v.items::jsonb) AS ji(value)" in sql


def test_sqlite_round_translation():
    sql = _paramstyle("SELECT ROUND(SUM(total), 2) FROM ventas")
    assert "ROUND(CAST(SUM(total) AS NUMERIC), 2)" in sql
