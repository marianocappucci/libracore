"""La carrera de la bandeja, contra el motor donde se rompe.

🔴 **Por que existe este archivo.** `upsert_comprobante` mira si el comprobante
ya esta, y si no lo inserta. Entre esas dos cosas otra alta simultanea del mismo
origen puede ganar la carrera: el `UNIQUE` la frena, el `except IntegrityError`
la atrapa y la respuesta correcta es devolver la fila que quedo.

Ese `except` hacia un `SELECT` **sobre la misma conexion**. En SQLite funciona.
Contra PostgreSQL no: ahi un error ABORTA la transaccion, y toda consulta
posterior sobre esa conexion muere con *"current transaction is aborted"*. O
sea que el manejo de la carrera fallaba **exactamente cuando la carrera
ocurria**, que es el unico momento en que corre.

No se veia porque los tests de esa funcion usan SQLite y porque la carrera es,
por definicion, rara. Lo anticipa el docstring de `_errores_como_sqlite3` en
`_postgres.py`: el adaptador traduce los NOMBRES de las excepciones y avisa que
esta diferencia de comportamiento queda afuera.
"""
import os

import pytest

from libracore.db import comprobantes_pendientes as cp
from libracore.db import core
from libracore.db.schema import init_core_schema


def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    return url


@pytest.fixture
def base_postgres():
    """Schema limpio en PostgreSQL, y `core` apuntado ahi."""
    url = _url()
    import psycopg

    crudo = url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(crudo, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS public CASCADE")
        c.execute("CREATE SCHEMA public")

    core.configure(db_path=url)
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    yield
    conn.close()
    core._db_path = None


ORIGEN = dict(
    origen_producto="libradesk",
    origen_instancia="compulibra",
    origen_tipo=cp.ORIGEN_CUOTA_CONTRATO,
    origen_id="42",
)


def _alta(**extra):
    datos = dict(
        cliente_razon="Ferreteria San Martin",
        cliente_cuit="30-71234567-9",
        items=[{"description": "Alquiler impresora", "qty": 1,
                "unit_price": 45000.0, "iva_rate": 0.21}],
        **ORIGEN,
    )
    datos.update(extra)
    return cp.upsert_comprobante(**datos)


class _ConexionQueNoVeLaFilaAjena:
    """Simula la carrera: el primer `SELECT` no ve la fila que ya esta.

    Es la unica forma determinista de llegar al `except`. Con dos hilos de
    verdad el test seria flaky y no probaria nada la mayoria de las corridas.
    """

    def __init__(self, real):
        self._real = real
        self._primera = True

    def execute(self, sql, *a, **kw):
        if self._primera and sql.strip().upper().startswith("SELECT"):
            self._primera = False
            return _CursorVacio()
        return self._real.execute(sql, *a, **kw)

    def __getattr__(self, nombre):
        return getattr(self._real, nombre)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *a):
        return self._real.__exit__(*a)


class _CursorVacio:
    def fetchone(self):
        return None

    def fetchall(self):
        return []


def test_la_carrera_devuelve_la_fila_que_gano(base_postgres, monkeypatch):
    """El caso que el UNIQUE viene a cubrir, contra el motor real.

    Sin el `rollback()` en el `except`, el `SELECT` de recuperacion muere con
    *"current transaction is aborted"* y el alta explota en vez de devolver la
    fila ajena.
    """
    ganador_id, creado = _alta()
    assert creado is True

    real = core.get_connection
    monkeypatch.setattr(
        cp, "get_connection",
        lambda: _ConexionQueNoVeLaFilaAjena(real()),
    )

    perdedor_id, creado2 = _alta(cliente_razon="Otro nombre")

    assert creado2 is False, "deberia reportar que no lo creo el"
    assert perdedor_id == ganador_id, (
        "no devolvio la fila que gano la carrera"
    )


def test_control_sin_carrera_el_alta_normal_sigue_andando(base_postgres):
    """🔑 El control. Sin esto, una `upsert_comprobante` que fallara SIEMPRE
    haria pasar el test de arriba por el camino equivocado --- o peor, el de
    arriba podria estar midiendo el `except` de un alta que nunca funciono.
    """
    primero, creado = _alta()
    assert creado is True
    segundo, creado2 = _alta(cliente_razon="Actualizado")
    assert creado2 is False and segundo == primero
