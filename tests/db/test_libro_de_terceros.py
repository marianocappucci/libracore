"""El libro de cuenta corriente de terceros (`libracore.db.libro_de_terceros`, ADR-026).

Lo que se fija acá, contra los dos motores:
- un asiento mueve el debe o el haber, y la base lo sostiene aunque se saltee la función;
- corregir cambia en el lugar, y no deja cambiar lo que es otro asiento;
- contraasentar invierte las columnas, con la fecha del original por defecto, una sola vez;
- saldo, extracto con saldo anterior y corrido, y saldos por cuenta;
- con `conn`, el asiento entra y sale con la transacción de quien llama (ADR-025);
- el dinero se guarda exacto en PostgreSQL (`NUMERIC`, ADR-024).
"""
import os
import sqlite3

import pytest

from libracore.db import core
from libracore.db import libro_de_terceros as libro
from libracore.db.schema import init_core_schema


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
        core.configure(db_path=str(tmp_path / "libro.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.commit()
    yield request.param
    core._db_path = None
    core._database_url = None


def test_un_gasto_de_dos_patas_y_sus_saldos(base):
    """El caso de LibraCargo: el proveedor al debe y el fletero al haber, por lo mismo."""
    libro.asentar(7, "proveedor", "2026-10-01", "Gasto 1", debe=1500)
    libro.asentar(9, "fletero", "2026-10-01", "Gasto 1", haber=1500)
    libro.asentar(9, "fletero", "2026-10-03", "Viaje 12", debe=4000)

    assert libro.saldo(7, "proveedor") == 1500
    assert libro.saldo(9, "fletero") == 2500
    # La misma persona en otro rol es otra cuenta.
    assert libro.saldo(9, "cliente") == 0
    assert libro.saldos() == [{"tercero_id": 9, "rol": "fletero", "saldo": 2500.0},
                              {"tercero_id": 7, "rol": "proveedor", "saldo": 1500.0}]
    assert libro.saldos("fletero") == [{"tercero_id": 9, "rol": "fletero", "saldo": 2500.0}]


@pytest.mark.parametrize("debe, haber", [(0, 0), (10, 10), (-5, 0)])
def test_un_asiento_mueve_el_debe_o_el_haber_y_uno_solo(base, debe, haber):
    with pytest.raises(libro.AsientoInvalido):
        libro.asentar(1, "cliente", "2026-10-01", "x", debe=debe, haber=haber)


def test_la_base_lo_sostiene_aunque_se_saltee_la_funcion(base):
    with core.get_connection() as c:
        with pytest.raises(Exception) as e:
            c.execute("INSERT INTO cc_asientos (fecha, tercero_id, rol, concepto, debe, haber) "
                      "VALUES ('2026-10-01', 1, 'cliente', 'x', 10, 10)")
        assert isinstance(e.value, sqlite3.IntegrityError) or "check" in str(e.value).lower()


def test_lo_del_legado_puede_traer_los_dos_en_cero(base):
    """Un sistema viejo puede tener asientos sin importe: ponerles uno sería inventarlo."""
    a = libro.asentar(1, "cliente", "2023-08-01", "Del legado", origen_legado="ctacte:12")
    assert libro.get_asiento(a)["debe"] == 0
    with pytest.raises(Exception):  # el origen en el legado no se repite
        libro.asentar(1, "cliente", "2023-08-01", "Otra", origen_legado="ctacte:12")


def test_corregir_cambia_en_el_lugar_y_no_lo_que_es_otro_asiento(base):
    a = libro.asentar(7, "proveedor", "2026-10-01", "Gasto 1", debe=1500)
    corregido = libro.corregir(a, debe=1800, concepto="Gasto 1 (corregido)")
    assert (corregido["debe"], corregido["concepto"]) == (1800, "Gasto 1 (corregido)")
    assert libro.saldo(7, "proveedor") == 1800
    # Editar el documento puede mover el asiento a otra cuenta.
    assert libro.corregir(a, rol="fletero", tercero_id=9)["rol"] == "fletero"
    assert libro.saldo(9, "fletero") == 1800 and libro.saldo(7, "proveedor") == 0
    with pytest.raises(libro.AsientoInvalido, match="otro asiento"):
        libro.corregir(a, origen_legado="x")
    with pytest.raises(libro.AsientoInvalido, match="rol"):
        libro.corregir(a, rol=" ")
    with pytest.raises(libro.AsientoInvalido):
        libro.corregir(a, haber=100)  # quedaría con las dos columnas


def test_contraasentar_invierte_con_la_fecha_del_original_y_una_sola_vez(base):
    a = libro.asentar(3, "cliente", "2026-09-15", "Factura A 0001-00000010", debe=1210, factura_id=None)
    r = libro.contraasentar(a)
    reversa = libro.get_asiento(r)
    assert (reversa["fecha"], reversa["debe"], reversa["haber"], reversa["contrapartida_de"]) == (
        "2026-09-15", 0, 1210, a)
    assert reversa["concepto"] == "Reversión: Factura A 0001-00000010"
    assert libro.saldo(3, "cliente") == 0
    with pytest.raises(libro.AsientoInvalido, match="ya tiene contrapartida"):
        libro.contraasentar(a)
    with pytest.raises(libro.AsientoInvalido, match="se revierte el original"):
        libro.contraasentar(r)
    # Un hecho nuevo se revierte con su fecha.
    b = libro.asentar(3, "cliente", "2026-09-20", "Cobro", haber=500)
    assert libro.get_asiento(libro.contraasentar(b, fecha="2026-10-06"))["fecha"] == "2026-10-06"


def test_borrar_saca_un_asiento_pero_no_uno_con_contrapartida(base):
    a = libro.asentar(9, "fletero", "2026-10-01", "Flete orden 1", debe=300)
    libro.borrar(a)
    assert libro.get_asiento(a) is None
    b = libro.asentar(9, "fletero", "2026-10-01", "Flete orden 2", debe=300)
    libro.contraasentar(b)
    with pytest.raises(libro.AsientoInvalido, match="tiene contrapartida"):
        libro.borrar(b)


def test_el_extracto_trae_el_saldo_anterior_y_el_corrido(base):
    libro.asentar(3, "cliente", "2026-08-30", "Factura 1", debe=1000)
    libro.asentar(3, "cliente", "2026-09-05", "Factura 2", debe=500)
    libro.asentar(3, "cliente", "2026-09-10", "Cobro", haber=700)
    libro.asentar(3, "cliente", "2026-10-01", "Factura 3", debe=100)

    ext = libro.extracto(3, "cliente", desde="2026-09-01", hasta="2026-09-30")
    assert ext["saldo_anterior"] == 1000
    assert [(a["concepto"], a["saldo"]) for a in ext["asientos"]] == [("Factura 2", 1500), ("Cobro", 800)]
    assert libro.saldo(3, "cliente", hasta="2026-09-30") == 800


def test_con_conn_entra_y_sale_con_la_transaccion_de_quien_llama(base):
    conn = core.get_connection()
    libro.asentar(5, "cliente", "2026-10-01", "Factura", debe=100, conn=conn)
    assert libro.saldo(5, "cliente", conn=conn) == 100  # adentro se ve
    conn.rollback()
    conn.close()
    assert libro.saldo(5, "cliente") == 0  # y no quedó

    conn = core.get_connection()
    libro.asentar(5, "cliente", "2026-10-01", "Factura", debe=100, conn=conn)
    conn.commit()
    conn.close()
    assert libro.saldo(5, "cliente") == 100


def test_en_postgres_el_dinero_se_guarda_exacto(base):
    if base != "postgres":
        pytest.skip("en SQLite REAL y NUMERIC guardan lo mismo")
    with core.get_connection() as c:
        tipos = dict(c.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'cc_asientos' AND column_name IN ('debe', 'haber')").fetchall())
    assert tipos == {"debe": "numeric", "haber": "numeric"}
    for _ in range(10):
        libro.asentar(1, "cliente", "2026-10-01", "x", debe=0.1)
    with core.get_connection() as c:
        assert str(c.execute("SELECT SUM(debe) FROM cc_asientos").fetchone()[0]) in ("1.0", "1", "1.00")
