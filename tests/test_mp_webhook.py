"""El webhook de MercadoPago, que es público y corre solo.

Las cuatro reglas que lo gobiernan tienen cada una su test **con las dos
mitades**: la firma que rechaza y la que deja pasar, el reintento que no
duplica y el pago nuevo que sí entra. Un test que sólo verifica el rechazo pasa
igual con un endpoint que rechaza todo.
"""

import hashlib
import hmac
import importlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore.db import clients as db_clients
from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db import mp as db_mp
from libracore.db.schema import init_core_schema

SECRETO = "un-secreto-de-webhook"
PAYMENT_ID = "123456789"
EMAIL = "pagador@test"


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENV", "development")
    import libracore.config_manager as cm
    importlib.reload(cm)

    core.configure(db_path=str(tmp_path / "webhook_test.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    cm.save({"mp_access_token": "TOKEN", "empresa_iva_condition": "Monotributista"})
    yield cm
    conn.close()
    core._db_path = None


def _app(**kw):
    import libracore.mp_webhook as mw
    importlib.reload(mw)
    aplicacion = FastAPI()
    aplicacion.include_router(mw.build_mp_webhook_router(**kw))
    return aplicacion, mw


@pytest.fixture
def armar(entorno):
    """Devuelve (client, modulo) ya con `obtener_pago` mockeado."""
    def _armar(pago=None, **kw):
        aplicacion, mw = _app(**kw)
        detalle = pago if pago is not None else _pago()
        consultas = []

        async def obtener_pago(payment_id, access_token):
            consultas.append(payment_id)
            return detalle

        mw.mp_api.obtener_pago = obtener_pago
        cliente = TestClient(aplicacion)
        cliente.consultas = consultas
        return cliente, mw
    return _armar


def _pago(**overrides):
    pago = {
        "status": "approved",
        "transaction_amount": 5000.0,
        "description": "Abono mensual",
        "payment_type_id": "credit_card",
        "payment_method_id": "visa",
        "external_reference": "",
        "payer": {
            "email": EMAIL, "first_name": "Ana", "last_name": "Pagadora",
            "identification": {"type": "CUIT", "number": "30712345678"},
        },
    }
    pago.update(overrides)
    return pago


def _cuerpo(payment_id=PAYMENT_ID, tipo="payment"):
    return json.dumps({"type": tipo, "data": {"id": payment_id}}).encode()


def _firmar(payment_id, request_id, secreto=SECRETO, ts="1700000000"):
    plantilla = f"id:{payment_id};request-id:{request_id};ts:{ts}"
    v1 = hmac.new(secreto.encode(), plantilla.encode(), hashlib.sha256).hexdigest()
    return {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}


def _postear(cliente, cuerpo=None, headers=None):
    return cliente.post(
        "/webhooks/mercadopago",
        content=cuerpo if cuerpo is not None else _cuerpo(),
        headers={"content-type": "application/json", **(headers or {})},
    )


# ── Regla 1: la firma ────────────────────────────────────────────────────────

def test_con_secreto_la_firma_manda_en_las_dos_direcciones(armar, entorno):
    """🔴 Las dos mitades. Sólo el rechazo pasaría igual con un endpoint que
    rechaza todo, y sólo la aceptación con uno que no verifica nada."""
    entorno.save({**entorno.load(), "mp_webhook_secret": SECRETO})
    cliente, _ = armar()

    mala = _postear(cliente, headers={"x-signature": "ts=1,v1=deadbeef",
                                      "x-request-id": "req-1"})
    assert mala.status_code == 400
    assert db_mp.get_mp_pago(PAYMENT_ID) is None, "una firma mala no puede registrar el pago"

    buena = _postear(cliente, headers=_firmar(PAYMENT_ID, "req-1"))
    assert buena.status_code == 200, buena.text
    assert db_mp.get_mp_pago(PAYMENT_ID) is not None


def test_sin_firma_pero_con_secreto_se_rechaza(armar, entorno):
    entorno.save({**entorno.load(), "mp_webhook_secret": SECRETO})
    cliente, _ = armar()
    assert _postear(cliente).status_code == 400


def test_la_firma_de_otro_payment_id_no_sirve(armar, entorno):
    """La plantilla lleva el id adentro: una firma válida capturada de otra
    notificación no puede reusarse para ésta."""
    entorno.save({**entorno.load(), "mp_webhook_secret": SECRETO})
    cliente, _ = armar()
    ajena = _firmar("999", "req-1")
    assert _postear(cliente, headers=ajena).status_code == 400


# ── Regla 2: el estado se le pregunta a MercadoPago ──────────────────────────

def test_el_importe_sale_de_la_api_y_no_del_cuerpo(armar):
    """🔑 El cuerpo de la notificación sólo aporta el id. Si el importe se
    tomara de ahí, cualquiera que conozca la URL factura lo que quiera."""
    cliente, _ = armar()
    cuerpo = json.dumps({
        "type": "payment",
        "data": {"id": PAYMENT_ID},
        "transaction_amount": 1.0,   # mentira del que llama
    }).encode()
    assert _postear(cliente, cuerpo).status_code == 200
    assert db_mp.get_mp_pago(PAYMENT_ID)["monto"] == 5000.0
    assert cliente.consultas == [PAYMENT_ID], "tiene que haber consultado el pago real"


# ── Regla 3: contesta 200 casi siempre ───────────────────────────────────────

def test_un_error_de_la_api_de_mp_no_dispara_reintentos(armar, entorno):
    """MercadoPago reintenta ante cualquier código que no sea 2xx. Un 500 por
    un problema propio convierte un error en una tormenta."""
    aplicacion, mw = _app()

    async def explota(payment_id, access_token):
        raise RuntimeError("timeout contra MP")

    mw.mp_api.obtener_pago = explota
    r = TestClient(aplicacion).post("/webhooks/mercadopago", content=_cuerpo())
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_sin_access_token_contesta_200_y_no_procesa(armar, entorno):
    entorno.save({**entorno.load(), "mp_access_token": ""})
    cliente, _ = armar()
    r = _postear(cliente)
    assert r.status_code == 200
    assert db_mp.get_mp_pago(PAYMENT_ID) is None


def test_json_ilegible_es_400(armar):
    """Acá el 400 sí corresponde: el reintento tampoco va a poder parsearlo."""
    cliente, _ = armar()
    assert _postear(cliente, b"{no es json").status_code == 400


def test_un_evento_que_no_es_pago_se_ignora(armar):
    cliente, _ = armar()
    r = _postear(cliente, _cuerpo(tipo="plan"))
    assert r.status_code == 200
    assert r.json()["msg"] == "ignored"


# ── Regla 4: idempotencia ────────────────────────────────────────────────────

def test_el_reintento_de_mp_no_duplica(armar):
    """Las dos mitades: el reintento no entra, pero un pago distinto sí."""
    cliente, _ = armar()
    assert _postear(cliente).status_code == 200
    repetido = _postear(cliente)
    assert repetido.json()["msg"] == "already processed"
    assert len(cliente.consultas) == 1, "el reintento ni siquiera consulta a MP"

    assert _postear(cliente, _cuerpo("otro-pago")).status_code == 200
    assert db_mp.get_mp_pago("otro-pago") is not None


# ── Auto-facturación ─────────────────────────────────────────────────────────

def test_sin_la_bandera_el_pago_queda_pendiente(armar):
    """El control negativo de la auto-facturación: un cliente que existe pero
    no la pidió **no** se factura solo."""
    db_clients.create_client(name="Cliente", email=EMAIL, iva_condition="Monotributista")
    cliente, _ = armar()
    _postear(cliente)
    guardado = db_mp.get_mp_pago(PAYMENT_ID)
    assert guardado["estado_factura"] == "pendiente"
    assert guardado["factura_id"] is None


def test_con_la_bandera_se_factura_solo(armar):
    cliente_id = db_clients.create_client(
        name="Auto SA", email=EMAIL, cuit_dni="30712345678",
        iva_condition="Monotributista",
    )
    db_clients.toggle_auto_facturar(cliente_id)
    cliente, _ = armar()
    _postear(cliente)

    guardado = db_mp.get_mp_pago(PAYMENT_ID)
    assert guardado["estado_factura"] == "facturado"
    factura = db_facturas.get_factura(guardado["factura_id"])
    assert factura["cliente_razon"] == "Auto SA"
    assert factura["total"] == 5000.0


def test_el_alias_decide_a_quien_se_le_auto_factura(armar):
    """El webhook resuelve el cliente por `resolver_cliente_pago`, no por su
    cuenta: el alias tiene que ganarle al placeholder más nuevo también acá."""
    real = db_clients.create_client(
        name="Real SA", email=EMAIL, cuit_dni="30712345678",
        iva_condition="Monotributista",
    )
    db_clients.toggle_auto_facturar(real)
    db_clients.create_client(name=EMAIL, email=EMAIL, iva_condition="Consumidor Final")
    db_mp.crear_alias_facturacion("email", EMAIL, real)

    cliente, _ = armar()
    _postear(cliente)
    guardado = db_mp.get_mp_pago(PAYMENT_ID)
    assert guardado["estado_factura"] == "facturado"
    assert db_facturas.get_factura(guardado["factura_id"])["cliente_razon"] == "Real SA"


def test_la_regla_de_negocio_del_producto_puede_facturar_sin_bandera(armar):
    """La costura de `debe_auto_facturar`: es como Contalibra factura sus
    cobros de *Hosting Mensual* sin que el motor sepa qué es eso."""
    db_clients.create_client(name="Sin Bandera", email=EMAIL,
                             iva_condition="Monotributista")

    def por_descripcion(client, contexto):
        return contexto["descripcion"].lower().startswith("abono")

    cliente, _ = armar(debe_auto_facturar=por_descripcion)
    _postear(cliente)
    assert db_mp.get_mp_pago(PAYMENT_ID)["estado_factura"] == "facturado"


# ── La costura de la referencia externa ──────────────────────────────────────

def test_una_referencia_conocida_va_a_su_manejador(armar):
    """El cobro por QR de una venta presencial no es una suscripción: lo
    resuelve el producto, con el id que venía en la referencia."""
    vistos = []

    async def manejar_venta(venta_id, payment_id, pago, cfg):
        vistos.append((venta_id, payment_id))
        return None

    # ⚠️ El detalle que devuelve MercadoPago trae OTRO id que el de la
    # notificacion. En produccion coinciden; el test los separa a proposito,
    # porque el manejador tiene que recibir el de la notificacion --- que es el
    # que sella la idempotencia y el que se guarda en `mp_pagos`.
    cliente, _ = armar(
        pago=_pago(external_reference="venta-42", id="otro-id-del-detalle"),
        manejadores_de_referencia={"venta-": manejar_venta},
    )
    r = _postear(cliente)
    assert r.status_code == 200
    assert vistos == [(42, PAYMENT_ID)], "el payment_id es el de la notificacion"
    assert db_mp.get_mp_pago(PAYMENT_ID) is not None


def test_si_el_manejador_falla_el_cobro_igual_queda_registrado(armar):
    """🔑 El cobro ya está hecho. Perderlo sería peor que quedarse sin la
    factura, que se puede emitir después a mano."""
    async def explota(venta_id, payment_id, pago, cfg):
        raise RuntimeError("la venta no existe")

    cliente, _ = armar(
        pago=_pago(external_reference="venta-42"),
        manejadores_de_referencia={"venta-": explota},
    )
    assert _postear(cliente).status_code == 200
    assert db_mp.get_mp_pago(PAYMENT_ID) is not None


def test_una_referencia_de_otro_prefijo_sigue_el_camino_normal(armar):
    """Control negativo de la costura: si el prefijo no matchea, el pago no
    puede desaparecer por el desvío."""
    llamado = []

    async def manejar_venta(venta_id, payment_id, pago, cfg):
        llamado.append(venta_id)
        return None

    cliente, _ = armar(
        pago=_pago(external_reference="reserva-7"),
        manejadores_de_referencia={"venta-": manejar_venta},
    )
    _postear(cliente)
    assert llamado == []
    assert db_mp.get_mp_pago(PAYMENT_ID)["estado_factura"] == "pendiente"


# ── Un pago no aprobado ──────────────────────────────────────────────────────

def test_un_pago_rechazado_se_registra_pero_no_entra_a_la_bandeja(armar):
    cliente, _ = armar(pago=_pago(status="rejected"))
    _postear(cliente)
    guardado = db_mp.get_mp_pago(PAYMENT_ID)
    assert guardado is not None, "queda el rastro del intento"
    assert guardado["estado_factura"] is None
