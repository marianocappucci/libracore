"""`create_caja_movimiento` sin `caja_id` explícito, con un turno abierto.

🔴 LibraCommerce llama a esta función pasando `turno_id` pero sin `caja_id`
(`erp/ventas.py`, dos lugares), y hasta ahora eso caía siempre a
`get_default_caja_id()`. En una instancia con varias cajas, toda venta
quedaba anotada en la caja por defecto aunque el turno estuviera abierto en
otra — `get_caja_movimientos(caja_id=)` y `get_caja_resumen(caja_id=)`
mentían. El arqueo y el cierre diario no se veían afectados porque van por
`turnos_caja.caja_id`, no por el `caja_id` del movimiento.
"""

from __future__ import annotations

import pytest

from libracore.db import caja as db_caja
from libracore.db import core
from libracore.db import turnos as db_turnos
from libracore.db.schema import init_core_schema


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "caja_del_turno.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def test_sin_caja_explicita_toma_la_del_turno(conn, crear_usuario):
    """La caja del turno NO es la default: si el movimiento cayera al
    default, este assert lo detecta."""
    uid = crear_usuario("cajero")
    default_id = db_caja.get_default_caja_id()
    otra = db_caja.create_caja_config("Buffet", "", ["efectivo"])
    assert otra != default_id

    tid = db_turnos.create_turno(uid, 0.0, caja_id=otra)

    mov_id = db_caja.create_caja_movimiento(
        "2026-09-16", "ingreso", "Venta", 1000, turno_id=tid, conn=conn
    )
    conn.commit()

    mov = db_caja.get_caja_movimientos(caja_id=otra)
    assert [m["id"] for m in mov] == [mov_id]


def test_caja_explicita_gana_sobre_la_del_turno(conn, crear_usuario):
    """Un `caja_id` explícito no se pisa con el del turno."""
    uid = crear_usuario("cajero")
    caja_del_turno = db_caja.create_caja_config("Buffet", "", ["efectivo"])
    caja_explicita = db_caja.create_caja_config("Delivery", "", ["efectivo"])

    tid = db_turnos.create_turno(uid, 0.0, caja_id=caja_del_turno)

    mov_id = db_caja.create_caja_movimiento(
        "2026-09-16", "ingreso", "Venta", 1000,
        caja_id=caja_explicita, turno_id=tid, conn=conn,
    )
    conn.commit()

    assert [m["id"] for m in db_caja.get_caja_movimientos(caja_id=caja_explicita)] == [mov_id]
    assert db_caja.get_caja_movimientos(caja_id=caja_del_turno) == []


def test_turno_sin_caja_sigue_cayendo_al_default(conn, crear_usuario):
    """El control de los cinco productos sin cajas múltiples: turno abierto
    sin `caja_id` (como siempre lo abren) sigue yendo a la default, igual que
    antes de este cambio."""
    uid = crear_usuario("cajero")
    default_id = db_caja.get_default_caja_id()

    tid = db_turnos.create_turno(uid, 0.0)
    assert db_turnos.get_turno(tid)["caja_id"] is None

    mov_id = db_caja.create_caja_movimiento(
        "2026-09-16", "ingreso", "Venta", 1000, turno_id=tid, conn=conn
    )
    conn.commit()

    assert [m["id"] for m in db_caja.get_caja_movimientos(caja_id=default_id)] == [mov_id]
