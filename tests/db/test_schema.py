import sqlite3
import pytest

from libracore.db import core
from libracore.db.schema import init_core_schema

CORE_TABLES = {
    "clients", "remitos", "presupuestos", "facturas", "cajas",
    "caja_movimientos", "mp_pagos", "mp_movimientos", "facturacion_alias",
    "arca_config", "usuarios", "modulos", "productos", "depositos",
    "categorias_producto", "categorias_egreso", "proveedores", "egresos",
    "egresos_pagos", "turnos_caja", "movimientos_stock", "ventas",
    "ventas_pagos", "cuentas_tesoreria", "movimientos_tesoreria",
    "auth_log", "listas_precio", "lista_precio_items", "cc_pagos",
    "cc_resumenes_enviados",
}


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "schema_test.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def test_todas_las_tablas_core_existen(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
    ).fetchall()}
    assert tables == CORE_TABLES


def test_productos_tiene_columnas_estacion_vendible(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(productos)").fetchall()}
    assert "estacion" in cols
    assert "vendible" in cols


def test_idempotente_correr_dos_veces(conn):
    # init_core_schema debe poder correr sobre una base ya inicializada
    # (arranque normal de la app en cada restart) sin duplicar seeds.
    init_core_schema(conn)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM cajas").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM depositos").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM categorias_egreso").fetchone()[0] == 10


def test_indice_unico_facturas_numero(conn):
    idxs = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert "idx_facturas_numero_unico" in idxs


def test_caja_default_seedeada(conn):
    row = conn.execute("SELECT * FROM cajas WHERE es_default=1").fetchone()
    assert row is not None
    assert row["nombre"] == "Caja Principal"


def test_deposito_default_seedeado(conn):
    row = conn.execute("SELECT * FROM depositos WHERE es_default=1").fetchone()
    assert row is not None


def test_upgrade_de_tabla_existente_sin_columnas_nuevas(tmp_path):
    """Regresión: `CREATE TABLE IF NOT EXISTS` es un no-op si la tabla ya
    existe — no agrega columnas nuevas a una base de datos real que ya
    corrió una versión anterior del schema. init_core_schema debe migrar
    ese caso con ALTER TABLE, no solo crear tablas frescas desde cero (que
    es todo lo que ejercitaban los demás tests, con tmp_path siempre
    vacío)."""
    db_path = str(tmp_path / "existing.db")
    core.configure(db_path=db_path)
    conn = core.get_connection()
    # Simula una base "vieja": productos sin estacion/vendible, tal como
    # estaba antes de que este schema las agregara.
    conn.executescript("""
        CREATE TABLE productos (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo       TEXT UNIQUE,
            nombre       TEXT NOT NULL,
            descripcion  TEXT DEFAULT '',
            precio_venta REAL NOT NULL DEFAULT 0,
            precio_costo REAL NOT NULL DEFAULT 0,
            unidad       TEXT NOT NULL DEFAULT 'u',
            categoria    TEXT DEFAULT '',
            activo       INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()

    init_core_schema(conn)
    conn.commit()

    cols = {r[1] for r in conn.execute("PRAGMA table_info(productos)").fetchall()}
    assert "estacion" in cols
    assert "vendible" in cols
    assert "stock_minimo" in cols
    conn.close()
    core._db_path = None
