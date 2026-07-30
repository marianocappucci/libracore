"""
Smoke tests de comportamiento para libracore.db.ventas (Fase 3, migración
real, Tier 2 — último módulo del corte, ya idéntico entre productos). El
dominio más entrelazado: depende de caja, stock, turnos y cuenta
corriente, todos ya migrados. Se prueba el flujo transaccional completo
(alta con pagos/caja/stock/turno, anulación con reversión) — ver
wiki/entities/libracore.md.
"""
import pytest

from libracore.db import core
from libracore.db.schema import init_core_schema
from libracore.db import caja, turnos, productos, stock, ventas, facturas, remitos_presupuestos

# `crear_usuario` es una fixture de tests/db/conftest.py — reemplaza al
# `usuarios.create_usuario` que se fue con el modulo de auth (2026-07-30).


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "ventas_test.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def test_crear_venta_directa_con_pago_stock_y_turno(conn, crear_usuario):
    uid = crear_usuario("cajero1")
    caja.create_caja_config("Caja 1", "", ["efectivo"])
    turnos.create_turno(uid, 1000)
    pid = productos.create_producto("Producto Test Venta")
    stock.add_movimiento_stock(pid, "compra", 20)

    items = [{"producto_id": pid, "qty": 3, "precio": 100}]
    venta_id = ventas.crear_venta_directa(
        fecha="2026-07-14", items=items, subtotal=300, descuento=0, total=300,
        cliente_id=None, cliente_nombre="Consumidor Final", usuario_id=uid,
        observaciones="", estado="cobrada",
        pagos=[{"medio": "efectivo", "monto": 300}],
        stock_habilitado=True,
    )

    venta = ventas.get_venta(venta_id)
    assert venta["total"] == 300
    assert venta["pagos"][0]["medio"] == "efectivo"
    assert stock.get_stock_actual(pid) == 17.0
    assert caja.get_caja_resumen()["ingresos"] == 300.0
    turno = turnos.get_turno_activo(uid)
    assert turno is not None


def test_crear_venta_directa_numeros_correlativos(conn):
    caja.create_caja_config("Caja", "", ["efectivo"])
    v1 = ventas.crear_venta_directa(
        fecha="2026-07-14", items=[], subtotal=100, descuento=0, total=100,
        cliente_id=None, cliente_nombre="Cliente", usuario_id=None,
        observaciones="", estado="cobrada",
        pagos=[{"medio": "efectivo", "monto": 100}], stock_habilitado=False,
    )
    v2 = ventas.crear_venta_directa(
        fecha="2026-07-14", items=[], subtotal=200, descuento=0, total=200,
        cliente_id=None, cliente_nombre="Cliente", usuario_id=None,
        observaciones="", estado="cobrada",
        pagos=[{"medio": "efectivo", "monto": 200}], stock_habilitado=False,
    )
    n1 = int(ventas.get_venta(v1)["numero"].split("-")[-1])
    n2 = int(ventas.get_venta(v2)["numero"].split("-")[-1])
    assert n2 == n1 + 1


def test_anular_venta_repone_stock_y_revierte_caja(conn):
    caja.create_caja_config("Caja", "", ["efectivo"])
    pid = productos.create_producto("Producto Anulacion")
    stock.add_movimiento_stock(pid, "compra", 20)

    venta_id = ventas.crear_venta_directa(
        fecha="2026-07-14", items=[{"producto_id": pid, "qty": 5, "precio": 100}],
        subtotal=500, descuento=0, total=500,
        cliente_id=None, cliente_nombre="Cliente", usuario_id=None,
        observaciones="", estado="cobrada",
        pagos=[{"medio": "efectivo", "monto": 500}], stock_habilitado=True,
    )
    assert stock.get_stock_actual(pid) == 15.0
    assert caja.get_caja_resumen()["ingresos"] == 500.0

    ventas.anular_venta(venta_id)

    assert stock.get_stock_actual(pid) == 20.0
    resumen = caja.get_caja_resumen()
    assert resumen["ingresos"] == 500.0
    assert resumen["egresos"] == 500.0
    assert ventas.get_venta(venta_id)["estado"] == "anulada"


def test_anular_venta_ya_anulada_es_no_op(conn):
    caja.create_caja_config("Caja", "", ["efectivo"])
    venta_id = ventas.crear_venta_directa(
        fecha="2026-07-14", items=[], subtotal=100, descuento=0, total=100,
        cliente_id=None, cliente_nombre="Cliente", usuario_id=None,
        observaciones="", estado="cobrada",
        pagos=[{"medio": "efectivo", "monto": 100}], stock_habilitado=False,
    )
    ventas.anular_venta(venta_id)
    ventas.anular_venta(venta_id)  # no debe revertir dos veces
    resumen = caja.get_caja_resumen()
    assert resumen["egresos"] == 100.0


def test_vincular_venta_factura_y_remito(conn):
    caja.create_caja_config("Caja", "", ["efectivo"])
    venta_id = ventas.crear_venta_directa(
        fecha="2026-07-14", items=[], subtotal=100, descuento=0, total=100,
        cliente_id=None, cliente_nombre="Cliente", usuario_id=None,
        observaciones="", estado="cobrada",
        pagos=[{"medio": "efectivo", "monto": 100}], stock_habilitado=False,
    )
    factura_id = facturas.create_factura(
        1, 1, 1, "2026-07-14", "20111111111", "Cliente Test", "RI",
        [{"nombre": "Item", "qty": 1, "precio": 100}], 100, 21, 121,
    )
    remito_id = remitos_presupuestos.create_remito(
        remitos_presupuestos.get_next_remito_number(), "2026-07-14", None, "Cliente Test",
        "Calle 123", "20111111111", "r@x.com", "", [{"nombre": "Item", "qty": 1, "precio": 100}],
        100, 0.21, 21, 121,
    )
    ventas.vincular_venta_factura(venta_id, factura_id)
    ventas.vincular_venta_remito(venta_id, remito_id)
    v = ventas.get_venta(venta_id)
    assert v["factura_id"] == factura_id
    assert v["remito_id"] == remito_id
