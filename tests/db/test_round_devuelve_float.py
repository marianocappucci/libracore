"""`ROUND(x, n)` tiene que devolver lo mismo en los dos motores: un float.

La traducción a PostgreSQL castea a `NUMERIC` porque `round(double precision,
integer)` no existe allá. Pero psycopg entrega `NUMERIC` como
`decimal.Decimal`, y el código de la familia hace aritmética con `float` —en
SQLite estas columnas son REAL—, así que multiplicar el resultado explota con
*unsupported operand type(s) for \\*: 'float' and 'decimal.Decimal'*.

Lo encontró la suite de [[restolibra]] el 2026-08-10, **lejos de la consulta**:
en el cálculo del costo de una receta. El `NUMERIC` lo introduce la traducción,
así que le toca a ella deshacerlo.
"""
import os

import pytest

from libracore.db import core


def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("LIBRACORE_POSTGRES_URL no está definida en CI")
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


def test_en_postgres_round_devuelve_float():
    core.configure(_url())
    conn = core.get_connection()
    try:
        valor = conn.execute("SELECT ROUND(CAST(1.005 AS REAL) * 3, 2)").fetchone()[0]
        assert isinstance(valor, float), f"llegó {type(valor).__name__}"
        # Y se puede seguir operando con él, que es lo que rompía.
        assert isinstance(valor * 2.0, float)
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


def test_en_sqlite_round_devuelve_float(tmp_path):
    """La contraparte: el tipo que hay que igualar."""
    core.configure(str(tmp_path / "t.db"))
    conn = core.get_connection()
    try:
        valor = conn.execute("SELECT ROUND(1.005 * 3, 2)").fetchone()[0]
        assert isinstance(valor, float)
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


def test_los_dos_motores_dan_el_mismo_numero(tmp_path):
    """Que ninguno rompa no alcanza: tienen que dar lo mismo."""
    url = _url()

    core.configure(str(tmp_path / "t.db"))
    lite = core.get_connection()
    en_sqlite = lite.execute("SELECT ROUND(2.34567 * 3, 3)").fetchone()[0]
    lite.close()
    core._db_path = None
    core._database_url = None

    core.configure(url)
    pg = core.get_connection()
    try:
        en_postgres = pg.execute("SELECT ROUND(CAST(2.34567 AS REAL) * 3, 3)").fetchone()[0]
    finally:
        pg.close()
        core._db_path = None
        core._database_url = None

    assert en_sqlite == en_postgres
