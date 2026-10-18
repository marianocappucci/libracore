"""La nota de débito de una factura a cuenta corriente hereda la cuenta (ADR-073).

Decisión del humano (2026-10-18), lo que el ADR-066 dejó abierto: la nota lleva `condicion_venta = 'Cuenta
Corriente'`, su emisión **asienta su débito en la cuenta del cliente** (la marca de `POST /api/facturas`, que el libro
`cc_asientos` lleva al saldo) en la misma transacción que la nota, y se paga desde la cuenta con un recibo (ADR-063),
igual que la factura. La nota de una factura al contado no cambia: se cobra por caja.

Todo por HTTP, con la nota emitida por `POST /api/facturas/{id}/nota-debito`, en SQLite y PostgreSQL. El saldo se lee
del libro (`get_cc_saldo`, opción B).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fechas import hoy
from test_cobro_no_supera_lo_pendiente import ITEMS
from test_facturas_router import ADMIN, API, USUARIO, _emitir, _montar

from libracore import cobros
from libracore import facturas_router as fr
from libracore import tipos_comprobante as tipos
from libracore.cuenta_corriente_router import build_cuenta_corriente_router
from libracore.db import core
from libracore.db import cuenta_corriente as db_cc
from libracore.db import facturas as db_facturas
from libracore.db.clients import create_client

CUIT = "20304050607"
CLIENTE = 1


@pytest.fixture(params=["sqlite", "postgres"])
def client(request, tmp_path, monkeypatch):
    destino = None if request.param == "sqlite" else request.getfixturevalue("bases").nueva()
    c = _montar(tmp_path, monkeypatch, db_path=destino)
    create_client("Juan Perez", cuit_dni=CUIT)
    yield c
    core._db_path = None
    core._database_url = None


def _factura(client, condicion="Cuenta Corriente") -> dict:
    """Una factura de 4.500 del cliente."""
    return _emitir(client, items=ITEMS, condicion_venta=condicion)


def _nota_de_debito(client, factura) -> dict:
    """Su nota de débito (copia el total: 4.500)."""
    r = client.post(f"{API}/{factura['id']}/nota-debito", headers=ADMIN)
    assert r.status_code == 200, r.text
    nota = r.json()
    assert nota["tipo"] in tipos.ND and nota["total"] == 4500.0
    return nota


def _cuenta():
    app = FastAPI()
    app.include_router(build_cuenta_corriente_router(usuario_actual=lambda: USUARIO, solo_admin=lambda: None))
    return TestClient(app)


def _pagar(monto, facturas):
    return _cuenta().post(f"/api/cuenta-corriente/{CLIENTE}/pagar", json={
        "monto": monto, "fecha": hoy(), "caja_id": 1, "medio_pago": "transferencia", "referencia": "",
        "facturas": facturas})


def _cobrar(client, comprobante, monto):
    return client.post(
        f"{API}/{comprobante['id']}/cobrar", json={"pagos": [{"medio_id": "efectivo", "monto": monto, "referencia": ""}]})


def _pendiente(client, comprobante) -> float:
    r = client.get(f"{API}/{comprobante['id']}")
    assert r.status_code == 200, r.text
    return r.json()["pendiente"]


def _pendientes_cc() -> list[tuple[int, float]]:
    return [(p["id"], p["pendiente"]) for p in db_cc.get_facturas_pendientes_cc(CLIENTE)]


def _sin_cae(monkeypatch):
    """ARCA rechaza lo que se emita de acá en adelante: `solicitar_cae` deja el motivo y no levanta (como el real)."""
    async def rechazar(factura_id, factura, ta, arca, *, conn=None):
        db_facturas.update_factura_cae_error(factura_id, "10016: rechazado por ARCA (simulado)")
        return db_facturas.get_factura(factura_id)

    monkeypatch.setattr(fr, "solicitar_cae", rechazar)


# ── Emitir ────────────────────────────────────────────────────────────────


def test_la_nota_de_una_factura_a_credito_lleva_la_condicion_y_asienta_su_debito(client):
    factura = _factura(client)
    assert db_cc.get_cc_saldo(CLIENTE) == 4500.0

    nota = _nota_de_debito(client, factura)

    assert nota["condicion_venta"] == "Cuenta Corriente"
    assert db_cc.get_cc_saldo(CLIENTE) == 9000.0, "el débito de la nota entra a la cuenta"
    movs = db_cc.get_cc_movimientos(CLIENTE)
    debito = [m for m in movs if m["factura_id"] == nota["id"]]
    assert len(debito) == 1 and debito[0]["tipo"] == "debito" and debito[0]["monto"] == 4500.0
    assert debito[0]["concepto"].startswith("ND C ")
    assert _pendientes_cc() == [(factura["id"], 4500.0), (nota["id"], 4500.0)]
    assert _pendiente(client, nota) == 4500.0


def test_varias_notas_de_debito_suman_cada_una_su_debito(client):
    factura = _factura(client)
    _nota_de_debito(client, factura)
    _nota_de_debito(client, factura)
    assert db_cc.get_cc_saldo(CLIENTE) == 13500.0


def test_la_nota_de_una_factura_al_contado_no_toca_la_cuenta(client):
    factura = _factura(client, condicion="Contado")
    nota = _nota_de_debito(client, factura)

    assert nota["condicion_venta"] != "Cuenta Corriente"
    assert db_cc.get_cc_saldo(CLIENTE) == 0.0
    assert db_cc.get_cc_movimientos(CLIENTE) == []
    assert _pendientes_cc() == []
    # Se cobra por caja, como hasta ahora, y la cuenta sigue sin moverse.
    assert _cobrar(client, nota, 4500.0).status_code == 200
    assert _pendiente(client, nota) == 0.0
    assert db_cc.get_cc_saldo(CLIENTE) == 0.0


def test_el_debito_va_en_la_misma_transaccion_que_la_nota(client, monkeypatch):
    """Si el débito no se puede escribir, la nota tampoco queda: nunca una nota «a cuenta corriente» sin su deuda."""
    factura = _factura(client)

    def falla(*_a, **_k):
        raise RuntimeError("se cayó la base a mitad de camino")

    monkeypatch.setattr(fr, "_debitar_nota_en_la_cuenta", falla)
    with pytest.raises(RuntimeError):
        client.post(f"{API}/{factura['id']}/nota-debito", headers=ADMIN)

    with core.get_connection() as conn:
        notas = conn.execute(
            f"SELECT COUNT(*) FROM facturas WHERE tipo IN ({','.join(str(t) for t in tipos.ND)})").fetchone()[0]
    assert notas == 0
    assert db_cc.get_cc_saldo(CLIENTE) == 4500.0


# ── Pagar desde la cuenta y cobrar por caja ───────────────────────────────


def test_un_recibo_paga_la_nota_y_baja_la_cuenta(client):
    factura = _factura(client)
    nota = _nota_de_debito(client, factura)

    r = _pagar(4500.0, [nota["id"]])

    assert r.status_code == 200, r.text
    assert db_cc.get_cc_saldo(CLIENTE) == 4500.0, "queda la factura"
    assert _pendientes_cc() == [(factura["id"], 4500.0)]
    assert _pendiente(client, nota) == 0.0
    assert cobros.estado_de_cobro(nota["id"])["cobrado"] == 4500.0
    # Ya pagada desde la cuenta, «Cobrar» no la cobra otra vez.
    assert _cobrar(client, nota, 100.0).status_code == 409


def test_cobrar_por_caja_la_nota_abona_la_cuenta_una_sola_vez(client):
    """«Cobrar» sobre la nota a cuenta corriente hace lo mismo que sobre la factura: el cobro entra a la caja y abona
    la cuenta (`registrar_cobro_factura`). Después, ni un recibo ni otro «Cobrar» la cobran de nuevo."""
    factura = _factura(client)
    nota = _nota_de_debito(client, factura)

    assert _cobrar(client, nota, 4500.0).status_code == 200

    assert db_cc.get_cc_saldo(CLIENTE) == 4500.0
    assert _pendiente(client, nota) == 0.0
    assert _pendientes_cc() == [(factura["id"], 4500.0)]
    assert _pagar(4500.0, [nota["id"]]).status_code == 422
    assert _cobrar(client, nota, 4500.0).status_code == 409
    assert db_cc.get_cc_saldo(CLIENTE) == 4500.0


def test_un_cobro_parcial_deja_el_resto_en_la_cuenta(client):
    factura = _factura(client)
    nota = _nota_de_debito(client, factura)

    assert _cobrar(client, nota, 1500.0).status_code == 200
    assert db_cc.get_cc_saldo(CLIENTE) == 7500.0
    assert _pendientes_cc() == [(factura["id"], 4500.0), (nota["id"], 3000.0)]

    assert _pagar(7500.0, [factura["id"], nota["id"]]).status_code == 200
    assert db_cc.get_cc_saldo(CLIENTE) == 0.0
    assert _pendientes_cc() == []


# ── Sin CAE, anular, borrar y notas de crédito ────────────────────────────


def test_sin_cae_la_nota_queda_con_su_debito_como_una_factura_a_credito(client, monkeypatch):
    """Como `crear`: si ARCA rechaza, la nota queda sin CAE **con** su débito. No se ofrece para pagar desde la cuenta
    (`get_facturas_pendientes_cc` excluye lo que no tiene CAE, igual que con una factura) y anularla saca la deuda."""
    factura = _factura(client)
    _sin_cae(monkeypatch)
    nota = _nota_de_debito(client, factura)

    assert not nota["cae"] and nota["cae_error"]
    assert nota["condicion_venta"] == "Cuenta Corriente"
    assert db_cc.get_cc_saldo(CLIENTE) == 9000.0
    assert _pendientes_cc() == [(factura["id"], 4500.0)]

    r = client.post(f"{API}/{nota['id']}/anular", headers=ADMIN, json={"motivo": "ARCA la rechazó"})
    assert r.status_code == 200, r.text
    assert db_cc.get_cc_saldo(CLIENTE) == 4500.0, "anulada, la deuda de la nota sale de la cuenta"
    assert [m["factura_id"] for m in db_cc.get_cc_movimientos(CLIENTE)] == [factura["id"]]


def test_sin_cae_borrarla_tambien_saca_su_debito(client, monkeypatch):
    factura = _factura(client)
    _sin_cae(monkeypatch)
    nota = _nota_de_debito(client, factura)
    assert db_cc.get_cc_saldo(CLIENTE) == 9000.0

    assert client.delete(f"{API}/{nota['id']}", headers=ADMIN).status_code == 200
    assert db_cc.get_cc_saldo(CLIENTE) == 4500.0


def test_autorizada_despues_entra_a_lo_que_se_paga_desde_la_cuenta(client, monkeypatch):
    """La deuda ya estaba asentada: autorizarla no escribe otro débito, sólo la suma a lo pendiente de la cuenta."""
    factura = _factura(client)
    _sin_cae(monkeypatch)
    nota = _nota_de_debito(client, factura)
    db_facturas.update_factura_cae(nota["id"], "71000000000001", "20261031")

    assert db_cc.get_cc_saldo(CLIENTE) == 9000.0
    assert _pendientes_cc() == [(factura["id"], 4500.0), (nota["id"], 4500.0)]


def test_la_nota_de_credito_de_la_factura_no_se_lleva_la_nota_de_debito(client):
    """La NC total de la factura abona la cuenta por la factura (como siempre); la nota de débito sigue debiéndose."""
    factura = _factura(client)
    nota = _nota_de_debito(client, factura)

    r = client.post(f"{API}/{factura['id']}/nota-credito", headers=ADMIN)
    assert r.status_code == 200, r.text

    assert db_cc.get_cc_saldo(CLIENTE) == 4500.0
    assert _pendientes_cc() == [(nota["id"], 4500.0)]


def test_una_nota_de_credito_sobre_la_nota_de_debito_no_se_emite_y_no_mueve_la_cuenta(client):
    """El motor no emite notas de crédito sobre una de débito (`TIPO_NC` mapea sólo facturas, ADR-064): 400 y el
    saldo queda como estaba. Si algún día se habilita, la nota ya lleva `condicion_venta` y `nota_credito` la abonaría
    a la cuenta como a una factura a crédito."""
    factura = _factura(client)
    nota = _nota_de_debito(client, factura)

    assert client.post(f"{API}/{nota['id']}/nota-credito", headers=ADMIN).status_code == 400
    assert db_cc.get_cc_saldo(CLIENTE) == 9000.0
