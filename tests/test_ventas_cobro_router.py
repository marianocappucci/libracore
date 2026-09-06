"""La factura desde la venta y el cobro por QR (P9-M3), con un puerto de ventas
en memoria: lo que `tests/test_venta_facturacion.py` de Contalibra y
`tests/test_mp_qr_endpoints.py` de Restolibra esperan, sin la venta real."""

from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore import pdf_generator as pdf_gen
from libracore import venta_facturacion as vf
from libracore.db import caja as db_caja
from libracore.db import core
from libracore.db.schema import init_core_schema
from libracore.ventas_cobro_router import ClienteMercadoPago, build_cobro_de_ventas_router

USUARIO = {"id": 7, "username": "cajero", "role": "admin"}


class VentasEnMemoria:
    """Un `PuertoDeVentas` sobre un dict: la forma de `libracommerce.erp.ventas`
    sin el motor. Los movimientos de caja sí son reales (LibraCore)."""

    def __init__(self):
        self.ventas: dict[int, dict] = {}
        self.acreditaciones: list[tuple] = []

    def alta(self, vid=1, total=1500.0, pagos=None, items=None, **extra):
        pagos = pagos if pagos is not None else [{"medio": "efectivo", "monto": total, "estado": "aprobado"}]
        v = {
            "id": vid, "numero": f"V-{vid:05d}", "fecha": "2026-09-06", "total": total,
            "subtotal": total, "descuento": 0.0, "estado": "cobrada", "status": "confirmed",
            "items": items or [{"nombre": "Agua sin gas", "qty": 1, "precio": total, "subtotal": total}],
            "pagos": pagos, "cliente_id": None, "usuario_id": USUARIO["id"],
            "factura_id": None, "mp_payment_id": "", "mp_order_id": "",
        }
        v.update(extra)
        self.ventas[vid] = v
        for p in pagos:
            if p["estado"] == "aprobado":
                db_caja.create_caja_movimiento(
                    v["fecha"], "ingreso", f"Venta {v['numero']} — {p['medio']}", p["monto"],
                    medio_pago=p["medio"])
        return v

    # ── el puerto ──
    def obtener(self, vid):
        return dict(self.ventas[vid]) if vid in self.ventas else None

    def vincular_factura(self, vid, factura_id):
        self.ventas[vid]["factura_id"] = factura_id

    def vincular_cobros(self, numero, factura_id):
        with core.get_connection() as conn:
            cur = conn.execute(
                "UPDATE caja_movimientos SET factura_id=? WHERE factura_id IS NULL AND concepto LIKE ?",
                (factura_id, f"Venta {numero} %"))
            return cur.rowcount

    def set_pago_mp(self, vid, payment_id):
        self.ventas[vid]["mp_payment_id"] = payment_id

    def set_orden_mp(self, vid, order_id):
        self.ventas[vid]["mp_order_id"] = order_id

    def acreditar(self, vid, payment_id, usuario_id):
        self.acreditaciones.append((vid, payment_id, usuario_id))
        v = self.ventas[vid]
        pendientes = [p for p in v["pagos"] if p["estado"] == "pendiente"]
        for p in pendientes:
            p["estado"] = "aprobado"
            p["referencia"] = f"MP#{payment_id}"
            db_caja.create_caja_movimiento(
                v["fecha"], "ingreso", f"Venta {v['numero']} — {p['medio']}", p["monto"],
                referencia=f"MP#{payment_id}", medio_pago=p["medio"])
        if pendientes:
            v["estado"], v["status"] = "cobrada", "confirmed"
        return bool(pendientes)

    def sellar_referencia_mp(self, vid, payment_id):
        for p in self.ventas[vid]["pagos"]:
            if p["medio"] == "mercadopago" and not p.get("referencia"):
                p["referencia"] = f"MP#{payment_id}"

    def puerto(self, **extra):
        return vf.PuertoDeVentas(
            obtener=self.obtener, vincular_factura=self.vincular_factura,
            vincular_cobros=self.vincular_cobros, set_pago_mp=self.set_pago_mp,
            acreditar=self.acreditar, sellar_referencia_mp=self.sellar_referencia_mp,
            set_orden_mp=self.set_orden_mp, **extra)


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    import libracore.config_manager as cm
    importlib.reload(cm)
    monkeypatch.setattr(pdf_gen, "FACTURAS_PDF_DIR", str(tmp_path / "pdf"))

    core.configure(db_path=str(tmp_path / "ventas.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?,?,?,?,?)",
        (USUARIO["id"], USUARIO["username"], "Cajero", "x", "admin"))
    conn.execute("INSERT INTO cajas (nombre, descripcion, medios_pago, es_default) VALUES ('Caja', '', '[]', 1)")
    conn.commit()
    conn.close()
    cm.save({"empresa_iva_condition": "Monotributista"})
    yield cm
    core._db_path = None


@pytest.fixture
def ventas(entorno):
    return VentasEnMemoria()


def _run(coro):
    return asyncio.run(coro)


def _facturas():
    with core.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM facturas").fetchone()[0]


def _caja(factura_id=None):
    with core.get_connection() as conn:
        if factura_id is None:
            return conn.execute("SELECT COUNT(*) FROM caja_movimientos").fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM caja_movimientos WHERE factura_id=?", (factura_id,)).fetchone()[0]


def _app(ventas: VentasEnMemoria, mp: ClienteMercadoPago | None = None, **extra) -> TestClient:
    app = FastAPI()
    app.include_router(build_cobro_de_ventas_router(
        ventas=ventas.puerto(**extra), usuario_actual=lambda: USUARIO, mercadopago=mp))
    return TestClient(app)


# ── facturar_venta ───────────────────────────────────────────────────────


def test_factura_la_venta_y_vincula_sin_tocar_la_caja(ventas):
    v = ventas.alta(1, 1500.0)
    antes = _caja()
    factura = _run(vf.facturar_venta(ventas.puerto(), 1, usuario_id=USUARIO["id"]))
    assert factura["tipo"] == 11 and factura["total"] == 1500.0 and factura["cae"]
    assert factura["cliente_razon"] == "Consumidor Final" and not factura["cliente_cuit"]
    assert factura["condicion_venta"] == "Contado" and factura["observaciones"] == f"Venta {v['numero']}"
    assert factura["usuario_id"] == USUARIO["id"]
    assert ventas.obtener(1)["factura_id"] == factura["id"]
    # No registró un cobro nuevo: vinculó el que ya estaba.
    assert _caja() == antes and _caja(factura["id"]) == 1
    # Idempotente: la segunda vez devuelve la misma.
    otra = _run(vf.facturar_venta(ventas.puerto(), 1))
    assert otra["id"] == factura["id"]
    assert _facturas() == 1


def test_no_factura_lo_que_no_existe_ni_lo_anulado(ventas):
    with pytest.raises(vf.VentaNoFacturable):
        _run(vf.facturar_venta(ventas.puerto(), 99))
    ventas.alta(2, estado="anulada", status="cancelled")
    with pytest.raises(vf.VentaNoFacturable, match="anulada"):
        _run(vf.facturar_venta(ventas.puerto(), 2))
    ventas.alta(3)
    ventas.ventas[3]["items"] = []
    with pytest.raises(vf.VentaNoFacturable, match="ítems"):
        _run(vf.facturar_venta(ventas.puerto(), 3))


def test_cliente_registrado_y_responsable_inscripto(ventas, entorno):
    entorno.save({"empresa_iva_condition": "Responsable Inscripto"})
    with core.get_connection() as conn:
        conn.execute("INSERT INTO clients (id, name, cuit_dni, iva_condition) VALUES (5, 'Complejo Padel SRL', '30712345678', 'Responsable Inscripto')")
    ventas.alta(1, 12100.0, cliente_id=5)
    factura = _run(vf.facturar_venta(ventas.puerto(), 1))
    assert factura["tipo"] == 1 and factura["cliente_razon"] == "Complejo Padel SRL"
    assert factura["cliente_cuit"] == "30712345678"
    # El IVA sale de la diferencia contra el total, y el total es el de la venta.
    assert factura["total"] == 12100.0 and factura["subtotal"] == 10000.0 and factura["iva_amount"] == 2100.0
    # Un cliente que no es RI recibe B.
    with core.get_connection() as conn:
        conn.execute("INSERT INTO clients (id, name, iva_condition) VALUES (6, 'Juan', 'Consumidor Final')")
    ventas.alta(2, 121.0, cliente_id=6)
    assert (_run(vf.facturar_venta(ventas.puerto(), 2)))["tipo"] == 6


def test_la_alicuota_propia_de_la_venta_manda(ventas, entorno):
    entorno.save({"empresa_iva_condition": "Responsable Inscripto"})
    ventas.alta(1, 1000.0)
    factura = _run(vf.facturar_venta(ventas.puerto(alicuota_de=lambda vid: 0.0), 1))
    assert factura["iva_amount"] == 0.0 and factura["total"] == 1000.0 and factura["subtotal"] == 1000.0
    ventas.alta(2, 1105.0)
    factura = _run(vf.facturar_venta(ventas.puerto(alicuota_de=lambda vid: 0.105), 2))
    assert factura["subtotal"] == 1000.0 and factura["iva_amount"] == 105.0


def test_descuento_y_condicion_de_venta(ventas):
    ventas.alta(1, 900.0, items=[{"nombre": "A", "qty": 1, "precio": 1000.0, "subtotal": 1000.0}], descuento=100.0,
                pagos=[{"medio": "tarjeta_debito", "monto": 900.0, "estado": "aprobado"}])
    factura = _run(vf.facturar_venta(ventas.puerto(), 1))
    assert factura["total"] == 900.0 and any(i["description"] == "Descuento" for i in factura["items"])
    assert factura["condicion_venta"] == "Tarjeta de Débito"
    ventas.alta(2, 200.0, pagos=[{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"},
                                 {"medio": "mercado_pago", "monto": 100.0, "estado": "aprobado"}])
    assert (_run(vf.facturar_venta(ventas.puerto(), 2)))["condicion_venta"] == "Otra"
    ventas.alta(3, 200.0, pagos=[{"medio": "qr", "monto": 200.0, "estado": "aprobado"}])
    assert (_run(vf.facturar_venta(ventas.puerto(), 3)))["condicion_venta"] == "Otros medios de pago electrónico"


def test_el_punto_de_venta_es_el_del_mostrador_donde_se_cobro(ventas):
    from libracore.db import turnos as db_turnos

    with core.get_connection() as conn:
        conn.execute("INSERT INTO cajas (nombre, descripcion, medios_pago, punto_venta) VALUES ('POS 2', '', '[]', 42)")
        caja_id = conn.execute("SELECT id FROM cajas WHERE nombre='POS 2'").fetchone()[0]
    db_turnos.create_turno(USUARIO["id"], 0, "", caja_id=caja_id)
    ventas.alta(1, 100.0)
    assert (_run(vf.facturar_venta(ventas.puerto(), 1)))["punto_venta"] == 42
    # Sin turno abierto (otro usuario), el de la empresa.
    ventas.alta(2, 100.0, usuario_id=None)
    assert (_run(vf.facturar_venta(ventas.puerto(), 2)))["punto_venta"] == 1


def test_el_producto_puede_poner_su_numerador(ventas):
    """El ambiente de la factura sale del `arca` con el que se numeró: con el
    numerador del producto parcheado a homologación, la factura queda marcada."""
    ventas.alta(1, 100.0)

    async def _homologacion(punto_venta, tipo):
        return 501, None, {"ambiente": "homologacion", "cuit": "20111111119"}

    factura = _run(vf.facturar_venta(ventas.puerto(numerar_comprobante=_homologacion), 1))
    assert factura["numero"] == 501 and factura["ambiente"] == "homologacion"
    # Sin ARCA (`arca=None`) el comprobante es real.
    ventas.alta(2, 100.0)

    async def _sin_arca(punto_venta, tipo):
        return 7, None, None

    assert _run(vf.facturar_venta(ventas.puerto(numerar_comprobante=_sin_arca), 2))["ambiente"] == "produccion"


def test_facturar_si_esta_prendida(ventas, entorno):
    ventas.alta(1)
    assert _run(vf.facturar_si_esta_prendida(ventas.puerto(), 1)) is None
    assert not ventas.obtener(1)["factura_id"]
    entorno.save({**entorno.load(), "mp_auto_facturar_ventas": True})
    fid = _run(vf.facturar_si_esta_prendida(ventas.puerto(), 1))
    assert fid and ventas.obtener(1)["factura_id"] == fid
    # No propaga: una venta que no existe da None, no una excepción.
    assert _run(vf.facturar_si_esta_prendida(ventas.puerto(), 99)) is None


def test_el_manejador_del_webhook_acredita_y_factura(ventas, entorno):
    ventas.alta(1, 500.0, pagos=[{"medio": "mercadopago", "monto": 500.0, "estado": "pendiente"}])
    antes = _caja()
    manejador = vf.manejador_de_cobro_por_qr(ventas.puerto())
    assert _run(manejador(1, "987", {"id": "otro"}, {"mp_auto_facturar_ventas": False})) is None
    v = ventas.obtener(1)
    assert v["mp_payment_id"] == "987" and v["estado"] == "cobrada" and _caja() == antes + 1
    assert v["pagos"][0]["referencia"] == "MP#987"
    fid = _run(manejador(1, "987", {}, {"mp_auto_facturar_ventas": True}))
    assert fid and _caja() == antes + 1  # acreditar es del puerto: sin pendientes no escribe
    assert ventas.acreditaciones == [(1, "987", None), (1, "987", None)]


# ── El router ────────────────────────────────────────────────────────────


def _mp(pago=None, orden=None):
    llamadas = []

    async def crear_orden_qr(**kw):
        llamadas.append(("orden", kw))
        if isinstance(orden, Exception):
            raise orden
        return orden or {}

    async def buscar(referencia, token):
        llamadas.append(("buscar", referencia, token))
        if isinstance(pago, Exception):
            raise pago
        return pago

    return ClienteMercadoPago(crear_orden_qr=crear_orden_qr, buscar_pago_por_referencia=buscar), llamadas


def _mp_configurado(entorno):
    entorno.save({**entorno.load(), "mp_access_token": "APP_USR-test", "mp_pos_id": "RESTODEV", "mp_user_id": "3392230021"})


def test_facturar_por_http(ventas):
    ventas.alta(1, 1500.0)
    client = _app(ventas)
    r = client.post("/api/ventas/1/facturar")
    assert r.status_code == 200, r.text
    assert r.json()["factura"]["cae"] and r.json()["venta"]["factura_id"] == r.json()["factura"]["id"]
    assert client.post("/api/ventas/1/facturar").json()["factura"]["id"] == r.json()["factura"]["id"]
    assert client.post("/api/ventas/99/facturar").status_code == 422
    ventas.alta(2, estado="anulada", status="cancelled")
    r = client.post("/api/ventas/2/facturar")
    assert r.status_code == 422 and "anulada" in r.json()["detail"].lower()


def test_mp_qr_sin_configurar_lo_dice_antes_de_salir_a_la_red(ventas):
    ventas.alta(1)
    mp, llamadas = _mp()
    r = _app(ventas, mp).post("/api/ventas/1/mp-qr")
    assert r.status_code == 400 and "POS ID" in r.json()["detail"]
    assert llamadas == []


def test_mp_qr_pone_la_orden_y_no_devuelve_imagen(ventas, entorno):
    _mp_configurado(entorno)
    ventas.alta(4, 5000.0)
    mp, llamadas = _mp()
    client = _app(ventas, mp)
    assert client.post("/api/ventas/999/mp-qr").status_code == 404
    r = client.post("/api/ventas/4/mp-qr")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "total": 5000.0, "pos_id": "RESTODEV"}
    kw = llamadas[0][1]
    assert kw["external_reference"] == "venta-4" and kw["total"] == 5000.0
    assert kw["user_id"] == "3392230021" and kw["access_token"] == "APP_USR-test"
    assert ventas.obtener(4)["mp_order_id"] == "venta-4"
    # Si MercadoPago devuelve la orden, se guarda ese id.
    mp, _ = _mp(orden={"in_store_order_id": "ORD-9"})
    _app(ventas, mp).post("/api/ventas/4/mp-qr")
    assert ventas.obtener(4)["mp_order_id"] == "ORD-9"


def test_mp_qr_rechazado_es_502(ventas, entorno):
    _mp_configurado(entorno)
    ventas.alta(1)
    mp, _ = _mp(orden=RuntimeError("caja inexistente"))
    r = _app(ventas, mp).post("/api/ventas/1/mp-qr")
    assert r.status_code == 502 and "caja inexistente" in r.json()["detail"]


def test_mp_status_sin_pago_dice_pending_y_no_toca_nada(ventas, entorno):
    _mp_configurado(entorno)
    ventas.alta(1, pagos=[{"medio": "mercadopago", "monto": 1500.0, "estado": "pendiente"}])
    antes = _caja()
    mp, llamadas = _mp(pago=None)
    client = _app(ventas, mp)
    assert client.get("/api/ventas/999/mp-status").status_code == 404
    assert client.get("/api/ventas/1/mp-status").json() == {"status": "pending"}
    assert _caja() == antes and llamadas[0][1] == "venta-1"


def test_mp_status_aprobado_acredita_y_el_poll_no_duplica(ventas, entorno):
    _mp_configurado(entorno)
    ventas.alta(1, 1500.0, pagos=[{"medio": "mercadopago", "monto": 1500.0, "estado": "pendiente"}])
    antes = _caja()
    mp, _ = _mp(pago={"id": 987654321, "status": "approved"})
    client = _app(ventas, mp)
    r = client.get("/api/ventas/1/mp-status")
    assert r.json() == {"status": "approved", "payment_id": "987654321", "factura_id": None}
    assert _caja() == antes + 1 and ventas.obtener(1)["estado"] == "cobrada"
    for _ in range(3):
        assert client.get("/api/ventas/1/mp-status").json()["status"] == "approved"
    assert _caja() == antes + 1
    # El cajero de la sesión queda en la acreditación.
    assert ventas.acreditaciones[0] == (1, "987654321", USUARIO["id"])


@pytest.mark.parametrize("status", ["authorized", "rejected", "in_process"])
def test_un_pago_que_no_esta_aprobado_no_acredita(ventas, entorno, status):
    _mp_configurado(entorno)
    ventas.alta(1, pagos=[{"medio": "mercadopago", "monto": 1500.0, "estado": "pendiente"}])
    antes = _caja()
    mp, _ = _mp(pago={"id": 1, "status": status})
    assert _app(ventas, mp).get("/api/ventas/1/mp-status").json() == {"status": status}
    assert _caja() == antes and ventas.obtener(1)["pagos"][0]["estado"] == "pendiente"


def test_mp_status_factura_si_la_automatica_esta_prendida(ventas, entorno):
    _mp_configurado(entorno)
    entorno.save({**entorno.load(), "mp_auto_facturar_ventas": True})
    ventas.alta(1, 1500.0, pagos=[{"medio": "mercadopago", "monto": 1500.0, "estado": "pendiente"}])
    mp, _ = _mp(pago={"id": 55, "status": "approved"})
    client = _app(ventas, mp)
    primera = client.get("/api/ventas/1/mp-status").json()["factura_id"]
    segunda = client.get("/api/ventas/1/mp-status").json()["factura_id"]
    assert primera and primera == segunda
    assert ventas.obtener(1)["factura_id"] == primera
    assert _facturas() == 1


def test_mp_status_sin_respuesta_de_mercadopago_es_502(ventas, entorno):
    _mp_configurado(entorno)
    ventas.alta(1)
    mp, _ = _mp(pago=RuntimeError("timeout"))
    assert _app(ventas, mp).get("/api/ventas/1/mp-status").status_code == 502
    entorno.save({**entorno.load(), "mp_access_token": ""})
    assert _app(ventas, mp).get("/api/ventas/1/mp-status").status_code == 400
