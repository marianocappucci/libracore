"""Facturar en un nodo offline de LibraEdge (ADR-074): con conexión como siempre; sin conexión, ticket y bandeja, y se
factura al volver. El QR de MercadoPago, bloqueado sin conexión. En SQLite y PostgreSQL.

El puerto de ventas es el de `test_ventas_cobro_router` (en memoria); la base del motor es real.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_ventas_cobro_router import USUARIO, VentasEnMemoria

from libracore import arca_wsfe, candados, nodo_offline
from libracore import pdf_generator as pdf_gen
from libracore import venta_facturacion as vf
from libracore.comprobantes_router import build_comprobantes_bandeja_router, build_comprobantes_ingesta_router
from libracore.db import arca_config as db_arca
from libracore.db import comprobantes_pendientes as db_pendientes
from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db.schema import init_core_schema
from libracore.facturas_del_nodo import FacturaEnConflicto, aplicar_factura_del_nodo, payload_factura
from libracore.nodo_offline import ConexionDelNodo, FacturaPendiente, NodoSinArca, SinConexion
from libracore.ventas_cobro_router import (
    ClienteMercadoPago,
    build_cobro_de_ventas_router,
    build_facturacion_del_nodo_router,
)

NODO = "nodo-s02"
PRODUCTO = "ventalibra"


def _preparar(destino: str, cm=None):
    core.configure(db_path=destino)
    conn = core.get_connection()
    init_core_schema(conn)
    conn.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?,?,?,?,?)",
        (USUARIO["id"], USUARIO["username"], "Cajero", "x", "admin"))
    conn.execute("INSERT INTO cajas (nombre, descripcion, medios_pago, es_default) VALUES ('Caja', '', '[]', 1)")
    conn.commit()
    conn.close()


@pytest.fixture(params=["sqlite", "postgres"])
def entorno(request, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    import libracore.config_manager as cm
    importlib.reload(cm)
    monkeypatch.setattr(pdf_gen, "FACTURAS_PDF_DIR", str(tmp_path / "pdf"))
    bases = request.getfixturevalue("bases") if request.param == "postgres" else None
    destino = str(tmp_path / "nodo.db") if bases is None else bases.nueva()
    _preparar(destino)
    cm.save({"empresa_iva_condition": "Monotributista"})
    yield {"cm": cm, "bases": bases, "tmp": tmp_path}
    core._db_path = None
    core._database_url = None


class Red:
    """La conexión del nodo, a mano."""

    def __init__(self, en_linea=False):
        self.en_linea = en_linea

    def conexion(self) -> ConexionDelNodo:
        return ConexionDelNodo(nodo=lambda: NODO, en_linea=lambda: self.en_linea)


@pytest.fixture
def red():
    return Red()


@pytest.fixture
def ventas(entorno):
    return VentasEnMemoria()


def _puerto(ventas, red, **extra):
    return ventas.puerto(conexion=red.conexion(), producto=PRODUCTO, **extra)


def _facturas() -> int:
    with core.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM facturas").fetchone()[0]


def _bandeja() -> list[dict]:
    return db_pendientes.list_por_estado(db_pendientes.ESTADO_PENDIENTE)


def _app(puerto, mp=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_cobro_de_ventas_router(ventas=puerto, usuario_actual=lambda: USUARIO, mercadopago=mp))
    app.include_router(build_facturacion_del_nodo_router(ventas=puerto, usuario_actual=lambda: USUARIO))
    return TestClient(app)


# ── El estado del nodo (sin base) ────────────────────────────────────────────


def _estado(ruta, **campos):
    ruta.write_text(json.dumps({"node_id": NODO, "pendientes": 0, "cursor": 0, **campos}), encoding="utf-8")
    return str(ruta)


def test_en_linea_segun_el_archivo_de_estado(tmp_path):
    ahora = datetime.now(UTC)
    ruta = tmp_path / "estado.json"
    assert not nodo_offline.esta_en_linea(str(ruta))  # sin archivo
    assert nodo_offline.esta_en_linea(_estado(ruta, en_linea=True, ultima_sincronizacion_ok=ahora.isoformat()))
    assert nodo_offline.en_linea_segun_estado(str(ruta))()
    viejo = (ahora - timedelta(minutes=6)).isoformat()
    assert not nodo_offline.esta_en_linea(_estado(ruta, en_linea=True, ultima_sincronizacion_ok=viejo))
    assert not nodo_offline.esta_en_linea(_estado(ruta, en_linea=False, ultima_sincronizacion_ok=ahora.isoformat()))
    assert not nodo_offline.esta_en_linea(_estado(ruta, en_linea=True))  # sin fecha
    futuro = (ahora + timedelta(hours=1)).isoformat()
    assert not nodo_offline.esta_en_linea(_estado(ruta, en_linea=True, ultima_sincronizacion_ok=futuro))
    sin_zona = ahora.replace(tzinfo=None).isoformat()
    assert nodo_offline.esta_en_linea(_estado(ruta, en_linea=True, ultima_sincronizacion_ok=sin_zona))
    ruta.write_text("{roto", encoding="utf-8")
    assert not nodo_offline.esta_en_linea(str(ruta))


def test_conexion_desde_el_entorno(tmp_path, monkeypatch):
    monkeypatch.delenv(nodo_offline.VARIABLE_NODO, raising=False)
    monkeypatch.delenv(nodo_offline.VARIABLE_ESTADO, raising=False)
    fuera = nodo_offline.conexion_desde_entorno()
    assert not fuera.es_nodo() and not fuera.sin_conexion()
    assert not nodo_offline.FUERA_DE_NODO.sin_conexion()

    monkeypatch.setenv(nodo_offline.VARIABLE_NODO, NODO)
    sin_archivo = nodo_offline.conexion_desde_entorno()
    assert sin_archivo.es_nodo() and sin_archivo.node_id() == NODO
    assert sin_archivo.sin_conexion()  # un nodo sin LIBRAEDGE_ESTADO: ante la duda, sin conexión
    with pytest.raises(SinConexion):
        sin_archivo.exigir()

    ruta = _estado(tmp_path / "e.json", en_linea=True, ultima_sincronizacion_ok=datetime.now(UTC).isoformat())
    monkeypatch.setenv(nodo_offline.VARIABLE_ESTADO, ruta)
    assert not nodo_offline.conexion_desde_entorno().sin_conexion()

    def revienta():
        raise OSError("disco")

    assert ConexionDelNodo(nodo=lambda: NODO, en_linea=revienta).sin_conexion()


# ── Sin conexión: ticket y bandeja ───────────────────────────────────────────


def test_sin_conexion_no_factura_y_deja_la_venta_en_la_bandeja(ventas, red):
    v = ventas.alta(1, 1500.0)
    puerto = _puerto(ventas, red)
    with pytest.raises(FacturaPendiente) as e:
        asyncio.run(vf.facturar_venta(puerto, 1))
    assert _facturas() == 0 and ventas.obtener(1)["factura_id"] is None
    [fila] = _bandeja()
    assert e.value.comprobante_pendiente_id == fila["id"]
    assert (fila["origen_producto"], fila["origen_instancia"], fila["origen_tipo"], fila["origen_id"]) == (
        PRODUCTO, NODO, "venta", "1")
    assert fila["total"] == pytest.approx(1500.0) and fila["cliente_razon"] == "Consumidor Final"
    assert v["numero"] in fila["observaciones"]
    assert vf.pendiente_de_facturar(puerto, 1)["id"] == fila["id"]
    # Reintentar sin conexión no duplica.
    with pytest.raises(FacturaPendiente):
        asyncio.run(vf.facturar_venta(puerto, 1))
    assert len(_bandeja()) == 1


def test_la_api_contesta_409_factura_pendiente(ventas, red):
    ventas.alta(1, 800.0)
    r = _app(_puerto(ventas, red)).post("/api/ventas/1/facturar")
    assert r.status_code == 409, r.text
    detalle = r.json()["detail"]
    assert detalle["codigo"] == "factura_pendiente" and detalle["comprobante_pendiente_id"]
    assert "ticket" in detalle["mensaje"] and "al volver" in detalle["mensaje"]


def test_auto_factura_del_qr_sin_conexion_no_rompe_el_cobro(ventas, red, entorno):
    entorno["cm"].save({"empresa_iva_condition": "Monotributista", "mp_auto_facturar_ventas": True})
    ventas.alta(1, 500.0)
    assert asyncio.run(vf.facturar_si_esta_prendida(_puerto(ventas, red), 1)) is None
    assert len(_bandeja()) == 1 and _facturas() == 0


# ── Al volver ────────────────────────────────────────────────────────────────


def test_al_volver_factura_la_bandeja_una_sola_vez(ventas, red):
    ventas.alta(1, 1500.0)
    ventas.alta(2, 300.0)
    subidas = []
    puerto = _puerto(ventas, red, al_facturar=lambda venta, factura: subidas.append((venta["id"], factura["id"])))
    for vid in (1, 2):
        with pytest.raises(FacturaPendiente):
            asyncio.run(vf.facturar_venta(puerto, vid))

    # Sigue sin conexión: la tanda no hace nada.
    nada = asyncio.run(vf.facturar_pendientes_del_nodo(puerto))
    assert nada["en_linea"] is False and nada["facturadas"] == [] and nada["quedan"] == 2

    red.en_linea = True
    hecho = asyncio.run(vf.facturar_pendientes_del_nodo(puerto))
    assert [f["venta_id"] for f in hecho["facturadas"]] == [1, 2] and hecho["quedan"] == 0
    assert all(f["cae"] for f in hecho["facturadas"])
    assert _facturas() == 2
    for vid in (1, 2):
        factura_id = ventas.obtener(vid)["factura_id"]
        fila = db_pendientes.get_por_origen(PRODUCTO, NODO, "venta", vid)
        assert fila["estado"] == "facturado" and fila["factura_id"] == factura_id
    assert subidas == [(1, ventas.obtener(1)["factura_id"]), (2, ventas.obtener(2)["factura_id"])]
    assert vf.pendiente_de_facturar(puerto, 1) is None

    # Otra pasada: nada nuevo.
    otra = asyncio.run(vf.facturar_pendientes_del_nodo(puerto))
    assert otra["facturadas"] == [] and _facturas() == 2


def test_al_volver_una_venta_anulada_sale_de_la_bandeja(ventas, red):
    ventas.alta(1, 100.0)
    puerto = _puerto(ventas, red)
    with pytest.raises(FacturaPendiente):
        asyncio.run(vf.facturar_venta(puerto, 1))
    ventas.ventas[1].update(estado="anulada", status="cancelled")
    red.en_linea = True
    hecho = asyncio.run(vf.facturar_pendientes_del_nodo(puerto))
    assert hecho["descartadas"][0]["venta_id"] == 1 and _facturas() == 0
    assert db_pendientes.get_por_origen(PRODUCTO, NODO, "venta", 1)["estado"] == "descartado"


def test_facturar_a_mano_una_venta_de_la_bandeja_la_cierra(ventas, red):
    ventas.alta(1, 100.0)
    puerto = _puerto(ventas, red)
    with pytest.raises(FacturaPendiente):
        asyncio.run(vf.facturar_venta(puerto, 1))
    red.en_linea = True
    r = _app(puerto).post("/api/ventas/1/facturar")
    assert r.status_code == 200, r.text
    assert db_pendientes.get_por_origen(PRODUCTO, NODO, "venta", 1)["estado"] == "facturado"


def test_dos_a_la_vez_no_emiten_dos_facturas(ventas, red):
    red.en_linea = True
    ventas.alta(1, 100.0)
    puerto = _puerto(ventas, red)
    nombre = vf._nombre_del_candado(puerto, 1)
    with core.get_connection() as conn, candados.candado_de_base(conn, nombre) as tomado:
        assert tomado
        with pytest.raises(vf.FacturacionEnCurso):
            asyncio.run(vf.facturar_venta(puerto, 1))
        r = _app(puerto).post("/api/ventas/1/facturar")
        assert r.status_code == 409 and "ya se está facturando" in r.json()["detail"]
    assert _facturas() == 0
    assert asyncio.run(vf.facturar_venta(puerto, 1))["id"]
    assert _facturas() == 1


def test_endpoints_del_nodo(ventas, red):
    ventas.alta(1, 100.0)
    puerto = _puerto(ventas, red)
    client = _app(puerto)
    client.post("/api/ventas/1/facturar")
    estado = client.get("/api/nodo/conexion").json()
    assert estado == {"es_nodo": True, "en_linea": False, "node_id": NODO, "a_facturar": 1}
    r = client.post("/api/nodo/facturar-pendientes")
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == "sin_conexion"
    red.en_linea = True
    r = client.post("/api/nodo/facturar-pendientes")
    assert r.status_code == 200 and r.json()["facturadas"][0]["venta_id"] == 1
    assert client.get("/api/nodo/conexion").json()["a_facturar"] == 0


def test_fuera_de_un_nodo_nada_cambia(ventas):
    ventas.alta(1, 100.0)
    puerto = ventas.puerto()
    app = FastAPI()
    app.include_router(build_facturacion_del_nodo_router(ventas=puerto, usuario_actual=lambda: USUARIO))
    client = TestClient(app)
    assert client.get("/api/nodo/conexion").json() == {
        "es_nodo": False, "en_linea": True, "node_id": None, "a_facturar": 0}
    assert client.post("/api/nodo/facturar-pendientes").json()["facturadas"] == []
    assert asyncio.run(vf.facturar_venta(puerto, 1))["cae"]
    assert _bandeja() == []


# ── Con conexión: ARCA, sin numeración local ─────────────────────────────────


ARCA_FICTICIA = {"id": 1, "cuit": "20000000001", "ambiente": "homologacion"}


def test_con_conexion_si_arca_no_da_el_numero_va_a_la_bandeja(ventas, red):
    red.en_linea = True
    ventas.alta(1, 100.0)

    async def numerar_sin_ticket(pv, tipo):
        return 1, None, ARCA_FICTICIA  # lo que da `get_next_numero_with_arca` cuando ARCA no contesta

    with pytest.raises(FacturaPendiente) as e:
        asyncio.run(vf.facturar_venta(_puerto(ventas, red, numerar_comprobante=numerar_sin_ticket), 1))
    assert "ARCA" in e.value.motivo and _facturas() == 0 and len(_bandeja()) == 1


def test_sin_arca_config_espejada_es_422(ventas, red):
    red.en_linea = True
    ventas.alta(1, 100.0)

    async def numerar_sin_arca(pv, tipo):
        return 1, None, None

    puerto = _puerto(ventas, red, numerar_comprobante=numerar_sin_arca)
    with pytest.raises(NodoSinArca):
        asyncio.run(vf.facturar_venta(puerto, 1))
    r = _app(puerto).post("/api/ventas/1/facturar")
    assert r.status_code == 422 and r.json()["detail"]["codigo"] == "nodo_sin_arca"
    assert _facturas() == 0 and _bandeja() == []


def test_numerador_con_ticket_prestado(ventas, red, monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    red.en_linea = True
    db_arca.crear_arca_config("Comercio Ficticio", "20000000001", 3, "", "", ambiente="homologacion")
    pedidos = []

    async def ultimo(pv, tipo, cuit, token, sign, ambiente):
        pedidos.append((pv, tipo, cuit, token, ambiente))
        return 41

    async def cae(factura, cuit, token, sign, ambiente):
        return {"cae": "70000000000001", "cae_vto": "20261030"}

    monkeypatch.setattr(arca_wsfe, "ultimo_numero_autorizado", ultimo)
    monkeypatch.setattr(arca_wsfe, "solicitar_cae", cae)

    async def ticket(arca):
        return {"token": "T", "sign": "S"}

    ventas.alta(1, 100.0)
    puerto = _puerto(ventas, red, numerar_comprobante=nodo_offline.numerador_con_ta(ticket))
    factura = asyncio.run(vf.facturar_venta(puerto, 1))
    assert factura["numero"] == 42 and factura["cae"] == "70000000000001"
    assert factura["ambiente"] == "homologacion"
    assert pedidos == [(3, 11, "20000000001", "T", "homologacion")]  # el PV de la arca_config

    async def central_caido(arca):
        raise ConnectionError("sin ruta")

    ventas.alta(2, 100.0)
    puerto = _puerto(ventas, red, numerar_comprobante=nodo_offline.numerador_con_ta(central_caido))
    with pytest.raises(FacturaPendiente):
        asyncio.run(vf.facturar_venta(puerto, 2))
    assert _facturas() == 1 and len(_bandeja()) == 1


# ── MercadoPago sin conexión ─────────────────────────────────────────────────


def _mp_que_no_se_llama():
    async def crear(**kw):
        raise AssertionError("no se tenía que llamar a MercadoPago")

    async def buscar(referencia, token):
        raise AssertionError("no se tenía que llamar a MercadoPago")

    return ClienteMercadoPago(crear_orden_qr=crear, buscar_pago_por_referencia=buscar)


def test_qr_bloqueado_sin_conexion(ventas, red, entorno):
    entorno["cm"].save({"empresa_iva_condition": "Monotributista", "mp_access_token": "TEST-ficticio",
                        "mp_user_id": "1", "mp_pos_id": "CAJA1"})
    ventas.alta(1, 100.0, pagos=[{"medio": "mercadopago", "monto": 100.0, "estado": "pendiente"}])
    client = _app(_puerto(ventas, red), mp=_mp_que_no_se_llama())
    r = client.post("/api/ventas/1/mp-qr")
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == "sin_conexion"
    assert "QR" in r.json()["detail"]["mensaje"]
    r = client.get("/api/ventas/1/mp-status")
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == "sin_conexion"
    # Un pago que ya estaba registrado se contesta con lo local, sin salir a la red.
    ventas.ventas[1]["mp_payment_id"] = "123"
    r = client.get("/api/ventas/1/mp-status")
    assert r.status_code == 200 and r.json()["status"] == "approved"


# ── La bandeja no factura ventas con el formulario ───────────────────────────


def test_la_bandeja_no_arma_el_formulario_de_una_venta(ventas, red):
    ventas.alta(1, 100.0)
    with pytest.raises(FacturaPendiente) as e:
        asyncio.run(vf.facturar_venta(_puerto(ventas, red), 1))
    app = FastAPI()
    app.include_router(build_comprobantes_bandeja_router())
    r = TestClient(app).post("/api/comprobantes-pendientes/facturar-prefill",
                             json={"ids": [e.value.comprobante_pendiente_id]})
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == "se_factura_desde_la_venta"

    ingesta = FastAPI()
    ingesta.include_router(build_comprobantes_ingesta_router())
    r = TestClient(ingesta).post("/api/comprobantes-pendientes", json={
        "origen_producto": "otro", "origen_tipo": "venta", "origen_id": "9", "cliente_razon": "X",
        "items": [{"description": "a", "qty": 1, "unit_price": 1, "iva_rate": 0}]})
    assert r.status_code == 422


# ── La factura del nodo, en el central ───────────────────────────────────────


def test_la_factura_del_nodo_llega_igual_al_central(ventas, red, entorno):
    red.en_linea = True
    ventas.alta(1, 1500.0)
    factura = asyncio.run(vf.facturar_venta(_puerto(ventas, red), 1))
    payload = json.loads(json.dumps(payload_factura(factura)))  # viaja como JSON

    # El central: otra base.
    bases, tmp = entorno["bases"], entorno["tmp"]
    _preparar(str(tmp / "central.db") if bases is None else bases.nueva())
    factura_id, creada = aplicar_factura_del_nodo(payload)
    assert creada
    en_central = db_facturas.get_factura(factura_id)
    for campo in ("tipo", "punto_venta", "numero", "cae", "total", "ambiente", "cliente_razon"):
        assert en_central[campo] == factura[campo], campo
    assert en_central["items"] == factura["items"] and en_central["usuario_id"] is None

    # Idempotente.
    assert aplicar_factura_del_nodo(payload) == (factura_id, False)
    assert _facturas() == 1
    # Otro total con el mismo número: conflicto, a revisión manual.
    with pytest.raises(FacturaEnConflicto):
        aplicar_factura_del_nodo({**payload, "total": 1.0})
    with pytest.raises(FacturaEnConflicto):
        aplicar_factura_del_nodo({**payload, "cae": "otro"})


def test_el_cae_que_llega_despues_completa_la_del_central(ventas, red, entorno):
    red.en_linea = True
    ventas.alta(1, 100.0)
    factura = asyncio.run(vf.facturar_venta(_puerto(ventas, red), 1))
    payload = payload_factura(factura)
    bases, tmp = entorno["bases"], entorno["tmp"]
    _preparar(str(tmp / "central.db") if bases is None else bases.nueva())
    factura_id, _ = aplicar_factura_del_nodo({**payload, "cae": "", "cae_vto": ""})
    assert not db_facturas.get_factura(factura_id)["cae"]
    aplicar_factura_del_nodo(payload)
    assert db_facturas.get_factura(factura_id)["cae"] == factura["cae"]


def test_aplicar_en_la_transaccion_del_receptor(ventas, red, entorno):
    red.en_linea = True
    ventas.alta(1, 100.0)
    payload = payload_factura(asyncio.run(vf.facturar_venta(_puerto(ventas, red), 1)))
    bases, tmp = entorno["bases"], entorno["tmp"]
    _preparar(str(tmp / "central.db") if bases is None else bases.nueva())
    conn = core.get_connection()
    try:
        aplicar_factura_del_nodo(payload, conn=conn)
        conn.rollback()
    finally:
        conn.close()
    assert _facturas() == 0


def test_ticket_de_una_venta_pendiente_dice_no_valido_como_factura():
    from test_ticket_generator import _texto_del_pdf

    from libracore.ticket_generator import generar_ticket_venta

    venta = {"id": 1, "fecha": "2026-10-19 10:00", "total": 100.0, "items": [
        {"nombre": "Agua", "cantidad": 1, "precio_unitario": 100.0}], "pagos": []}
    assert "NO V" not in _texto_del_pdf(generar_ticket_venta(venta))
    texto = _texto_del_pdf(generar_ticket_venta({**venta, "factura_pendiente": True}))
    assert "NO V" in texto and "FACTURA" in texto


# ── La fecha de la factura al volver (decisión del humano, 2026-10-19) ─────────


def test_fecha_de_la_factura_segun_lo_que_admite_arca():
    from datetime import date

    hoy = date(2026, 10, 19)
    f = vf.fecha_de_la_factura
    assert f("2026-10-17 13:05:00", hoy=hoy) == date(2026, 10, 17)  # dentro: la de la venta
    assert f("2026-10-14", hoy=hoy) == date(2026, 10, 14)  # borde: 5 días justos, productos
    assert f("2026-10-13", hoy=hoy) == hoy  # 6 días: ARCA no la admite, hoy
    assert f("2026-10-09", concepto=2, hoy=hoy) == date(2026, 10, 9)  # servicios: 10 días justos
    assert f("2026-10-08", concepto=3, hoy=hoy) == hoy
    assert f("2026-10-20", hoy=hoy) == hoy  # del futuro
    assert f("", hoy=hoy) == hoy and f(None, hoy=hoy) == hoy and f("20261017", hoy=hoy) == hoy
    # Nunca antes que el último comprobante de la serie (correlatividad de fechas en ARCA).
    assert f("2026-10-15", hoy=hoy, piso=date(2026, 10, 18)) == date(2026, 10, 18)
    assert f("2026-10-18", hoy=hoy, piso=date(2026, 10, 15)) == date(2026, 10, 18)


def _facturar_al_volver(ventas, red, fechas_de_venta):
    for vid, fecha in fechas_de_venta.items():
        ventas.alta(vid, 100.0, fecha=fecha)
    puerto = _puerto(ventas, red)
    for vid in fechas_de_venta:
        with pytest.raises(FacturaPendiente):
            asyncio.run(vf.facturar_venta(puerto, vid))
    red.en_linea = True
    asyncio.run(vf.facturar_pendientes_del_nodo(puerto))
    return {vid: db_facturas.get_factura(ventas.obtener(vid)["factura_id"])["fecha"] for vid in fechas_de_venta}


def test_al_volver_la_factura_lleva_la_fecha_de_la_venta_si_arca_la_admite(ventas, red):
    from libracore import fechas

    hoy = fechas.hoy()
    hace = {d: (hoy - timedelta(days=d)).isoformat() for d in (5, 6)}
    emitidas = _facturar_al_volver(ventas, red, {1: hace[5] + " 21:40:00", 2: hace[6]})
    assert emitidas[1] == hace[5]  # el borde: 5 días justos
    assert emitidas[2] == hoy.isoformat()  # pasó el plazo: la del día


def test_al_volver_no_queda_antes_que_una_factura_ya_emitida(ventas, red):
    from libracore import fechas

    hoy = fechas.hoy()
    ventas.alta(9, 50.0, fecha=hoy.isoformat())
    red.en_linea = True
    hoy_emitida = asyncio.run(vf.facturar_venta(_puerto(ventas, red), 9))  # una venta nueva, facturada en línea
    assert hoy_emitida["fecha"] == hoy.isoformat()
    red.en_linea = False
    emitidas = _facturar_al_volver(ventas, red, {1: (hoy - timedelta(days=2)).isoformat()})
    assert emitidas[1] == hoy.isoformat()


def test_en_linea_la_fecha_sigue_siendo_la_del_dia(ventas, red):
    from libracore import fechas

    red.en_linea = True
    ventas.alta(1, 100.0, fecha=(fechas.hoy() - timedelta(days=2)).isoformat())
    assert asyncio.run(vf.facturar_venta(_puerto(ventas, red), 1))["fecha"] == fechas.hoy().isoformat()


# ── El ticket prestado: corte o rechazo del central ──────────────────────────


@pytest.fixture
def con_arca(ventas, red, monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    red.en_linea = True
    db_arca.crear_arca_config("Comercio Ficticio", "20000000001", 3, "", "", ambiente="homologacion")

    async def ultimo(*args):
        return 7

    async def cae(*args):
        return {"cae": "70000000000002", "cae_vto": "20261030"}

    monkeypatch.setattr(arca_wsfe, "ultimo_numero_autorizado", ultimo)
    monkeypatch.setattr(arca_wsfe, "solicitar_cae", cae)


def _con_ticket(ventas, red, falla):
    async def obtener_ta(arca):
        raise falla

    return _puerto(ventas, red, numerar_comprobante=nodo_offline.numerador_con_ta(obtener_ta))


def _status(codigo):
    import httpx

    pedido = httpx.Request("POST", "https://central.invalid/sync/v1/arca/ta")
    return httpx.HTTPStatusError("x", request=pedido, response=httpx.Response(codigo, request=pedido))


class TicketRechazadoDeEjemplo(Exception):
    """Como el `TicketRechazado` de libraedge, marcado por el producto."""

    rechazo_del_central = True


@pytest.mark.parametrize("falla", [
    ConnectionError("sin ruta"), TimeoutError(), SinConexion("central caído"), _status(503),
], ids=["conexion", "tiempo", "sin_conexion", "5xx"])
def test_un_corte_del_central_manda_la_venta_a_la_bandeja(con_arca, ventas, red, falla):
    ventas.alta(1, 100.0)
    with pytest.raises(FacturaPendiente):
        asyncio.run(vf.facturar_venta(_con_ticket(ventas, red, falla), 1))
    assert len(_bandeja()) == 1 and _facturas() == 0


@pytest.mark.parametrize("falla", [
    nodo_offline.RechazoDelCentral("nodo revocado"), NodoSinArca("el central no tiene ARCA"),
    TicketRechazadoDeEjemplo("401"),
], ids=["rechazo", "nodo_sin_arca", "marcado"])
def test_un_rechazo_del_central_es_422_y_no_va_a_la_bandeja(con_arca, ventas, red, falla):
    ventas.alta(1, 100.0)
    puerto = _con_ticket(ventas, red, falla)
    with pytest.raises(NodoSinArca):
        asyncio.run(vf.facturar_venta(puerto, 1))
    r = _app(puerto).post("/api/ventas/1/facturar")
    assert r.status_code == 422 and r.json()["detail"]["codigo"] == "nodo_sin_arca"
    assert _bandeja() == [] and _facturas() == 0
    # Y la tanda al volver no la toma por un corte: la informa y corta.
    with pytest.raises(FacturaPendiente):
        asyncio.run(vf.facturar_venta(_puerto(ventas, Red(en_linea=False)), 1))
    hecho = asyncio.run(vf.facturar_pendientes_del_nodo(puerto))
    assert hecho["facturadas"] == [] and hecho["errores"][0]["venta_id"] == 1 and hecho["quedan"] == 1


def test_otro_error_del_ticket_no_se_disfraza_de_corte(con_arca, ventas, red):
    ventas.alta(1, 100.0)
    with pytest.raises(ValueError, match="mal armado"):
        asyncio.run(vf.facturar_venta(_con_ticket(ventas, red, ValueError("mal armado")), 1))
    with pytest.raises(type(_status(401))):
        asyncio.run(vf.facturar_venta(_con_ticket(ventas, red, _status(401)), 1))
    assert _bandeja() == [] and _facturas() == 0


# ── El hilo de la facturación al volver ──────────────────────────────────────


def _esperar(condicion, segundos=5.0):
    import time

    limite = time.monotonic() + segundos
    while not condicion() and time.monotonic() < limite:
        time.sleep(0.01)
    return condicion()


def test_el_hilo_reconcilia_al_arrancar_y_despues_de_facturar(ventas, red, monkeypatch):
    red.en_linea = True
    pasos = []

    async def facturar(puerto, **kw):
        pasos.append("facturar")
        return {}

    monkeypatch.setattr(vf, "facturar_pendientes_del_nodo", facturar)
    parar = vf.arrancar_facturacion_al_volver(_puerto(ventas, red), intervalo=0.02,
                                              en_cada_vuelta=lambda: pasos.append("reconciliar"))
    try:
        assert _esperar(lambda: pasos.count("reconciliar") >= 3)
    finally:
        parar.set()
    assert pasos[0] == "reconciliar"  # una vez al arrancar, antes de la primera espera
    # Después, en cada vuelta: primero facturar, después reconciliar.
    assert pasos[1:5] == ["facturar", "reconciliar", "facturar", "reconciliar"]


def test_sin_conexion_igual_reconcilia(ventas, red, monkeypatch):
    llamadas = []

    async def facturar(puerto, **kw):
        raise AssertionError("sin conexión no se factura")

    monkeypatch.setattr(vf, "facturar_pendientes_del_nodo", facturar)
    parar = vf.arrancar_facturacion_al_volver(_puerto(ventas, red), intervalo=0.02,
                                              en_cada_vuelta=lambda: llamadas.append(1))
    try:
        assert _esperar(lambda: len(llamadas) >= 3)
    finally:
        parar.set()


def test_un_error_no_mata_el_hilo(ventas, red, monkeypatch, caplog):
    red.en_linea = True
    vueltas = {"facturar": 0, "reconciliar": 0}

    async def facturar(puerto, **kw):
        vueltas["facturar"] += 1
        raise RuntimeError("token-secreto-que-no-va-al-log")

    def reconciliar():
        vueltas["reconciliar"] += 1
        raise RuntimeError("otro-dato-que-no-va-al-log")

    monkeypatch.setattr(vf, "facturar_pendientes_del_nodo", facturar)
    with caplog.at_level("ERROR", logger=vf.logger.name):
        parar = vf.arrancar_facturacion_al_volver(_puerto(ventas, red), intervalo=0.02, en_cada_vuelta=reconciliar)
        try:
            assert _esperar(lambda: vueltas["facturar"] >= 3 and vueltas["reconciliar"] >= 4)
        finally:
            parar.set()
    assert "RuntimeError" in caplog.text
    assert "token-secreto" not in caplog.text and "otro-dato" not in caplog.text


def test_fuera_de_un_nodo_no_arranca_ni_reconcilia(ventas):
    llamadas = []
    assert vf.arrancar_facturacion_al_volver(ventas.puerto(), en_cada_vuelta=lambda: llamadas.append(1)) is None
    assert llamadas == []
