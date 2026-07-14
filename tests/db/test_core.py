import sqlite3
import pytest

from libracore.db import core


@pytest.fixture(autouse=True)
def _reset_core():
    """Cada test arranca sin configurar — evita que el orden de tests filtre estado."""
    core._db_path = None
    core._timeout = 5
    core._extra_pragmas = ()
    core._resolver_receta = None
    yield
    core._db_path = None
    core._timeout = 5
    core._extra_pragmas = ()
    core._resolver_receta = None


def test_get_connection_sin_configurar_falla():
    with pytest.raises(RuntimeError):
        core.get_connection()


def test_configure_y_get_connection(tmp_path):
    db_path = str(tmp_path / "test.db")
    core.configure(db_path=db_path, timeout=15)
    conn = core.get_connection()
    assert isinstance(conn, sqlite3.Connection)
    assert conn.row_factory is sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()

    conn2 = core.get_connection()
    assert conn2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    conn2.close()


def test_ar_now_formato():
    ts = core._ar_now()
    assert len(ts) == 19
    assert ts[4] == "-" and ts[13] == ":"


def test_minutos_desde():
    assert core.minutos_desde("") == 0
    assert core.minutos_desde(None) == 0
    ahora = core._ar_now()
    assert core.minutos_desde(ahora) == 0


def test_resolver_receta_default_none():
    assert core.get_resolver_receta() is None


def test_configure_resolver_receta():
    def _resolver(pid):
        return {"ingredientes": []}
    core.configure_resolver_receta(_resolver)
    assert core.get_resolver_receta() is _resolver
    core.configure_resolver_receta(None)
    assert core.get_resolver_receta() is None
