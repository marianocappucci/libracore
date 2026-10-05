"""El dinero del motor se guarda exacto en PostgreSQL (ADR-024): `NUMERIC`, y se lee como siempre.

Lo que se fija:
- Las 33 columnas de `COLUMNAS_DE_DINERO` quedan `numeric`; las alícuotas y las
  cantidades, no.
- La conversión de una base vieja (`double precision`) **no pierde ni redondea**
  lo que había.
- Una suma en la base deja de arrastrar el error de punto flotante.
- La lectura sigue siendo `float`: ningún producto cambia su aritmética.
- Una columna que llegó con otro tipo se respeta.
- En SQLite no cambia nada.
"""
import os

import pytest

from libracore.db import core
from libracore.db.schema import COLUMNAS_DE_DINERO, init_core_schema


@pytest.fixture
def pg():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    import psycopg

    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS public CASCADE")
        c.execute("CREATE SCHEMA public")
    core.configure(db_path=url)
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    yield conn
    conn.close()
    core._db_path = None
    core._database_url = None


def _tipos(conn):
    return {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        ).fetchall()
    }


def test_las_columnas_de_dinero_son_numeric_y_las_demas_no(pg):
    tipos = _tipos(pg)
    assert {tipos[c] for c in COLUMNAS_DE_DINERO} == {"numeric"}
    for no_es_plata in [("egresos", "iva_pct"), ("presupuestos", "tax_rate"), ("remitos", "tax_rate"),
                        ("movimientos_stock", "cantidad"), ("productos", "stock_minimo")]:
        assert tipos[no_es_plata] == "double precision", no_es_plata
    # No quedó ninguna de plata afuera: todo `double precision` que queda es de la lista de arriba.
    assert {k for k, v in tipos.items() if v == "double precision"} == {
        ("egresos", "iva_pct"), ("presupuestos", "tax_rate"), ("remitos", "tax_rate"),
        ("movimientos_stock", "cantidad"), ("productos", "stock_minimo")}


def test_una_base_vieja_se_convierte_sin_perder_ni_redondear(pg):
    """Como estaba antes de la 0018: `double precision`, con montos de más de dos decimales."""
    pg.execute("ALTER TABLE productos ALTER COLUMN precio_costo TYPE DOUBLE PRECISION")
    pg.execute("INSERT INTO productos (nombre, precio_costo) VALUES ('Tornillo', 0.355), ('Tuerca', 1234567.89)")
    pg.commit()
    init_core_schema(pg)
    pg.commit()
    assert _tipos(pg)[("productos", "precio_costo")] == "numeric"
    filas = pg.execute("SELECT precio_costo FROM productos ORDER BY id").fetchall()
    assert [f[0] for f in filas] == [0.355, 1234567.89]


def test_la_suma_en_la_base_no_arrastra_error_de_punto_flotante(pg):
    for monto in (0.1, 0.2):
        pg.execute(
            "INSERT INTO caja_movimientos (fecha, tipo, concepto, monto) VALUES ('2026-10-05', 'ingreso', 'x', ?)",
            (monto,))
    pg.commit()
    total = pg.execute("SELECT SUM(monto) FROM caja_movimientos").fetchone()[0]
    assert total == 0.3  # en double precision da 0.30000000000000004
    assert isinstance(total, float)  # y se lee como siempre


def test_una_columna_con_otro_tipo_se_respeta(pg):
    pg.execute("ALTER TABLE ventas ALTER COLUMN descuento TYPE INTEGER USING descuento::integer")
    pg.commit()
    init_core_schema(pg)
    pg.commit()
    assert _tipos(pg)[("ventas", "descuento")] == "integer"


def test_correr_dos_veces_no_cambia_nada(pg):
    antes = _tipos(pg)
    init_core_schema(pg)
    pg.commit()
    assert _tipos(pg) == antes


def test_en_sqlite_no_cambia_nada(tmp_path):
    core.configure(db_path=str(tmp_path / "dinero.db"))
    conn = core.get_connection()
    try:
        init_core_schema(conn)
        tipos = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(facturas)").fetchall()}
        assert (tipos["subtotal"], tipos["total"]) == ("REAL", "REAL")
    finally:
        conn.close()
        core._db_path = None
