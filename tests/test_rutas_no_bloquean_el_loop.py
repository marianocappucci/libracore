"""Las rutas del motor que tocan algo sincrónico no frenan el loop de uvicorn.

🔴 Los productos corren uvicorn con **un solo proceso**. Una ruta `async def`
que llama sincrónico a la base —o a `openssl` por subproceso, o arma un PDF—
frena el loop entero mientras dura: ningún otro request avanza, `/health`
incluido. En un test común no se ve, porque la ruta contesta bien: lo que hace
mal es retener a los demás. Acá se mide eso y nada más.

Cómo: una llamada de cada ruta se reemplaza por una que duerme con
`time.sleep` —bloquea el hilo donde corre, como la consulta real— y, mientras
duerme, se pide `/health` por el **mismo loop**. Si la ruta corre fuera del
loop, `/health` termina antes de que la llamada lenta se despierte; si lo
bloquea, `/health` no puede ni empezar hasta entonces. Se compara contra el
instante en que la llamada lenta **se despertó**, no contra un umbral de
tiempo, así que el resultado no depende de lo rápida que sea la máquina.

Donde la ruta llama a una corrutina del motor que mezcla red con trabajo
sincrónico, lo lento va **adentro** de esa corrutina, como la firma del TRA:
pasar la ruta a `def` sin sacar la corrutina del loop de uvicorn dejaría el
test en rojo igual.

Es la misma medición que `tests/test_rutas_no_bloquean_el_loop.py` de
Contalibra y de Restolibra. Cada test se probó volviendo su ruta a como estaba
en `origin/develop`: se ponen rojos.
"""
import asyncio
import importlib
import threading
import time

import httpx
import pytest
from conftest import make_valid_cert_key
from fastapi import FastAPI

from libracore import config_manager
from libracore import pdf_generator as pdf_gen
from libracore import venta_facturacion as vf
from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db import mp as db_mp
from libracore.db.schema import init_core_schema

#: Lo que duerme la llamada reemplazada. Alcanza con que sea mucho más que lo
#: que tarda un `/health` sin carga.
LENTO = 0.5

USUARIO = {"id": 1, "username": "admin", "nombre": "Administrador", "role": "admin"}


class _Lento:
    """Una llamada sincrónica que tarda.

    `time.sleep` y no `asyncio.sleep` es el punto entero: una consulta a la
    base o `openssl` por subproceso no le ceden el control a nadie.
    """

    def __init__(self):
        self.entro = threading.Event()
        self.desperto_en: float | None = None

    def dormir(self):
        self.entro.set()
        time.sleep(LENTO)
        if self.desperto_en is None:
            self.desperto_en = time.monotonic()


def _app(*routers) -> FastAPI:
    """La app de juguete con los routers montados y un `/health` que no toca
    nada: si tarda, es porque el loop está tomado."""
    app = FastAPI()
    for router in routers:
        app.include_router(router)

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


def _mientras_duerme(app: FastAPI, lento: _Lento, pedir):
    """Corre `pedir(cliente)` y, con la llamada lenta ya adentro, un `/health`
    por el MISMO loop. Devuelve la respuesta del pedido, la de `/health` y el
    instante en que `/health` terminó."""

    async def _correr():
        transporte = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transporte, base_url="http://testserver") as quien_pide,
            httpx.AsyncClient(transport=transporte, base_url="http://testserver") as otro,
        ):
            tarea = asyncio.create_task(pedir(quien_pide))
            # La espera va a un hilo para no ocupar el loop con la espera misma.
            assert await asyncio.to_thread(lento.entro.wait, 10), (
                "la llamada lenta nunca empezó: el parche no intercepta la ruta")
            health = await otro.get("/health")
            health_termino = time.monotonic()
            respuesta = await asyncio.wait_for(tarea, 30)
        return respuesta, health, health_termino

    return asyncio.run(_correr())


def _no_bloqueo(lento: _Lento, health, health_termino: float):
    assert health.status_code == 200, health.text
    # Sin esto el test pasaría si el parche no interceptara nada: sin llamada
    # lenta, no hay nada que bloquee.
    assert lento.desperto_en is not None, "la parte lenta no llegó a correr"
    assert health_termino < lento.desperto_en, (
        f"/health terminó {health_termino - lento.desperto_en:.2f}s DESPUÉS de "
        "que se despertara la llamada lenta: la ruta bloqueó el loop mientras dormía"
    )


@pytest.fixture
def base(tmp_path, monkeypatch):
    """La instancia mínima: base con el schema, `config.json` y PDFs aislados."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    importlib.reload(config_manager)
    monkeypatch.setattr(pdf_gen, "FACTURAS_PDF_DIR", str(tmp_path / "pdf"))

    core.configure(db_path=str(tmp_path / "loop.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?,?,?,?,?)",
        (USUARIO["id"], USUARIO["username"], USUARIO["nombre"], "x", "admin"))
    conn.commit()
    conn.close()
    config_manager.save({"empresa_iva_condition": "Monotributista",
                         "mp_access_token": "APP_USR-test", "mp_pos_id": "CAJA1",
                         "mp_user_id": "123"})
    yield tmp_path
    core._db_path = None


# ── ventas_cobro_router: facturar, mp-qr y mp-status ─────────────────────


def _puerto(lento: _Lento | None = None, **extra):
    """Un `PuertoDeVentas` sobre una sola venta en memoria. `lento`, si viene,
    hace dormir a `obtener`: es lo que en un producto es la base."""
    venta = {
        "id": 1, "numero": "V-00001", "fecha": "2026-09-11", "total": 1500.0,
        "subtotal": 1500.0, "descuento": 0.0, "estado": "pendiente", "status": "draft",
        "items": [{"nombre": "Agua", "qty": 1, "precio": 1500.0, "subtotal": 1500.0}],
        "pagos": [{"medio": "mercadopago", "monto": 1500.0, "estado": "pendiente"}],
        "cliente_id": None, "usuario_id": USUARIO["id"], "factura_id": None,
        "mp_payment_id": "", "mp_order_id": "",
    }

    def obtener(vid):
        if lento is not None:
            lento.dormir()
        return dict(venta) if vid == 1 else None

    def _set(clave):
        def setter(vid, valor, *_):
            venta[clave] = valor
            return True
        return setter

    return vf.PuertoDeVentas(
        obtener=obtener, vincular_factura=_set("factura_id"),
        vincular_cobros=lambda numero, factura_id: 0, set_pago_mp=_set("mp_payment_id"),
        acreditar=_set("acreditada"), sellar_referencia_mp=_set("referencia"),
        set_orden_mp=_set("mp_order_id"), **extra)


def _cobro(puerto, pago=None):
    from libracore.ventas_cobro_router import ClienteMercadoPago, build_cobro_de_ventas_router

    async def crear_orden_qr(**kw):
        return {}

    async def buscar(referencia, token):
        return pago

    return _app(build_cobro_de_ventas_router(
        ventas=puerto, usuario_actual=lambda: USUARIO,
        mercadopago=ClienteMercadoPago(crear_orden_qr=crear_orden_qr,
                                       buscar_pago_por_referencia=buscar)))


def test_facturar_la_venta_no_frena_el_loop(base):
    """🔑 Lo lento va ADENTRO de `facturar_venta` —el numerador, que en
    producción autentica contra el WSAA firmando con `openssl`—. Es el caso que
    un `await` desde el loop no resuelve aunque la ruta fuera `def`."""
    lento = _Lento()

    async def numerar_con_openssl(punto_venta, tipo):
        lento.dormir()
        return 1, None, None

    app = _cobro(_puerto(numerar_comprobante=numerar_con_openssl))
    respuesta, health, fin = _mientras_duerme(app, lento, lambda c: c.post("/api/ventas/1/facturar"))
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["factura"]["numero"] == 1
    _no_bloqueo(lento, health, fin)


def test_poner_la_venta_en_el_qr_no_frena_el_loop(base):
    lento = _Lento()
    app = _cobro(_puerto(lento))
    respuesta, health, fin = _mientras_duerme(app, lento, lambda c: c.post("/api/ventas/1/mp-qr"))
    assert respuesta.status_code == 200, respuesta.text
    _no_bloqueo(lento, health, fin)


def test_el_poll_de_mp_status_no_frena_el_loop(base):
    """El que más pesa de los tres: la pantalla lo consulta cada pocos
    segundos mientras el cliente escanea."""
    lento = _Lento()
    app = _cobro(_puerto(lento), pago={"id": 77, "status": "approved"})
    respuesta, health, fin = _mientras_duerme(app, lento, lambda c: c.get("/api/ventas/1/mp-status"))
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["status"] == "approved"
    _no_bloqueo(lento, health, fin)


# ── mp_bandeja_router: sincronizar y los dos facturar ────────────────────


def _bandeja():
    import libracore.mp_bandeja_router as mbr
    return _app(mbr.build_mp_bandeja_router())


def test_sincronizar_la_bandeja_no_frena_el_loop(base, monkeypatch):
    """Lo lento va adentro de `mp_sync.ingerir`, que escribe la base entre
    medio de lo que le pide a MercadoPago."""
    from libracore import mp_sync
    lento = _Lento()

    async def ingerir(cfg, dias=7, referencias_a_omitir=()):
        lento.dormir()
        return []

    monkeypatch.setattr(mp_sync, "ingerir", ingerir)
    respuesta, health, fin = _mientras_duerme(
        _bandeja(), lento, lambda c: c.post("/api/mp-bandeja/sincronizar", json={"dias": 1}))
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {"nuevos": 0}
    _no_bloqueo(lento, health, fin)


def _factura_existente() -> int:
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, items, subtotal, "
            "iva_amount, total) VALUES (11, 1, 1, '2026-09-11', '[]', 100.0, 0.0, 100.0)")
    return db_facturas.get_facturas_filtradas("", "", "", "facturas", 10, 0)["items"][0]["id"]


@pytest.mark.parametrize("que", ["pagos", "movimientos"])
def test_facturar_desde_la_bandeja_no_frena_el_loop(base, monkeypatch, que):
    """Lo lento va adentro de `generar_factura_mp`: la numeración con ARCA, el
    PDF y el mail, todo sincrónico entre medio."""
    from libracore import mp_facturacion
    factura_id = _factura_existente()
    if que == "pagos":
        db_mp.create_mp_pago(mp_payment_id="p1", status="approved", monto=100.0,
                             payer_email="a@test", payer_name="A", estado_factura="pendiente")
        fila_id = db_mp.get_mp_pago("p1")["id"]
    else:
        db_mp.create_mp_movimiento(mp_movement_id="m1", tipo="bank_transfer", monto=100.0,
                                   fecha="2026-09-11", estado_factura="pendiente")
        fila_id = db_mp.get_mp_movimiento_by_mp_id("m1")["id"]
    lento = _Lento()

    async def generar(**kw):
        lento.dormir()
        return factura_id, 1, "Factura C", False

    monkeypatch.setattr(mp_facturacion, "generar_factura_mp", generar)
    respuesta, health, fin = _mientras_duerme(
        _bandeja(), lento,
        lambda c: c.post(f"/api/mp-bandeja/{que}/{fila_id}/facturar", json={"concepto": ""}))
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["factura_id"] == factura_id
    _no_bloqueo(lento, health, fin)


# ── config_router: subir el logo y restaurar un backup ───────────────────


def _config(tmp_path):
    import sqlite3

    import libracore.config_router as cr
    from libracore.respaldo import Instancia
    importlib.reload(cr)
    # Una instancia sin ninguna base no se puede armar; el restore real no
    # corre acá, así que alcanza con un archivo.
    producto = tmp_path / "producto.db"
    sqlite3.connect(str(producto)).close()
    instancia = Instancia(nombre="producto", bases=[producto], directorios=[])
    return cr, _app(cr.build_empresa_admin_router(),
                    cr.build_backup_router(instancia, tmp_path / "backups"))


def test_subir_el_logo_no_frena_el_loop(base, monkeypatch):
    cr, app = _config(base)
    lento = _Lento()
    real = cr.config_manager.save

    def guardar_lento(data, *a, **k):
        lento.dormir()
        return real(data, *a, **k)

    monkeypatch.setattr(cr.config_manager, "save", guardar_lento)
    respuesta, health, fin = _mientras_duerme(app, lento, lambda c: c.post(
        "/api/config/empresa/logo", files={"logo": ("logo.png", b"\x89PNG\r\n", "image/png")}))
    assert respuesta.status_code == 200, respuesta.text
    _no_bloqueo(lento, health, fin)


def test_restaurar_un_backup_no_frena_el_loop(base, monkeypatch):
    cr, app = _config(base)
    lento = _Lento()

    def restaurar_lento(instancia, contenido, backups_dir, **kw):
        lento.dormir()
        assert contenido == b"zip-de-prueba", "el archivo tiene que llegar entero"
        return {"ok": True}

    monkeypatch.setattr(cr, "restaurar_backup", restaurar_lento)
    respuesta, health, fin = _mientras_duerme(app, lento, lambda c: c.post(
        "/api/config/restore",
        files={"backup_file": ("b.zip", b"zip-de-prueba", "application/zip")}))
    assert respuesta.status_code == 200, respuesta.text
    _no_bloqueo(lento, health, fin)


# ── facturas_router: emitir, reintentar el CAE, las notas y el borrador ──


def _facturas():
    from libracore import facturas_router as fr

    def gate():
        return None

    return fr, _app(fr.build_comprobantes_router(usuario_actual=lambda: USUARIO, solo_admin=gate))


def _cuerpo_factura(**extra):
    cuerpo = {
        "tipo": 11, "punto_venta": 1, "fecha": "2026-09-11",
        "condicion_venta": "Contado", "client_name": "Juan Perez",
        "client_cuit": "20304050607", "client_iva": "Consumidor Final",
        "items": [{"description": "Alquiler de cancha", "qty": 1, "unit_price": 14000.0}],
    }
    cuerpo.update(extra)
    return cuerpo


def test_emitir_un_comprobante_no_frena_el_loop(base, monkeypatch):
    """🔑 Lo lento va adentro de la numeración con ARCA, que es corrutina."""
    fr, app = _facturas()
    lento = _Lento()
    real = fr.get_next_numero_with_arca

    async def numerar_con_openssl(punto_venta, tipo):
        lento.dormir()
        return await real(punto_venta, tipo)

    monkeypatch.setattr(fr, "get_next_numero_with_arca", numerar_con_openssl)
    respuesta, health, fin = _mientras_duerme(
        app, lento, lambda c: c.post("/api/facturas", json=_cuerpo_factura()))
    assert respuesta.status_code == 200, respuesta.text
    _no_bloqueo(lento, health, fin)


def _emitida(app) -> dict:
    """Una factura ya emitida, pedida por el loop normal (sin nada lento)."""

    async def _emitir():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as c:
            return await c.post("/api/facturas", json=_cuerpo_factura())

    r = asyncio.run(_emitir())
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("ruta", ["autorizar", "nota-credito", "nota-debito"])
def test_reintentar_el_cae_y_las_notas_no_frenan_el_loop(base, monkeypatch, ruta):
    """Lo lento es la lectura de la factura original, que es la base: una ruta
    `async` la haría en el loop."""
    fr, app = _facturas()
    factura = _emitida(app)
    lento = _Lento()
    real = db_facturas.get_factura

    def leer_lento(factura_id):
        lento.dormir()
        return real(factura_id)

    monkeypatch.setattr(db_facturas, "get_factura", leer_lento)
    respuesta, health, fin = _mientras_duerme(
        app, lento, lambda c: c.post(f"/api/facturas/{factura['id']}/{ruta}"))
    assert respuesta.status_code == 200, respuesta.text
    _no_bloqueo(lento, health, fin)


def test_el_borrador_en_pdf_no_frena_el_loop(base, monkeypatch):
    """Armar el PDF es CPU puro: en el loop, frena a todos mientras dura."""
    fr, app = _facturas()
    lento = _Lento()
    real = fr.pdf_gen.generate_pdf_factura

    def pdf_lento(*a, **k):
        lento.dormir()
        return real(*a, **k)

    monkeypatch.setattr(fr.pdf_gen, "generate_pdf_factura", pdf_lento)
    respuesta, health, fin = _mientras_duerme(
        app, lento, lambda c: c.post("/api/facturas/borrador-pdf", json=_cuerpo_factura()))
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.headers["content-type"] == "application/pdf"
    _no_bloqueo(lento, health, fin)


# ── arca_router: subir el par y probarlo contra ARCA ─────────────────────


def _arca():
    import libracore.arca_router as ar
    importlib.reload(ar)
    return ar, _app(ar.build_arca_router())


@pytest.mark.parametrize("mitad", ["certificado", "clave"])
def test_subir_el_par_de_arca_no_frena_el_loop(base, monkeypatch, mitad):
    ar, app = _arca()
    cert_path, key_path = make_valid_cert_key(base)
    contenido = open(cert_path if mitad == "certificado" else key_path, "rb").read()
    lento = _Lento()
    nombre = "leer_certificado" if mitad == "certificado" else "leer_clave"
    real = getattr(ar.arca_certificados, nombre)

    def leer_lento(datos):
        lento.dormir()
        return real(datos)

    monkeypatch.setattr(ar.arca_certificados, nombre, leer_lento)
    respuesta, health, fin = _mientras_duerme(app, lento, lambda c: c.post(
        f"/config/arca/{mitad}", files={"archivo": (f"x.{mitad}", contenido, "application/octet-stream")}))
    assert respuesta.status_code == 200, respuesta.text
    _no_bloqueo(lento, health, fin)


def test_probar_el_par_contra_arca_no_frena_el_loop(base, monkeypatch):
    """🔑 Lo lento va ADENTRO de `autenticar`, que es donde está la firma del
    TRA con `openssl` por subproceso."""
    ar, app = _arca()
    cert_path, key_path = make_valid_cert_key(base)

    async def _subir():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as c:
            for mitad, ruta in (("certificado", cert_path), ("clave", key_path)):
                r = await c.post(f"/config/arca/{mitad}",
                                 files={"archivo": ("x", open(ruta, "rb").read(), "application/octet-stream")})
                assert r.status_code == 200, r.text
            r = await c.put("/config/arca", json={"cuit": "20289933604", "ambiente": "homologacion"})
            assert r.status_code == 200, r.text

    asyncio.run(_subir())
    lento = _Lento()

    async def autenticar_con_openssl(*a, **k):
        lento.dormir()
        return {"token": "t", "sign": "s", "expiracion": ""}

    monkeypatch.setattr(ar.arca_wsaa, "autenticar", autenticar_con_openssl)
    respuesta, health, fin = _mientras_duerme(app, lento, lambda c: c.post("/config/arca/probar"))
    assert respuesta.status_code == 200, respuesta.text
    _no_bloqueo(lento, health, fin)


# ── mp_webhook: la notificación de MercadoPago ───────────────────────────


def test_el_webhook_de_mercadopago_no_frena_el_loop(base, monkeypatch):
    """🔑 Lo lento va adentro de la consulta del pago, que es corrutina: el
    webhook sigue siendo `async` por el cuerpo, y el resto tiene que salir del
    loop entero."""
    import libracore.mp_webhook as mw
    importlib.reload(mw)
    app = _app(mw.build_mp_webhook_router())
    lento = _Lento()

    async def obtener_pago(payment_id, access_token):
        lento.dormir()
        return {"status": "pending", "transaction_amount": 10.0, "payer": {}}

    monkeypatch.setattr(mw.mp_api, "obtener_pago", obtener_pago)
    respuesta, health, fin = _mientras_duerme(app, lento, lambda c: c.post(
        "/webhooks/mercadopago", json={"type": "payment", "data": {"id": "999"}}))
    assert respuesta.status_code == 200, respuesta.text
    assert db_mp.get_mp_pago("999") is not None, "el pago tenía que quedar registrado"
    _no_bloqueo(lento, health, fin)
