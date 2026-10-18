"""Lo que falta cobrar de una nota de débito se ve donde se ve lo de una factura (ADR-066).

El ADR-064 le dio tope de cobro a la nota de débito (`tipos.COBRABLES`), pero la pantalla siguió mirando sólo
facturas: el detalle de una nota decía `pendiente = 0` y no ofrecía «Cobrar», la columna `pendiente` del
listado valía 0, y la nota nunca figuraba en «Sin cobrar», el resumen ni el tablero. Acá se mide por HTTP, con
la nota emitida por `POST /api/facturas/{id}/nota-debito`, y en SQLite y PostgreSQL.

La cuenta de cada superficie contra la del tope, caso por caso, está en `test_pendiente_con_notas_de_credito.py`.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fechas import del_anio, hoy
from test_cobro_no_supera_lo_pendiente import ITEMS
from test_facturas_router import ADMIN, API, USUARIO, _emitir, _montar

from libracore import cobros
from libracore import tipos_comprobante as tipos
from libracore.cuenta_corriente_router import build_cuenta_corriente_router
from libracore.dashboard_router import build_dashboard_router
from libracore.db import core
from libracore.db import cuenta_corriente as db_cc
from libracore.db.clients import create_client
from libracore.db.dashboard import get_dashboard_data
from libracore.db.resumen import get_resumen_core

CUIT = "20304050607"


@pytest.fixture(params=["sqlite", "postgres"])
def client(request, tmp_path, monkeypatch):
    destino = None if request.param == "sqlite" else request.getfixturevalue("bases").nueva()
    c = _montar(tmp_path, monkeypatch, db_path=destino)
    create_client("Juan Perez", cuit_dni=CUIT)
    yield c
    core._db_path = None
    core._database_url = None


def _con_su_nota_de_debito(client, **extra) -> tuple[dict, dict]:
    """Una factura de 4.500 y su nota de débito (que copia el total), emitidas por el router."""
    factura = _emitir(client, items=ITEMS, **extra)
    r = client.post(f"{API}/{factura['id']}/nota-debito", headers=ADMIN)
    assert r.status_code == 200, r.text
    nota = r.json()
    assert nota["tipo"] in tipos.ND and nota["total"] == 4500.0
    return factura, nota


def _detalle(client, comprobante) -> dict:
    r = client.get(f"{API}/{comprobante['id']}")
    assert r.status_code == 200, r.text
    return r.json()


def _cobrar(client, comprobante, monto, medio="efectivo"):
    return client.post(
        f"{API}/{comprobante['id']}/cobrar", json={"pagos": [{"medio_id": medio, "monto": monto, "referencia": ""}]})


def _fila(client, vista, comprobante) -> dict | None:
    items = client.get(f"{API}?vista={vista}").json()["items"]
    return next((f for f in items if f["id"] == comprobante["id"]), None)


# ── El detalle: el «Cobrar» de la pantalla ────────────────────────────────


def test_el_detalle_de_una_nota_de_debito_sin_cobrar_muestra_lo_que_falta(client):
    _, nota = _con_su_nota_de_debito(client)

    d = _detalle(client, nota)

    assert (d["pendiente"], d["total_cobrado"], d["total_acreditado"], d["cobros"]) == (4500.0, 0.0, 0.0, [])


def test_el_detalle_sigue_lo_cobrado_de_la_nota_y_lo_que_ofrece_cobrar_entra(client):
    """El monto que la pantalla precarga es el `pendiente`: tiene que ser exactamente lo que el tope acepta."""
    _, nota = _con_su_nota_de_debito(client)

    assert _cobrar(client, nota, 1000.0).status_code == 200
    d = _detalle(client, nota)
    assert (d["pendiente"], d["total_cobrado"]) == (3500.0, 1000.0)
    assert [c["monto"] for c in d["cobros"]] == [1000.0]

    assert _cobrar(client, nota, d["pendiente"]).status_code == 200
    d = _detalle(client, nota)
    assert (d["pendiente"], d["total_cobrado"]) == (0.0, 4500.0)
    assert _cobrar(client, nota, 0.01).status_code == 409


def test_el_detalle_de_una_nota_de_credito_no_tiene_nada_que_cobrar(client):
    factura = _emitir(client, items=ITEMS)
    nc = client.post(f"{API}/{factura['id']}/nota-credito", headers=ADMIN, json={"importe": 1000}).json()
    assert nc["tipo"] in tipos.NC

    d = _detalle(client, nc)

    assert (d["pendiente"], d["total_cobrado"], d["cobros"]) == (0.0, 0.0, [])


def test_la_factura_sigue_igual_y_la_nota_de_debito_no_se_mezcla_con_ella(client):
    factura, nota = _con_su_nota_de_debito(client)
    assert _cobrar(client, nota, 4500.0).status_code == 200

    d = _detalle(client, factura)

    assert (d["pendiente"], d["total_cobrado"], d["total_acreditado"]) == (4500.0, 0.0, 0.0)
    assert [n["id"] for n in d["notas_debito"]] == [nota["id"]]


# ── El listado ────────────────────────────────────────────────────────────


def test_la_columna_pendiente_del_listado_de_notas_de_debito(client):
    _, nota = _con_su_nota_de_debito(client)
    assert _fila(client, "nd", nota)["pendiente"] == 4500.0

    assert _cobrar(client, nota, 4500.0).status_code == 200

    fila = _fila(client, "nd", nota)
    assert (fila["pendiente"], fila["total_cobrado"]) == (0.0, 4500.0)


def test_la_vista_sin_cobrar_incluye_la_nota_de_debito_hasta_que_se_cobra(client):
    factura, nota = _con_su_nota_de_debito(client)
    assert _cobrar(client, factura, 4500.0).status_code == 200

    sin_cobrar = client.get(f"{API}?vista=sin_cobrar").json()
    assert [f["id"] for f in sin_cobrar["items"]] == [nota["id"]] and sin_cobrar["total"] == 1
    assert sin_cobrar["items"][0]["pendiente"] == 4500.0

    assert _cobrar(client, nota, 4500.0).status_code == 200
    vacia = client.get(f"{API}?vista=sin_cobrar").json()
    assert (vacia["items"], vacia["total"]) == ([], 0)


def test_las_notas_de_credito_siguen_afuera_de_sin_cobrar(client):
    factura = _emitir(client, items=ITEMS)
    nc = client.post(f"{API}/{factura['id']}/nota-credito", headers=ADMIN, json={"importe": 1000}).json()

    ids = [f["id"] for f in client.get(f"{API}?vista=sin_cobrar").json()["items"]]

    assert ids == [factura["id"]] and nc["id"] not in ids
    assert _fila(client, "nc", nc)["pendiente"] == 0


# ── El resumen y el tablero ───────────────────────────────────────────────


def test_el_resumen_y_el_tablero_cuentan_la_nota_de_debito_sin_cobrar(client):
    """Se debe la factura (4.500) y la nota de débito (4.500): 9.000 en dos comprobantes, y «facturado» sigue
    siendo sólo el de las facturas."""
    factura, nota = _con_su_nota_de_debito(client)

    resumen = get_resumen_core(*del_anio())
    assert resumen["sin_cobrar"] == {"cantidad": 2, "monto": 9000.0}
    assert (resumen["facturado"], resumen["comprobantes"]) == (4500.0, 1)
    tablero = get_dashboard_data(*del_anio())
    assert {f["id"]: f["pendiente"] for f in tablero["facturas_sin_cobrar"]} == {factura["id"]: 4500.0, nota["id"]: 4500.0}
    assert (tablero["facturado_mes"], tablero["cant_facturas_mes"]) == (4500.0, 1)

    assert _cobrar(client, nota, 1500.0).status_code == 200

    assert get_resumen_core(*del_anio())["sin_cobrar"] == {"cantidad": 2, "monto": 7500.0}
    assert _cobrar(client, nota, 3000.0).status_code == 200
    assert get_resumen_core(*del_anio())["sin_cobrar"] == {"cantidad": 1, "monto": 4500.0}
    assert [f["id"] for f in get_dashboard_data(*del_anio())["facturas_sin_cobrar"]] == [factura["id"]]


def test_el_tablero_por_http_rotula_la_nota_de_debito_con_su_tipo(client):
    """La fila del tablero trae la letra y el número; con la nota en la lista, la letra sola («C») no dice si es
    la factura o su nota: `tipo_label` la nombra. La letra sale de la tabla única (la FCE y las notas la tienen)."""
    factura, nota = _con_su_nota_de_debito(client)
    app = FastAPI()
    app.include_router(build_dashboard_router(usuario_actual=lambda: USUARIO))

    filas = {f["id"]: f for f in TestClient(app).get("/api/dashboard").json()["facturas_sin_cobrar"]}

    assert (filas[factura["id"]]["tipo_label"], filas[factura["id"]]["letra"]) == ("Factura C", "C")
    assert (filas[nota["id"]]["tipo_label"], filas[nota["id"]]["letra"]) == ("Nota de Débito C", "C")
    assert filas[nota["id"]]["pendiente"] == 4500.0


def test_todo_lo_que_se_ve_coincide_con_lo_que_acepta_el_tope(client):
    """El detalle, la columna del listado, el resumen y el tablero dan lo que el tope deja cobrar."""
    _, nota = _con_su_nota_de_debito(client)
    assert _cobrar(client, nota, 1234.56).status_code == 200

    tope = cobros.estado_de_cobro(nota["id"])["pendiente"]

    assert tope == 3265.44
    assert _detalle(client, nota)["pendiente"] == tope
    assert _fila(client, "nd", nota)["pendiente"] == tope
    assert {f["id"]: f["pendiente"] for f in get_dashboard_data(*del_anio())["facturas_sin_cobrar"]}[nota["id"]] == tope
    assert _cobrar(client, nota, round(tope + 0.01, 2)).status_code == 409


# ── La cuenta corriente ───────────────────────────────────────────────────


def _cuenta():
    app = FastAPI()
    app.include_router(build_cuenta_corriente_router(usuario_actual=lambda: USUARIO, solo_admin=lambda: None))
    return TestClient(app)


def test_la_nota_de_debito_de_una_factura_a_credito_se_paga_desde_la_cuenta(client):
    """La regla del ADR-066 (la nota entra a lo pendiente de la cuenta si ella misma lleva `condicion_venta`), con lo
    que cambió el ADR-073: la nota de una factura a crédito **la lleva** y asentó su débito, así que entra a la lista y
    un pago aplicado la cubre. La cuenta queda en cero. El resto (Cobrar, sin CAE, anular, la nota de contado) está
    en `test_nota_de_debito_a_cuenta_corriente.py`."""
    factura, nota = _con_su_nota_de_debito(client, condicion_venta="Cuenta Corriente")
    assert nota["condicion_venta"] == "Cuenta Corriente"
    assert db_cc.get_cc_saldo(1) == 9000.0
    pendientes = db_cc.get_facturas_pendientes_cc(1)
    assert [(p["id"], p["pendiente"]) for p in pendientes] == [(factura["id"], 4500.0), (nota["id"], 4500.0)]
    assert pendientes[1]["concepto"].startswith("ND C ")

    r = _cuenta().post("/api/cuenta-corriente/1/pagar", json={
        "monto": 9000.0, "fecha": hoy(), "caja_id": 1, "medio_pago": "transferencia", "referencia": "T-1",
        "facturas": [factura["id"], nota["id"]]})

    assert r.status_code == 200, r.text
    assert r.json()["facturas_pendientes"] == [] and r.json()["saldo"] == 0.0
    assert cobros.estado_de_cobro(nota["id"])["cobrado"] == 4500.0
    assert _detalle(client, nota)["pendiente"] == 0.0
