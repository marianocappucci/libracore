"""Las altas de clientes en un nodo offline (ADR-077): id provisorio negativo y gancho dentro de la transacción.

Lo que se fija:

- **fuera de un nodo no cambia nada** (sin configurar, o con un `nodo()` que contesta `None`): ids de la base, sin avisar;
- **en un nodo** los tres caminos que insertan en `clients` (`create_client`, `crear_cliente`, `resolver_cliente_externo`)
  guardan al cliente con un id negativo —y a su `parties`— y avisan **una vez**, con la conexión del alta, **adentro de su
  transacción** (lo prueba una segunda conexión que todavía no ve la fila) y, si el gancho levanta, **el alta no queda**;
- los ids provisorios bajan y no se reusan;
- `crear_cliente(conn=)` y `get_client_by_cuit(conn=)`: lo que necesita el central para aplicar el alta de un nodo;
- `REFERENCIAS_DE_CLIENTES` cubre toda columna del schema que guarda un cliente sin clave foránea.
"""

from __future__ import annotations

import re

import pytest

from libracore import altas_en_nodo, auditoria
from libracore.altas_en_nodo import AltasEnNodo
from libracore.db import clients, core

NODO = "S02-pc1"

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


@pytest.fixture(autouse=True)
def _sin_configuracion():
    """La configuración es global del proceso: ningún test hereda la del anterior ni se la deja al siguiente."""
    altas_en_nodo.configurar(None)
    yield
    altas_en_nodo.configurar(None)


@pytest.fixture
def db(tmp_path, crear_schema):
    core.configure(db_path=str(tmp_path / "nodo.db"))
    conn = core.get_connection()
    crear_schema(conn)
    conn.execute(_PARTIES_DDL)
    conn.commit()
    conn.close()
    yield
    core._db_path = None


class Nodo:
    """Un nodo de mentira: el contador de ids provisorios y el gancho, que anota qué se llamó, con qué conexión y si la
    fila ya se veía desde una conexión ajena (la prueba de que corre dentro de la transacción del alta)."""

    def __init__(self, nodo: str | None = NODO, levantar: bool = False):
        self.nodo = nodo
        self.levantar = levantar
        self.ultimo = 0
        self.llamadas: list[dict] = []
        self.conexiones: list[object] = []
        self.visibles_desde_afuera: list[int] = []

    def id_provisorio(self, conn) -> int:
        self.ultimo -= 1
        return self.ultimo

    def al_crear_cliente(self, conn, datos: dict) -> None:
        self.llamadas.append(datos)
        self.conexiones.append(conn)
        ajena = core.get_connection()
        try:
            self.visibles_desde_afuera.append(ajena.execute("SELECT COUNT(*) FROM clients").fetchone()[0])
        finally:
            ajena.close()
        if self.levantar:
            raise RuntimeError("el outbox falló")

    def instalar(self) -> Nodo:
        altas_en_nodo.configurar(AltasEnNodo(
            nodo=lambda: self.nodo, id_provisorio=self.id_provisorio, al_crear_cliente=self.al_crear_cliente))
        return self


def _filas(sql, params=()):
    conn = core.get_connection()
    try:
        return [dict(f) for f in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ── fuera de un nodo no cambia nada ─────────────────────────────────────────

def test_sin_configurar_las_altas_son_las_de_siempre(db):
    cid = clients.create_client("Ana", cuit_dni="20-1")
    otro = clients.crear_cliente({"name": "Beto"})
    externo = clients.resolver_cliente_externo("ext-1", "Cora")
    assert [cid, otro, externo] == [1, 2, 3]
    assert [f["id"] for f in _filas("SELECT id FROM parties ORDER BY id")] == [1, 2]   # el externo no crea party


def test_configurado_pero_fuera_de_un_nodo_no_asigna_ids_ni_avisa(db):
    """El central: la misma configuración, un `nodo()` que contesta `None`. Es el caso de producción."""
    nodo = Nodo(nodo=None).instalar()
    cid = clients.create_client("Ana")
    clients.crear_cliente({"name": "Beto"})
    clients.resolver_cliente_externo("ext-1", "Cora")
    assert cid == 1
    assert nodo.llamadas == [] and nodo.ultimo == 0, "ni siquiera pidió un id provisorio"
    assert altas_en_nodo.en_un_nodo() is False and altas_en_nodo.nodo_actual() is None


def test_un_nodo_con_nombre_vacio_no_es_un_nodo(db):
    Nodo(nodo="").instalar()
    assert clients.create_client("Ana") == 1


# ── en un nodo ──────────────────────────────────────────────────────────────

def test_create_client_en_un_nodo_usa_un_id_negativo_y_su_party(db):
    nodo = Nodo().instalar()
    cid = clients.create_client("Ana", address="Calle 1", cuit_dni="20-1", email="a@x.com", phone="221", iva_condition="CF")

    assert cid == -1
    assert clients.get_client(-1)["name"] == "Ana"
    assert _filas("SELECT id, display_name, tax_id FROM parties") == [{"id": -1, "display_name": "Ana", "tax_id": "20-1"}]
    assert altas_en_nodo.en_un_nodo() and altas_en_nodo.nodo_actual() == NODO
    assert len(nodo.llamadas) == 1


def test_el_gancho_recibe_el_cliente_con_su_id_provisorio(db):
    nodo = Nodo().instalar()
    clients.create_client("Ana", address="Calle 1", cuit_dni="20-1", email="a@x.com", phone="221", iva_condition="CF")
    datos = nodo.llamadas[0]
    assert datos == {
        "version": altas_en_nodo.VERSION, "cliente_id": -1, "name": "Ana", "address": "Calle 1", "cuit_dni": "20-1",
        "email": "a@x.com", "phone": "221", "iva_condition": "CF", "external_ref": None, "activo": 1,
        "empresa": "", "ciudad": "", "observaciones": "", "tipo_facturacion": "por_servicio",
    }
    assert "id" not in datos and "created_at" not in datos and "auto_facturar" not in datos


def test_los_ids_provisorios_bajan_y_no_se_reusan(db):
    Nodo().instalar()
    ids = [clients.create_client(f"C{n}") for n in range(3)]
    assert ids == [-1, -2, -3]
    clients.delete_client(-2)
    assert clients.create_client("otro") == -4


def test_los_clientes_del_central_que_baja_el_espejo_no_chocan_con_los_provisorios(db):
    """Lo que motivo todo: el nodo creaba el 1 y el espejo traia el 1 del central. Con negativos conviven."""
    Nodo().instalar()
    conn = core.get_connection()
    conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Del central'), (2, 'Otro del central')")
    conn.commit()
    conn.close()
    nuevo = clients.create_client("Del nodo")
    assert nuevo < 0
    assert sorted(f["id"] for f in _filas("SELECT id FROM clients")) == [nuevo, 1, 2]


def test_crear_cliente_en_un_nodo(db):
    nodo = Nodo().instalar()
    cid = clients.crear_cliente({"name": "Beto", "cuit_dni": "20-2", "activo": 0})
    assert cid == -1
    assert _filas("SELECT id, name, activo FROM clients") == [{"id": -1, "name": "Beto", "activo": 0}]
    assert _filas("SELECT id, active FROM parties") == [{"id": -1, "active": 0}]
    assert nodo.llamadas[0]["cliente_id"] == -1 and nodo.llamadas[0]["activo"] == 0


def test_crear_cliente_sigue_sin_aceptar_un_id_ajeno(db):
    Nodo().instalar()
    with pytest.raises(ValueError, match="id"):
        clients.crear_cliente({"id": 5, "name": "Beto"})
    assert _filas("SELECT COUNT(*) AS n FROM clients") == [{"n": 0}]


def test_resolver_cliente_externo_en_un_nodo(db):
    nodo = Nodo().instalar()
    cid = clients.resolver_cliente_externo("ext-1", "Cora", cuit_dni="20-3")
    assert cid == -1
    assert nodo.llamadas[0]["external_ref"] == "ext-1"
    # La segunda vez es el mismo cliente: no hay alta, no se avisa.
    assert clients.resolver_cliente_externo("ext-1", "Cora Maria") == -1
    assert len(nodo.llamadas) == 1


def test_editar_desactivar_y_borrar_no_son_altas(db):
    nodo = Nodo().instalar()
    cid = clients.create_client("Ana")
    clients.update_client(cid, name="Ana M")
    clients.desactivar_cliente(cid)
    clients.activar_cliente(cid)
    assert len(nodo.llamadas) == 1, "sólo el alta sube (la edición en el nodo no es parte de esta decisión)"


# ── adentro de la transacción ───────────────────────────────────────────────

def test_el_gancho_corre_adentro_de_la_transaccion_del_alta(db):
    """Una segunda conexión no ve la fila todavía: el gancho corre antes del commit del alta."""
    nodo = Nodo().instalar()
    clients.create_client("Ana")
    assert nodo.visibles_desde_afuera == [0]
    assert _filas("SELECT COUNT(*) AS n FROM clients") == [{"n": 1}], "y después del alta sí está"


def test_si_el_gancho_levanta_el_alta_no_queda(db):
    """Un cliente que se da de alta en el nodo y no sube no es «a medias»: el central no lo ve."""
    Nodo(levantar=True).instalar()
    with pytest.raises(RuntimeError, match="outbox"):
        clients.create_client("Ana")
    with pytest.raises(RuntimeError, match="outbox"):
        clients.crear_cliente({"name": "Beto"})
    with pytest.raises(RuntimeError, match="outbox"):
        clients.resolver_cliente_externo("ext-1", "Cora")
    assert _filas("SELECT COUNT(*) AS n FROM clients") == [{"n": 0}]
    assert _filas("SELECT COUNT(*) AS n FROM parties") == [{"n": 0}]


def test_el_gancho_recibe_la_conexion_del_alta(db):
    nodo = Nodo().instalar()
    clients.create_client("Ana")
    assert len(nodo.conexiones) == 1 and nodo.conexiones[0] is not None


def test_el_gancho_corre_despues_de_la_auditoria(db):
    """Va al final: si el registrador falla, no se avisa de un alta que no quedó."""
    orden = []
    nodo = Nodo().instalar()
    original = nodo.al_crear_cliente

    def gancho(conn, datos):
        orden.append("gancho")
        original(conn, datos)

    altas_en_nodo.configurar(AltasEnNodo(nodo=lambda: NODO, id_provisorio=nodo.id_provisorio, al_crear_cliente=gancho))
    auditoria.configurar_auditoria(lambda conn, cambio: orden.append("auditoria"))
    try:
        clients.create_client("Ana")
    finally:
        auditoria.configurar_auditoria(None)
    assert orden == ["auditoria", "gancho"]


# ── lo que necesita el central ──────────────────────────────────────────────

def test_crear_cliente_con_conn_deja_el_commit_a_quien_llama(db):
    conn = core.get_connection()
    cid = clients.crear_cliente({"name": "Ana", "cuit_dni": "20-1"}, conn=conn)
    ajena = core.get_connection()
    assert ajena.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0, "sin commitear no se ve"
    ajena.close()
    conn.rollback()
    conn.close()
    assert _filas("SELECT COUNT(*) AS n FROM clients") == [{"n": 0}]
    assert cid == 1


def test_crear_cliente_con_conn_y_commit(db):
    conn = core.get_connection()
    cid = clients.crear_cliente({"name": "Ana"}, conn=conn)
    conn.commit()
    conn.close()
    assert [f["id"] for f in _filas("SELECT id FROM clients")] == [cid]
    assert [f["id"] for f in _filas("SELECT id FROM parties")] == [cid]


def test_el_central_no_asigna_ids_provisorios_al_aplicar_el_alta_de_un_nodo(db):
    """El mismo proceso del central tiene la configuración (la arma el producto, igual en las dos puntas) y su
    `nodo()` contesta `None`: lo que aplica lleva el id de su secuencia."""
    Nodo(nodo=None).instalar()
    conn = core.get_connection()
    cid = clients.crear_cliente({"name": "Ana"}, conn=conn)
    conn.commit()
    conn.close()
    assert cid == 1


def test_get_client_by_cuit_con_conn_ve_lo_que_la_transaccion_escribio(db):
    conn = core.get_connection()
    clients.crear_cliente({"name": "Ana", "cuit_dni": "20-31781916-2"}, conn=conn)
    assert clients.get_client_by_cuit("20317819162", conn=conn)["name"] == "Ana"
    assert clients.get_client_by_cuit("20317819162") is None, "sin conn abre otra conexión y no ve lo no commiteado"
    conn.rollback()
    conn.close()


# ── la configuración ────────────────────────────────────────────────────────

def test_configurar_pide_una_altas_en_nodo():
    with pytest.raises(TypeError, match="AltasEnNodo"):
        altas_en_nodo.configurar(lambda: None)
    altas_en_nodo.configurar(None)
    assert altas_en_nodo.configuracion() is None


def test_un_id_provisorio_tiene_que_ser_negativo(db):
    altas_en_nodo.configurar(AltasEnNodo(nodo=lambda: NODO, id_provisorio=lambda conn: 5))
    with pytest.raises(ValueError, match="negativo"):
        clients.create_client("Ana")
    assert _filas("SELECT COUNT(*) AS n FROM clients") == [{"n": 0}]


def test_el_gancho_no_es_obligatorio(db):
    """Un nodo que solo quiere ids provisorios (un test, un producto que sube por otro camino) no declara el gancho."""
    altas_en_nodo.configurar(AltasEnNodo(nodo=lambda: NODO, id_provisorio=Nodo().id_provisorio))
    assert clients.create_client("Ana") == -1


def test_datos_de_cliente_omite_las_columnas_que_la_instancia_no_tiene():
    fila = {"id": -3, "name": "Ana", "cuit_dni": "20-1", "created_at": "x", "auto_facturar": 1}
    assert altas_en_nodo.datos_de_cliente(fila) == {
        "version": altas_en_nodo.VERSION, "cliente_id": -3, "name": "Ana", "cuit_dni": "20-1"}


def test_avisar_no_hace_nada_para_un_cliente_con_id_del_central(db):
    nodo = Nodo().instalar()
    altas_en_nodo.avisar_alta_de_cliente(None, {"id": 57, "name": "Ana"})
    altas_en_nodo.avisar_alta_de_cliente(None, None)
    assert nodo.llamadas == []


# ── las referencias sin clave foránea ───────────────────────────────────────

_COLUMNA_DE_CLIENTE = re.compile(r"(^|_)(cliente|client)_id$|^tercero_id$")


def test_toda_columna_que_guarda_un_cliente_tiene_clave_foranea_o_esta_declarada(db):
    """`libraedge` reapunta las FK solo, leyéndolas del catálogo; lo que no tiene FK hay que declararlo. Si alguien agrega
    una columna que guarda un cliente sin FK, este test falla en vez de dejar un id provisorio colgado para siempre."""
    conn = core.get_connection()
    try:
        declaradas = {(t, c) for t, c, _ in altas_en_nodo.REFERENCIAS_DE_CLIENTES}
        sin_fk = set()
        con_fk = set()
        for (tabla,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            fks = {f[3] for f in conn.execute(f"PRAGMA foreign_key_list({tabla})").fetchall() if f[2] == "clients"}
            for columna in (c[1] for c in conn.execute(f"PRAGMA table_info({tabla})").fetchall()):
                if columna in fks:
                    con_fk.add((tabla, columna))
                elif _COLUMNA_DE_CLIENTE.search(columna):
                    sin_fk.add((tabla, columna))
    finally:
        conn.close()
    # `parties.id` / `sales.customer_party_id` son de LibraCommerce y tienen su FK; acá solo está el schema del motor.
    assert sin_fk == declaradas, f"columnas con un cliente y sin FK que no están declaradas: {sorted(sin_fk - declaradas)}"
    assert {("ventas", "cliente_id"), ("remitos", "client_id"), ("cc_pagos", "cliente_id")} <= con_fk
