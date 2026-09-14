"""La línea de tiempo contra PostgreSQL real, sin la tabla de cierres diarios.

`db.logs.get_actividad_log()` saca sola la parte de `cierres_diarios` si la
tabla todavía no existe (la crea la migración `0009`, que un `-dev` no corre).
En SQLite eso lo cubre `tests/test_logs_reportes_libros.py`; acá se prueba la
rama de PostgreSQL de `_tabla_existe` (`to_regclass`), que es la que corren
todos los consumidores en producción: si fallara, se rompería la pantalla de
Logs de todos. Mismo arnés que `test_set_addon.py`.
"""
import os

import pytest

from libracore.db import cierre_diario, core
from libracore.db import logs as db_logs
from libracore.db.schema import init_core_schema


def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    return url


@pytest.fixture
def base_postgres():
    """Schema limpio en PostgreSQL, `core` apuntado ahí, SIN la migración 0009."""
    url = _url()
    import psycopg

    crudo = url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(crudo, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS public CASCADE")
        c.execute("CREATE SCHEMA public")

    core.configure(db_path=url)
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.execute(
            "INSERT INTO usuarios (id, username, nombre, password_hash, role) "
            "VALUES (7, 'ana', 'Ana', 'x', 'admin')"
        )
        conn.execute(
            "INSERT INTO turnos_caja (usuario_id, apertura, monto_inicial) "
            "VALUES (7, '2026-09-14 08:00:00', 500)"
        )
        conn.commit()
    yield
    core._db_path = None


def test_tabla_existe_en_postgres(base_postgres):
    with core.get_connection() as conn:
        assert db_logs._tabla_existe(conn, "cierres_diarios") is False
        assert db_logs._tabla_existe(conn, "turnos_caja") is True
        cierre_diario.crear_tablas(conn)
        conn.commit()
        assert db_logs._tabla_existe(conn, "cierres_diarios") is True


def test_sin_la_migracion_la_linea_de_tiempo_anda_en_postgres(base_postgres):
    """El caso real: el pin nuevo sin la 0009. La línea de tiempo tiene que
    mostrar el turno, y la conexión tiene que seguir sirviendo después (en
    PostgreSQL, tocar una tabla ausente aborta la transacción)."""
    with core.get_connection() as conn:
        tipos = {f["tipo"] for f in db_logs.get_actividad_log(conn=conn)}
        assert "turno" in tipos
        assert "cierre_diario" not in tipos
        # La misma conexión sigue viva: no quedó una transacción abortada.
        assert conn.execute("SELECT COUNT(*) FROM turnos_caja").fetchone()[0] == 1
