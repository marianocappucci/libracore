"""Las altas de un nodo (ADR-077) contra PostgreSQL, que es el motor del nodo real.

En PostgreSQL el `INSERT` agrega `RETURNING id` para emular `lastrowid`, el `INSERT OR IGNORE` de `parties` se traduce a
`ON CONFLICT DO NOTHING` y `clients.id` es un `bigint` con una secuencia que no sabe nada de los ids negativos: nada de eso
se ve en SQLite. Se prueba que un id provisorio negativo entra a mano, que la secuencia de la base sigue su camino
(los clientes que el central crea después no se saltean ni repiten nada) y que el alta, el espejo y el gancho viven y
mueren juntos.
"""
import os

import pytest

from libracore import altas_en_nodo
from libracore.altas_en_nodo import AltasEnNodo
from libracore.db import clients, core
from libracore.db.schema import init_core_schema

_PARTIES = """
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


def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("LIBRACORE_POSTGRES_URL no está definida en CI — los tests contra PostgreSQL no se saltean acá")
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


@pytest.fixture
def conn():
    core.configure(_url())
    c = core.get_connection()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    init_core_schema(c)
    c.execute(_PARTIES)
    c.commit()
    yield c
    altas_en_nodo.configurar(None)
    c.close()
    core._db_path = None
    core._database_url = None


def _nodo(avisos, nodo="S02-pc1"):
    contador = iter(range(-1, -100, -1))
    altas_en_nodo.configurar(AltasEnNodo(
        nodo=lambda: nodo, id_provisorio=lambda c: next(contador),
        al_crear_cliente=lambda c, datos: avisos.append(datos)))


def test_el_alta_en_un_nodo_entra_con_id_negativo_y_su_party(conn):
    avisos = []
    _nodo(avisos)

    cid = clients.create_client("Ana", cuit_dni="20-1")
    otro = clients.crear_cliente({"name": "Beto"})

    assert (cid, otro) == (-1, -2)
    assert [tuple(f) for f in conn.execute("SELECT id, name FROM clients ORDER BY id DESC").fetchall()] == [
        (-1, "Ana"), (-2, "Beto")]
    assert [f[0] for f in conn.execute("SELECT id FROM parties ORDER BY id DESC").fetchall()] == [-1, -2]
    assert [a["cliente_id"] for a in avisos] == [-1, -2]


def test_la_secuencia_de_la_base_no_se_entera_de_los_provisorios(conn):
    """El central (o el espejo, que escribe ids explícitos del central) sigue su camino sin saltearse ni repetir."""
    _nodo([])
    clients.create_client("Provisorio")
    altas_en_nodo.configurar(None)                # el mismo proceso, ya sin nodo: la base asigna el suyo
    assert clients.create_client("Del central") == 1
    assert clients.create_client("Otro del central") == 2


def test_el_espejo_puede_traer_un_id_que_el_nodo_ya_tenia_como_provisorio_en_otro_signo(conn):
    """Un id del central (57) y uno provisorio (-57) son filas distintas: nunca chocan."""
    _nodo([])
    conn.execute("INSERT INTO clients (id, name) VALUES (57, 'Del central')")
    conn.commit()
    assert clients.create_client("Del nodo") == -1
    assert [f[0] for f in conn.execute("SELECT id FROM clients ORDER BY id").fetchall()] == [-1, 57]


def test_si_el_gancho_levanta_ni_el_cliente_ni_su_party_quedan(conn):
    def falla(c, datos):
        raise RuntimeError("el outbox falló")

    altas_en_nodo.configurar(AltasEnNodo(nodo=lambda: "S02-pc1", id_provisorio=lambda c: -1, al_crear_cliente=falla))
    with pytest.raises(RuntimeError, match="outbox"):
        clients.create_client("Ana")
    c = core.get_connection()
    try:
        assert c.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM parties").fetchone()[0] == 0
    finally:
        c.close()


def test_crear_cliente_con_conn_en_postgres(conn):
    """El central aplica el alta en la transacción del receptor: las dos cosas, o ninguna."""
    c = core.get_connection()
    cid = clients.crear_cliente({"name": "Ana", "cuit_dni": "20-31781916-2"}, conn=c)
    assert clients.get_client_by_cuit("20317819162", conn=c)["id"] == cid
    c.rollback()
    c.close()
    assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0
