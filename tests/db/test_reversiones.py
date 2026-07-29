"""Deshacer el dinero de una venta.

Lo que ordena estos tests: **el fiado no se reversa por la caja**. Cuando un
pago fue a cuenta corriente nunca hubo plata en el cajón, así que su
reversión le baja la deuda al cliente en vez de generar un egreso. Si eso se
rompe, el arqueo cierra con plata que no existió.
"""
import pytest

from libracore.db import caja, clients, core, cuenta_corriente, reversiones
from libracore.db.schema import init_core_schema


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "reversiones.db"))
    c = core.get_connection()
    init_core_schema(c)
    # El commit va ANTES de tocar nada por otra conexión: el schema deja una
    # transacción abierta en ésta y `create_caja_config` abre la suya, así
    # que sin esto la base queda lockeada hasta el timeout.
    c.commit()
    cid = caja.create_caja_config("Caja 1", "", ["efectivo"])
    caja.set_default_caja(cid)
    yield c
    c.close()
    core._db_path = None


def _resumen():
    return caja.get_caja_resumen()


def test_anular_saca_de_la_caja_lo_que_habia_entrado(conn):
    caja.create_caja_movimiento("2026-07-28", "ingreso", "Venta V-1", 3000,
                                referencia="sale-1", medio_pago="efectivo")
    assert _resumen()["ingresos"] == 3000.0

    reversiones.revertir_cobro_venta(
        venta_id=1, numero="V-1", fecha="2026-07-28",
        pagos=[{"id": 10, "medio": "efectivo", "monto": 3000}],
    )

    resumen = _resumen()
    assert resumen["ingresos"] == 3000.0   # el ingreso original no se borra
    assert resumen["egresos"] == 3000.0    # se compensa con el egreso
    assert resumen["saldo_periodo"] == 0.0


def test_cada_medio_se_reversa_por_separado(conn):
    """En un cobro mixto la caja tiene que poder decir qué salió de dónde."""
    reversiones.revertir_cobro_venta(
        venta_id=1, numero="V-1", fecha="2026-07-28",
        pagos=[
            {"id": 1, "medio": "efectivo", "monto": 1000},
            {"id": 2, "medio": "transferencia", "monto": 2000},
        ],
    )

    movimientos = caja.get_caja_movimientos()
    egresos = [m for m in movimientos if m["tipo"] == "egreso"]
    assert len(egresos) == 2
    assert {m["medio_pago"] for m in egresos} == {"efectivo", "transferencia"}
    assert _resumen()["egresos"] == 3000.0


def test_el_fiado_baja_la_deuda_y_no_descuadra_el_arqueo(conn):
    """El corazón del asunto: por esa venta nunca entró plata al cajón.

    El movimiento de caja igual se registra — el historial tiene que mostrar
    la reversión completa — pero `get_caja_resumen()` filtra el medio
    `cuenta_corriente` de los totales, así que no descuadra el arqueo. Se
    probó omitir la fila y se descartó: desaparecía del listado que ve el
    usuario (ver el docstring de `revertir_cobro_venta`).
    """
    cliente = clients.create_client("Vecina del 12")
    cuenta_corriente.create_cc_debito(cliente, 2000.0, "2026-07-28", "Venta V-1", "sale-1")
    assert cuenta_corriente.get_cc_saldo(cliente) == 2000.0

    reversiones.revertir_cobro_venta(
        venta_id=1, numero="V-1", fecha="2026-07-28",
        pagos=[{"id": 1, "medio": "cuenta_corriente", "monto": 2000}],
        cliente_id=cliente,
    )

    assert cuenta_corriente.get_cc_saldo(cliente) == 0.0
    # El total de caja no se mueve...
    assert _resumen()["egresos"] == 0.0
    # ...pero la fila está, para que el historial no tenga un agujero.
    movimientos = caja.get_caja_movimientos()
    assert [m["medio_pago"] for m in movimientos] == ["cuenta_corriente"]


def test_un_cobro_mixto_con_parte_fiada_reversa_cada_parte_donde_va(conn):
    cliente = clients.create_client("Vecina del 12")
    cuenta_corriente.create_cc_debito(cliente, 2000.0, "2026-07-28", "Venta V-1", "sale-1")

    reversiones.revertir_cobro_venta(
        venta_id=1, numero="V-1", fecha="2026-07-28",
        pagos=[
            {"id": 1, "medio": "efectivo", "monto": 1000},
            {"id": 2, "medio": "cuenta_corriente", "monto": 2000},
        ],
        cliente_id=cliente,
    )

    # Sólo la parte que era plata de verdad cuenta para el arqueo.
    assert _resumen()["egresos"] == 1000.0
    assert cuenta_corriente.get_cc_saldo(cliente) == 0.0
    # Las dos filas quedan igual en el historial.
    assert len(caja.get_caja_movimientos()) == 2


def test_anular_dos_veces_no_saca_dos_veces_de_la_caja(conn):
    """Un reintento del botón no puede vaciar la caja."""
    pagos = [{"id": 1, "medio": "efectivo", "monto": 1000}]
    reversiones.revertir_cobro_venta(1, "V-1", "2026-07-28", pagos)
    reversiones.revertir_cobro_venta(1, "V-1", "2026-07-28", pagos)

    assert _resumen()["egresos"] == 1000.0


def test_la_reversion_de_dos_ventas_distintas_no_se_pisa(conn):
    # La idempotencia es por venta y pago, no global: dos ventas distintas
    # del mismo importe se reversan las dos.
    reversiones.revertir_cobro_venta(1, "V-1", "2026-07-28",
                                     [{"id": 1, "medio": "efectivo", "monto": 500}])
    reversiones.revertir_cobro_venta(2, "V-2", "2026-07-28",
                                     [{"id": 1, "medio": "efectivo", "monto": 500}])

    assert _resumen()["egresos"] == 1000.0


def test_el_fiado_sin_cliente_no_rompe(conn):
    # Una venta fiada sin cliente no debería existir, pero si aparece una
    # vieja, anularla tiene que funcionar igual en vez de reventar.
    reversiones.revertir_cobro_venta(
        1, "V-1", "2026-07-28",
        [{"id": 1, "medio": "cuenta_corriente", "monto": 500}],
        cliente_id=None,
    )

    assert _resumen()["egresos"] == 0.0
    assert len(caja.get_caja_movimientos()) == 1


def test_el_concepto_dice_de_que_venta_y_medio_es(conn):
    """El que arquea tiene que poder explicar el egreso sin abrir el sistema."""
    reversiones.revertir_cobro_venta(
        1, "POS-000123", "2026-07-28",
        [{"id": 1, "medio": "efectivo", "monto": 500}],
    )

    concepto = caja.get_caja_movimientos()[0]["concepto"]
    assert "POS-000123" in concepto
    assert "Efectivo" in concepto


# ── Devolución parcial ───────────────────────────────────────────────────────

def test_la_devolucion_reintegra_por_caja(conn):
    reversiones.reintegrar_devolucion(
        venta_id=1, numero="V-1", fecha="2026-07-28", monto=750.0,
    )

    assert _resumen()["egresos"] == 750.0
    assert "Devolución" in caja.get_caja_movimientos()[0]["concepto"]


def test_la_devolucion_de_algo_fiado_baja_la_deuda(conn):
    """Si la compra estaba fiada y todavía no se pagó, devolver no saca plata
    del cajón: descuenta lo que el cliente debe."""
    cliente = clients.create_client("Vecina del 12")
    cuenta_corriente.create_cc_debito(cliente, 3000.0, "2026-07-28", "Venta V-1", "sale-1")

    reversiones.reintegrar_devolucion(
        venta_id=1, numero="V-1", fecha="2026-07-28", monto=1000.0,
        medio_pago="cuenta_corriente", cliente_id=cliente,
    )

    assert cuenta_corriente.get_cc_saldo(cliente) == 2000.0
    assert _resumen()["egresos"] == 0.0


def test_dos_devoluciones_de_la_misma_venta_se_registran_las_dos(conn):
    """Devolver una cosa hoy y otra mañana son dos reintegros, no un
    duplicado: por eso la referencia la elige el producto."""
    reversiones.reintegrar_devolucion(1, "V-1", "2026-07-28", 500.0,
                                      referencia="devolucion:venta:1:1")
    reversiones.reintegrar_devolucion(1, "V-1", "2026-07-29", 300.0,
                                      referencia="devolucion:venta:1:2")

    assert _resumen()["egresos"] == 800.0


def test_repetir_la_misma_devolucion_no_reintegra_dos_veces(conn):
    reversiones.reintegrar_devolucion(1, "V-1", "2026-07-28", 500.0,
                                      referencia="devolucion:venta:1:1")
    reversiones.reintegrar_devolucion(1, "V-1", "2026-07-28", 500.0,
                                      referencia="devolucion:venta:1:1")

    assert _resumen()["egresos"] == 500.0
