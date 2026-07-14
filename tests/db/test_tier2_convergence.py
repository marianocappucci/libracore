"""
Smoke tests de comportamiento para los 4 módulos Tier 2 migrados a
libracore.db (Fase 3, migración real) — clients, mp, facturas, productos.
A diferencia de Tier 1, estos módulos tenían divergencia real de
comportamiento entre Contalibra y Restolibra; la versión migrada a core es
la convergencia confirmada con el usuario (ver
wiki/entities/libracore.md, sección Tier 2).
"""
import pytest

from libracore.db import core
from libracore.db.schema import init_core_schema
from libracore.db import clients, mp, facturas, productos


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "tier2_test.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def test_clients_activar_cliente(conn):
    cid = clients.create_client("Cliente Uno", cuit_dni="20111111111")
    clients.desactivar_cliente(cid)
    assert clients.get_client(cid)["activo"] == 0
    clients.activar_cliente(cid)
    assert clients.get_client(cid)["activo"] == 1


def test_clients_rechaza_cuit_duplicado(conn):
    clients.create_client("Cliente Uno", cuit_dni="20-11111111-1")
    with pytest.raises(ValueError):
        clients.create_client("Cliente Dos (mismo CUIT)", cuit_dni="20111111111")


def test_clients_busqueda_normalizada(conn):
    clients.create_client("Cliente Mail", email="Test@Ejemplo.com")
    assert clients.get_client_by_email("test@ejemplo.com") is not None
    assert clients.get_client_by_email("TEST@EJEMPLO.COM") is not None

    # Duplicado de CUIT simulado como si viniera de datos preexistentes
    # (create_client ya rechaza duplicados nuevos — ver test de arriba); acá
    # se prueba que get_client_by_cuit desempata bien un duplicado ya en la
    # base, priorizando el activo y luego el más reciente.
    inactivo_id = clients.create_client("Cliente CUIT Inactivo", cuit_dni="20-22222222-2")
    clients.desactivar_cliente(inactivo_id)
    with conn as c:
        cur = c.execute(
            "INSERT INTO clients (name, cuit_dni, activo) VALUES (?,?,1)",
            ("Cliente CUIT Activo", "20222222222"),
        )
        activo_id = cur.lastrowid
    found = clients.get_client_by_cuit("20222222222")
    assert found["id"] == activo_id


def test_mp_alias_facturacion(conn):
    cid = clients.create_client("Cliente Alias", email="alias@x.com")
    mp.crear_alias_facturacion("email", "pagador@mp.com", cid)
    resuelto = mp.resolver_cliente_pago(payer_email="pagador@mp.com")
    assert resuelto["id"] == cid

    with pytest.raises(ValueError):
        mp.crear_alias_facturacion("email", "pagador@mp.com", cid)


def test_mp_resolver_sin_alias_matchea_por_email(conn):
    cid = clients.create_client("Cliente Directo", email="directo@x.com")
    resuelto = mp.resolver_cliente_pago(payer_email="directo@x.com")
    assert resuelto["id"] == cid


def test_facturas_numeracion_con_retry(conn):
    n = facturas.get_next_factura_numero(1, 1)
    fid = facturas.create_factura(
        1, 1, n, "2026-07-14", "20111111111", "Cliente Test", "RI",
        [{"nombre": "Item", "qty": 1, "precio": 100}], 100, 21, 121,
    )
    assert facturas.get_factura(fid)["numero"] == n


def test_productos_generar_codigo(conn):
    codigo1 = productos.generar_codigo_producto("Bebidas")
    assert codigo1 == "BEB-0001"
    productos.create_producto("Coca Cola", codigo=codigo1, categoria="Bebidas")
    codigo2 = productos.generar_codigo_producto("Bebidas")
    assert codigo2 == "BEB-0002"


def test_productos_estacion_vendible(conn):
    pid = productos.create_producto("Plato del día", estacion="cocina", vendible=1)
    p = productos.get_producto(pid)
    assert p["estacion"] == "cocina"
    assert p["vendible"] == 1

    productos.create_producto("Insumo interno", vendible=0)
    vendibles = productos.get_all_productos(solo_vendibles=True)
    assert all(p["vendible"] == 1 for p in vendibles)
