"""Tests para libracore.db.productos, en particular la distinción
producto/servicio (`productos.tipo`) agregada para que un profesional
pueda facturar servicios sin catálogo/stock — ver wiki/entities/libracommerce.md
para el contexto completo (empujado desde una necesidad real de Contalibra)."""
import pytest

from libracore.db import core
from libracore.db.schema import init_core_schema
from libracore.db import productos


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "productos_test.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def test_create_producto_defaults_to_producto(conn):
    pid = productos.create_producto("Yerba")
    assert productos.get_producto(pid)["tipo"] == "producto"


def test_create_producto_persists_servicio(conn):
    pid = productos.create_producto("Consulta profesional", tipo="servicio")
    assert productos.get_producto(pid)["tipo"] == "servicio"


def test_create_producto_rejects_invalid_tipo(conn):
    with pytest.raises(ValueError):
        productos.create_producto("Yerba", tipo="otro")


def test_update_producto_persists_tipo(conn):
    pid = productos.create_producto("Yerba")
    productos.update_producto(
        pid, nombre="Yerba", codigo="", descripcion="", precio_venta=0,
        precio_costo=0, unidad="u", categoria="", activo=1, tipo="servicio",
    )
    assert productos.get_producto(pid)["tipo"] == "servicio"


def test_update_producto_rejects_invalid_tipo(conn):
    pid = productos.create_producto("Yerba")
    with pytest.raises(ValueError):
        productos.update_producto(
            pid, nombre="Yerba", codigo="", descripcion="", precio_venta=0,
            precio_costo=0, unidad="u", categoria="", activo=1, tipo="otro",
        )


def test_get_stock_por_deposito_excludes_servicios(conn):
    deposito_id = productos.get_default_deposito_id()
    producto_id = productos.create_producto("Yerba", stock_minimo=1)
    servicio_id = productos.create_producto("Consulta", tipo="servicio", stock_minimo=1)

    listado = productos.get_stock_por_deposito(deposito_id)
    ids = {row["id"] for row in listado}
    assert producto_id in ids
    assert servicio_id not in ids


def test_get_all_productos_filters_by_tipo(conn):
    producto_id = productos.create_producto("Yerba")
    servicio_id = productos.create_producto("Consulta")
    productos.update_producto(
        servicio_id, nombre="Consulta", codigo="", descripcion="", precio_venta=0,
        precio_costo=0, unidad="u", categoria="", activo=1, tipo="servicio",
    )

    solo_productos = {p["id"] for p in productos.get_all_productos(tipo="producto")}
    solo_servicios = {p["id"] for p in productos.get_all_productos(tipo="servicio")}
    todos = {p["id"] for p in productos.get_all_productos()}

    assert solo_productos == {producto_id}
    assert solo_servicios == {servicio_id}
    assert todos == {producto_id, servicio_id}


def test_get_all_productos_rejects_invalid_tipo(conn):
    with pytest.raises(ValueError):
        productos.get_all_productos(tipo="otro")
