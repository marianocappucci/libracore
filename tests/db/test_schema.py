import os
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
    "cc_debitos", "cc_resumenes_enviados", "recibos", "cc_asientos",
    "comprobantes_pendientes",
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
    """La numeración es única por emisor y ambiente; el índice viejo, más estricto, ya no está."""
    idxs = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert "idx_facturas_numeracion" in idxs
    assert "idx_facturas_numero_unico" not in idxs


def _factura(conn, numero, *, ambiente="produccion", emisor_id=None):
    conn.execute(
        "INSERT INTO facturas (tipo, punto_venta, numero, fecha, items, subtotal, iva_amount, "
        "total, ambiente, emisor_id) VALUES (1, 1, ?, '2026-10-05', '[]', 1, 0, 1, ?, ?)",
        (numero, ambiente, emisor_id),
    )


def test_la_numeracion_choca_dentro_del_mismo_emisor_y_ambiente(conn):
    """Sin emisor (todos los comprobantes de la familia) la unicidad sigue rigiendo.

    Es la razón del `COALESCE`: en un UNIQUE un `NULL` no es igual a otro, y sin él
    dos facturas A 0001-00000007 sin emisor entrarían las dos.
    """
    _factura(conn, 7)
    with pytest.raises(sqlite3.IntegrityError):
        _factura(conn, 7)


def test_homologacion_y_produccion_numeran_por_separado(conn):
    """ARCA lleva secuencias independientes: la factura de prueba 7 y la real 7 conviven."""
    _factura(conn, 7, ambiente="homologacion")
    _factura(conn, 7, ambiente="produccion")


def test_dos_emisores_numeran_por_separado(conn):
    """Dos razones sociales con punto de venta 1 tienen, cada una, su Factura A 0001-00000007."""
    a = conn.execute(
        "INSERT INTO arca_config (empresa, cuit, punto_venta, clave_path, certificado_path) "
        "VALUES ('a', '20111111112', 1, '', '')").lastrowid
    b = conn.execute(
        "INSERT INTO arca_config (empresa, cuit, punto_venta, clave_path, certificado_path) "
        "VALUES ('b', '20222222223', 1, '', '')").lastrowid
    _factura(conn, 7, emisor_id=a)
    _factura(conn, 7, emisor_id=b)
    _factura(conn, 7)
    with pytest.raises(sqlite3.IntegrityError):
        _factura(conn, 7, emisor_id=a)


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


def test_schema_completo_postgres_cuando_esta_configurado():
    """Gate de CI: el schema canónico debe poder nacer en PostgreSQL."""
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")

    core.configure(url)
    with core.get_connection() as conn:
        init_core_schema(conn)
        tablas = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            ).fetchall()
        }
        assert {"clients", "facturas", "productos", "usuarios", "cajas"} <= tablas
