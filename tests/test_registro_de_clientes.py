"""Un producto cuyos clientes NO viven en `libracore.db.clients`.

Es el caso de [[gestiolibra]] y [[medlibra]] —sus clientes salen de LibraGenda,
con id `String(100)`— y de [[libraclub]] y [[ventalibra]], que tienen el suyo.

El registro de prueba de acá usa **ids de texto a propósito**: si el módulo
asumiera en algún lado que el id es un entero, estos tests lo destapan.

Y el control que sostiene todo lo demás: **sin registro explícito, el
comportamiento es el de LibraCore**, que es lo que Contalibra y Restolibra ya
tenían. Si eso se rompe, la costura no es una costura, es un cambio.
"""

import asyncio
import importlib

import pytest

from libracore.db import clients as db_clients
from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db import mp as db_mp
from libracore.db.schema import init_core_schema
from libracore.registro_de_clientes import RegistroDeClientes

EMAIL = "socio@club.test"
CUIT = "20111111112"


class RegistroDeJuguete:
    """Como el de un producto cuyos clientes viven en otro motor.

    Guarda en un dict, con **id de texto**, y tiene sus propios alias — porque
    `facturacion_alias.cliente_id` es `INTEGER` y no le sirve.
    """

    def __init__(self):
        self.clientes: dict[str, dict] = {}
        self.alias: dict[str, str] = {}
        self.creados: list[str] = []
        self.consultas: list[tuple[str, str]] = []

    def agregar(self, id_, **datos):
        self.clientes[id_] = {"id": id_, "name": datos.get("name", ""),
                              "cuit_dni": datos.get("cuit_dni", ""),
                              "email": datos.get("email", ""),
                              "iva_condition": datos.get("iva_condition", "Consumidor Final"),
                              "address": datos.get("address", ""),
                              "auto_facturar": datos.get("auto_facturar", False)}
        return self.clientes[id_]

    # ── el puerto ───────────────────────────────────────────────────────────

    def resolver(self, payer_email: str, payer_cuit: str) -> dict | None:
        self.consultas.append((payer_email, payer_cuit))
        for clave in (payer_email, payer_cuit):
            if clave and clave in self.alias:
                return self.clientes[self.alias[clave]]
        for c in self.clientes.values():
            if payer_email and c["email"] == payer_email:
                return c
            if payer_cuit and c["cuit_dni"].replace("-", "") == payer_cuit:
                return c
        return None

    def crear(self, *, nombre, email="", cuit_dni="",
              iva_condition="Consumidor Final", address="") -> dict:
        id_ = f"socio-{len(self.clientes) + 1}"
        self.creados.append(id_)
        return self.agregar(id_, name=nombre, email=email, cuit_dni=cuit_dni,
                            iva_condition=iva_condition, address=address)

    def buscar_muchos(self, emails, cuits):
        por_email = {c["email"]: c for c in self.clientes.values() if c["email"] in emails}
        por_cuit = {
            c["cuit_dni"].replace("-", ""): c
            for c in self.clientes.values()
            if c["cuit_dni"].replace("-", "") in cuits
        }
        return por_email, por_cuit


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENV", "development")
    import libracore.config_manager as cm
    importlib.reload(cm)
    core.configure(db_path=str(tmp_path / "registro_test.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    cm.save({"mp_access_token": "TOKEN", "empresa_iva_condition": "Monotributista"})
    yield cm
    conn.close()
    core._db_path = None


@pytest.fixture
def registro():
    return RegistroDeJuguete()


def test_cumple_el_protocolo(registro):
    assert isinstance(registro, RegistroDeClientes)


# ── La factura sale con el cliente del registro del producto ────────────────

def test_la_factura_usa_el_registro_del_producto(entorno, registro):
    from libracore import mp_facturacion

    registro.agregar("socio-7", name="CLUB EL SAUCE SA", cuit_dni=CUIT,
                     email=EMAIL, iva_condition="Responsable Inscripto")

    factura_id, _, _, _ = asyncio.run(mp_facturacion.generar_factura_mp(
        monto=8000.0, payer_email=EMAIL, payer_name="Quien Paga",
        referencia="mp-1", cfg=entorno.load(), registro=registro,
    ))
    factura = db_facturas.get_factura(factura_id)
    assert factura["cliente_razon"] == "CLUB EL SAUCE SA"
    assert factura["cliente_cuit"] == CUIT


def test_no_deja_clientes_basura_en_la_tabla_de_libracore(entorno, registro):
    """🔴 Es el defecto que la costura evita.

    Sin registro propio, un cobro que no matchea crea un cliente placeholder en
    `libracore.db.clients` — una tabla que ese producto **no usa**: filas basura,
    y una factura a "Consumidor Final" con el email como razón social.
    """
    factura_id, _, _, _ = asyncio.run(mp_facturacion_de().generar_factura_mp(
        monto=8000.0, payer_email=EMAIL, payer_name="Quien Paga",
        referencia="mp-1", cfg=entorno.load(), registro=registro,
    ))
    assert db_clients.get_all_clients() == [], "no puede haber tocado la tabla de libracore"
    assert registro.creados == ["socio-1"], "el alta va al registro del producto"
    assert db_facturas.get_factura(factura_id)["cliente_razon"] == "Quien Paga"


def mp_facturacion_de():
    from libracore import mp_facturacion
    return mp_facturacion


def test_el_id_de_texto_no_rompe_nada(entorno, registro):
    """`libragenda.clients.id` es `String(100)`. El motor no puede asumir un
    entero en ningún lado del camino."""
    registro.agregar("un-id-larguisimo-de-texto", name="Con Id De Texto",
                     cuit_dni=CUIT, email=EMAIL)
    factura_id, numero, _, _ = asyncio.run(mp_facturacion_de().generar_factura_mp(
        monto=1000.0, payer_email=EMAIL, payer_name="x",
        referencia="mp-2", cfg=entorno.load(), registro=registro,
    ))
    assert db_facturas.get_factura(factura_id)["cliente_razon"] == "Con Id De Texto"
    assert numero.startswith("0001-")


# ── El control: sin registro, el de LibraCore ──────────────────────────────

def test_sin_registro_usa_el_de_libracore(entorno):
    """La otra mitad, y es la que sostiene que esto no cambie nada en
    Contalibra ni en Restolibra."""
    db_clients.create_client(name="EL DE SIEMPRE SA", email=EMAIL, cuit_dni=CUIT,
                             iva_condition="Monotributista")
    factura_id, _, _, _ = asyncio.run(mp_facturacion_de().generar_factura_mp(
        monto=500.0, payer_email=EMAIL, payer_name="x",
        referencia="mp-3", cfg=entorno.load(),
    ))
    assert db_facturas.get_factura(factura_id)["cliente_razon"] == "EL DE SIEMPRE SA"


# ── Los otros tres caminos ─────────────────────────────────────────────────

def test_el_cron_usa_el_registro(entorno, registro):
    import libracore.mp_sync as ms
    importlib.reload(ms)

    registro.agregar("socio-3", name="AUTO SA", cuit_dni=CUIT, email=EMAIL,
                     auto_facturar=True)

    async def usuario_info(_t):
        return {"id": "555", "email": "yo@test"}

    async def movimientos(_t, _d, _h):
        return [{
            "id": "mov-1", "collector_id": "555", "transaction_amount": 3000.0,
            "external_reference": "", "description": "Cuota",
            "payment_type_id": "credit_card", "payment_method_id": "visa",
            "date_approved": "2026-08-24T10:00:00.000-03:00",
            "payer": {"email": EMAIL, "first_name": "", "last_name": "",
                      "identification": {"type": "CUIT", "number": CUIT}},
        }]

    ms.mp_api.obtener_usuario_info = usuario_info
    ms.mp_api.obtener_movimientos = movimientos

    resultado = asyncio.run(ms.sincronizar_y_facturar(dias=2, registro=registro))
    assert resultado["facturados"] == 1, resultado
    movimiento = db_mp.get_mp_movimiento_by_mp_id("mov-1")
    assert db_facturas.get_factura(movimiento["factura_id"])["cliente_razon"] == "AUTO SA"
    assert db_clients.get_all_clients() == []


def test_el_webhook_usa_el_registro(entorno, registro):
    import libracore.mp_webhook as mw
    importlib.reload(mw)
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    registro.agregar("socio-4", name="WEBHOOK SA", cuit_dni=CUIT, email=EMAIL,
                     auto_facturar=True)

    async def obtener_pago(_pid, _tok):
        return {
            "status": "approved", "transaction_amount": 4000.0,
            "description": "Cuota", "payment_type_id": "credit_card",
            "payment_method_id": "visa", "external_reference": "",
            "payer": {"email": EMAIL, "first_name": "", "last_name": "",
                      "identification": {"type": "CUIT", "number": CUIT}},
        }

    mw.mp_api.obtener_pago = obtener_pago
    app = FastAPI()
    app.include_router(mw.build_mp_webhook_router(registro=registro))
    r = TestClient(app).post(
        "/webhooks/mercadopago",
        content=json.dumps({"type": "payment", "data": {"id": "pago-9"}}).encode(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200, r.text
    pago = db_mp.get_mp_pago("pago-9")
    assert pago["estado_factura"] == "facturado", pago
    assert db_facturas.get_factura(pago["factura_id"])["cliente_razon"] == "WEBHOOK SA"


def test_la_bandeja_enriquece_con_el_registro(entorno, registro):
    import libracore.mp_bandeja_router as mbr
    importlib.reload(mbr)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    registro.agregar("socio-5", name="EN LA BANDEJA SA", cuit_dni=CUIT, email=EMAIL)
    db_mp.create_mp_pago(mp_payment_id="p1", status="approved", monto=1.0,
                         payer_email=EMAIL, payer_name="", estado_factura="pendiente")
    db_mp.create_mp_pago(mp_payment_id="p2", status="approved", monto=1.0,
                         payer_email="nadie@test", payer_name="",
                         estado_factura="pendiente")

    app = FastAPI()
    app.include_router(mbr.build_mp_bandeja_router(registro=registro))
    datos = TestClient(app).get("/api/mp-bandeja").json()
    por_id = {p["mp_payment_id"]: p for p in datos["pendientes"]}
    assert por_id["p1"]["cliente"]["name"] == "EN LA BANDEJA SA"
    assert por_id["p2"]["cliente"] is None, "el que no matchea viene en null"
