"""El espejo `clients` -> `parties` (LibraCommerce).

Contexto del bug que motiva estos tests (2026-07-30): `sales.customer_party_id`
tiene FK a `parties`, pero los clientes de Contalibra/Restolibra viven en
`clients`. La migracion P7 creo los parties espejo de los clientes de entonces
y nada volvio a crearlos, asi que el primer cliente dado de alta despues de P7
no tenia party y venderle fallaba con FOREIGN KEY constraint. Lo encontro la
suite nueva de Contalibra.

`parties` es de LibraCommerce, no de LibraCore: la mitad de los productos no la
tiene. Por eso hay tests de los dos escenarios — con la tabla y sin ella.
"""
import pytest

from libracore.db import clients, core
from libracore.db.schema import init_core_schema

# Subconjunto de la definicion real de LibraCommerce (libracommerce/db/schema.py):
# LibraCore no puede importar LibraCommerce, asi que los tests la declaran igual
# que el motor que la crea de verdad.
_PARTIES_DDL = """
    CREATE TABLE parties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        party_type TEXT NOT NULL,
        display_name TEXT NOT NULL,
        legal_name TEXT,
        tax_id TEXT,
        tax_id_type TEXT,
        email TEXT,
        phone TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
"""


@pytest.fixture
def conn_sin_parties(tmp_path):
    """Producto sin LibraCommerce (Gestiolibra, MedLibra, LibraDesk)."""
    core.configure(db_path=str(tmp_path / "sin_parties.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


@pytest.fixture
def conn_con_parties(tmp_path):
    """Producto con LibraCommerce (Contalibra, Restolibra, VentaLibra)."""
    core.configure(db_path=str(tmp_path / "con_parties.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.execute(_PARTIES_DDL)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def _party(conn, pid):
    row = conn.execute("SELECT * FROM parties WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


def test_alta_crea_el_party_con_el_mismo_id(conn_con_parties):
    cid = clients.create_client("Almacen Don Pepe", cuit_dni="20111111111",
                                email="pepe@test.com", phone="221-555")
    party = _party(conn_con_parties, cid)
    assert party is not None, "el alta no creo el party espejo"
    assert party["id"] == cid
    assert party["display_name"] == "Almacen Don Pepe"
    assert party["tax_id"] == "20111111111"
    assert party["party_type"] == "person"
    assert party["active"] == 1


def test_sin_libracommerce_el_alta_funciona_igual(conn_sin_parties):
    """LibraCore no puede asumir que existe `parties`: los productos que no
    tienen LibraCommerce deben poder dar de alta clientes sin ningun error."""
    cid = clients.create_client("Cliente sin commerce")
    assert clients.get_client(cid)["name"] == "Cliente sin commerce"


def test_editar_el_cliente_actualiza_el_party(conn_con_parties):
    cid = clients.create_client("Nombre viejo", email="viejo@test.com")
    clients.update_client(cid, name="Nombre nuevo", email="nuevo@test.com")
    party = _party(conn_con_parties, cid)
    assert party["display_name"] == "Nombre nuevo"
    assert party["email"] == "nuevo@test.com"


def test_desactivar_y_activar_se_reflejan_en_el_party(conn_con_parties):
    cid = clients.create_client("Intermitente")
    clients.desactivar_cliente(cid)
    assert _party(conn_con_parties, cid)["active"] == 0
    clients.activar_cliente(cid)
    assert _party(conn_con_parties, cid)["active"] == 1


def test_backfill_crea_los_espejos_que_faltan(conn_con_parties):
    """El caso real: clientes que ya existian sin party (los creados entre
    P7 y este fix)."""
    # Los dos altas primero: entre un DELETE sin commitear y el proximo
    # create_client (que abre su propia conexion) SQLite da "database is
    # locked" -- el estado a simular se arma despues, de una sola vez.
    cid = clients.create_client("Con espejo")
    huerfano = clients.create_client("Sin espejo")
    conn_con_parties.execute("DELETE FROM parties WHERE id IN (?, ?)", (cid, huerfano))
    conn_con_parties.commit()

    creados = clients.sincronizar_parties_de_clientes()
    assert creados == 2
    assert _party(conn_con_parties, cid)["display_name"] == "Con espejo"
    assert _party(conn_con_parties, huerfano)["display_name"] == "Sin espejo"


def test_backfill_es_idempotente(conn_con_parties):
    clients.create_client("Uno")
    clients.create_client("Dos")
    assert clients.sincronizar_parties_de_clientes() == 0
    assert clients.sincronizar_parties_de_clientes() == 0


def test_backfill_sin_parties_no_falla(conn_sin_parties):
    clients.create_client("Cliente")
    assert clients.sincronizar_parties_de_clientes() == 0


def test_backfill_no_pisa_un_party_existente(conn_con_parties):
    """Un party ajeno con ese id (ej. migrado a mano) no se sobreescribe."""
    conn_con_parties.execute(
        "INSERT INTO parties (id, party_type, display_name) VALUES (1, 'organization', 'Ya existia')"
    )
    conn_con_parties.commit()
    cid = clients.create_client("Cliente nuevo")
    if cid == 1:
        assert _party(conn_con_parties, 1)["display_name"] == "Ya existia"


def test_el_party_permite_vender_al_cliente(conn_con_parties):
    """La prueba de fondo: con la FK activa, insertar una venta que
    referencia al cliente recien creado no explota. Es exactamente el
    INSERT que hacia `crear_venta_directa` y fallaba."""
    conn_con_parties.execute("""
        CREATE TABLE sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT NOT NULL UNIQUE,
            customer_party_id INTEGER REFERENCES parties(id),
            total NUMERIC NOT NULL DEFAULT 0
        )
    """)
    conn_con_parties.execute("PRAGMA foreign_keys=ON")
    cid = clients.create_client("Comprador")
    conn_con_parties.execute(
        "INSERT INTO sales (number, customer_party_id, total) VALUES ('0001', ?, 100)",
        (cid,),
    )
    conn_con_parties.commit()
    assert conn_con_parties.execute(
        "SELECT customer_party_id FROM sales WHERE number='0001'"
    ).fetchone()[0] == cid
