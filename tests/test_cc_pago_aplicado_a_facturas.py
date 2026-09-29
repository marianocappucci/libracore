"""Un pago a cuenta se puede aplicar a facturas, y el motor avisa cuáles quedan.

El caso que lo motivó: Municipalidad de Suipacha pagó las FC 74 y 75 con una
transferencia cargada como "Pago a cuenta" (2026-09-14). El saldo bajó bien,
pero las dos facturas quedaron "Sin cobrar" para siempre, porque sólo el cobro
de una factura escribe el movimiento de caja que la marca "Cobrada". Y
"Registrar cobro" después las habría cobrado dos veces.
"""
import datetime

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracore.cuenta_corriente_router import build_cuenta_corriente_router
from libracore.db import core, cuenta_corriente
from libracore.db.schema import init_core_schema

HOY = datetime.date.today().isoformat()
CUIT_FICHA = "30-66513816-6"
CUIT_FACTURA = "30665138166"


def _usuario():
    return {"id": 7, "username": "cajero", "role": "admin"}


def _solo_admin(user: dict = Depends(_usuario)):
    if user.get("role") != "admin":
        raise HTTPException(403, "solo administradores")


@pytest.fixture
def entorno(tmp_path):
    core.configure(db_path=str(tmp_path / "cc_aplicar.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (7,'c','C','x','admin')")
    conn.execute("INSERT INTO clients (id, name, cuit_dni) VALUES (1, 'Municipalidad', ?)", (CUIT_FICHA,))
    conn.commit()
    conn.close()
    yield
    core._db_path = None


@pytest.fixture
def client(entorno):
    app = FastAPI()
    app.include_router(build_cuenta_corriente_router(usuario_actual=_usuario, solo_admin=_solo_admin))
    return TestClient(app)


def _factura(numero, total, fecha="2026-08-28", cae="123", cond="Cuenta Corriente", tipo=11, asoc=None):
    """Emite una factura y, si es a crédito, su débito en cuenta corriente."""
    with core.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, items,"
            " subtotal, iva_amount, total, cae, condicion_venta, cbte_asoc_tipo, cbte_asoc_pv, cbte_asoc_nro)"
            " VALUES (?,5,?,?,?,?,'[]',?,0,?,?,?,?,?,?)",
            (tipo, numero, fecha, CUIT_FACTURA, "Municipalidad", total, total, cae, cond,
             *(asoc or (0, 0, 0))),
        )
        fid = cur.lastrowid
        if cond == "Cuenta Corriente" and tipo == 11:
            conn.execute(
                "INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, factura_id, medio_pago)"
                " VALUES (?, 'ingreso', 'Factura', ?, ?, 'Cuenta Corriente')", (fecha, total, fid))
    return fid


def _pagar(client, monto, **extra):
    caja = 1
    return client.post("/api/cuenta-corriente/1/pagar", json={
        "monto": monto, "fecha": HOY, "caja_id": caja, "medio_pago": "transferencia",
        "referencia": "Transf. 2348", **extra})


def _cobrado(fid):
    with core.get_connection() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(monto),0) FROM caja_movimientos WHERE factura_id=? AND tipo='ingreso'"
            " AND lower(medio_pago) NOT IN ('cuenta corriente','cuenta_corriente') AND anulado=0",
            (fid,)).fetchone()[0]


def test_el_detalle_lista_las_facturas_sin_cobro_aunque_el_cuit_difiera_en_guiones(client):
    f74 = _factura(74, 920000)
    _factura(75, 573750)
    pend = client.get("/api/cuenta-corriente/1").json()["facturas_pendientes"]
    assert [p["numero"] for p in pend] == [74, 75]
    assert pend[0]["id"] == f74 and pend[0]["pendiente"] == 920000
    assert pend[0]["concepto"] == "FACTURA C 0005-00000074"


def test_no_son_pendientes_las_notas_las_sin_cae_las_de_contado_ni_las_anuladas(client):
    _factura(1, 100, cond="Contado")
    _factura(2, 100, cae="PENDIENTE")
    _factura(3, 100, tipo=13, cond="Cuenta Corriente")
    anulada = _factura(4, 100)
    _factura(5, 100, tipo=13, asoc=(11, 5, 4))  # la NC que anula la 4
    con_cobro = _factura(6, 100)
    with core.get_connection() as conn:
        conn.execute("INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, factura_id, medio_pago)"
                     " VALUES (?, 'ingreso', 'Cobro', 100, ?, 'efectivo')", (HOY, con_cobro))
    assert anulada and cuenta_corriente.get_facturas_pendientes_cc(1) == []


def test_un_cobro_parcial_deja_el_resto_pendiente(client):
    fid = _factura(74, 1000)
    with core.get_connection() as conn:
        conn.execute("INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, factura_id, medio_pago)"
                     " VALUES (?, 'ingreso', 'Cobro', 400, ?, 'efectivo')", (HOY, fid))
    assert cuenta_corriente.get_facturas_pendientes_cc(1)[0]["pendiente"] == 600


def test_pagar_sin_facturas_baja_el_saldo_pero_no_marca_nada_cobrado(client):
    f74 = _factura(74, 920000)
    r = _pagar(client, 920000).json()
    assert r["saldo"] == 0
    assert _cobrado(f74) == 0
    assert [p["id"] for p in r["facturas_pendientes"]] == [f74]  # lo que el diálogo usa para avisar


def test_pagar_aplicando_facturas_las_marca_cobradas_sin_mover_el_saldo_de_mas(client):
    f74, f75 = _factura(74, 920000), _factura(75, 573750)
    r = _pagar(client, 1493750, facturas=[f75, f74]).json()
    assert r["saldo"] == 0                               # el abono es uno solo
    assert _cobrado(f74) == 920000 and _cobrado(f75) == 573750
    assert r["facturas_pendientes"] == []
    with core.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cc_pagos").fetchone()[0] == 1
        # La plata entra a caja una sola vez: 1.493.750 repartido, sin el "Pago CC" suelto.
        assert conn.execute(
            "SELECT SUM(monto) FROM caja_movimientos WHERE lower(medio_pago) NOT IN"
            " ('cuenta corriente','cuenta_corriente')").fetchone()[0] == 1493750
        assert conn.execute(
            "SELECT COUNT(*) FROM caja_movimientos WHERE concepto LIKE 'Pago CC%'").fetchone()[0] == 0


def test_un_pago_menor_cubre_la_mas_vieja_y_el_resto_queda_pendiente(client):
    f74 = _factura(74, 920000, fecha="2026-08-01")
    f75 = _factura(75, 573750, fecha="2026-08-02")
    r = _pagar(client, 1000000, facturas=[f75, f74]).json()
    assert _cobrado(f74) == 920000 and _cobrado(f75) == 80000
    assert [(p["id"], p["pendiente"]) for p in r["facturas_pendientes"]] == [(f75, 493750)]


def test_lo_que_sobra_queda_como_pago_a_cuenta_suelto(client):
    f74 = _factura(74, 920000)
    _pagar(client, 1000000, facturas=[f74])
    with core.get_connection() as conn:
        assert conn.execute(
            "SELECT monto FROM caja_movimientos WHERE concepto LIKE 'Pago CC%'").fetchone()[0] == 80000
    assert _cobrado(f74) == 920000


def test_aplicar_a_una_factura_que_no_esta_pendiente_se_rechaza_sin_escribir_nada(client):
    f74 = _factura(74, 920000)
    _pagar(client, 920000, facturas=[f74])
    r = _pagar(client, 920000, facturas=[f74])          # ya cobrada
    assert r.status_code == 422
    with core.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cc_pagos").fetchone()[0] == 1


def test_sin_caja_igual_queda_el_cobro_ligado_a_la_factura(client):
    f74 = _factura(74, 920000)
    client.post("/api/cuenta-corriente/1/pagar", json={
        "monto": 920000, "fecha": HOY, "medio_pago": "transferencia", "facturas": [f74]})
    assert _cobrado(f74) == 920000
