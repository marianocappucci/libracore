"""La moneda del libro de terceros y del recibo (ADR-088, `libracore.monedas`).

Lo que se fija acá, contra los dos motores:
- un asiento sin moneda es en pesos, y una moneda que no existe no se asienta;
- la reversión lleva la moneda del original;
- `saldo`, `extracto` (con su saldo anterior) y `saldos` no suman pesos con dólares;
- el recibo de un cobro en dólares es en dólares, y el papel dice `US$`;
- la `0031` agrega las columnas a una base de antes, con lo que había en pesos, y no se baja.
"""
import io
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader

from libracore import monedas, recibos
from libracore.db import core
from libracore.db import libro_de_terceros as libro
from libracore.db.schema import init_core_schema
from libracore.pdf_generator import generate_pdf_recibo_doc

RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["sqlite", "postgres"])
def base(request, tmp_path):
    if request.param == "postgres":
        url = os.environ.get("LIBRACORE_POSTGRES_URL")
        if not url:
            pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
        import psycopg

        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True) as c:
            c.execute("DROP SCHEMA IF EXISTS public CASCADE")
            c.execute("CREATE SCHEMA public")
        core.configure(db_path=url)
    else:
        core.configure(db_path=str(tmp_path / "multimoneda.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.execute("INSERT INTO clients (id, name, cuit_dni, address) "
                     "VALUES (7, 'Inquilina Ficticia', '27-11111111-3', 'Calle Inventada 100')")
        conn.commit()
    yield request.param
    core._db_path = None
    core._database_url = None


def test_sin_moneda_es_en_pesos_y_una_que_no_existe_no_se_asienta(base):
    a = libro.asentar(7, "inquilino", "2026-11-01", "Alquiler", debe=1000)
    assert libro.get_asiento(a)["moneda"] == "ARS"
    b = libro.asentar(7, "inquilino", "2026-11-01", "Alquiler", debe=500, moneda="usd")
    assert libro.get_asiento(b)["moneda"] == "USD"
    with pytest.raises(libro.AsientoInvalido, match="Moneda inválida"):
        libro.asentar(7, "inquilino", "2026-11-01", "Alquiler", debe=1, moneda="EUR")


def test_la_reversion_lleva_la_moneda_del_original(base):
    a = libro.asentar(7, "inquilino", "2026-11-01", "Alquiler", debe=800, moneda="USD")
    r = libro.contraasentar(a)
    assert libro.get_asiento(r)["moneda"] == "USD"
    assert libro.saldo(7, "inquilino", moneda="USD") == 0


def test_los_saldos_no_suman_pesos_con_dolares(base):
    libro.asentar(7, "inquilino", "2026-10-01", "Alquiler octubre", debe=100000)
    libro.asentar(7, "inquilino", "2026-10-01", "Alquiler octubre (USD)", debe=800, moneda="USD")
    libro.asentar(7, "inquilino", "2026-11-01", "Alquiler noviembre (USD)", debe=800, moneda="USD")
    libro.asentar(7, "inquilino", "2026-11-05", "Cobro (USD)", haber=300, moneda="USD")

    assert libro.saldo(7, "inquilino", moneda="ARS") == 100000
    assert libro.saldo(7, "inquilino", moneda="USD") == 1300

    saldos = {(s["tercero_id"], s["moneda"]): s["saldo"] for s in libro.saldos("inquilino")}
    assert saldos == {(7, "ARS"): 100000, (7, "USD"): 1300}
    assert [s["moneda"] for s in libro.saldos("inquilino", moneda="USD")] == ["USD"]

    ext = libro.extracto(7, "inquilino", desde="2026-11-01", moneda="USD")
    assert ext["saldo_anterior"] == 800
    assert [a["saldo"] for a in ext["asientos"]] == [1600, 1300]
    assert {a["moneda"] for a in ext["asientos"]} == {"USD"}


def test_el_recibo_de_un_cobro_en_dolares_es_en_dolares(base):
    a = libro.asentar(7, "inquilino", "2026-11-05", "Alquiler noviembre 2026", haber=800, moneda="USD")
    recibo = recibos.emitir_recibo_de_asiento(
        a, pagos=[{"fecha": "2026-11-05", "medio_pago": "transferencia", "monto": 800}])
    assert recibo["moneda"] == "USD"

    texto = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(generate_pdf_recibo_doc(recibo))).pages)
    assert "US$ 800,00" in texto


def test_un_recibo_en_pesos_sigue_diciendo_pesos(base):
    a = libro.asentar(7, "inquilino", "2026-11-05", "Alquiler noviembre 2026", haber=150000)
    recibo = recibos.emitir_recibo_de_asiento(
        a, pagos=[{"fecha": "2026-11-05", "medio_pago": "efectivo", "monto": 150000}])
    assert recibo["moneda"] == "ARS"
    texto = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(generate_pdf_recibo_doc(recibo))).pages)
    assert "US$" not in texto and "$ 150.000,00" in texto


def test_monedas():
    assert monedas.simbolo(None) == "$" and monedas.simbolo("usd") == "US$"
    assert monedas.nombre("USD") == "Dólares estadounidenses"
    with pytest.raises(monedas.MonedaInvalida):
        monedas.validar("BRL")


# ── la migración ──────────────────────────────────────────────────────────────────────────────────────


def _alembic(destino: str, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=RAIZ, env={**os.environ, "DATABASE_URL": destino, "PYTHONPATH": str(RAIZ)}, capture_output=True, text=True)


def test_la_0031_agrega_la_moneda_a_una_base_de_antes_y_no_se_baja(tmp_path):
    destino = str(tmp_path / "antes.db")
    assert _alembic(destino, "upgrade", "0030_indices_ajuste").returncode == 0
    # La 0001 de hoy ya las crea con `init_core_schema`: se las sacamos para tener una base de antes.
    conn = sqlite3.connect(destino)
    conn.execute("ALTER TABLE cc_asientos DROP COLUMN moneda")
    conn.execute("ALTER TABLE recibos DROP COLUMN moneda")
    conn.execute("INSERT INTO cc_asientos (fecha, tercero_id, rol, concepto, debe) "
                 "VALUES ('2026-10-01', 7, 'inquilino', 'Alquiler', 1000)")
    conn.commit()
    conn.close()

    assert _alembic(destino, "upgrade", "head").returncode == 0
    conn = sqlite3.connect(destino)
    try:
        assert conn.execute("SELECT moneda FROM cc_asientos").fetchall() == [("ARS",)]
        assert "moneda" in {f[1] for f in conn.execute("PRAGMA table_info(recibos)").fetchall()}
    finally:
        conn.close()

    bajar = _alembic(destino, "downgrade", "0030_indices_ajuste")
    assert bajar.returncode != 0 and "No se baja" in bajar.stderr
