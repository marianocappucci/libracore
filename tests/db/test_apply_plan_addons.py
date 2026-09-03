"""`apply_plan` no toca los add-ons opcionales (`plans.ADDONS`).

Un add-on es un módulo pago que se habilita por instancia y NO pertenece a
ningún plan. `apply_plan` recorre las filas de `modulos` y fuerza a 0 todo lo
que el plan no incluye, así que sin un skip explícito un add-on se apagaría
solo en cada cambio de plan — un adicional que se desactiva en silencio al
subir o bajar de plan. Eso es lo que estos tests fijan.

Corre contra PostgreSQL (el motor real de la familia). `apply_plan` hace
`import plans` para resolver el plan→módulos; libracore es genérico y no tiene
`plans.py`, así que cada test inyecta uno falso en `sys.modules`.
"""
import os
import sys
import types

import pytest

from libracore.db import core
from libracore.db.modulos import apply_plan, get_modulos
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


def _plans_falso(*, con_addons: bool):
    """Un `plans.py` de juguete: tres planes anidados y, opcionalmente,
    `ADDONS`. Sin el atributo `ADDONS` reproduce un producto viejo (Restolibra)
    para el chequeo de retrocompatibilidad."""
    m = types.ModuleType("plans")
    m.PLAN_MODULOS = {
        "basico": {"caja", "ventas"},
        "estandar": {"caja", "ventas", "facturacion"},
        "premium": {"caja", "ventas", "facturacion", "stock"},
    }
    m.modulos_de_plan = lambda p: set(m.PLAN_MODULOS.get(p, set()))
    if con_addons:
        m.ADDONS = {"mayorista"}
    return m


def _seed(modulo, habilitado, plan):
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO modulos (modulo, habilitado, plan) VALUES (?,?,?)",
            (modulo, habilitado, plan),
        )


def _fila(modulo):
    with core.get_connection() as conn:
        return conn.execute(
            "SELECT habilitado, plan FROM modulos WHERE modulo=?", (modulo,)
        ).fetchone()


def test_addon_habilitado_sobrevive_a_cada_cambio_de_plan(base_postgres, monkeypatch):
    monkeypatch.setitem(sys.modules, "plans", _plans_falso(con_addons=True))
    _seed("caja", 1, "basico")
    _seed("stock", 0, "basico")
    _seed("mayorista", 1, "addon")

    # El add-on está prendido, y ningún plan lo baja — ni siquiera uno que no lo
    # incluye (ninguno lo incluye, ese es el punto).
    for plan in ("basico", "estandar", "premium", "estandar", "basico"):
        apply_plan(plan)
        mods = get_modulos()
        assert mods["mayorista"] is True, f"el add-on se apagó al aplicar {plan!r}"
        # Control: apply_plan SÍ sigue gateando lo que es de un plan. Si esto no
        # cambiara, el test de arriba pasaría con una función que no hace nada.
        assert mods["stock"] is (plan == "premium")

    # Y su columna `plan` quedó intacta: apply_plan no la reescribió con el
    # último plan aplicado.
    fila = _fila("mayorista")
    assert fila["plan"] == "addon"


def test_addon_deshabilitado_no_lo_prende_ningun_plan(base_postgres, monkeypatch):
    monkeypatch.setitem(sys.modules, "plans", _plans_falso(con_addons=True))
    _seed("caja", 1, "basico")
    _seed("mayorista", 0, "addon")

    apply_plan("premium")
    assert get_modulos()["mayorista"] is False


def test_sin_addons_declarados_el_comportamiento_es_el_de_antes(base_postgres, monkeypatch):
    """Retrocompatibilidad: un `plans.py` sin `ADDONS` (Restolibra) no gana
    ningún skip. Una fila que no está en el plan se apaga, como siempre."""
    monkeypatch.setitem(sys.modules, "plans", _plans_falso(con_addons=False))
    _seed("caja", 1, "basico")
    _seed("mayorista", 1, "addon")

    apply_plan("basico")
    # `mayorista` no está en ningún plan del `plans` falso y no hay ADDONS que
    # lo proteja: apply_plan lo apaga.
    assert get_modulos()["mayorista"] is False
    assert get_modulos()["caja"] is True
