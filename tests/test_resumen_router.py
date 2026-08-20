"""La factory del endpoint de resumen.

Lo que fija, en orden de importancia:

1. 🔴 **Un bloque que el producto no tiene se OMITE, no viaja en cero.** Una
   sucursal de MedLibra no tiene ventas de buffet: mandar `0` dice "no vendieron
   nada" cuando lo cierto es "esto no se mide aca", y consolidando un cero se
   suma y un ausente no.
2. El guard viene **inyectado**: LibraCore y LibraAuth son motores peers y este
   paquete no depende de aquel.
3. El periodo por defecto es el mes en curso, y una fecha invalida da 422.
"""
import datetime

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracore.db import core
from libracore.resumen_router import build_resumen_router

IDENTIDAD = {"nombre": "Complejo Centro", "cuit": "20-11111111-2", "punto_venta": 3}


@pytest.fixture(autouse=True)
def _base(tmp_path, crear_schema):
    core._db_path = None
    core.configure(db_path=str(tmp_path / "router.db"))
    with core.get_connection() as conn:
        crear_schema(conn)
        conn.commit()
    yield
    core._db_path = None


def _abre(_request=None):
    return {"username": "@panel", "role": "admin"}


def _cierra(_request=None):
    raise HTTPException(403, "No autorizado")


def _cliente(*, guard=_abre, bloques=None):
    app = FastAPI()
    app.include_router(build_resumen_router(
        identidad=lambda: dict(IDENTIDAD), guard=guard, bloques=bloques,
    ))
    return TestClient(app)


def test_el_guard_inyectado_decide_quien_entra():
    assert _cliente().get("/api/resumen").status_code == 200
    assert _cliente(guard=_cierra).get("/api/resumen").status_code == 403


def test_sin_bloques_solo_viene_el_nucleo():
    cuerpo = _cliente().get("/api/resumen").json()
    assert set(cuerpo) == {"instancia", "periodo", "nucleo"}


def test_un_bloque_ausente_NO_viaja_en_cero():
    """🔴 La propiedad que define el contrato.

    El producto que no monta LibraCommerce no manda `comercio`. Si mandara
    `{"ventas": 0}`, el panel lo sumaria como una sucursal que no vendio nada.
    """
    cuerpo = _cliente().get("/api/resumen").json()
    assert "comercio" not in cuerpo
    assert "agenda" not in cuerpo


def test_los_bloques_que_el_producto_pasa_si_viajan():
    cliente = _cliente(bloques={
        "comercio": lambda d, h: {"ventas": {"cantidad": 2, "monto": 300.0}},
        "agenda": lambda d, h: {"turnos": 7},
    })
    cuerpo = cliente.get("/api/resumen").json()

    assert cuerpo["comercio"]["ventas"]["cantidad"] == 2
    assert cuerpo["agenda"]["turnos"] == 7


def test_el_bloque_recibe_el_periodo():
    visto = {}

    def bloque(desde, hasta):
        visto["desde"], visto["hasta"] = desde, hasta
        return {}

    _cliente(bloques={"comercio": bloque}).get(
        "/api/resumen", params={"desde": "2026-03-01", "hasta": "2026-03-31"})

    assert visto == {"desde": "2026-03-01", "hasta": "2026-03-31"}


def test_la_instancia_se_identifica():
    """El CUIT es lo que le permite al panel el consolidado por razon social,
    que es el unico que cierra contra los libros."""
    assert _cliente().get("/api/resumen").json()["instancia"] == IDENTIDAD


def test_por_defecto_es_el_mes_en_curso():
    hoy = datetime.date.today()
    periodo = _cliente().get("/api/resumen").json()["periodo"]
    assert periodo["desde"] == hoy.replace(day=1).isoformat()
    assert periodo["hasta"] == hoy.isoformat()


def test_una_fecha_que_no_es_fecha_da_422():
    assert _cliente().get(
        "/api/resumen", params={"desde": "01/08/2026"}).status_code == 422


def test_desde_posterior_a_hasta_da_422():
    assert _cliente().get(
        "/api/resumen", params={"desde": "2026-08-31", "hasta": "2026-08-01"}
    ).status_code == 422
