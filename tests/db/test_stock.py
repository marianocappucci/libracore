"""
Smoke tests de comportamiento para libracore.db.stock (Fase 3, migración
real, Tier 2 — el módulo más delicado: `descontar_stock_venta` es
receta-aware vía un hook inyectado (`configure_resolver_receta`), no vía
import directo a un módulo de recetas (que no existe en Contalibra). Se
prueban los dos escenarios reales: sin resolver configurado (Contalibra)
y con resolver configurado (Restolibra) — ver wiki/entities/libracore.md.
"""
import pytest

from libracore.db import core
from libracore.db.schema import init_core_schema
from libracore.db import productos, stock


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "stock_test.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None
    core.configure_resolver_receta(None)


def test_descontar_stock_venta_sin_resolver_descuenta_producto(conn):
    pid = productos.create_producto("Bebida embotellada")
    stock.add_movimiento_stock(pid, "compra", 100)
    stock.descontar_stock_venta(None, [{"producto_id": pid, "qty": 3}])
    assert stock.get_stock_actual(pid) == 97.0


def test_descontar_stock_venta_con_resolver_descuenta_receta(conn):
    hamburguesa_id = productos.create_producto("Hamburguesa")
    pan_id = productos.create_producto("Pan")
    carne_id = productos.create_producto("Carne")
    stock.add_movimiento_stock(pan_id, "compra", 50)
    stock.add_movimiento_stock(carne_id, "compra", 50)

    def resolver_receta(producto_id):
        if producto_id == hamburguesa_id:
            return {
                "ingredientes": [
                    {"ingrediente_id": pan_id, "cantidad": 1},
                    {"ingrediente_id": carne_id, "cantidad": 1},
                ]
            }
        return None

    core.configure_resolver_receta(resolver_receta)
    stock.descontar_stock_venta(None, [{"producto_id": hamburguesa_id, "qty": 2}])

    assert stock.get_stock_actual(pan_id) == 48.0
    assert stock.get_stock_actual(carne_id) == 48.0
    # el producto vendido (hamburguesa) no tiene movimiento propio, solo insumos
    assert stock.get_stock_actual(hamburguesa_id) == 0.0


def test_descontar_stock_venta_con_resolver_pero_sin_receta_descuenta_producto(conn):
    embotellada_id = productos.create_producto("Gaseosa")
    stock.add_movimiento_stock(embotellada_id, "compra", 20)
    core.configure_resolver_receta(lambda pid: None)
    stock.descontar_stock_venta(None, [{"producto_id": embotellada_id, "qty": 5}])
    assert stock.get_stock_actual(embotellada_id) == 15.0


def test_descontar_stock_venta_con_modificadores(conn):
    hamburguesa_id = productos.create_producto("Hamburguesa")
    pan_id = productos.create_producto("Pan")
    queso_id = productos.create_producto("Queso")
    carne_id = productos.create_producto("Carne")
    for pid in (pan_id, queso_id, carne_id):
        stock.add_movimiento_stock(pid, "compra", 50)

    def resolver_receta(producto_id):
        return {
            "ingredientes": [
                {"ingrediente_id": pan_id, "cantidad": 1},
                {"ingrediente_id": queso_id, "cantidad": 1},
                {"ingrediente_id": carne_id, "cantidad": 1},
            ]
        }

    core.configure_resolver_receta(resolver_receta)
    import json
    modificadores = json.dumps([
        {"ingrediente_id": queso_id, "modo": "quitar"},
        {"ingrediente_id": carne_id, "modo": "doble"},
    ])
    stock.descontar_stock_venta(
        None, [{"producto_id": hamburguesa_id, "qty": 1, "modificadores": modificadores}]
    )
    assert stock.get_stock_actual(pan_id) == 49.0
    assert stock.get_stock_actual(queso_id) == 50.0  # "quitar" -> no se descuenta
    assert stock.get_stock_actual(carne_id) == 48.0   # "doble" -> se descuenta 2x


def test_descontar_stock_venta_no_genera_movimiento_para_servicio(conn):
    consulta_id = productos.create_producto("Consulta profesional", tipo="servicio")
    stock.descontar_stock_venta(None, [{"producto_id": consulta_id, "qty": 1}])
    assert stock.get_stock_actual(consulta_id) == 0.0
    assert stock.get_movimientos_stock(producto_id=consulta_id) == []


def test_descontar_stock_venta_servicio_con_resolver_no_descuenta_receta(conn):
    # Un servicio nunca tiene inventario propio, ni siquiera si por error
    # quedara configurada una receta para su id — el chequeo de tipo va
    # antes de resolver la receta.
    consulta_id = productos.create_producto("Consulta profesional", tipo="servicio")
    insumo_id = productos.create_producto("Insumo")
    stock.add_movimiento_stock(insumo_id, "compra", 10)

    core.configure_resolver_receta(
        lambda pid: {"ingredientes": [{"ingrediente_id": insumo_id, "cantidad": 1}]}
        if pid == consulta_id
        else None
    )
    stock.descontar_stock_venta(None, [{"producto_id": consulta_id, "qty": 1}])
    assert stock.get_stock_actual(insumo_id) == 10.0


def test_resumen_modificadores():
    import json
    modificadores = json.dumps([
        {"ingrediente_id": 1, "modo": "quitar", "ingrediente_nombre": "Cheddar"},
        {"ingrediente_id": 2, "modo": "doble", "ingrediente_nombre": "Medallón"},
    ])
    resumen = stock._resumen_modificadores(modificadores)
    assert resumen == "Sin Cheddar, Doble Medallón"
    assert stock._resumen_modificadores(None) == ""
    assert stock._resumen_modificadores("json invalido") == ""
