import os

import pytest

from libracore.db import core


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
