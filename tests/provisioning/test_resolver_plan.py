"""Un producto puede retirar planes sin romper a quien todavía pide el viejo.

VentaLibra pasó a un plan único (2026-10-09) y el backoffice y `crear_cliente`
siguen mandando `plan="basico"` por defecto.
"""
import types

import pytest

from libracore.provisioning import resolver_plan


def _plans(planes, retirados=None):
    mod = types.ModuleType("plans")
    mod.PLANES = planes
    if retirados is not None:
        mod.PLANES_RETIRADOS = retirados
    return mod


def test_un_plan_vigente_queda_como_esta():
    assert resolver_plan(_plans(["basico", "premium"]), "premium") == "premium"


def test_sin_plan_va_el_primero():
    assert resolver_plan(_plans(["unico"]), None) == "unico"
    assert resolver_plan(_plans(["unico"]), "  ") == "unico"


def test_un_plan_retirado_se_resuelve_con_su_reemplazo_y_avisa(caplog):
    plans = _plans(["unico"], {"basico": "unico", "premium": "unico"})
    with caplog.at_level("WARNING"):
        assert resolver_plan(plans, "basico") == "unico"
    assert "basico" in caplog.text and "unico" in caplog.text


def test_un_reemplazo_que_no_es_vigente_no_se_acepta():
    with pytest.raises(ValueError):
        resolver_plan(_plans(["unico"], {"basico": "otro"}), "basico")


def test_un_plan_desconocido_falla_aunque_el_producto_no_declare_retirados():
    with pytest.raises(ValueError, match="no-existe"):
        resolver_plan(_plans(["basico", "pro"]), "no-existe")
