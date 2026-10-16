"""«Lo que falta cobrar de una factura» es UNA cuenta, y las notas de crédito restan (ADR-059).

`pendiente = total − cobrado − acreditado`, donde acreditado es lo que restan las notas de crédito **con
CAE y vigentes** de esa factura. Antes de la 1.172.0 las notas no restaban en ningún cálculo, salvo en
`get_facturas_pendientes_cc`, que sacaba la factura entera con cualquier nota, incluso una parcial.

Cada caso de la tabla se mide contra **cada** cálculo del motor que dice «cuánto falta» —el tope del cobro,
la vista `sin_cobrar`, el resumen, el tablero, la cuenta corriente, y la versión en Python de las notas— y
todos tienen que dar lo mismo. En SQLite y en PostgreSQL: los fragmentos de SQL son los mismos, pero el
motor que los corre no.
"""

from __future__ import annotations

import datetime

import pytest

from libracore import cobros, notas_de_credito
from libracore.db import core
from libracore.db import cuenta_corriente as db_cc
from libracore.db import facturas as db_facturas
from libracore.db.clients import create_client
from libracore.db.dashboard import get_dashboard_data
from libracore.db.resumen import get_resumen_core
from libracore.db.schema import init_core_schema

HOY = datetime.date.today().isoformat()
PERIODO = (HOY[:8] + "01", HOY)
CUIT = "20304050607"

# (nombre, total, cobros, notas, pendiente esperado)
#   cobro: (monto, medio, anulado)      nota: (total, cae, anulada)
#
# Los dos CAE que no cuentan son los dos que hay en la base: vacío (un borrador) y `PENDIENTE` (se pidió y
# no volvió).
CASOS = [
    ("sin_notas_ni_cobros", 1000.0, [], [], 1000.0),
    ("nc_parcial", 1000.0, [], [(400.0, "123", False)], 600.0),
    ("nc_total", 1000.0, [], [(1000.0, "123", False)], 0.0),
    ("dos_nc_parciales_suman", 1000.0, [], [(400.0, "123", False), (250.0, "124", False)], 350.0),
    ("nc_sin_cae_no_resta", 1000.0, [], [(1000.0, "", False)], 1000.0),
    ("nc_pendiente_de_cae_no_resta", 1000.0, [], [(1000.0, "PENDIENTE", False)], 1000.0),
    ("nc_anulada_no_resta", 1000.0, [], [(1000.0, "123", True)], 1000.0),
    ("cobro_y_nc_que_completan", 1000.0, [(600.0, "efectivo", 0)], [(400.0, "123", False)], 0.0),
    ("cobro_y_nc_que_no_completan", 1000.0, [(300.0, "efectivo", 0)], [(400.0, "123", False)], 300.0),
    ("cobrada_y_despues_acreditada_no_da_negativo", 1000.0, [(1000.0, "efectivo", 0)],
     [(1000.0, "123", False)], 0.0),
    ("cobro_anulado_no_cuenta", 1000.0, [(1000.0, "efectivo", 1)], [], 1000.0),
    ("la_marca_de_cuenta_corriente_no_es_cobro", 1000.0, [(1000.0, "Cuenta Corriente", 0)], [], 1000.0),
    ("cobro_parcial_sin_notas", 1000.0, [(400.0, "efectivo", 0)], [], 600.0),
]


@pytest.fixture(params=["sqlite", "postgres"])
def motor(request, tmp_path):
    """El destino de la base, en el motor que toca: una ruta SQLite o la URL de una base PostgreSQL propia."""
    return str(tmp_path / "pendiente.db") if request.param == "sqlite" else request.getfixturevalue("bases").nueva()


@pytest.fixture
def base(motor):
    """Una base limpia con un usuario y un cliente (para los tests que escriben las filas a mano)."""
    core.configure(db_path=motor)
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.execute("INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (1,'a','A','x','admin')")
    create_client("Juan Perez", cuit_dni=CUIT)
    yield motor
    core._db_path = None
    core._database_url = None


def _factura(numero=1, total=1000.0, *, cond="Contado", tipo=11, cae="123", emisor_id=None, ambiente="produccion",
             asoc=(0, 0, 0), anulada=False) -> int:
    with core.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, items, subtotal,"
            " iva_amount, total, cae, condicion_venta, emisor_id, ambiente, cbte_asoc_tipo, cbte_asoc_pv,"
            " cbte_asoc_nro, anulada_en) VALUES (?,5,?,?,?,?,'[]',?,0,?,?,?,?,?,?,?,?,?)",
            (tipo, numero, HOY, CUIT, "Juan Perez", total, total, cae, cond, emisor_id, ambiente, *asoc,
             "2026-10-16 10:00:00" if anulada else None),
        )
        return cur.lastrowid


def _nota_de(fid, total, cae, anulada=False, *, numero=900, emisor_id=None, ambiente="produccion"):
    original = db_facturas.get_factura(fid)
    return _factura(numero, total, tipo=13, cae=cae, anulada=anulada, emisor_id=emisor_id, ambiente=ambiente,
                    asoc=(original["tipo"], original["punto_venta"], original["numero"]))


def _cobro(fid, monto, medio="efectivo", anulado=0):
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, factura_id, medio_pago, anulado)"
            " VALUES (?, 'ingreso', 'Cobro', ?, ?, ?, ?)", (HOY, monto, fid, medio, anulado))


def _armar(total, cobros_, notas, **opciones) -> int:
    fid = _factura(1, total, **opciones)
    for i, (monto, medio, anulado) in enumerate(cobros_):
        _cobro(fid, monto, medio, anulado)
    for i, (n_total, cae, anulada) in enumerate(notas):
        _nota_de(fid, n_total, cae, anulada, numero=900 + i)
    return fid


def _ids(items):
    return {f["id"] for f in items}


# ── Cada cálculo, con cada caso ───────────────────────────────────────────


@pytest.mark.parametrize("nombre,total,cobros_,notas,esperado", CASOS, ids=[c[0] for c in CASOS])
def test_todos_los_calculos_dicen_lo_mismo(base, nombre, total, cobros_, notas, esperado):
    fid = _armar(total, cobros_, notas)
    debe = esperado > 0

    # 1. El tope del cobro: lo que se puede cobrar es exactamente lo pendiente.
    estado = cobros.estado_de_cobro(fid)
    assert estado["pendiente"] == esperado
    if debe:
        cobros.exigir_cobro_posible(fid, esperado)
        with pytest.raises(cobros.CobroNoPermitido) as exc:
            cobros.exigir_cobro_posible(fid, esperado + 0.01)
        assert exc.value.codigo == cobros.CobroNoPermitido.EXCEDE
    else:
        with pytest.raises(cobros.CobroNoPermitido) as exc:
            cobros.exigir_cobro_posible(fid, 0.01)
        assert exc.value.codigo == cobros.CobroNoPermitido.SIN_SALDO

    # 2. La vista «sin cobrar» del listado, y las columnas de cada fila.
    sin_cobrar = db_facturas.get_facturas_filtradas(vista="sin_cobrar")["items"]
    assert (fid in _ids(sin_cobrar)) is debe
    fila = next(f for f in db_facturas.get_facturas_filtradas(vista="facturas")["items"] if f["id"] == fid)
    assert fila["pendiente"] == esperado
    assert fila["total_acreditado"] == pytest.approx(estado["acreditado"])
    assert fila["total_cobrado"] == pytest.approx(estado["cobrado"])

    # 3. El resumen del panel: la cantidad y el monto de lo que falta (no el total de la factura).
    sc = get_resumen_core(*PERIODO)["sin_cobrar"]
    assert sc == {"cantidad": 1 if debe else 0, "monto": esperado}

    # 4. El tablero, con y sin `sin_fiado`: la marca de cuenta corriente sólo cuenta como cobro sin él (el
    #    criterio de siempre de los productos que no separan fiar de cobrar).
    for sin_fiado in (True, False):
        fiar_es_cobrar = not sin_fiado and nombre == "la_marca_de_cuenta_corriente_no_es_cobro"
        filas = get_dashboard_data(*PERIODO, sin_fiado=sin_fiado)["facturas_sin_cobrar"]
        assert (fid in _ids(filas)) is (debe and not fiar_es_cobrar)
        if debe and not fiar_es_cobrar:
            assert filas[0]["pendiente"] == esperado and filas[0]["total"] == total

    # 5. La versión en Python de las notas (`saldo_acreditable`) acredita lo mismo que el SQL.
    original = db_facturas.get_factura(fid)
    previas = db_facturas.get_nc_de_factura(original["tipo"], original["punto_venta"], original["numero"],
                                            emisor_id=original.get("emisor_id"), ambiente=original["ambiente"])
    assert float(notas_de_credito.acreditado(original, previas)) == pytest.approx(estado["acreditado"])


SIN_LA_MARCA = [c for c in CASOS if c[0] != "la_marca_de_cuenta_corriente_no_es_cobro"]


@pytest.mark.parametrize("nombre,total,cobros_,notas,esperado", SIN_LA_MARCA, ids=[c[0] for c in SIN_LA_MARCA])
def test_la_cuenta_corriente_dice_lo_mismo_para_una_factura_a_credito(base, nombre, total, cobros_, notas, esperado):
    """`get_facturas_pendientes_cc`: la misma cuenta, sobre una factura a crédito. (La marca de cuenta
    corriente de la emisión es la deuda, no un cobro: acá no se carga, como en la emisión real del test
    de abajo.)"""
    fid = _armar(total, cobros_, notas, cond="Cuenta Corriente")

    pendientes = db_cc.get_facturas_pendientes_cc(1)

    assert [p["id"] for p in pendientes] == ([fid] if esperado > 0 else [])
    if esperado > 0:
        acreditado = sum(n[0] for n in notas if n[1] not in ("", "PENDIENTE") and not n[2])
        assert (pendientes[0]["pendiente"], pendientes[0]["acreditado"]) == (esperado, acreditado)


def test_una_nota_de_otro_emisor_o_de_otro_ambiente_no_resta(base):
    """`(tipo, pv, número)` solo no identifica a la factura (ADR-021): la FC 5-1 de otro emisor, o la de
    homologación, no es ésta."""
    fid = _factura(1, 1000.0)
    original = db_facturas.get_factura(fid)
    asoc = (original["tipo"], original["punto_venta"], original["numero"])
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO arca_config (id, empresa, cuit, punto_venta, clave_path, certificado_path, activo)"
            " VALUES (7, 'Otra', '30111111118', 5, 'k', 'c', 1)")
    _factura(901, 1000.0, tipo=13, emisor_id=7, asoc=asoc)
    _factura(902, 1000.0, tipo=13, ambiente="homologacion", asoc=asoc)

    assert cobros.estado_de_cobro(fid)["pendiente"] == 1000.0
    assert _ids(db_facturas.get_facturas_filtradas(vista="sin_cobrar")["items"]) >= {fid}


def test_una_nota_de_debito_no_resta(base):
    fid = _factura(1, 1000.0)
    original = db_facturas.get_factura(fid)
    _factura(903, 300.0, tipo=12, asoc=(original["tipo"], original["punto_venta"], original["numero"]))

    assert cobros.estado_de_cobro(fid)["acreditado"] == 0.0
    assert cobros.estado_de_cobro(fid)["pendiente"] == 1000.0


def test_las_notas_y_las_anuladas_no_figuran_como_sin_cobrar(base):
    """Sólo las facturas vigentes tienen «pendiente»: la nota de crédito en sí no es una deuda."""
    fid = _factura(1, 1000.0)
    _nota_de(fid, 400.0, "123")
    anulada = _factura(2, 500.0, cae="", anulada=True)  # sólo se anula lo que no tiene CAE (ADR-022)

    assert _ids(db_facturas.get_facturas_filtradas(vista="sin_cobrar")["items"]) == {fid}
    assert get_resumen_core(*PERIODO)["sin_cobrar"] == {"cantidad": 1, "monto": 600.0}
    assert anulada not in _ids(get_dashboard_data(*PERIODO)["facturas_sin_cobrar"])
    filas = db_facturas.get_facturas_filtradas(vista="nc")["items"]
    assert [f["pendiente"] for f in filas] == [0], "una nota no tiene «lo que falta cobrar»"


# ── Las notas, en la pantalla y en el tope (por HTTP) ─────────────────────


def test_el_tope_rechaza_cobrar_una_factura_acreditada_por_completo_y_ofrece_lo_que_queda(motor, tmp_path, monkeypatch):
    """De punta a punta con el router: la nota de crédito la emite el motor (CAE simulado), el detalle muestra lo
    que queda y el cobro de ESO entra; un centavo más no."""
    from test_facturas_router import ADMIN, API, _emitir

    client = _montar_en(tmp_path, monkeypatch, motor)
    items = [{"description": "Servicio", "qty": 1, "unit_price": 1000.0}]

    parcial = _emitir(client, items=items)
    assert client.post(f"{API}/{parcial['id']}/nota-credito", headers=ADMIN, json={"importe": 400}).status_code == 200
    detalle = client.get(f"{API}/{parcial['id']}").json()
    assert (detalle["total_acreditado"], detalle["pendiente"]) == (400.0, 600.0)

    demas = client.post(f"{API}/{parcial['id']}/cobrar", json={"pagos": [{"medio_id": "efectivo", "monto": 600.01}]})
    assert demas.status_code == 409 and "notas de crédito acreditan $ 400,00" in demas.json()["detail"], demas.text
    justo = client.post(f"{API}/{parcial['id']}/cobrar", json={"pagos": [{"medio_id": "efectivo", "monto": 600.0}]})
    assert justo.status_code == 200 and justo.json()["pendiente"] == 0.0

    total = _emitir(client, items=items)
    assert client.post(f"{API}/{total['id']}/nota-credito", headers=ADMIN).status_code == 200
    r = client.post(f"{API}/{total['id']}/cobrar", json={"pagos": [{"medio_id": "efectivo", "monto": 1.0}]})
    assert r.status_code == 409 and "acreditada por completo" in r.json()["detail"], r.text
    assert cobros.estado_de_cobro(total["id"])["cobrado"] == 0.0

    # El listado: la parcial cobrada con su nota y la total acreditada ya no figuran sin cobrar.
    assert client.get(f"{API}?vista=sin_cobrar").json()["total"] == 0


def _montar_en(tmp_path, monkeypatch, destino):
    """El router de comprobantes sobre la base de este test, con el cliente de la factura en el padrón."""
    from test_facturas_router import _montar

    client = _montar(tmp_path, monkeypatch, db_path=destino)
    create_client("Juan Perez", cuit_dni=CUIT)
    return client


# ── La cuenta corriente del cliente: la nota no se descuenta dos veces ────


def _app_cc():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from test_facturas_router import USUARIO

    from libracore.cuenta_corriente_router import build_cuenta_corriente_router

    app = FastAPI()
    app.include_router(build_cuenta_corriente_router(usuario_actual=lambda: USUARIO, solo_admin=lambda: None))
    return TestClient(app)


def test_la_nota_ya_descuenta_el_saldo_y_el_pendiente_por_factura_no_la_descuenta_otra_vez(motor, tmp_path, monkeypatch):
    """Medido: la nota de una factura a crédito ya abona la cuenta del cliente (`cc_pagos`, de donde sale el
    saldo del libro `cc_asientos`). El `pendiente` por factura NO escribe ni lee el libro: sólo dice cuánto de
    CADA factura queda por cobrar. Si además se descontara de ahí, el saldo quedaría a favor por la nota."""
    from test_facturas_router import ADMIN, API, _emitir

    client = _montar_en(tmp_path, monkeypatch, motor)
    cuenta = _app_cc()
    cliente = 1
    items = [{"description": "Servicio", "qty": 1, "unit_price": 1000.0}]
    f = _emitir(client, items=items, condicion_venta="Cuenta Corriente")
    assert db_cc.get_cc_saldo(cliente) == 1000.0

    assert client.post(f"{API}/{f['id']}/nota-credito", headers=ADMIN, json={"importe": 400}).status_code == 200
    assert db_cc.get_cc_saldo(cliente) == 600.0, "la nota abona el libro una vez"
    pendientes = cuenta.get(f"/api/cuenta-corriente/{cliente}").json()["facturas_pendientes"]
    assert [(p["id"], p["pendiente"]) for p in pendientes] == [(f["id"], 600.0)], "la parcial deja lo que se debe"

    # Un pago a cuenta de 1.000 aplicado a la factura: sólo se aplican los 600 que se deben.
    r = cuenta.post(f"/api/cuenta-corriente/{cliente}/pagar", json={
        "monto": 1000.0, "fecha": HOY, "caja_id": 1, "medio_pago": "transferencia", "referencia": "T-1",
        "facturas": [f["id"]]})
    assert r.status_code == 200, r.text
    assert cobros.estado_de_cobro(f["id"])["cobrado"] == 600.0
    assert r.json()["facturas_pendientes"] == []
    # El saldo: 1.000 de la factura − 400 de la nota − 1.000 del pago = 400 a favor (el pago fue de más). Sin la
    # nota sería 0 y el pago se habría aplicado entero a la factura.
    assert r.json()["saldo"] == -400.0


def test_cobrar_el_resto_de_una_factura_a_credito_con_nota_deja_la_cuenta_en_cero(motor, tmp_path, monkeypatch):
    from test_facturas_router import ADMIN, API, _emitir

    client = _montar_en(tmp_path, monkeypatch, motor)
    f = _emitir(client, items=[{"description": "S", "qty": 1, "unit_price": 1000.0}],
                condicion_venta="Cuenta Corriente")
    client.post(f"{API}/{f['id']}/nota-credito", headers=ADMIN, json={"importe": 400})

    r = client.post(f"{API}/{f['id']}/cobrar", json={"pagos": [{"medio_id": "efectivo", "monto": 600.0}]})

    assert r.status_code == 200, r.text
    assert (r.json()["pendiente"], r.json()["total_acreditado"], r.json()["total_cobrado"]) == (0.0, 400.0, 600.0)
    assert db_cc.get_cc_saldo(1) == 0.0, "1.000 de la factura − 400 de la nota − 600 del cobro"


def test_cobrada_y_despues_acreditada_deja_la_cuenta_a_favor_como_siempre(motor, tmp_path, monkeypatch):
    """No cambió y lo dejamos dicho: la nota de una factura a crédito ya cobrada abona de nuevo la cuenta
    (cobro 1.000 + nota 1.000 sobre una factura de 1.000 → −1.000 a favor). La factura queda en 0, sin
    «saldo a favor» propio; lo a favor vive en la cuenta del cliente. Qué hacer con esa plata (devolverla,
    dejarla a favor) es una decisión de producto, y es lo que había antes de este cambio."""
    from test_facturas_router import ADMIN, API, _emitir

    client = _montar_en(tmp_path, monkeypatch, motor)
    f = _emitir(client, items=[{"description": "S", "qty": 1, "unit_price": 1000.0}],
                condicion_venta="Cuenta Corriente")
    client.post(f"{API}/{f['id']}/cobrar", json={"pagos": [{"medio_id": "efectivo", "monto": 1000.0}]})

    client.post(f"{API}/{f['id']}/nota-credito", headers=ADMIN)

    assert db_cc.get_cc_saldo(1) == -1000.0
    detalle = client.get(f"{API}/{f['id']}").json()
    assert (detalle["pendiente"], detalle["total_cobrado"], detalle["total_acreditado"]) == (0.0, 1000.0, 1000.0)
