"""`build_recibos_router`: el contrato HTTP de recibos, extraído de Contalibra.
La emisión en sí (idempotencia, qué pasa con cobros parciales, etc.) ya la
prueba `test_recibos.py` sobre `libracore.recibos`; acá se prueba que el
router la cablee bien -- listar/detalle/pdf, los tres orígenes, el gate de
`anular`, y el único gancho propio (`get_venta`).
"""

from __future__ import annotations

import datetime

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracore.db import caja as db_caja
from libracore.db import core
from libracore.db import cuenta_corriente as db_cc
from libracore.db import facturas as db_facturas
from libracore.db.schema import init_core_schema
from libracore.recibos_router import build_recibos_router

HOY = datetime.date.today().isoformat()
USUARIO = {"id": 7, "username": "cajero", "role": "staff"}


def _usuario():
    return USUARIO


def _solo_admin(user: dict = Depends(_usuario)):
    if user.get("role") != "admin":
        raise HTTPException(403, "solo administradores")


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "recibos_router.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.execute("INSERT INTO clients (id, name, cuit_dni, address) VALUES (1, 'Kiosco SRL', '20111111119', '')")
    c.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (7, 'cajero', 'Cajero', 'x', 'staff')"
    )
    c.commit()
    yield c
    c.close()
    core._db_path = None


@pytest.fixture
def client(conn, request):
    get_venta = getattr(request, "param", None)
    app = FastAPI()
    app.include_router(build_recibos_router(usuario_actual=_usuario, solo_admin=_solo_admin, get_venta=get_venta))
    with TestClient(app) as c:
        yield c


def _crear_pago(monto=1000.0):
    return db_cc.create_cc_pago(
        cliente_id=1, monto=monto, fecha=HOY, concepto="Pago a cuenta",
        referencia="", medio_pago="efectivo", caja_id=None, usuario_id=7,
    )


def _crear_factura_cobrada(total=5000.0):
    factura_id = db_facturas.create_factura(
        tipo=6, punto_venta=1, numero=1, fecha=HOY, cliente_cuit="20111111119",
        cliente_razon="Kiosco SRL", cliente_iva_cond="CF", items=[], subtotal=total,
        iva_amount=0, total=total, ambiente="produccion",
    )
    db_caja.create_caja_movimiento(
        fecha=HOY, tipo="ingreso", concepto="Cobro factura", monto=total,
        referencia="r1", factura_id=factura_id, usuario_id=7, medio_pago="efectivo",
    )
    return factura_id


def test_listar_arranca_vacio(client):
    r = client.get("/api/recibos")
    assert r.status_code == 200, r.text
    assert r.json() == {"recibos": [], "total": 0, "page": 1, "page_size": 50}


def test_emitir_de_cobranza_y_despues_listarlo_y_verlo(client):
    pago_id = _crear_pago()
    r = client.post(f"/api/recibos/cobranza/{pago_id}")
    assert r.status_code == 200, r.text
    recibo_id = r.json()["id"]

    listado = client.get("/api/recibos").json()
    assert listado["total"] == 1
    assert listado["recibos"][0]["id"] == recibo_id
    assert listado["recibos"][0]["origen_tipo"] == "cc_pago"

    detalle = client.get(f"/api/recibos/{recibo_id}")
    assert detalle.status_code == 200, detalle.text
    assert detalle.json()["total"] == 1000.0
    assert "pagos" in detalle.json()


def test_emitir_de_cobranza_es_idempotente(client):
    pago_id = _crear_pago()
    primero = client.post(f"/api/recibos/cobranza/{pago_id}").json()
    segundo = client.post(f"/api/recibos/cobranza/{pago_id}").json()
    assert primero["id"] == segundo["id"]


def test_cobranza_de_un_pago_inexistente_da_409(client):
    r = client.post("/api/recibos/cobranza/99999")
    assert r.status_code == 409


def test_emitir_de_factura(client):
    factura_id = _crear_factura_cobrada()
    r = client.post(f"/api/recibos/factura/{factura_id}")
    assert r.status_code == 200, r.text
    assert r.json()["origen_tipo"] == "factura"


def test_factura_sin_cobros_da_409(client):
    factura_id = db_facturas.create_factura(
        tipo=6, punto_venta=1, numero=2, fecha=HOY, cliente_cuit="20111111119",
        cliente_razon="Kiosco SRL", cliente_iva_cond="CF", items=[], subtotal=100,
        iva_amount=0, total=100, ambiente="produccion",
    )
    r = client.post(f"/api/recibos/factura/{factura_id}")
    assert r.status_code == 409


def test_venta_inexistente_da_409_con_el_default_del_motor(client):
    """Sin pasar `get_venta` al armar el router, usa el default de
    `libracore.recibos.emitir_recibo_venta` (`libracore.db.ventas.get_venta`,
    la tabla `ventas` del propio esquema del motor)."""
    r = client.post("/api/recibos/venta/99999")
    assert r.status_code == 409


@pytest.mark.parametrize("client", [lambda vid: {
    "id": vid, "numero": 501, "fecha": HOY, "cliente_nombre": "Mostrador",
    "cliente_id": None, "cliente_cuit": None, "cliente_domicilio": None,
    "pagos": [{"medio": "efectivo", "referencia": "", "monto": 750.0}],
}], indirect=True)
def test_el_gancho_get_venta_es_el_que_se_usa_para_emitir(client):
    """El único parámetro propio del router: sin él no hay forma de decirle
    que la venta sale de `sales` (LibraCommerce) y no de `ventas` (el motor)."""
    r = client.post("/api/recibos/venta/1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["origen_tipo"] == "venta"
    assert body["total"] == 750.0
    assert body["cliente_razon"] == "Mostrador"


def test_pdf_de_un_recibo_inexistente_da_404(client):
    assert client.get("/api/recibos/99999/pdf").status_code == 404


def test_pdf_de_un_recibo_existente(client):
    pago_id = _crear_pago()
    recibo_id = client.post(f"/api/recibos/cobranza/{pago_id}").json()["id"]
    r = client.get(f"/api/recibos/{recibo_id}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"


def test_anular_esta_gateado_a_admin(client):
    pago_id = _crear_pago()
    recibo_id = client.post(f"/api/recibos/cobranza/{pago_id}").json()["id"]
    r = client.post(f"/api/recibos/{recibo_id}/anular", json={"motivo": "error de carga"})
    assert r.status_code == 403


def test_un_admin_puede_anular(client):
    USUARIO["role"] = "admin"
    try:
        pago_id = _crear_pago()
        recibo_id = client.post(f"/api/recibos/cobranza/{pago_id}").json()["id"]
        r = client.post(f"/api/recibos/{recibo_id}/anular", json={"motivo": "error de carga"})
        assert r.status_code == 200, r.text
        assert r.json()["anulado"] is True

        otra_vez = client.post(f"/api/recibos/{recibo_id}/anular", json={"motivo": "de nuevo"})
        assert otra_vez.status_code == 409
    finally:
        USUARIO["role"] = "staff"


def test_anular_un_recibo_inexistente_da_404(client):
    USUARIO["role"] = "admin"
    try:
        r = client.post("/api/recibos/99999/anular", json={"motivo": "x"})
        assert r.status_code == 404
    finally:
        USUARIO["role"] = "staff"
