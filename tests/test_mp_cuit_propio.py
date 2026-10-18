"""Los cobros del propio titular nacen ignorados (ADR-072).

Una transferencia, un DEBIN o un pago cuyo CUIT de pagador es el de quien
factura es plata del titular entre sus cuentas: no es una venta. Nace
`ignorado`, queda registrada y no se auto-factura. Cada regla con sus dos
mitades: el CUIT propio que se ignora y el de un tercero que sigue pendiente.
CUIT ficticios (prefijo `30-999…`, que ARCA no asigna).
"""

import asyncio
import importlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore.db import arca_config as db_arca
from libracore.db import clients as db_clients
from libracore.db import core
from libracore.db import mp as db_mp
from libracore.db.schema import init_core_schema

CUIT_EMPRESA = "30-99999904-9"
CUIT_OTRO_EMISOR = "30999999057"
CUIT_TERCERO = "20999999078"
MI_USER_ID = "555"
MI_EMAIL = "comercio@miempresa.test"


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    import libracore.config_manager as cm
    importlib.reload(cm)

    core.configure(db_path=str(tmp_path / "mp_cuit_propio.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    cm.save({
        "mp_access_token": "TOKEN", "empresa_iva_condition": "Monotributista",
        "empresa_cuit": CUIT_EMPRESA,
    })
    yield cm
    conn.close()
    core._db_path = None


def _movimiento(mp_id, cuit, estado="pendiente"):
    return db_mp.create_mp_movimiento(
        mp_movement_id=mp_id, tipo="bank_transfer", monto=1000.0, fecha="2026-10-18",
        payer_id_type="CUIT", payer_id_number=cuit, estado_factura=estado,
    )


# ── La regla en el alta ──────────────────────────────────────────────────────

def test_el_cuit_de_la_empresa_nace_ignorado_y_el_de_un_tercero_pendiente(entorno):
    propio = _movimiento("m1", "30999999049")
    tercero = _movimiento("m2", CUIT_TERCERO)
    assert db_mp.get_mp_movimiento_by_id(propio)["estado_factura"] == "ignorado"
    assert db_mp.get_mp_movimiento_by_id(tercero)["estado_factura"] == "pendiente"
    assert db_mp.get_mp_pending_count() == 1


def test_el_cuit_se_compara_solo_en_digitos(entorno):
    """La config lo tiene con guiones; MercadoPago lo puede mandar con puntos."""
    mov = _movimiento("m1", "30.99999904.9")
    assert db_mp.get_mp_movimiento_by_id(mov)["estado_factura"] == "ignorado"


def test_cuenta_el_cuit_de_cualquier_emisor_activo(entorno):
    """Un producto con varias razones sociales: todas son el titular."""
    db_arca.crear_arca_config("Otra Razon SA", CUIT_OTRO_EMISOR, 2, "k", "c")
    mov = _movimiento("m1", CUIT_OTRO_EMISOR)
    assert db_mp.get_mp_movimiento_by_id(mov)["estado_factura"] == "ignorado"
    assert db_mp.cuits_propios() == {"30999999049", CUIT_OTRO_EMISOR}


def test_sin_cuit_de_emisor_no_cambia_nada(entorno):
    entorno.save({**entorno.load(), "empresa_cuit": ""})
    assert db_mp.cuits_propios() == set()
    mov = _movimiento("m1", "30999999049")
    assert db_mp.get_mp_movimiento_by_id(mov)["estado_factura"] == "pendiente"


def test_sin_cuit_de_pagador_queda_pendiente(entorno):
    mov = _movimiento("m1", "")
    assert db_mp.get_mp_movimiento_by_id(mov)["estado_factura"] == "pendiente"


def test_los_pagos_siguen_la_misma_regla_y_otros_estados_no_se_tocan(entorno):
    db_mp.create_mp_pago("p1", "approved", 10.0, "", "", estado_factura="pendiente",
                         payer_id_number="30999999049")
    db_mp.create_mp_pago("p2", "approved", 10.0, "", "", estado_factura="pendiente",
                         payer_id_number=CUIT_TERCERO)
    db_mp.create_mp_pago("p3", "rejected", 10.0, "", "", estado_factura=None,
                         payer_id_number="30999999049")
    assert db_mp.get_mp_pago("p1")["estado_factura"] == "ignorado"
    assert db_mp.get_mp_pago("p2")["estado_factura"] == "pendiente"
    assert db_mp.get_mp_pago("p3")["estado_factura"] is None


# ── Los pendientes que ya existían ───────────────────────────────────────────

def test_aplicar_la_regla_a_los_existentes_es_idempotente(entorno):
    # Nacieron antes de la regla (sin CUIT de empresa cargado).
    entorno.save({**entorno.load(), "empresa_cuit": ""})
    viejo = _movimiento("m1", "30999999049")
    tercero = _movimiento("m2", CUIT_TERCERO)
    db_mp.create_mp_pago("p1", "approved", 10.0, "", "", estado_factura="pendiente",
                         payer_id_number="30-99999904-9")
    facturado = _movimiento("m3", "30999999049", estado="facturado")
    entorno.save({**entorno.load(), "empresa_cuit": CUIT_EMPRESA})

    assert db_mp.ignorar_pendientes_de_cuit_propio() == {"pagos": 1, "movimientos": 1}
    assert db_mp.ignorar_pendientes_de_cuit_propio() == {"pagos": 0, "movimientos": 0}
    assert db_mp.get_mp_movimiento_by_id(viejo)["estado_factura"] == "ignorado"
    assert db_mp.get_mp_movimiento_by_id(tercero)["estado_factura"] == "pendiente"
    assert db_mp.get_mp_movimiento_by_id(facturado)["estado_factura"] == "facturado"


# ── Los caminos: cron, webhook, bandeja ──────────────────────────────────────

def _cobro_de_mp(mp_id, cuit):
    return {
        "id": mp_id, "collector_id": MI_USER_ID, "transaction_amount": 5000.0,
        "external_reference": "", "description": "Transferencia",
        "payment_type_id": "bank_transfer", "payment_method_id": "cvu",
        "date_approved": "2026-10-18T10:00:00.000-03:00",
        "payer": {"email": "", "first_name": "Titular", "last_name": "",
                  "identification": {"type": "CUIT", "number": cuit}},
    }


@pytest.fixture
def sync(entorno):
    import libracore.mp_sync as ms
    importlib.reload(ms)

    async def usuario_info(token):
        return {"id": MI_USER_ID, "email": MI_EMAIL}

    ms.mp_api.obtener_usuario_info = usuario_info

    def cargar(movimientos):
        async def obtener_movimientos(token, desde, hasta):
            return movimientos
        ms.mp_api.obtener_movimientos = obtener_movimientos

    ms.cargar = cargar
    return ms


def test_el_cron_no_auto_factura_al_titular(sync):
    """Aunque exista un cliente con ese CUIT y auto-facturación: no es una venta."""
    titular = db_clients.create_client(
        name="Titular", cuit_dni="30999999049", iva_condition="Monotributista",
    )
    db_clients.toggle_auto_facturar(titular)
    sync.cargar([_cobro_de_mp("800001", "30999999049"), _cobro_de_mp("800002", CUIT_TERCERO)])

    resumen = asyncio.run(sync.sincronizar_y_facturar())

    assert resumen == {"nuevos": 1, "facturados": 0, "pendientes": 1}
    propio = db_mp.get_mp_movimiento_by_mp_id("800001")
    assert propio["estado_factura"] == "ignorado"
    assert propio["factura_id"] is None
    assert db_mp.get_mp_movimiento_by_mp_id("800002")["estado_factura"] == "pendiente"


def test_el_webhook_no_auto_factura_al_titular(entorno):
    titular = db_clients.create_client(
        name="Titular", cuit_dni="30999999049", iva_condition="Monotributista",
    )
    db_clients.toggle_auto_facturar(titular)
    import libracore.mp_webhook as mw
    importlib.reload(mw)

    async def obtener_pago(payment_id, access_token):
        return {
            "status": "approved", "transaction_amount": 5000.0, "description": "DEBIN",
            "payment_type_id": "bank_transfer", "payment_method_id": "debin",
            "external_reference": "",
            "payer": {"email": "", "identification": {"type": "CUIT", "number": "30999999049"}},
        }

    mw.mp_api.obtener_pago = obtener_pago
    aplicacion = FastAPI()
    aplicacion.include_router(mw.build_mp_webhook_router())
    respuesta = TestClient(aplicacion).post(
        "/webhooks/mercadopago",
        content=json.dumps({"type": "payment", "data": {"id": "123"}}).encode(),
        headers={"content-type": "application/json"},
    )
    assert respuesta.status_code == 200
    guardado = db_mp.get_mp_pago("123")
    assert guardado["estado_factura"] == "ignorado"
    assert guardado["factura_id"] is None


def test_la_bandeja_dice_por_que_esta_ignorado(entorno):
    import libracore.mp_bandeja_router as mbr
    importlib.reload(mbr)
    _movimiento("m1", "30999999049")
    manual = _movimiento("m2", CUIT_TERCERO)
    db_mp.update_mp_movimiento_estado(manual, "ignorado")

    aplicacion = FastAPI()
    aplicacion.include_router(mbr.build_mp_bandeja_router(permitir_siembra_de_demo=False))
    datos = TestClient(aplicacion).get("/api/mp-bandeja").json()

    assert datos["transferencias"] == []
    motivos = {m["mp_movement_id"]: m["motivo_ignorado"] for m in datos["transferencias_hist"]}
    assert motivos == {"m1": "cuit_propio", "m2": None}
