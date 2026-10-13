"""Los adicionales contra PostgreSQL, con las DOS formas de `modulos.habilitado` (ADR-048).

`habilitado` es `INTEGER` en contalibra, restolibra y ventalibra y `BOOLEAN` en libradesk, gestiolibra y medlibra (ver `libracore.db.modulos`):
un test que sólo mire una mitad no ve el defecto de la otra. Acá el router de adicionales (sin inyectar nada: con `get_modulos`/`set_addon` del
motor), `apply_plan` y `verificar_alta` corren contra las dos.
"""
import importlib
import os
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore.db import core
from libracore.db.modulos import _valor_habilitado, apply_plan, get_modulos, set_addon
from libracore.db.schema import init_core_schema

SERVICIO = {"x-internal-auth": "t"}


def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    return url


def _libre():
    return None


@pytest.fixture(params=["integer", "boolean"])
def base(request, tmp_path, monkeypatch):
    """Schema limpio en PostgreSQL con `modulos.habilitado` del tipo que pide el parámetro, y `DATA_DIR` propio."""
    url = _url()
    import psycopg

    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS public CASCADE")
        c.execute("CREATE SCHEMA public")
    core.configure(db_path=url)
    with core.get_connection() as conn:
        init_core_schema(conn)
        if request.param == "boolean":
            conn.execute("ALTER TABLE modulos ALTER COLUMN habilitado DROP DEFAULT")
            conn.execute("ALTER TABLE modulos ALTER COLUMN habilitado TYPE boolean USING (habilitado <> 0)")
            conn.execute("ALTER TABLE modulos ALTER COLUMN habilitado SET DEFAULT true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    yield request.param
    core._db_path = None


@pytest.fixture
def plans(monkeypatch):
    m = types.ModuleType("plans")
    m.PLANES = ["basico", "pro"]
    m.PLAN_MODULOS = {"basico": {"ventas"}, "pro": {"ventas", "stock"}}
    m.modulos_de_plan = lambda p: set(m.PLAN_MODULOS.get(p, set()))
    m.ADDONS = {"resguardo_externo"}
    m.ADICIONALES = {"offline": {"label": "Modo offline", "modulos": {"offline"}, "unidad": "sucursal", "limite": "sucursales_offline"}}
    monkeypatch.setitem(sys.modules, "plans", m)
    return m


@pytest.fixture
def client(base, plans):
    import libracore.adicionales as ad
    import libracore.adicionales_router as router_mod
    import libracore.config_manager as cm
    importlib.reload(cm)
    importlib.reload(ad)
    importlib.reload(router_mod)
    app = FastAPI()
    app.include_router(router_mod.build_adicionales_router(_libre, _libre, plans=plans))
    return TestClient(app)


def _sembrar(*modulos):
    """Filas apagadas, con el valor del tipo que tiene la columna (`False` en la boolean, `0` en la integer)."""
    with core.get_connection() as conn:
        apagado = _valor_habilitado(conn, False)
        for m in modulos:
            conn.execute("INSERT OR IGNORE INTO modulos (modulo, habilitado, plan) VALUES (?,?,?)", (m, apagado, "basico"))


def _fila(modulo):
    with core.get_connection() as conn:
        return conn.execute("SELECT habilitado, plan FROM modulos WHERE modulo=?", (modulo,)).fetchone()


def test_el_router_prende_y_apaga_con_la_columna_de_los_dos_tipos(client):
    """Sin la fila previa: `set_addon` la crea con `plan='addon'`, y el valor tiene el tipo que pide la columna."""
    r = client.put("/api/adicionales/offline", json={"cantidad": 2})
    assert r.status_code == 200, r.text
    assert get_modulos()["offline"] is True
    assert _fila("offline")["plan"] == "addon"
    assert client.get("/api/adicionales").json()["adicionales"][0]["habilitado"] is True
    assert client.put("/api/adicionales/offline", json={"cantidad": None}).status_code == 200
    assert get_modulos()["offline"] is False


def test_el_si_no_del_router_con_la_columna_de_los_dos_tipos(client):
    assert client.put("/api/adicionales/resguardo_externo", json={"habilitado": True}).status_code == 200
    assert get_modulos()["resguardo_externo"] is True
    assert client.put("/api/adicionales/resguardo_externo", json={"habilitado": False}).status_code == 200
    assert get_modulos()["resguardo_externo"] is False


def test_cambiar_de_plan_conserva_el_adicional_prendido_y_su_cantidad(client, plans):
    """El corazón del ADR-048: subir y bajar de plan no apaga el offline ni toca cuántas sucursales se contrataron."""
    _sembrar("ventas", "stock")
    client.put("/api/adicionales/offline", json={"cantidad": 3})
    for plan in ("pro", "basico", "pro"):
        apply_plan(plan)
        mods = get_modulos()
        assert mods["offline"] is True, plan
        assert _fila("offline")["plan"] == "addon", plan
        assert client.get("/api/adicionales").json()["adicionales"][0]["cantidad"] == 3
    assert get_modulos()["stock"] is True and _fila("stock")["plan"] == "pro"
    apply_plan("basico")
    assert get_modulos()["stock"] is False


def test_apply_plan_no_apaga_un_modulo_de_adicional_que_no_esta_en_addons(base, plans):
    """`offline` no está en `ADDONS`, sólo en `ADICIONALES`: `modulos_de_adicionales` (el superconjunto) es lo que lo protege."""
    assert "offline" not in plans.ADDONS
    _sembrar("ventas", "stock", "offline")
    set_addon("offline", True)
    apply_plan("basico")
    assert get_modulos()["offline"] is True


def test_verificar_alta_lee_el_modulo_de_la_base(client, plans):
    import libracore.adicionales as ad
    with pytest.raises(ad.AdicionalNoContratado):
        ad.verificar_alta(plans, "offline", 0)
    client.put("/api/adicionales/offline", json={"cantidad": 1})
    ad.verificar_alta(plans, "offline", 0)
    with pytest.raises(ad.AdicionalExcedido):
        ad.verificar_alta(plans, "offline", 1)


def test_aplicar_modulos_de_adicionales_respeta_el_tipo_y_no_pisa_el_plan(base, tmp_path):
    from libracore.provisioning import aplicar_modulos_de_adicionales
    _sembrar("offline")
    aplicar_modulos_de_adicionales(_url(), {"offline": True, "resguardo_externo": True})
    assert get_modulos() == {**get_modulos(), "offline": True, "resguardo_externo": True}
    assert _fila("offline")["plan"] == "basico"  # la fila que ya existía conserva su etiqueta
    assert _fila("resguardo_externo")["plan"] == "addon"
    aplicar_modulos_de_adicionales(_url(), {"offline": False})
    assert get_modulos()["offline"] is False
