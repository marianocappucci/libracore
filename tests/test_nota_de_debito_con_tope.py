"""La nota de débito tiene tope de cobro, como la factura (ADR-064).

Hasta acá `estado_de_cobro` decía `con_tope = tipo in FACTURAS` y `exigir_cobro_posible` salía sin mirar si no:
a una nota de débito se le podía cobrar cualquier monto, más de su total incluido. Pero una nota de débito
tiene su propio total a cobrar (el monto nuevo que agrega: intereses, un gasto, una diferencia), y cobrarle más
es el mismo defecto que el ADR-058 cerró para las facturas: plata que nadie debía, asentada en la caja.

`pendiente = total − cobrado − acreditado` con los mismos fragmentos SQL que la factura. Las notas de crédito
siguen sin tope: no se cobran, restan.

Van por HTTP (`POST /api/facturas/{id}/cobrar` sobre una nota emitida por `POST …/nota-debito`), en SQLite y en
PostgreSQL.
"""

from __future__ import annotations

import threading

import pytest
from test_cobro_no_supera_lo_pendiente import ITEMS, _escrito
from test_facturas_router import ADMIN, API, _emitir, _montar

from libracore import cobros
from libracore import tipos_comprobante as tipos
from libracore.db import core
from libracore.db import cuenta_corriente as db_cc
from libracore.db import facturas as db_facturas
from libracore.db.clients import create_client


@pytest.fixture(params=["sqlite", "postgres"])
def client(request, tmp_path, monkeypatch):
    destino = None if request.param == "sqlite" else request.getfixturevalue("bases").nueva()
    yield _montar(tmp_path, monkeypatch, db_path=destino)
    core._db_path = None
    core._database_url = None


def _nota_de_debito(client) -> dict:
    """Una factura de 4.500 y su nota de débito (que copia el total: 4.500), emitidas por el router."""
    factura = _emitir(client, items=ITEMS)
    r = client.post(f"{API}/{factura['id']}/nota-debito", headers=ADMIN)
    assert r.status_code == 200, r.text
    nota = r.json()
    assert nota["tipo"] in tipos.ND and nota["total"] == 4500.0
    return nota


def _cobrar(client, nota, monto, medio="efectivo"):
    return client.post(
        f"{API}/{nota['id']}/cobrar", json={"pagos": [{"medio_id": medio, "monto": monto, "referencia": ""}]})


# ── El defecto ────────────────────────────────────────────────────────────


def test_a_una_nota_de_debito_no_se_le_puede_cobrar_mas_que_su_total(client):
    nota = _nota_de_debito(client)

    r = _cobrar(client, nota, 9999.0)

    assert r.status_code == 409, r.text
    detalle = r.json()["detail"]
    assert "nota de débito" in detalle and "$ 4.500,00" in detalle and "$ 9.999,00" in detalle, detalle
    assert _escrito(nota) == {"movimientos": 0, "suma": 0.0, "abonos": 0}


def test_acepta_exactamente_su_total_y_despues_ya_no_admite_otro_cobro(client):
    nota = _nota_de_debito(client)

    assert _cobrar(client, nota, 4500.0).status_code == 200
    r = _cobrar(client, nota, 1.0)

    assert r.status_code == 409, r.text
    assert "Esta nota de débito ya está cobrada por completo" in r.json()["detail"], r.json()
    assert _escrito(nota)["suma"] == 4500.0


def test_dos_cobros_parciales_que_juntos_superan_rechazan_el_segundo(client):
    nota = _nota_de_debito(client)
    assert _cobrar(client, nota, 3000.0).status_code == 200

    r = _cobrar(client, nota, 1600.0)

    assert r.status_code == 409 and "$ 1.500,00" in r.json()["detail"], r.text
    assert _cobrar(client, nota, 1500.0).status_code == 200
    assert _escrito(nota)["suma"] == 4500.0


def test_cobrar_de_a_dos_medios_cuenta_la_suma(client):
    nota = _nota_de_debito(client)

    r = client.post(f"{API}/{nota['id']}/cobrar", json={"pagos": [
        {"medio_id": "efectivo", "monto": 3000.0}, {"medio_id": "transferencia", "monto": 2000.0}]})

    assert r.status_code == 409, r.text
    assert _escrito(nota)["movimientos"] == 0


def test_dos_cobros_simultaneos_a_la_misma_nota_solo_deja_pasar_uno(client):
    """Mismo candado que la factura: sin él los seis miraban 4.500 pendientes y los seis entraban."""
    nota = _nota_de_debito(client)
    estados = []
    salida = threading.Barrier(6)

    def pedir():
        salida.wait()
        estados.append(_cobrar(client, nota, 4000.0).status_code)

    hilos = [threading.Thread(target=pedir) for _ in range(6)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert sorted(estados) == [200, 409, 409, 409, 409, 409], estados
    assert _escrito(nota)["suma"] == 4000.0


# ── Lo que resta y lo que sigue sin tope ──────────────────────────────────


def test_una_nota_de_credito_sobre_la_nota_de_debito_resta_de_lo_que_falta(client):
    """`sql_acreditado` mira el comprobante asociado sea cual sea su tipo: una NC con CAE que apunta a la nota de
    débito baja lo que se le puede cobrar."""
    nota = _nota_de_debito(client)
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, items, subtotal,"
            " iva_amount, total, cae, cbte_asoc_tipo, cbte_asoc_pv, cbte_asoc_nro, ambiente, emisor_id)"
            " VALUES (13,?,900,?,?,?,'[]',1000,0,1000,'123',?,?,?,?,?)",
            (nota["punto_venta"], nota["fecha"], nota["cliente_cuit"], nota["cliente_razon"],
             nota["tipo"], nota["punto_venta"], nota["numero"], nota["ambiente"], nota.get("emisor_id")))

    estado = cobros.estado_de_cobro(nota["id"])
    assert (estado["acreditado"], estado["pendiente"]) == (1000.0, 3500.0)
    assert _cobrar(client, nota, 3500.01).status_code == 409
    assert _cobrar(client, nota, 3500.0).status_code == 200


def test_el_pendiente_de_una_nota_de_debito_es_el_de_las_consultas_sql(client):
    """La cuenta del tope y la de `sql_pendiente` (listados, resumen, tablero) tienen que dar lo mismo."""
    nota = _nota_de_debito(client)
    assert _cobrar(client, nota, 1200.0).status_code == 200

    cobrado, acreditado = db_facturas.get_cobrado_y_acreditado(nota["id"])
    with core.get_connection() as conn:
        sql = conn.execute(
            f"SELECT {db_facturas.sql_pendiente('f')} FROM facturas f WHERE f.id = ?", (nota["id"],)).fetchone()[0]
    estado = cobros.estado_de_cobro(nota["id"])

    assert (cobrado, acreditado) == (1200.0, 0.0)
    assert estado["pendiente"] == sql == 3300.0


def test_una_nota_de_credito_sigue_sin_tope_y_las_facturas_siguen_con_el_suyo(client):
    """Los controles de lo de arriba: un tope que rechazara todo pasaría los tests de rechazo."""
    factura = _emitir(client, items=ITEMS)
    nota_de_credito = client.post(f"{API}/{factura['id']}/nota-credito", headers=ADMIN).json()

    assert nota_de_credito["tipo"] in tipos.NC
    assert cobros.estado_de_cobro(nota_de_credito["id"])["con_tope"] is False
    cobros.exigir_cobro_posible(nota_de_credito["id"], 999999.0)          # no levanta
    assert cobros.estado_de_cobro(factura["id"])["con_tope"] is True
    assert cobros.estado_de_cobro(_nota_de_debito(client)["id"])["con_tope"] is True


# ── Lo que NO cambia: la cuenta corriente ─────────────────────────────────


def test_la_nota_de_debito_de_una_factura_al_contado_no_es_deuda_de_la_cuenta(client):
    """La nota de débito de una factura **al contado** no lleva `condicion_venta` ni escribe un débito en la cuenta:
    `get_facturas_pendientes_cc` no la lista. La de una factura a cuenta corriente sí (ADR-073,
    `test_nota_de_debito_a_cuenta_corriente.py`)."""
    create_client("Juan Perez", cuit_dni="20304050607")
    nota = _nota_de_debito(client)
    assert nota["condicion_venta"] != "Cuenta Corriente"

    assert db_cc.get_facturas_pendientes_cc(1) == []
