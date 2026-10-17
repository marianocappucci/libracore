"""El pago a cuenta corriente aplicado a facturas toma el candado del cobro (ADR-063).

`POST /api/cuenta-corriente/{id}/pagar` con `facturas` leía lo pendiente por un lado y escribía el cobro
por otro, sin candado: dos pagos simultáneos a la misma factura pasaban los dos el tope y la cobraban de más
(el mismo defecto que el ADR-058 cerró para `POST /api/facturas/{id}/cobrar`).

Los tests van por HTTP, **en SQLite y PostgreSQL** (el candado es distinto en cada motor) y miran lo escrito
en la base, no sólo el status. Los de concurrencia abren una conexión por hilo, como lo hace el servidor, y
ensanchan a propósito el hueco entre «miré lo pendiente» y «escribí» con un gancho `validar_pago` lento: sin
el candado los dos hilos ven la factura entera pendiente.
"""
from __future__ import annotations

import datetime
import threading
import time

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracore import cobros
from libracore.cuenta_corriente_router import (
    CobroAprobado,
    OpcionesCuentaCorriente,
    build_cuenta_corriente_router,
)
from libracore.db import caja, core
from libracore.db.schema import init_core_schema

HOY = datetime.date.today().isoformat()
CUIT = "30665138166"


def _usuario():
    return {"id": 7, "username": "cajero", "role": "admin"}


def _solo_admin(user: dict = Depends(_usuario)):
    if user.get("role") != "admin":
        raise HTTPException(403, "solo administradores")


def _lento(payload, user):
    """El gancho del producto, lento: entre que se lee lo pendiente y se escribe el cobro pasa un rato."""
    time.sleep(0.3)
    return CobroAprobado(caja_id=payload.caja_id)


@pytest.fixture(params=["sqlite", "postgres"])
def base(request, tmp_path):
    destino = str(tmp_path / "pago_cc.db") if request.param == "sqlite" \
        else request.getfixturevalue("bases").nueva()
    core.configure(db_path=destino)
    conn = core.get_connection()
    init_core_schema(conn)
    conn.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (7,'c','C','x','admin')")
    conn.execute("INSERT INTO clients (id, name, cuit_dni) VALUES (1, 'Cliente de Prueba', '30-66513816-6')")
    conn.commit()
    conn.close()
    yield request.param
    core._db_path = None
    core._database_url = None


def _montar(opciones=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_cuenta_corriente_router(
        usuario_actual=_usuario, solo_admin=_solo_admin, opciones=opciones))
    return TestClient(app)


@pytest.fixture
def client(base):
    return _montar()


@pytest.fixture
def client_lento(base):
    return _montar(OpcionesCuentaCorriente(validar_pago=_lento))


def _factura(numero, total, fecha="2026-08-28") -> int:
    """Una factura C a cuenta corriente con CAE, y su débito en la cuenta (como la deja la emisión)."""
    with core.get_connection() as conn:
        fid = conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, items,"
            " subtotal, iva_amount, total, cae, condicion_venta)"
            " VALUES (11,5,?,?,?,?,'[]',?,0,?,'123','Cuenta Corriente')",
            (numero, fecha, CUIT, "Cliente de Prueba", total, total),
        ).lastrowid
        caja.create_caja_movimiento(fecha, "ingreso", "Factura", total, factura_id=fid,
                                    medio_pago="Cuenta Corriente", conn=conn)
    return fid


def _pagar(client, monto, facturas, referencia=""):
    return client.post("/api/cuenta-corriente/1/pagar", json={
        "monto": monto, "fecha": HOY, "caja_id": 1, "medio_pago": "transferencia",
        "referencia": referencia, "facturas": facturas})


def _cobrado(fid) -> float:
    with core.get_connection() as conn:
        return float(conn.execute(
            "SELECT COALESCE(SUM(monto),0) FROM caja_movimientos WHERE factura_id=? AND tipo='ingreso'"
            " AND lower(medio_pago) NOT IN ('cuenta corriente','cuenta_corriente') AND anulado=0",
            (fid,)).fetchone()[0])


def _cantidad(tabla) -> int:
    with core.get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


def _en_paralelo(*pedidos):
    """Corre cada callable en su hilo, todos a la vez, y devuelve sus resultados en orden."""
    salida = threading.Barrier(len(pedidos))
    resultados = [None] * len(pedidos)

    def correr(i, pedido):
        salida.wait()
        resultados[i] = pedido()

    hilos = [threading.Thread(target=correr, args=(i, p)) for i, p in enumerate(pedidos)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    return resultados


# ── Concurrencia ──────────────────────────────────────────────────────────


def test_dos_pagos_simultaneos_a_la_misma_factura_no_la_cobran_de_mas(client_lento):
    """Cada pago (1.000) cubre solo la factura entera; juntos la cobrarían dos veces. Sin el candado los dos
    veían 1.000 pendientes y los dos escribían. Con él, el segundo espera, ve la factura cobrada y se rechaza
    con el 422 de siempre («sin cobro pendiente»), sin dejar nada escrito."""
    f = _factura(1, 1000.0)

    r1, r2 = _en_paralelo(lambda: _pagar(client_lento, 1000.0, [f]), lambda: _pagar(client_lento, 1000.0, [f]))

    assert sorted([r1.status_code, r2.status_code]) == [200, 422], (r1.text, r2.text)
    assert _cobrado(f) == 1000.0
    assert _cantidad("cc_pagos") == 1          # el pago rechazado no dejó su abono a medias


def test_dos_pagos_parciales_simultaneos_se_aplican_sobre_lo_que_dejo_el_primero(client_lento):
    """700 + 700 sobre 1.000: el primero cubre 700; el segundo espera, ve 300 pendientes y aplica **sólo
    lo que falta**; los 400 restantes quedan como pago a cuenta suelto (como en un pago secuencial)."""
    f = _factura(1, 1000.0)

    r1, r2 = _en_paralelo(lambda: _pagar(client_lento, 700.0, [f]), lambda: _pagar(client_lento, 700.0, [f]))

    assert (r1.status_code, r2.status_code) == (200, 200), (r1.text, r2.text)
    assert _cobrado(f) == 1000.0
    with core.get_connection() as conn:
        suelto = conn.execute(
            "SELECT COALESCE(SUM(monto),0) FROM caja_movimientos WHERE concepto LIKE 'Pago CC%'").fetchone()[0]
    assert suelto == 400.0


def test_dos_pagos_con_las_mismas_facturas_en_distinto_orden_no_se_interbloquean(client_lento, monkeypatch):
    """`[f1, f2]` y `[f2, f1]` a la vez: con candados tomados en el orden del pedido, cada pago toma uno y
    espera el otro (interbloqueo; PostgreSQL aborta uno con error). Se toman en orden de id ascendente, así
    que el segundo espera en el primero. El candado se frena a propósito después de tomarlo para que el cruce
    sea seguro y no cuestión de suerte."""
    f1, f2 = _factura(1, 1000.0), _factura(2, 1000.0)
    original = cobros._bloquear_factura

    def lento(conn, factura_id):
        original(conn, factura_id)
        time.sleep(0.3)

    monkeypatch.setattr(cobros, "_bloquear_factura", lento)

    r1, r2 = _en_paralelo(lambda: _pagar(client_lento, 2000.0, [f1, f2]),
                          lambda: _pagar(client_lento, 2000.0, [f2, f1]))

    assert sorted([r1.status_code, r2.status_code]) == [200, 422], (r1.text, r2.text)
    assert _cobrado(f1) == 1000.0 and _cobrado(f2) == 1000.0


def test_un_pago_y_un_cobro_simultaneos_a_la_misma_factura_tampoco_la_cobran_de_mas(client_lento, monkeypatch):
    """Los dos caminos comparten el candado: `registrar_cobro_factura` (el «Registrar cobro» del comprobante)
    y el pago a cuenta aplicado a esa misma factura se hacen cola."""
    f = _factura(1, 1000.0)
    with core.get_connection() as conn:
        fila = dict(conn.execute("SELECT * FROM facturas WHERE id=?", (f,)).fetchone())

    def cobrar():
        try:
            cobros.registrar_cobro_factura(fila, [{"medio_id": "efectivo", "monto": 1000.0}], fecha=HOY)
            return "ok"
        except cobros.CobroNoPermitido as exc:
            return exc.codigo

    pago, cobro = _en_paralelo(lambda: _pagar(client_lento, 1000.0, [f]), cobrar)

    # Uno entra y el otro se rechaza, según quién llegue primero.
    assert (pago.status_code, cobro) in [(200, "sin_saldo"), (422, "ok")], (pago.text, cobro)
    assert _cobrado(f) == 1000.0


# ── Conducta secuencial ───────────────────────────────────────────────────


def test_el_pago_aplicado_de_siempre_sigue_funcionando(client):
    """El control de lo de arriba: un candado que rechazara todo pasaría los tests de rechazo."""
    f1, f2 = _factura(1, 1000.0, "2026-08-01"), _factura(2, 500.0, "2026-08-02")

    r = _pagar(client, 1600.0, [f2, f1])

    assert r.status_code == 200, r.text
    assert _cobrado(f1) == 1000.0 and _cobrado(f2) == 500.0
    assert r.json()["facturas_pendientes"] == []
    assert r.json()["saldo"] == -100.0                       # lo que sobra queda a favor
    assert _cantidad("cc_pagos") == 1


def test_una_factura_ya_cobrada_se_rechaza_con_422_y_sin_escribir_nada(client):
    f = _factura(1, 1000.0)
    assert _pagar(client, 1000.0, [f]).status_code == 200

    r = _pagar(client, 1000.0, [f])

    assert r.status_code == 422, r.text
    assert _cobrado(f) == 1000.0 and _cantidad("cc_pagos") == 1


def test_un_pago_que_falla_a_mitad_de_camino_no_deja_nada_escrito(client, monkeypatch):
    """Todo en una transacción: si el cobro de la segunda factura revienta, tampoco queda el abono ni el
    cobro de la primera (antes quedaban, cada escritura confirmaba por su lado)."""
    f1, f2 = _factura(1, 1000.0), _factura(2, 1000.0)
    original = caja.create_caja_movimiento
    llamadas = []

    def falla_en_la_segunda(*args, **kwargs):
        llamadas.append(1)
        if len(llamadas) == 2:
            raise RuntimeError("se cayó la base")
        return original(*args, **kwargs)

    monkeypatch.setattr(caja, "create_caja_movimiento", falla_en_la_segunda)

    with pytest.raises(RuntimeError):
        _pagar(client, 2000.0, [f1, f2])

    monkeypatch.setattr(caja, "create_caja_movimiento", original)
    assert _cantidad("cc_pagos") == 0
    assert _cobrado(f1) == 0.0 and _cobrado(f2) == 0.0
    assert _pagar(client, 2000.0, [f1, f2]).status_code == 200      # y la factura no quedó trabada


def test_el_rechazo_del_gancho_del_producto_no_deja_nada_escrito_ni_el_candado_tomado(base):
    def rechaza(payload, user):
        raise HTTPException(409, "no hay un turno de caja abierto")

    client = _montar(OpcionesCuentaCorriente(validar_pago=rechaza))
    f = _factura(1, 1000.0)

    assert _pagar(client, 1000.0, [f]).status_code == 409
    assert _cantidad("cc_pagos") == 0 and _cobrado(f) == 0.0
    assert _pagar(_montar(), 1000.0, [f]).status_code == 200        # el candado se soltó
