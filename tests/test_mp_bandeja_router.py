"""La bandeja de MercadoPago.

Los dos tests que mandan son el del **filtro que no está** —una transferencia
real marcada `account_fund` con el email propio tiene que entrar igual— y el
del **CUIT del pagador**, que es lo que le faltaba a la copia de Restolibra y
sin lo cual un alias por CUIT no puede resolver nunca.
"""

import importlib

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore.db import clients as db_clients
from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db import mp as db_mp
from libracore.db.schema import init_core_schema

ADMIN = {"x-rol": "admin"}
MI_EMAIL = "comercio@miempresa.test"
MI_USER_ID = "555"


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    import libracore.config_manager as cm
    importlib.reload(cm)

    core.configure(db_path=str(tmp_path / "bandeja_test.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    cm.save({"mp_access_token": "TOKEN", "empresa_iva_condition": "Monotributista"})
    yield cm
    conn.close()
    core._db_path = None


@pytest.fixture
def armar(entorno):
    def _armar(movimientos=None, **kw):
        import libracore.mp_bandeja_router as mbr
        import libracore.mp_sync as ms
        importlib.reload(ms)
        importlib.reload(mbr)

        async def usuario_info(token):
            return {"id": MI_USER_ID, "email": MI_EMAIL}

        async def obtener_movimientos(token, desde, hasta):
            return movimientos or []

        # La ingesta la hace `mp_sync`, no el router: es la misma funcion que
        # corre el cron. Parchear `mbr.mp_api` no tocaria nada.
        ms.mp_api.obtener_usuario_info = usuario_info
        ms.mp_api.obtener_movimientos = obtener_movimientos

        def gate(x_rol: str = Header(default="")):
            if x_rol != "admin":
                raise HTTPException(403, "solo administradores")

        aplicacion = FastAPI()
        aplicacion.include_router(
            mbr.build_mp_bandeja_router(**kw), dependencies=[Depends(gate)]
        )
        return TestClient(aplicacion)
    return _armar


def _movimiento_de_mp(**over):
    mov = {
        "id": "900001",
        "collector_id": MI_USER_ID,
        "transaction_amount": 15000.0,
        "external_reference": "",
        "description": "Transferencia",
        "payment_type_id": "bank_transfer",
        "payment_method_id": "cvu",
        "date_approved": "2026-08-20T10:00:00.000-03:00",
        "payer": {
            "email": "cliente@test", "first_name": "Juan", "last_name": "Perez",
            "identification": {"type": "CUIT", "number": "20111111112"},
        },
    }
    mov.update(over)
    return mov


# ── El gate ──────────────────────────────────────────────────────────────────

def test_el_gate_lo_pone_el_producto(armar):
    cliente = armar()
    assert cliente.get("/api/mp-bandeja").status_code == 403
    assert cliente.get("/api/mp-bandeja", headers=ADMIN).status_code == 200


# ── Las cuatro listas ────────────────────────────────────────────────────────

def test_la_bandeja_separa_pagos_de_transferencias(armar):
    db_mp.create_mp_pago(
        mp_payment_id="p1", status="approved", monto=100.0,
        payer_email="a@test", payer_name="A", estado_factura="pendiente",
    )
    db_mp.create_mp_movimiento(
        mp_movement_id="m1", tipo="bank_transfer", monto=200.0,
        fecha="2026-08-20", estado_factura="pendiente",
    )
    datos = armar().get("/api/mp-bandeja", headers=ADMIN).json()
    assert [p["mp_payment_id"] for p in datos["pendientes"]] == ["p1"]
    assert [m["mp_movement_id"] for m in datos["transferencias"]] == ["m1"]


def test_cada_fila_trae_su_cliente_por_email_o_por_cuit(armar):
    """El CUIT se matchea **normalizado**: el cliente lo tiene con guiones y
    MercadoPago lo manda sin."""
    db_clients.create_client(name="Por Email", email="a@test")
    db_clients.create_client(name="Por Cuit", email="", cuit_dni="20-11111111-2")

    db_mp.create_mp_pago(mp_payment_id="p1", status="approved", monto=1.0,
                         payer_email="a@test", payer_name="", estado_factura="pendiente")
    db_mp.create_mp_pago(mp_payment_id="p2", status="approved", monto=1.0,
                         payer_email="", payer_name="", estado_factura="pendiente",
                         payer_id_number="20111111112")
    db_mp.create_mp_pago(mp_payment_id="p3", status="approved", monto=1.0,
                         payer_email="nadie@test", payer_name="", estado_factura="pendiente")

    por_id = {p["mp_payment_id"]: p for p in
              armar().get("/api/mp-bandeja", headers=ADMIN).json()["pendientes"]}
    assert por_id["p1"]["cliente"]["name"] == "Por Email"
    assert por_id["p2"]["cliente"]["name"] == "Por Cuit"
    assert por_id["p3"]["cliente"] is None, "el que no matchea tiene que venir en null"


# ── Sincronizar ──────────────────────────────────────────────────────────────

def test_sin_access_token_no_sincroniza(armar, entorno):
    entorno.save({**entorno.load(), "mp_access_token": ""})
    r = armar().post("/api/mp-bandeja/sincronizar", headers=ADMIN, json={"dias": 7})
    assert r.status_code == 400
    assert "Access Token" in r.json()["detail"]


def test_una_transferencia_real_marcada_account_fund_entra_igual(armar):
    """🔴 El filtro que NO está, y no es un olvido.

    MercadoPago marca `account_fund` con el email propio a *cualquier*
    movimiento que no sea un pago clásico de un tercero — transferencias reales
    incluidas. Contalibra filtró por eso nueve días y una transferencia real de
    un cliente quedó invisible en la bandeja.
    """
    fondeo_que_es_real = _movimiento_de_mp(
        id="900002",
        operation_type="account_fund",
        payer={"email": MI_EMAIL, "first_name": "", "last_name": "",
               "identification": {}},
    )
    cliente = armar(movimientos=[fondeo_que_es_real])
    r = cliente.post("/api/mp-bandeja/sincronizar", headers=ADMIN, json={"dias": 7})
    assert r.json()["nuevos"] == 1, "no puede descartarse por account_fund"

    guardado = db_mp.get_mp_movimiento_by_mp_id("900002")
    assert guardado is not None
    assert guardado["payer_email"] == "", "mi propio email no es el de un pagador"


def test_un_cobro_de_otra_cuenta_no_entra(armar):
    """Este filtro sí es seguro: un `collector_id` que no es el mío es
    literalmente el cobro de otro."""
    ajeno = _movimiento_de_mp(id="900003", collector_id="999")
    cliente = armar(movimientos=[ajeno])
    assert cliente.post("/api/mp-bandeja/sincronizar",
                        headers=ADMIN, json={"dias": 7}).json()["nuevos"] == 0


def test_las_referencias_que_el_producto_maneja_aparte_se_omiten(armar):
    """Las dos mitades: la referencia configurada se omite y **cualquier otra
    entra**. Sin la segunda, un filtro que descarte todo pasaría igual."""
    cliente = armar(
        movimientos=[
            _movimiento_de_mp(id="900004", external_reference="venta-7"),
            _movimiento_de_mp(id="900005", external_reference="reserva-7"),
        ],
        referencias_a_omitir=("venta-",),
    )
    assert cliente.post("/api/mp-bandeja/sincronizar",
                        headers=ADMIN, json={"dias": 7}).json()["nuevos"] == 1
    assert db_mp.get_mp_movimiento_by_mp_id("900004") is None
    assert db_mp.get_mp_movimiento_by_mp_id("900005") is not None


def test_sincronizar_dos_veces_no_duplica(armar):
    cliente = armar(movimientos=[_movimiento_de_mp()])
    assert cliente.post("/api/mp-bandeja/sincronizar",
                        headers=ADMIN, json={"dias": 7}).json()["nuevos"] == 1
    assert cliente.post("/api/mp-bandeja/sincronizar",
                        headers=ADMIN, json={"dias": 7}).json()["nuevos"] == 0


def test_un_movimiento_sin_importe_no_entra(armar):
    cliente = armar(movimientos=[_movimiento_de_mp(id="900006", transaction_amount=0)])
    assert cliente.post("/api/mp-bandeja/sincronizar",
                        headers=ADMIN, json={"dias": 7}).json()["nuevos"] == 0


def test_si_mp_no_contesta_es_502(armar):
    import libracore.mp_sync as ms
    cliente = armar()

    async def explota(token, desde, hasta):
        raise RuntimeError("timeout")

    ms.mp_api.obtener_movimientos = explota
    r = cliente.post("/api/mp-bandeja/sincronizar", headers=ADMIN, json={"dias": 7})
    assert r.status_code == 502


# ── Facturar ─────────────────────────────────────────────────────────────────

def test_facturar_un_pago_usa_el_cuit_del_pagador(armar):
    """🔑 Sin `payer_cuit`, un alias por CUIT no puede resolver **nunca**. Es
    exactamente lo que le faltaba a la copia de Restolibra.

    El pago llega sin email y con CUIT: si el CUIT no viajara, no habría por
    dónde encontrar al cliente y se crearía un placeholder.
    """
    real = db_clients.create_client(
        name="Real SA", email="admin@real.test", cuit_dni="30712345678",
        iva_condition="Monotributista",
    )
    db_mp.crear_alias_facturacion("cuit", "20111111112", real)
    pago_id = db_mp.create_mp_pago(
        mp_payment_id="p9", status="approved", monto=5000.0,
        payer_email="", payer_name="Quien Paga", estado_factura="pendiente",
        payment_type="credit_card", payer_id_number="20111111112",
    )

    cliente = armar()
    r = cliente.post(f"/api/mp-bandeja/pagos/{pago_id}/facturar",
                     headers=ADMIN, json={"concepto": ""})
    assert r.status_code == 200, r.text
    factura = db_facturas.get_factura(r.json()["factura_id"])
    assert factura["cliente_razon"] == "Real SA"
    assert db_mp.get_mp_pago("p9")["estado_factura"] == "facturado"


def test_el_concepto_del_dialogo_pisa_al_de_la_configuracion(armar, entorno):
    entorno.save({**entorno.load(), "mp_concepto_descripcion": "El de la config"})
    pago_id = db_mp.create_mp_pago(
        mp_payment_id="p10", status="approved", monto=1000.0,
        payer_email="x@test", payer_name="X", estado_factura="pendiente",
    )
    r = armar().post(f"/api/mp-bandeja/pagos/{pago_id}/facturar",
                     headers=ADMIN, json={"concepto": "Lo que escribio la persona"})
    factura = db_facturas.get_factura(r.json()["factura_id"])
    descripciones = [i["description"] for i in factura["items"]]
    assert descripciones == ["Lo que escribio la persona"]


def test_facturar_dos_veces_el_mismo_pago_no_emite_dos(armar):
    pago_id = db_mp.create_mp_pago(
        mp_payment_id="p11", status="approved", monto=1000.0,
        payer_email="x@test", payer_name="X", estado_factura="pendiente",
    )
    cliente = armar()
    assert cliente.post(f"/api/mp-bandeja/pagos/{pago_id}/facturar",
                        headers=ADMIN, json={}).status_code == 200
    assert cliente.post(f"/api/mp-bandeja/pagos/{pago_id}/facturar",
                        headers=ADMIN, json={}).status_code == 404


def test_facturar_una_transferencia(armar):
    mov_id = db_mp.create_mp_movimiento(
        mp_movement_id="m9", tipo="bank_transfer", monto=8000.0,
        fecha="2026-08-20", origen_nombre="Quien Transfirio",
        payer_email="t@test", payer_name="Quien Transfirio",
        payer_id_number="20111111112", estado_factura="pendiente",
    )
    r = armar().post(f"/api/mp-bandeja/movimientos/{mov_id}/facturar",
                     headers=ADMIN, json={})
    assert r.status_code == 200, r.text
    assert db_mp.get_mp_movimiento_by_id(mov_id)["estado_factura"] == "facturado"
    assert db_facturas.get_factura(r.json()["factura_id"])["total"] == 8000.0


def test_ignorar_saca_de_pendientes(armar):
    pago_id = db_mp.create_mp_pago(
        mp_payment_id="p12", status="approved", monto=1.0,
        payer_email="x@test", payer_name="X", estado_factura="pendiente",
    )
    cliente = armar()
    assert len(cliente.get("/api/mp-bandeja", headers=ADMIN).json()["pendientes"]) == 1
    cliente.post(f"/api/mp-bandeja/pagos/{pago_id}/ignorar", headers=ADMIN)
    assert cliente.get("/api/mp-bandeja", headers=ADMIN).json()["pendientes"] == []


# ── Completar los datos de una transferencia ────────────────────────────────

def test_crear_cliente_desde_un_movimiento_lo_deja_vinculado(armar):
    """Cargar el cliente y no pegarlo al movimiento deja la transferencia
    igual de imposible de facturar que antes."""
    mov_id = db_mp.create_mp_movimiento(
        mp_movement_id="m10", tipo="bank_transfer", monto=100.0,
        fecha="2026-08-20", estado_factura="pendiente",
    )
    armar().post(f"/api/mp-bandeja/movimientos/{mov_id}/crear-cliente", headers=ADMIN,
                 json={"nombre": "Nuevo SA", "email": "nuevo@test",
                       "cuit_dni": "20111111112"})
    mov = db_mp.get_mp_movimiento_by_id(mov_id)
    assert mov["payer_email"] == "nuevo@test"
    assert mov["payer_id_number"] == "20111111112"
    assert db_clients.get_client_by_email("nuevo@test")["name"] == "Nuevo SA"


def test_crear_cliente_sin_nombre_se_rechaza(armar):
    mov_id = db_mp.create_mp_movimiento(
        mp_movement_id="m11", tipo="bank_transfer", monto=100.0,
        fecha="2026-08-20", estado_factura="pendiente",
    )
    r = armar().post(f"/api/mp-bandeja/movimientos/{mov_id}/crear-cliente",
                     headers=ADMIN, json={"nombre": "   "})
    assert r.status_code == 422


# ── Reenviar ─────────────────────────────────────────────────────────────────

def test_reenviar_sin_smtp_lo_dice(armar):
    factura_id = db_facturas.create_factura(
        tipo=11, punto_venta=1, numero=1, fecha="2026-08-20",
        cliente_cuit="", cliente_razon="X", cliente_iva_cond=5,
        items=[], subtotal=1.0, iva_amount=0.0, total=1.0,
    )
    r = armar().post(f"/api/mp-bandeja/facturas/{factura_id}/reenviar", headers=ADMIN)
    assert r.status_code == 400
    assert "SMTP" in r.json()["detail"]


# ── La siembra de demo ───────────────────────────────────────────────────────

def test_la_ruta_de_siembra_no_existe_fuera_de_una_demo(armar):
    """🔑 Las dos mitades, y es estructural: la ruta **no se arma**. Un `if`
    adentro del endpoint dejaría la ruta publicada en el openapi de la
    instancia de un cliente."""
    sin_demo = armar()
    assert sin_demo.post("/api/mp-bandeja/demo/sembrar",
                         headers=ADMIN, json=[]).status_code == 404

    con_demo = armar(permitir_siembra_de_demo=True)
    assert con_demo.post("/api/mp-bandeja/demo/sembrar",
                         headers=ADMIN, json=[]).status_code == 200


def test_la_siembra_es_idempotente(armar):
    cliente = armar(permitir_siembra_de_demo=True)
    items = [
        {"mp_payment_id": "demo-1", "monto": 1000.0, "payer_name": "Demo Uno",
         "payer_email": "uno@demo", "clase": "pago"},
        {"mp_payment_id": "demo-2", "monto": 2000.0, "payer_name": "Demo Dos",
         "clase": "transferencia"},
    ]
    assert cliente.post("/api/mp-bandeja/demo/sembrar",
                        headers=ADMIN, json=items).json()["creados"] == 2
    assert cliente.post("/api/mp-bandeja/demo/sembrar",
                        headers=ADMIN, json=items).json()["creados"] == 0

    datos = cliente.get("/api/mp-bandeja", headers=ADMIN).json()
    assert len(datos["pendientes"]) == 1
    assert len(datos["transferencias"]) == 1, "las dos solapas, no una"
