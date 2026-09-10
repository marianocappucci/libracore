"""`set_addon`: prender y apagar un add-on suelto en la instancia.

La implementación vivía sólo en `contalibra/app/db_modulos.py`. Se movió al
motor el 2026-09-09 porque el backoffice la invoca por `docker exec` como
`app.database.set_addon` en CUALQUIER producto que declare `plans.ADDONS`, y
LibraDesk declaró `modo_simple` sin tenerla — el toggle moría con `ImportError`.
Una sola implementación acá, reexportada por cada producto.

Corre contra PostgreSQL (el motor real de la familia), con el mismo arnés que
`test_apply_plan_addons.py`.
"""
import os
import sys
import types

import pytest

from libracore.db import core
from libracore.db.modulos import apply_plan, get_modulos, set_addon
from libracore.db.schema import init_core_schema


def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    return url


@pytest.fixture
def base_postgres():
    """Schema limpio en PostgreSQL con la tabla `modulos`, y `core` apuntado ahí."""
    url = _url()
    import psycopg

    crudo = url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(crudo, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS public CASCADE")
        c.execute("CREATE SCHEMA public")

    core.configure(db_path=url)
    with core.get_connection() as conn:
        init_core_schema(conn)
    yield
    core._db_path = None


def _sembrar(*modulos):
    with core.get_connection() as conn:
        for m in modulos:
            conn.execute(
                "INSERT OR IGNORE INTO modulos (modulo, habilitado, plan) VALUES (?,?,?)",
                (m, 0, "basico"),
            )


@pytest.fixture
def columna_boolean(base_postgres):
    """La otra mitad del parque: `modulos.habilitado` declarada `boolean`.

    🔑 `init_core_schema()` la crea `INTEGER`, y así la tienen contalibra,
    restolibra y ventalibra. Pero libradesk, gestiolibra y medlibra la crean con
    el Alembic propio del producto (`sa.Boolean()`) — medido el 2026-09-09
    contra las bases vivas del VPS.

    PostgreSQL no castea entre los dos, así que **un test que corra sólo contra
    el schema del motor no puede ver el defecto**: la versión original de
    `set_addon` (la de Contalibra, con `1`/`0`) pasa en `integer` y muere en
    `boolean`. Este fixture es lo que hace que la suite mire donde el defecto
    se escondía.
    """
    with core.get_connection() as conn:
        conn.execute("ALTER TABLE modulos ALTER COLUMN habilitado DROP DEFAULT")
        conn.execute(
            "ALTER TABLE modulos ALTER COLUMN habilitado TYPE boolean "
            "USING (habilitado <> 0)"
        )
        conn.execute("ALTER TABLE modulos ALTER COLUMN habilitado SET DEFAULT true")
    yield


def test_prende_y_apaga_con_la_columna_boolean(columna_boolean):
    """El caso LibraDesk. Con `1`/`0` esto muere con `is of type boolean`."""
    set_addon("modo_simple", True)
    assert get_modulos()["modo_simple"] is True

    set_addon("modo_simple", False)
    assert get_modulos()["modo_simple"] is False


def test_la_columna_boolean_tambien_crea_la_fila(columna_boolean):
    assert "modo_simple" not in get_modulos()
    set_addon("modo_simple", True)
    assert get_modulos()["modo_simple"] is True


def test_prende_y_apaga_un_modulo_existente(base_postgres):
    _sembrar("mayorista")
    assert get_modulos()["mayorista"] is False

    set_addon("mayorista", True)
    assert get_modulos()["mayorista"] is True

    set_addon("mayorista", False)
    assert get_modulos()["mayorista"] is False


def test_crea_la_fila_si_falta(base_postgres):
    """El add-on de un producto que nunca lo sembró todavía no tiene fila.

    Sin el `INSERT`, el `UPDATE` afectaría cero filas y la función saldría con
    éxito **sin haber prendido nada** — un toggle que reporta OK y no hace nada.
    """
    assert "modo_simple" not in get_modulos()

    set_addon("modo_simple", True)

    assert get_modulos()["modo_simple"] is True


def test_es_idempotente(base_postgres):
    """Prenderlo dos veces no duplica la fila ni lo apaga de rebote."""
    set_addon("modo_simple", True)
    set_addon("modo_simple", True)
    assert get_modulos()["modo_simple"] is True
    with core.get_connection() as conn:
        filas = conn.execute(
            "SELECT count(*) AS n FROM modulos WHERE modulo=?", ("modo_simple",)
        ).fetchone()
    assert filas["n"] == 1


def test_el_addon_prendido_sobrevive_a_un_cambio_de_plan(base_postgres):
    """El control que le da sentido a todo esto: `apply_plan` no lo pisa.

    Sin este assert, un `set_addon` correcto seguiría siendo inútil — el add-on
    se apagaría solo en el próximo cambio de plan.
    """
    _sembrar("caja", "ventas", "stock")
    set_addon("modo_simple", True)

    m = types.ModuleType("plans")
    m.PLAN_MODULOS = {"basico": {"caja"}, "premium": {"caja", "ventas", "stock"}}
    m.modulos_de_plan = lambda p: set(m.PLAN_MODULOS.get(p, set()))
    m.ADDONS = {"modo_simple"}
    sys.modules["plans"] = m
    try:
        apply_plan("basico")
    finally:
        del sys.modules["plans"]

    mods = get_modulos()
    assert mods["modo_simple"] is True   # el add-on quedó donde estaba
    assert mods["ventas"] is False       # y el plan sí movió lo suyo
    assert mods["caja"] is True
