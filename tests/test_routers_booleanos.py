"""`true`/`false` no son un número en los cuerpos de los routers de libracore (ADR-013).

Los campos `int`/`float` de pydantic convierten `true` en `1` y `false` en `0` antes de que el servicio los vea: `{"monto": true}` entraba como un pago de 1 peso y `{"caja_id": true}` como la caja 1.
`sin_booleanos` lo cierra en cada campo donde un 1 o un 0 cambian algo del negocio (plata, ids, cantidades, porcentajes).

Cada caso es un endpoint con un cuerpo válido y la lista de sus campos numéricos. Para cada campo, `true` y `false` dan **422 (con «<campo> tiene que ser un número, no un booleano») y no escriben nada**
(se compara el contenido de TODAS las tablas antes y después); el cuerpo numérico y el mismo cuerpo con cada número como texto (`"2"`) siguen funcionando (200). Es el mismo mecanismo que
`tests/test_web_booleanos.py` de libracommerce. Los campos que valen un `bool` de verdad (`activo`, `auto_facturar`) no se tocan.

Corre sobre SQLite: el 422 sale de pydantic antes de abrir ninguna conexión, así que el motor de base de datos no interviene; la comparación de tablas lo confirma.
"""

from __future__ import annotations

import copy
import itertools
import shutil
import sqlite3

import pytest
from conftest import _crear_schema
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore import (
    arca_router,
    caja_router,
    comprobantes_router,
    cuenta_corriente_router,
    egresos_router,
    mp_bandeja_router,
    presupuestos_router,
    remitos_router,
    tesoreria_router,
)
from libracore.db import caja as db_caja
from libracore.db import clients as db_clients
from libracore.db import core
from libracore.db import egresos as db_egresos
from libracore.db import tesoreria as db_tes

HOY = "2026-09-04"
USUARIO = {"id": 7, "username": "cajero", "role": "admin"}


# ═══════════════════════════════════════════════════ El mecanismo: un caso es un endpoint


def _instantanea() -> dict:
    """El contenido de TODAS las tablas, para afirmar que un 422 no escribió nada en ninguna."""
    with core.get_connection() as conn:
        if isinstance(conn, sqlite3.Connection):
            tablas = [f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        else:
            tablas = [f[0] for f in conn.execute("SELECT table_name FROM information_schema.tables "
                                                 "WHERE table_schema='public' AND table_type='BASE TABLE'").fetchall()]
        return {t: sorted(repr(tuple(f)) for f in conn.execute(f"SELECT * FROM {t}").fetchall()) for t in sorted(tablas)}


def _poner(cuerpo, ruta: tuple, valor):
    copia = copy.deepcopy(cuerpo)
    destino = copia
    for clave in ruta[:-1]:
        destino = destino[clave]
    destino[ruta[-1]] = valor
    return copia


def _nombre(ruta: tuple) -> str:
    """El nombre del campo en el mensaje: el último que es texto y no un índice de lista."""
    return [p for p in ruta if isinstance(p, str)][-1]


def _como_texto(cuerpo, ruta: tuple):
    """El mismo cuerpo con el número de `ruta` escrito como texto (`2` pasa a `"2"`): pydantic lo convierte igual que antes."""
    destino = cuerpo
    for clave in ruta[:-1]:
        destino = destino[clave]
    return _poner(cuerpo, ruta, str(destino[ruta[-1]]))


def _verificar(client, metodo: str, url, cuerpo_ok, campos: list[tuple], *, estado_ok: tuple = (200,)):
    """`cuerpo_ok(n)` arma el cuerpo válido del intento `n` (para lo que no se puede repetir con el mismo texto: nombres, fechas). `url` es un texto o una función de `n`."""
    dir_ = url if callable(url) else (lambda n: url)
    base = cuerpo_ok(0)
    antes = _instantanea()
    for campo in campos:
        for valor in (True, False):
            r = client.request(metodo, dir_(0), json=_poner(base, campo, valor))
            assert r.status_code == 422, (campo, valor, r.status_code, r.text)
            assert f"{_nombre(campo)} tiene que ser un número, no un booleano" in r.text, (campo, valor, r.text)
    assert _instantanea() == antes, "un 422 por booleano escribió algo"
    # El cuerpo numérico de siempre sigue funcionando...
    r = client.request(metodo, dir_(0), json=base)
    assert r.status_code in estado_ok, r.text
    # ...y, con los números como texto, cada campo por separado.
    intento = itertools.count(1)
    for campo in campos:
        n = next(intento)
        try:
            cuerpo = _como_texto(cuerpo_ok(n), campo)
        except (KeyError, TypeError):
            continue        # el campo no viene en el cuerpo válido (un opcional): sólo se prueba el rechazo
        r = client.request(metodo, dir_(n), json=cuerpo)
        assert r.status_code in estado_ok, (campo, r.status_code, r.text)


# ═══════════════════════════════════════════════════ El mundo de las pruebas


class Mundo:
    """La app armada como la arma un producto y los ids que los cuerpos necesitan."""


def _gate():
    return None


@pytest.fixture(scope="module")
def _base_armada(tmp_path_factory):
    """La base de una instancia REAL (`init_core_schema()` más las revisiones de Alembic: `cierres_diarios` viene de una), armada una sola vez por módulo y copiada para cada test."""
    ruta = tmp_path_factory.mktemp("plantilla") / "plantilla.db"
    core.configure(db_path=str(ruta))
    conn = core.get_connection()
    _crear_schema(conn)
    conn.execute("INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?,?,?,?,?)",
                 (USUARIO["id"], USUARIO["username"], "Cajero", "x", "admin"))
    conn.commit()
    conn.close()
    core._db_path = None
    return ruta


@pytest.fixture
def mundo(tmp_path, monkeypatch, _base_armada):
    destino = tmp_path / "booleanos.db"
    shutil.copyfile(_base_armada, destino)
    core.configure(db_path=str(destino))

    # La sincronización de MercadoPago no sale a la red ni lee el `config.json` de nadie: se prueba la validación, no a MercadoPago.
    async def _ingerir(config, *, dias, referencias_a_omitir=()):
        return []

    monkeypatch.setattr(mp_bandeja_router.mp_sync, "ingerir", _ingerir)
    monkeypatch.setattr(mp_bandeja_router.config_manager, "load", lambda: {})

    app = FastAPI()
    app.include_router(caja_router.build_caja_router(usuario_actual=lambda: USUARIO))
    app.include_router(caja_router.build_cajas_router())
    app.include_router(caja_router.build_turnos_router(usuario_actual=lambda: USUARIO))
    app.include_router(caja_router.build_cierre_diario_router(usuario_actual=lambda: USUARIO))
    app.include_router(cuenta_corriente_router.build_cuenta_corriente_router(usuario_actual=lambda: USUARIO, solo_admin=_gate))
    app.include_router(egresos_router.build_egresos_router(usuario_actual=lambda: USUARIO))
    app.include_router(tesoreria_router.build_tesoreria_router(usuario_actual=lambda: USUARIO))
    app.include_router(arca_router.build_arca_router())
    app.include_router(comprobantes_router.build_comprobantes_ingesta_router())
    app.include_router(comprobantes_router.build_comprobantes_bandeja_router())
    app.include_router(remitos_router.build_remitos_router(usuario_actual=lambda: USUARIO, generar_pdf=lambda r: f"/tmp/r-{r['id']}.pdf"))
    app.include_router(presupuestos_router.build_presupuestos_router(
        usuario_actual=lambda: USUARIO, generar_pdf=lambda p: f"/tmp/p-{p['id']}.pdf", convertir_a_remito=lambda p, v: None,
        smtp_configurado=lambda: True, enviar_comprobante=lambda **kw: None, moneda=lambda v: f"{v:.2f}"))
    app.include_router(mp_bandeja_router.build_mp_bandeja_router(permitir_siembra_de_demo=True))

    w = Mundo()
    w.client = TestClient(app)
    w.caja = db_caja.create_caja_config("Mostrador", "", ["efectivo"])
    w.cliente = db_clients.create_client("Municipalidad", cuit_dni="30-66513816-6")
    with core.get_connection() as c:
        # Una factura a cuenta corriente con CAE y un total grande, para que los pagos repetidos le sigan cabiendo.
        cur = c.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, items, subtotal, iva_amount, total, cae, condicion_venta)"
            " VALUES (11, 5, 1, ?, '30665138166', 'Municipalidad', '[]', 1000000, 0, 1000000, '123', 'Cuenta Corriente')", (HOY,))
        w.factura = cur.lastrowid
        c.commit()
    w.egreso = db_egresos.create_egreso(fecha=HOY, concepto="Hielo", total=121.0, monto_neto=100.0, iva_pct=0.21, iva_monto=21.0)
    w.proveedor = db_egresos.create_proveedor("Distribuidora SA")
    w.cuenta = db_tes.create_cuenta_tesoreria("Banco", "banco", saldo_inicial=0)
    w.cuenta2 = db_tes.create_cuenta_tesoreria("Caja fuerte", "efectivo", saldo_inicial=0)
    yield w
    core._db_path = None


# ═══════════════════════════════════════════════════ caja_router


def test_movimiento_de_caja(mundo):
    w = mundo
    ok = lambda n: {"fecha": HOY, "tipo": "ingreso", "concepto": f"Venta {n}", "monto": 100.0, "medio_pago": "efectivo",  # noqa: E731
                    "caja_id": w.caja, "factura_id": w.factura}
    _verificar(w.client, "POST", "/api/caja", ok, [("monto",), ("caja_id",), ("factura_id",)])


def test_alta_y_edicion_de_cajas(mundo):
    w = mundo
    campos = [("punto_venta",), ("sucursal_id",)]
    _verificar(w.client, "POST", "/api/cajas", lambda n: {"nombre": f"Caja {n}", "punto_venta": 10 + n, "sucursal_id": 3}, campos)
    # La edición hereda el modelo del alta: los mismos campos, y `activo` es un `bool` de verdad que sigue aceptando `true`/`false`.
    ok = lambda n: {"nombre": f"Mostrador {n}", "punto_venta": 50 + n, "sucursal_id": 3, "activo": True}  # noqa: E731
    _verificar(w.client, "PUT", f"/api/cajas/{w.caja}", ok, campos)
    assert w.client.put(f"/api/cajas/{w.caja}", json={**ok(0), "activo": False}).status_code == 200
    assert w.client.put(f"/api/cajas/{w.caja}", json={**ok(0), "activo": True}).status_code == 200


def test_un_booleano_no_queda_como_punto_de_venta_ni_como_sucursal(mundo):
    """El defecto de punta a punta: antes `punto_venta: true` dejaba la caja con el punto de venta 1 de ARCA."""
    w = mundo
    antes = w.client.get("/api/cajas").json()
    r = w.client.post("/api/cajas", json={"nombre": "Otra", "punto_venta": True})
    assert r.status_code == 422
    assert w.client.get("/api/cajas").json() == antes


def test_apertura_de_turno(mundo):
    w = mundo
    _verificar(w.client, "POST", "/api/turnos/abrir", lambda n: {"monto_inicial": 100.0, "caja_id": w.caja}, [("monto_inicial",), ("caja_id",)])


def test_cierre_de_turno(mundo):
    w = mundo

    def url(n):
        """Un turno abierto nuevo por intento: uno cerrado no se vuelve a cerrar."""
        with core.get_connection() as c:
            c.execute("UPDATE turnos_caja SET estado='cerrado' WHERE estado='abierto'")
            cur = c.execute("INSERT INTO turnos_caja (usuario_id, apertura, monto_inicial, caja_id) VALUES (?, ?, 100, ?)", (USUARIO["id"], HOY, w.caja))
            c.commit()
            return f"/api/turnos/{cur.lastrowid}/cerrar"

    # `url(0)` abre el turno del intento 0 una sola vez por `_verificar`, antes de los rechazos: los 422 no lo cierran.
    abierto = url(0)
    _verificar(w.client, "POST", lambda n: abierto if n == 0 else url(n), lambda n: {"monto_declarado": 100.0}, [("monto_declarado",)])


def test_cierre_diario(mundo):
    w = mundo
    ok = lambda n: {"sucursal_id": 1, "fecha": f"2026-01-{10 + n:02d}"}  # noqa: E731
    _verificar(w.client, "POST", "/api/cierre-diario/cerrar", ok, [("sucursal_id",)])


# ═══════════════════════════════════════════════════ cuenta_corriente_router, egresos_router, tesoreria_router


def test_pago_a_cuenta_corriente(mundo):
    w = mundo
    ok = lambda n: {"monto": 10.0, "fecha": HOY, "caja_id": w.caja, "facturas": [w.factura]}  # noqa: E731
    _verificar(w.client, "POST", f"/api/cuenta-corriente/{w.cliente}/pagar", ok, [("monto",), ("caja_id",), ("facturas", 0)])


def test_un_booleano_no_es_un_pago_de_un_peso(mundo):
    """El defecto de punta a punta: antes `{"monto": true}` registraba un pago de 1 peso y bajaba el saldo."""
    w = mundo
    saldo = w.client.get(f"/api/cuenta-corriente/{w.cliente}").json()["saldo"]
    r = w.client.post(f"/api/cuenta-corriente/{w.cliente}/pagar", json={"monto": True, "fecha": HOY})
    assert r.status_code == 422
    assert w.client.get(f"/api/cuenta-corriente/{w.cliente}").json()["saldo"] == saldo


def test_alta_de_egreso(mundo):
    w = mundo
    ok = lambda n: {"concepto": f"Hielo {n}", "proveedor_id": w.proveedor, "monto_neto": 100.0, "iva_pct": 0.21}  # noqa: E731
    _verificar(w.client, "POST", "/api/egresos", ok, [("proveedor_id",), ("monto_neto",), ("iva_pct",)])


def test_pago_de_un_egreso(mundo):
    w = mundo
    ok = lambda n: {"monto": 10.0, "caja_id": w.caja, "fecha": HOY, "medio_pago": "efectivo"}  # noqa: E731
    _verificar(w.client, "POST", f"/api/egresos/{w.egreso}/pagar", ok, [("monto",), ("caja_id",)])
    # El monto es opcional (sin él se paga el total): `null` sigue valiendo, `true` no.
    assert w.client.post(f"/api/egresos/{w.egreso}/pagar", json={"monto": None, "fecha": HOY}).status_code == 200


def test_cuentas_de_tesoreria(mundo):
    w = mundo
    _verificar(w.client, "POST", "/api/tesoreria/cuentas", lambda n: {"nombre": f"Cuenta {n}", "saldo_inicial": 500.0}, [("saldo_inicial",)])
    _verificar(w.client, "PUT", f"/api/tesoreria/cuentas/{w.cuenta}", lambda n: {"nombre": f"Banco {n}", "saldo_inicial": 500.0}, [("saldo_inicial",)])


def test_movimiento_de_tesoreria(mundo):
    w = mundo
    ok = lambda n: {"tipo": "ingreso", "monto": 25.0, "concepto": f"Depósito {n}", "fecha": HOY}  # noqa: E731
    _verificar(w.client, "POST", f"/api/tesoreria/cuentas/{w.cuenta}/movimiento", ok, [("monto",)])


def test_transferencia_entre_cuentas(mundo):
    w = mundo
    ok = lambda n: {"cuenta_origen_id": w.cuenta, "cuenta_destino_id": w.cuenta2, "monto": 25.0, "fecha": HOY}  # noqa: E731
    _verificar(w.client, "POST", "/api/tesoreria/transferencia", ok, [("cuenta_origen_id",), ("cuenta_destino_id",), ("monto",)])


# ═══════════════════════════════════════════════════ arca_router, comprobantes, remitos, presupuestos, bandeja de MercadoPago


def test_punto_de_venta_de_arca(mundo):
    w = mundo
    ok = lambda n: {"empresa": "default", "cuit": "20123456786", "punto_venta": 3 + n, "ambiente": "homologacion"}  # noqa: E731
    _verificar(w.client, "PUT", "/config/arca", ok, [("punto_venta",)])
    # `false` no es un punto de venta 0 que el `ge=1` rechace por su cuenta: es un booleano, y el mensaje lo dice.
    r = w.client.put("/config/arca", json=ok(0) | {"punto_venta": False})
    assert r.status_code == 422 and "no un booleano" in r.text


def _comprobante(w):
    """El cuerpo de un comprobante pendiente del intento `n` (`origen_id` distinto cada vez: es lo que hace idempotente el reenvío)."""
    return lambda n: {"origen_producto": "libradesk", "origen_tipo": "incidencia", "origen_id": f"O-{n}", "cliente_razon": "Cliente SA",
                      "cliente_id": w.cliente, "items": [{"description": "Servicio", "qty": 2, "unit_price": 100.0, "iva_rate": 0.21}]}


def test_ingesta_de_comprobantes_pendientes(mundo):
    w = mundo
    campos = [("cliente_id",), ("items", 0, "qty"), ("items", 0, "unit_price"), ("items", 0, "iva_rate")]
    _verificar(w.client, "POST", "/api/comprobantes-pendientes", _comprobante(w), campos, estado_ok=(201,))


def test_un_booleano_no_es_una_alicuota_del_cien_por_ciento(mundo):
    w = mundo
    r = w.client.post("/api/comprobantes-pendientes", json=_comprobante(w)(1) | {"items": [{"description": "S", "qty": 1, "unit_price": 10, "iva_rate": True}]})
    assert r.status_code == 422
    assert w.client.get("/api/comprobantes-pendientes").json()["total_pendientes"] == 0


def test_bandeja_de_comprobantes_pendientes(mundo):
    w = mundo
    ids = [w.client.post("/api/comprobantes-pendientes", json=_comprobante(w)(n)).json()["id"] for n in range(1, 4)]
    # `facturar-prefill` no escribe: el mismo cuerpo se repite sin problema.
    _verificar(w.client, "POST", "/api/comprobantes-pendientes/facturar-prefill", lambda n: {"ids": [ids[0]]}, [("ids", 0)])
    # `marcar-facturado` cierra los pendientes: se repite con otro id cada vez, y uno ya resuelto sigue dando 200 (lo informa aparte).
    _verificar(w.client, "POST", "/api/comprobantes-pendientes/marcar-facturado", lambda n: {"ids": [ids[1]], "factura_id": w.factura},
               [("ids", 0), ("factura_id",)])


def test_remito(mundo):
    w = mundo
    ok = lambda n: {"date": HOY, "client_id": w.cliente, "items": [{"description": "Prod A", "qty": 2}]}  # noqa: E731
    _verificar(w.client, "POST", "/api/remitos", ok, [("client_id",), ("items", 0, "qty")])


def test_presupuesto(mundo):
    w = mundo
    ok = lambda n: {"date": HOY, "client_id": w.cliente, "tax_rate": 0.21, "items": [{"description": "A", "qty": 2, "unit_price": 100.0}]}  # noqa: E731
    campos = [("client_id",), ("tax_rate",), ("items", 0, "qty"), ("items", 0, "unit_price")]
    _verificar(w.client, "POST", "/api/presupuestos", ok, campos)
    pid = w.client.get("/api/presupuestos").json()["items"][0]["id"]
    _verificar(w.client, "PUT", f"/api/presupuestos/{pid}", ok, campos)


def test_sincronizacion_de_la_bandeja_de_mercadopago(mundo):
    w = mundo
    _verificar(w.client, "POST", "/api/mp-bandeja/sincronizar", lambda n: {"dias": 7}, [("dias",)])


def test_siembra_de_la_bandeja_de_demo(mundo):
    w = mundo
    ok = lambda n: [{"mp_payment_id": f"demo-{n}", "monto": 1500.0, "payer_name": "Ana"}]  # noqa: E731
    _verificar(w.client, "POST", "/api/mp-bandeja/demo/sembrar", ok, [(0, "monto")])
