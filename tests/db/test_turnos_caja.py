"""
Arqueo de turno calculado sobre `caja_movimientos` en vez de sobre `ventas`
(ver libracore.db.turnos.get_resumen_turno_caja).

Es la variante que necesita un producto cuyas ventas no viven en la tabla
`ventas` de LibraCore -- VentaLibra las tiene en LibraCommerce, y con el
resumen viejo su arqueo daba siempre cero.
"""
import pytest

from libracore.db import caja, core, turnos
from libracore.db.schema import init_core_schema


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "turnos_caja_test.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


@pytest.fixture
def turno_abierto(crear_usuario):
    """Antes era una funcion de modulo que llamaba a
    `usuarios.create_usuario`; pasa a fixture porque el alta de usuario ahora
    la resuelve el helper de conftest (el modulo de auth salio de LibraCore el
    2026-07-30, ver tests/db/conftest.py)."""
    def _abrir(monto_inicial=5000.0):
        return turnos.create_turno(crear_usuario("cajero"), monto_inicial)

    return _abrir


def test_caja_movimientos_acepta_turno_id(conn, turno_abierto):
    tid = turno_abierto()
    mid = caja.create_caja_movimiento(
        "2026-07-28", "ingreso", "Venta POS-1", 1500, referencia="sale-1-efectivo",
        medio_pago="efectivo", turno_id=tid,
    )
    fila = conn.execute("SELECT turno_id FROM caja_movimientos WHERE id=?", (mid,)).fetchone()
    assert fila["turno_id"] == tid


def test_movimiento_sin_turno_sigue_siendo_valido(conn):
    """Un ajuste o un egreso fuera de caja no pertenece a ningun turno."""
    mid = caja.create_caja_movimiento("2026-07-28", "egreso", "Compra insumos", 800)
    fila = conn.execute("SELECT turno_id FROM caja_movimientos WHERE id=?", (mid,)).fetchone()
    assert fila["turno_id"] is None


def test_resumen_agrupa_por_medio_de_pago(conn, turno_abierto):
    tid = turno_abierto()
    caja.create_caja_movimiento("2026-07-28", "ingreso", "V1", 1000, referencia="s1-efectivo",
                                medio_pago="efectivo", turno_id=tid)
    caja.create_caja_movimiento("2026-07-28", "ingreso", "V2", 2500, referencia="s2-efectivo",
                                medio_pago="efectivo", turno_id=tid)
    caja.create_caja_movimiento("2026-07-28", "ingreso", "V3", 4000, referencia="s3-tarjeta",
                                medio_pago="tarjeta_debito", turno_id=tid)

    resumen = turnos.get_resumen_turno_caja(tid)

    assert resumen["pagos_por_medio"] == {"efectivo": 3500.0, "tarjeta_debito": 4000.0}
    assert resumen["total_ventas"] == 7500.0
    assert resumen["efectivo_ventas"] == 3500.0
    assert len(resumen["movimientos"]) == 3


def test_resumen_ignora_los_movimientos_de_otro_turno(conn, turno_abierto):
    primero = turno_abierto()
    caja.create_caja_movimiento("2026-07-28", "ingreso", "V1", 1000, referencia="s1",
                                medio_pago="efectivo", turno_id=primero)
    turnos.cerrar_turno_caja(primero, 6000.0)

    segundo = turnos.create_turno(1, 2000.0)
    caja.create_caja_movimiento("2026-07-28", "ingreso", "V2", 700, referencia="s2",
                                medio_pago="efectivo", turno_id=segundo)

    assert turnos.get_resumen_turno_caja(primero)["efectivo_ventas"] == 1000.0
    assert turnos.get_resumen_turno_caja(segundo)["efectivo_ventas"] == 700.0


def test_egreso_resta_en_el_arqueo(conn, turno_abierto):
    """Sacar plata de la caja durante el turno baja lo que se espera contar."""
    tid = turno_abierto()
    caja.create_caja_movimiento("2026-07-28", "ingreso", "V1", 5000, referencia="s1",
                                medio_pago="efectivo", turno_id=tid)
    caja.create_caja_movimiento("2026-07-28", "egreso", "Pago flete", 1200, referencia="e1",
                                medio_pago="efectivo", turno_id=tid)

    assert turnos.get_resumen_turno_caja(tid)["efectivo_ventas"] == 3800.0


def test_cierre_calcula_el_esperado_como_inicial_mas_efectivo(conn, turno_abierto):
    tid = turno_abierto(monto_inicial=5000.0)
    caja.create_caja_movimiento("2026-07-28", "ingreso", "V1", 3000, referencia="s1",
                                medio_pago="efectivo", turno_id=tid)
    # la tarjeta no entra al cajon: no cuenta para el arqueo de efectivo
    caja.create_caja_movimiento("2026-07-28", "ingreso", "V2", 9000, referencia="s2",
                                medio_pago="tarjeta_debito", turno_id=tid)

    cerrado = turnos.cerrar_turno_caja(tid, monto_declarado=8000.0, notas="cierre normal")

    assert cerrado["estado"] == "cerrado"
    assert cerrado["monto_esperado_cierre"] == 8000.0
    assert cerrado["monto_declarado_cierre"] == 8000.0
    assert cerrado["cierre"] is not None


def test_cierre_deja_registrada_la_diferencia(conn, turno_abierto):
    """Faltante real: se declara menos de lo esperado y el turno lo conserva
    para que se pueda auditar despues."""
    tid = turno_abierto(monto_inicial=1000.0)
    caja.create_caja_movimiento("2026-07-28", "ingreso", "V1", 4000, referencia="s1",
                                medio_pago="efectivo", turno_id=tid)

    cerrado = turnos.cerrar_turno_caja(tid, monto_declarado=4500.0)

    assert cerrado["monto_esperado_cierre"] == 5000.0
    assert cerrado["monto_declarado_cierre"] == 4500.0
    assert cerrado["monto_esperado_cierre"] - cerrado["monto_declarado_cierre"] == 500.0


def test_cerrar_un_turno_inexistente_no_explota(conn):
    assert turnos.cerrar_turno_caja(9999, 100.0) is None


def test_turno_cerrado_ya_no_figura_como_activo(conn, crear_usuario):
    uid = crear_usuario("cajero2")
    tid = turnos.create_turno(uid, 1000.0)
    assert turnos.get_turno_activo(uid) is not None

    turnos.cerrar_turno_caja(tid, 1000.0)

    assert turnos.get_turno_activo(uid) is None
