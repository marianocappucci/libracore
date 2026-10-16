"""El cobro de una factura entra por el turno abierto de quien cobra, sin reemplazar el cobro del motor (ADR-060).

Lo pedía LibraClub, que lleva la caja **por turno** (`turnos_caja`, con su arqueo al cerrar) y por eso reemplazaba
`registrar_cobro` entero: el cobro por defecto escribe el movimiento sin `turno_id`, plata que ningún cierre
cuenta. El reemplazo se llevaba también el tope y el candado del ADR-058. Ahora el motor lo resuelve con
`exigir_turno` / `usuario_del_turno` y el producto no escribe nada.

En SQLite y en PostgreSQL. Cada test mira lo escrito en la base, no sólo el status.
"""

from __future__ import annotations

import datetime
import threading

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_facturas_router import API, USUARIO, _emitir, _montar

from libracore import cobros
from libracore import facturas_router as fr
from libracore.db import caja as db_caja
from libracore.db import core
from libracore.db import turnos as db_turnos
from libracore.db.clients import create_client

HOY = datetime.date.today().isoformat()
ITEMS = [{"description": "Servicio", "qty": 1, "unit_price": 1000.0}]
SIN_TURNO = "No hay una caja abierta. Abrí el turno antes de cobrar."

#: Como llega el usuario a un producto cuyos usuarios viven en otra base: el `id` que ve el motor es `None`
#: (la FK de `facturas.usuario_id` apunta a la tabla del motor) y el verdadero va aparte.
DEL_DOMINIO = {"id": None, "id_dominio": 1, "username": "cajero"}


@pytest.fixture(params=["sqlite", "postgres"])
def motor(request, tmp_path):
    return None if request.param == "sqlite" else request.getfixturevalue("bases").nueva()


@pytest.fixture
def emisor(motor, tmp_path, monkeypatch):
    """`(client del emisor de facturas, función que monta el router de cobro con las opciones que se pidan)`."""
    base = _montar(tmp_path, monkeypatch, db_path=motor)
    create_client("Juan Perez", cuit_dni="20304050607")

    def montar(usuario=USUARIO, **opciones) -> TestClient:
        app = FastAPI()
        app.include_router(fr.build_comprobantes_router(
            usuario_actual=lambda: usuario, solo_admin=lambda: None, **opciones))
        return TestClient(app)

    yield base, montar
    core._db_path = None
    core._database_url = None


def _turno(caja_nombre="Mostrador", usuario_id=1) -> tuple[int, int]:
    """Abre un turno sobre una caja propia; devuelve `(turno_id, caja_id)`."""
    caja_id = db_caja.create_caja_config(caja_nombre, "", ["efectivo", "transferencia"])
    return db_turnos.create_turno(usuario_id, 0.0, caja_id=caja_id), caja_id


def _cobrar(client, factura, monto=1000.0, medio="efectivo", **extra):
    return client.post(f"{API}/{factura['id']}/cobrar",
                       json={"pagos": [{"medio_id": medio, "monto": monto}], **extra})


def _movimientos(factura) -> list[dict]:
    with core.get_connection() as c:
        filas = c.execute("SELECT * FROM caja_movimientos WHERE factura_id=? AND tipo='ingreso' ORDER BY id",
                          (factura["id"],)).fetchall()
    return [dict(f) for f in filas]


# ── Con turno ─────────────────────────────────────────────────────────────


def test_el_cobro_entra_en_el_turno_abierto_con_su_caja_su_dueno_y_el_dia_de_hoy(emisor):
    base, montar = emisor
    factura = _emitir(base, items=ITEMS)
    turno, caja = _turno()
    cobro = montar(exigir_turno=True)

    r = _cobrar(cobro, factura, 1000.0, caja_id=99, fecha="2000-01-01")

    assert r.status_code == 200, r.text
    (mov,) = _movimientos(factura)
    # La caja y el día los define el turno, no el pedido (aceptarlos sería una segunda puerta).
    assert (mov["turno_id"], mov["caja_id"], mov["usuario_id"], mov["fecha"]) == (turno, caja, 1, HOY)
    # Y es lo que el arqueo cuenta: la razón de ser de todo esto.
    assert db_turnos.get_resumen_turno_caja(turno)["pagos_por_medio"]["efectivo"] == 1000.0


def test_con_el_usuario_del_dominio_el_turno_es_el_de_quien_cobra_y_no_el_id_que_ve_el_motor(emisor):
    """El `id` ante el motor es `None`: sin `usuario_del_turno` no habría de dónde sacar el turno."""
    base, montar = emisor
    factura = _emitir(base, items=ITEMS)
    turno, _ = _turno()

    sin = montar(usuario=DEL_DOMINIO, exigir_turno=True)
    assert _cobrar(sin, factura).status_code == 409

    con = montar(usuario=DEL_DOMINIO, exigir_turno=True, usuario_del_turno=lambda u: u["id_dominio"])
    r = _cobrar(con, factura)
    assert r.status_code == 200, r.text
    assert [m["turno_id"] for m in _movimientos(factura)] == [turno]


def test_sin_turno_abierto_y_con_turno_exigido_es_409_y_no_se_escribe_nada(emisor):
    base, montar = emisor
    factura = _emitir(base, items=ITEMS)
    _turno(usuario_id=1)
    otro = {**USUARIO, "id": 2}
    with core.get_connection() as c:
        c.execute("INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (2,'b','B','x','admin')")

    r = _cobrar(montar(usuario=otro, exigir_turno=True), factura)

    assert r.status_code == 409 and r.json()["detail"] == SIN_TURNO, r.text
    assert _movimientos(factura) == [], "el turno abierto es de otro usuario: ése no cobra en el suyo"


def test_un_turno_cerrado_no_cuenta(emisor):
    base, montar = emisor
    factura = _emitir(base, items=ITEMS)
    turno, _ = _turno()
    with core.get_connection() as c:
        c.execute("UPDATE turnos_caja SET estado='cerrado' WHERE id=?", (turno,))

    r = _cobrar(montar(exigir_turno=True), factura)

    assert r.status_code == 409 and r.json()["detail"] == SIN_TURNO
    assert _movimientos(factura) == []


def test_el_tope_sigue_rigiendo_con_turno(emisor):
    base, montar = emisor
    factura = _emitir(base, items=ITEMS)
    _turno()
    cobro = montar(exigir_turno=True)
    assert _cobrar(cobro, factura, 400.0).status_code == 200

    r = _cobrar(cobro, factura, 700.0)

    assert r.status_code == 409 and "$ 600,00" in r.json()["detail"], r.text
    assert [m["monto"] for m in _movimientos(factura)] == [400.0]
    assert _cobrar(cobro, factura, 600.0).status_code == 200


def test_el_turno_sin_turno_exigido_se_ve_pero_no_se_pide(emisor):
    """`usuario_del_turno` solo: con turno abierto el cobro va por él; sin turno, entra suelto como siempre."""
    base, montar = emisor
    factura = _emitir(base, items=ITEMS)
    cobro = montar(usuario=DEL_DOMINIO, usuario_del_turno=lambda u: u["id_dominio"])

    assert _cobrar(cobro, factura, 300.0).status_code == 200
    assert [m["turno_id"] for m in _movimientos(factura)] == [None]

    turno, _ = _turno()
    assert _cobrar(cobro, factura, 300.0).status_code == 200
    assert [m["turno_id"] for m in _movimientos(factura)] == [None, turno]


def test_sin_las_opciones_el_cobro_es_el_de_siempre_aunque_haya_un_turno_abierto(emisor):
    """El control: Contalibra y Restolibra (caja suelta) no cambian."""
    base, montar = emisor
    factura = _emitir(base, items=ITEMS)
    _turno()

    r = _cobrar(montar(), factura, 1000.0, fecha="2026-01-02")

    assert r.status_code == 200, r.text
    (mov,) = _movimientos(factura)
    assert (mov["turno_id"], mov["fecha"]) == (None, "2026-01-02")


def test_una_factura_a_credito_cobrada_en_un_turno_abona_la_cuenta_con_el_dueno_del_turno(emisor):
    base, montar = emisor
    factura = _emitir(base, items=ITEMS, condicion_venta="Cuenta Corriente")
    turno, caja = _turno()

    r = _cobrar(montar(exigir_turno=True), factura, 1000.0)

    assert r.status_code == 200, r.text
    (mov,) = [m for m in _movimientos(factura) if m["medio_pago"] == "efectivo"]
    assert mov["turno_id"] == turno
    with core.get_connection() as c:
        pago = c.execute("SELECT caja_id, usuario_id, monto FROM cc_pagos").fetchone()
    assert (pago[0], pago[1], pago[2]) == (caja, 1, 1000.0)


# ── Lo que es sólo del producto ───────────────────────────────────────────


def test_validar_cobro_rechaza_antes_de_todo_y_no_deja_nada_escrito(emisor):
    base, montar = emisor
    factura = _emitir(base, items=ITEMS)
    _turno()
    vistos = []

    def validar(f, pagos, usuario):
        vistos.append((f["id"], [p["medio_id"] for p in pagos], usuario["id"]))
        if any(p["medio_id"] == "cheque" for p in pagos):
            raise HTTPException(400, "Medio de pago desconocido: 'cheque'.")

    cobro = montar(exigir_turno=True, validar_cobro=validar)

    r = _cobrar(cobro, factura, 1000.0, medio="cheque")
    assert r.status_code == 400 and "cheque" in r.json()["detail"]
    assert _movimientos(factura) == []
    assert _cobrar(cobro, factura, 1000.0).status_code == 200
    assert vistos == [(factura["id"], ["cheque"], 1), (factura["id"], ["efectivo"], 1)]


# ── Concurrencia, con turno ───────────────────────────────────────────────


def test_seis_cobros_simultaneos_en_el_turno_que_juntos_superan_dejan_pasar_uno(emisor):
    base, montar = emisor
    factura = _emitir(base, items=ITEMS)
    turno, _ = _turno()
    cobro = montar(exigir_turno=True)
    estados = []
    salida = threading.Barrier(6)

    def pedir():
        salida.wait()
        estados.append(_cobrar(cobro, factura, 800.0).status_code)

    hilos = [threading.Thread(target=pedir) for _ in range(6)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert sorted(estados) == [200, 409, 409, 409, 409, 409], estados
    assert [(m["monto"], m["turno_id"]) for m in _movimientos(factura)] == [(800.0, turno)]


# ── Para el cobro propio de un producto: la transacción con tope y candado ─


def test_transaccion_de_cobro_escribe_con_el_tope_y_confirma_al_salir(emisor):
    base, _ = emisor
    factura = _emitir(base, items=ITEMS)
    turno, _caja = _turno()

    with cobros.transaccion_de_cobro(factura["id"], 600.0) as c:
        db_caja.create_caja_movimiento(HOY, "ingreso", "Cobro de la agenda", 600.0, factura_id=factura["id"],
                                       medio_pago="efectivo", turno_id=turno, conn=c)
    assert [m["monto"] for m in _movimientos(factura)] == [600.0]

    with pytest.raises(cobros.CobroNoPermitido) as exc:
        with cobros.transaccion_de_cobro(factura["id"], 500.0) as c:
            db_caja.create_caja_movimiento(HOY, "ingreso", "Cobro de la agenda", 500.0, factura_id=factura["id"],
                                           medio_pago="efectivo", turno_id=turno, conn=c)
    assert exc.value.codigo == cobros.CobroNoPermitido.EXCEDE and "$ 400,00" in str(exc.value)
    assert [m["monto"] for m in _movimientos(factura)] == [600.0], "el rechazo no escribe"


def test_transaccion_de_cobro_deshace_lo_escrito_si_algo_falla_adentro(emisor):
    base, _ = emisor
    factura = _emitir(base, items=ITEMS)
    turno, _caja = _turno()

    with pytest.raises(RuntimeError):
        with cobros.transaccion_de_cobro(factura["id"], 600.0) as c:
            db_caja.create_caja_movimiento(HOY, "ingreso", "Cobro", 600.0, factura_id=factura["id"],
                                           medio_pago="efectivo", turno_id=turno, conn=c)
            raise RuntimeError("se cayó antes de confirmar")

    assert _movimientos(factura) == []


def test_exigir_turno_sin_de_donde_sacarlo_es_un_error_de_quien_lo_arma():
    with pytest.raises(ValueError):
        cobros.registrar_cobro_factura({"id": 1, "tipo": 11, "punto_venta": 1, "numero": 1, "total": 10.0,
                                        "cliente_razon": "X"}, [{"medio_id": "efectivo", "monto": 1.0}],
                                       exigir_turno=True)
