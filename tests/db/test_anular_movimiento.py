"""Anular un movimiento de caja: la fila queda, el arqueo no la cuenta.

🔴 **Un movimiento de caja se ANULA, no se borra.** Antes del 2026-08-28 la
unica forma era `delete_caja_movimiento`, y borrar deja un agujero que nadie
puede auditar. En [[libraclub]] rompia mas: un cobro de turno borrado hace que la
reserva vuelva a figurar impaga, y un cobro por QR queda con
`caja_movimiento_id` colgando --- con lo cual el poll no lo vuelve a registrar y
la plata desaparece del cajon para siempre.

Lo pidio el humano mirando la pantalla: *"no deberian poder borrarse, tienen que
quedar registrados"*.
"""

from __future__ import annotations

import pytest

from libracore.db import caja as db_caja
from libracore.db import core
from libracore.db import dashboard as db_dashboard
from libracore.db import reportes as db_reportes
from libracore.db import turnos as db_turnos
from libracore.db.schema import init_core_schema


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "anular.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.execute(
        "INSERT INTO usuarios (username, nombre, password_hash, role)"
        " VALUES ('ana', 'Ana', 'x', 'admin')"
    )
    c.commit()
    yield c
    c.close()
    core._db_path = None


FECHA = "2026-08-28"


def _turno(conn) -> int:
    return db_turnos.create_turno(1, 1000, "")


def test_anular_deja_la_fila_y_la_saca_del_arqueo(conn):
    """Las dos mitades a la vez, que es lo que hace util a `anulado`."""
    tid = _turno(conn)
    uno = db_caja.create_caja_movimiento(
        FECHA, "ingreso", "Turno cancha 1", 14000, medio_pago="efectivo", turno_id=tid,
    )
    db_caja.create_caja_movimiento(
        FECHA, "ingreso", "Buffet", 1200, medio_pago="efectivo", turno_id=tid,
    )

    antes = db_turnos.get_resumen_turno_caja(tid)
    assert antes["pagos_por_medio"]["efectivo"] == 15200, "el control del total"
    assert len(antes["movimientos"]) == 2

    db_caja.anular_caja_movimiento(uno)

    despues = db_turnos.get_resumen_turno_caja(tid)
    # 🔑 La fila QUEDA: una lista que esconde los anulados no se distingue de
    # una que los borra, y era eso lo que se venia a arreglar.
    assert len(despues["movimientos"]) == 2, "el movimiento anulado tiene que seguir estando"
    assert [m["anulado"] for m in despues["movimientos"]] == [1, 0]
    # Y sale de los totales: el arqueo tiene que dar lo que hay en el cajon.
    assert despues["pagos_por_medio"]["efectivo"] == 1200


def test_anular_dos_veces_deja_lo_mismo(conn):
    """Idempotente. Un doble click en el boton no puede restar dos veces."""
    tid = _turno(conn)
    uno = db_caja.create_caja_movimiento(
        FECHA, "ingreso", "Turno", 5000, medio_pago="efectivo", turno_id=tid,
    )
    db_caja.anular_caja_movimiento(uno)
    db_caja.anular_caja_movimiento(uno)
    resumen = db_turnos.get_resumen_turno_caja(tid)
    assert resumen["pagos_por_medio"].get("efectivo", 0) == 0
    assert len(resumen["movimientos"]) == 1


def test_un_egreso_anulado_tampoco_resta(conn):
    """El control del signo.

    Los egresos entran al total en negativo. Si `anulado` se filtrara solo en la
    rama de los ingresos, anular un egreso lo dejaria restando --- y el esperado
    en el cajon daria de menos sin que nada lo explique.
    """
    tid = _turno(conn)
    db_caja.create_caja_movimiento(
        FECHA, "ingreso", "Turno", 10000, medio_pago="efectivo", turno_id=tid,
    )
    egreso = db_caja.create_caja_movimiento(
        FECHA, "egreso", "Retiro a banco", 4000, medio_pago="efectivo", turno_id=tid,
    )
    assert db_turnos.get_resumen_turno_caja(tid)["pagos_por_medio"]["efectivo"] == 6000

    db_caja.anular_caja_movimiento(egreso)
    assert db_turnos.get_resumen_turno_caja(tid)["pagos_por_medio"]["efectivo"] == 10000


def test_los_movimientos_nacen_sin_anular(conn):
    """El control del default.

    Sin esto, una columna que naciera en 1 haria pasar los tests de arriba --- y
    dejaria todos los movimientos fuera del arqueo desde el minuto cero.
    """
    tid = _turno(conn)
    db_caja.create_caja_movimiento(
        FECHA, "ingreso", "Turno", 7000, medio_pago="efectivo", turno_id=tid,
    )
    resumen = db_turnos.get_resumen_turno_caja(tid)
    assert resumen["movimientos"][0]["anulado"] == 0
    assert resumen["pagos_por_medio"]["efectivo"] == 7000


# -- El barrido, medido por conducta ---------------------------------------
#
# El guard de `test_barrido_de_anulados.py` lee los fuentes y dice que el filtro
# ESTA. Estos dicen que FUNCIONA, que no es lo mismo: un filtro escrito sobre la
# columna equivocada, o con el alias mal, pasa el guard y no filtra nada.


def _con_un_anulado(conn):
    """Dos movimientos de 1000, uno anulado. Lo que cuente tiene que dar 1000."""
    tid = _turno(conn)
    uno = db_caja.create_caja_movimiento(
        FECHA, "ingreso", "Se anula", 1000, medio_pago="efectivo", turno_id=tid,
    )
    db_caja.create_caja_movimiento(
        FECHA, "ingreso", "Queda", 1000, medio_pago="efectivo", turno_id=tid,
    )
    db_caja.anular_caja_movimiento(uno)
    return tid


def test_el_resumen_de_caja_no_cuenta_el_anulado(conn):
    """`get_caja_resumen` es el arqueo que ven Contalibra y Restolibra."""
    _con_un_anulado(conn)
    r = db_caja.get_caja_resumen(FECHA, FECHA)
    assert r["ingresos"] == 1000, f'conto el anulado: {r["ingresos"]}'
    assert r["saldo_total"] == 1000


def test_el_reporte_por_tipo_no_cuenta_el_anulado(conn):
    _con_un_anulado(conn)
    filas = {f["tipo"]: f for f in db_reportes.get_reporte_caja(FECHA, FECHA)}
    assert filas["ingreso"]["total"] == 1000
    # Y la CANTIDAD tambien: dos filas, una anulada, cuenta una.
    assert filas["ingreso"]["cantidad"] == 1


def test_el_reporte_por_medio_no_cuenta_el_anulado(conn):
    _con_un_anulado(conn)
    filas = db_reportes.get_reporte_caja_medios(FECHA, FECHA)
    total = sum(f["total"] for f in filas if f["tipo"] == "ingreso")
    assert total == 1000


def test_el_tablero_no_cuenta_el_anulado(conn):
    """Los KPI del mes y el saldo historico."""
    _con_un_anulado(conn)
    d = db_dashboard.get_dashboard_data(FECHA, FECHA)
    assert d["saldo_total"] == 1000, f'el saldo conto el anulado: {d["saldo_total"]}'


def test_la_lista_del_operador_SI_muestra_el_anulado(conn):
    """El control de la regla: los numeros filtran, la lista no.

    Sin esto, el barrido se podria "arreglar" filtrando en todos lados --- y
    entonces anular volveria a ser indistinguible de borrar, que es el defecto
    del que salimos.
    """
    _con_un_anulado(conn)
    filas = db_caja.get_caja_movimientos(FECHA, FECHA)
    assert len(filas) == 2, "la lista tiene que traer los dos"
    assert sorted(f["anulado"] for f in filas) == [0, 1]
