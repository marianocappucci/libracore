"""Cuenta corriente cuando el id del party de LibraCommerce NO es el id del
cliente de LibraCore — el caso real de VentaLibra.

`VENTAS_LIBRACOMMERCE` (y `VENTAS_LIBRACORE`) asumen que `sales.customer_party_id`
identifica directamente a `clients.id`. Eso vale en Contalibra/Restolibra porque
`clients._espejar_party` crea el party con el MISMO id al dar de alta — pero en
VentaLibra el cliente se enlaza después por `clients.external_ref =
'party-<party_id>'`, y los ids no coinciden entre sí (medido en `ventalibra-dev`:
0 de 3 coinciden en nombre). Estos tests fijan que
`VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF` resuelve el cliente correcto por esa
referencia, y documentan qué pasaría si se usara el origen equivocado.
"""
import os

import pytest

from libracore.db import clients, core, cuenta_corriente
from libracore.db.cuenta_corriente import (
    VENTAS_LIBRACOMMERCE,
    VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF,
)
from libracore.db.schema import init_core_schema


@pytest.fixture(autouse=True)
def _reset_ids():
    _id_seq["sales"] = 0


_id_seq = {"sales": 0}


def _crear_sales(conn):
    """`sales` no es del schema de LibraCore: la crea LibraCommerce al migrar
    las ventas del producto. Acá se declara el mínimo que la cuenta corriente
    le pide, igual que en `test_cuenta_corriente_origenes.py`."""
    conn.execute("""
        CREATE TABLE sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT,
            occurred_on TEXT,
            customer_party_id INTEGER
        )
    """)
    conn.execute("DROP TABLE ventas_pagos")
    conn.execute("""
        CREATE TABLE ventas_pagos (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            venta_id   INTEGER NOT NULL,
            medio      TEXT NOT NULL,
            monto      REAL NOT NULL,
            referencia TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def _venta_a_party(conn, party_id, monto, numero="S-1", fecha="2026-09-01"):
    """Una venta de LibraCommerce cobrada a cuenta corriente, identificada
    por el id del PARTY (nunca el del cliente de LibraCore -- ese es
    justamente el punto)."""
    _id_seq["sales"] += 1
    venta_id = _id_seq["sales"]
    conn.execute(
        "INSERT INTO sales (id, number, occurred_on, customer_party_id) VALUES (?,?,?,?)",
        (venta_id, numero, fecha, party_id),
    )
    conn.execute(
        "INSERT INTO ventas_pagos (venta_id, medio, monto) VALUES (?,?,?)",
        (venta_id, "cuenta_corriente", monto),
    )
    conn.commit()
    return venta_id


@pytest.fixture
def conn(tmp_path, crear_schema):
    core.configure(db_path=str(tmp_path / "cc_external_ref.db"))
    c = core.get_connection()
    crear_schema(c)
    _crear_sales(c)
    yield c
    c.close()
    core._db_path = None


def _escenario_ventalibra(conn):
    """El caso medido en `ventalibra-dev`: parties 1..4 (el 2 es "proveedor",
    sin cliente de LibraCore enlazado). clients 1..3 con nombres DISTINTOS a
    los parties del mismo id -- `external_ref` los enlaza a otro party:

        client 1 (Ana)     -> party-3
        client 2 (Beto)    -> party-1
        client 3 (Carla)   -> party-4
        party 2 (proveedor)-> sin cliente enlazado
    """
    ana = clients.create_client("Ana")
    beto = clients.create_client("Beto")
    carla = clients.create_client("Carla")
    conn.execute("UPDATE clients SET external_ref=? WHERE id=?", ("party-3", ana))
    conn.execute("UPDATE clients SET external_ref=? WHERE id=?", ("party-1", beto))
    conn.execute("UPDATE clients SET external_ref=? WHERE id=?", ("party-4", carla))
    conn.commit()
    return ana, beto, carla


def test_cada_cliente_ve_su_propia_deuda_cruzando_por_external_ref(conn):
    ana, beto, carla = _escenario_ventalibra(conn)
    _venta_a_party(conn, party_id=3, monto=1000.0, numero="V-ANA")
    _venta_a_party(conn, party_id=1, monto=500.0, numero="V-BETO")
    _venta_a_party(conn, party_id=4, monto=300.0, numero="V-CARLA")

    assert cuenta_corriente.get_cc_saldo(ana, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF) == 1000.0
    assert cuenta_corriente.get_cc_saldo(beto, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF) == 500.0
    assert cuenta_corriente.get_cc_saldo(carla, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF) == 300.0


def test_con_el_origen_viejo_la_deuda_le_queda_a_otro_cliente(conn):
    """Documenta el problema: cruzar por `customer_party_id == clients.id`
    (lo que hace `VENTAS_LIBRACOMMERCE`) le pone la venta de Ana (party 3) en
    la cuenta del cliente cuyo ID es 3 -- que es Carla, no Ana."""
    ana, beto, carla = _escenario_ventalibra(conn)
    _venta_a_party(conn, party_id=3, monto=1000.0, numero="V-ANA")

    # Con el origen correcto, la deuda es de Ana.
    assert cuenta_corriente.get_cc_saldo(ana, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF) == 1000.0
    # Con el origen viejo, la misma venta aparece en la cuenta de Carla (id=3)
    # -- que no tiene nada que ver -- y Ana queda en cero.
    assert cuenta_corriente.get_cc_saldo(carla, VENTAS_LIBRACOMMERCE) == 1000.0
    assert cuenta_corriente.get_cc_saldo(ana, VENTAS_LIBRACOMMERCE) == 0.0


def test_una_venta_de_party_sin_cliente_enlazado_no_suma_a_nadie(conn):
    """El party 2 ("proveedor") no tiene ningún `clients.external_ref` que lo
    nombre. La venta no debe sumarle a nadie, y la consulta no debe romper."""
    ana, beto, carla = _escenario_ventalibra(conn)
    _venta_a_party(conn, party_id=2, monto=999.0, numero="V-PROVEEDOR")

    assert cuenta_corriente.get_cc_saldo(ana, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF) == 0.0
    assert cuenta_corriente.get_cc_saldo(beto, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF) == 0.0
    assert cuenta_corriente.get_cc_saldo(carla, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF) == 0.0
    deudores = cuenta_corriente.get_clientes_con_saldo_cc(VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF)
    assert deudores == []


def test_get_cc_movimientos_trae_el_numero_de_venta_del_cliente_correcto(conn):
    ana, beto, carla = _escenario_ventalibra(conn)
    _venta_a_party(conn, party_id=3, monto=1000.0, fecha="2026-09-05", numero="V-ANA")
    _venta_a_party(conn, party_id=1, monto=500.0, numero="V-BETO")

    movs = cuenta_corriente.get_cc_movimientos(ana, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF)
    assert len(movs) == 1
    assert movs[0]["concepto"] == "Venta #V-ANA"
    assert movs[0]["fecha"] == "2026-09-05"
    assert movs[0]["tipo"] == "debito"


def test_el_saldo_combina_venta_debito_directo_y_pago_por_external_ref(conn):
    """El escenario completo: venta de LibraCommerce por external_ref +
    débito directo (`cc_debitos`) + pago (`cc_pagos`)."""
    ana, _, _ = _escenario_ventalibra(conn)
    _venta_a_party(conn, party_id=3, monto=1000.0, numero="V-ANA")
    cuenta_corriente.create_cc_debito(ana, 250.0, "2026-09-02", "Venta POS-9", "sale-9")
    cuenta_corriente.create_cc_pago(ana, 300.0, "2026-09-03", "Pago", "", "efectivo", None, None)

    # 1000 (venta) + 250 (débito directo) − 300 (pago)
    assert cuenta_corriente.get_cc_saldo(ana, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF) == 950.0

    movs = cuenta_corriente.get_cc_movimientos(ana, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF)
    assert len(movs) == 3
    assert sum(1 for m in movs if m["tipo"] == "debito") == 2

    periodo = cuenta_corriente.get_cc_movimientos_periodo(
        ana, "2026-09-01", "2026-09-30", VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF
    )
    assert periodo["saldo_final"] == 950.0


def test_el_listado_de_deudores_agrupa_por_cliente_no_por_party(conn):
    ana, beto, carla = _escenario_ventalibra(conn)
    _venta_a_party(conn, party_id=3, monto=1000.0, numero="V-ANA")
    _venta_a_party(conn, party_id=1, monto=500.0, numero="V-BETO")

    deudores = cuenta_corriente.get_clientes_con_saldo_cc(VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF)
    por_id = {d["id"]: d["saldo"] for d in deudores}
    assert por_id == {ana: 1000.0, beto: 500.0}


# ── La concatenación `'party-' || id` en PostgreSQL, ejecutada de verdad ────
#
# `test_cuenta_corriente_origenes.py` y los tests de arriba corren sólo contra
# SQLite (vía tmp_path). El motor real de la familia es PostgreSQL, y ahí el
# operador `||` con un entero de un lado no es garantía sin probarlo: se
# ejecuta contra un PostgreSQL de verdad (`LIBRACORE_POSTGRES_URL`) el mismo
# escenario, y se compara con SQLite -- no alcanza con "no explotó".


def _correr_escenario(db_path, limpiar_schema=False):
    core.configure(db_path)
    conn = core.get_connection()
    if limpiar_schema:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    init_core_schema(conn)
    conn.commit()
    _crear_sales(conn)

    ana = clients.create_client("Ana")
    beto = clients.create_client("Beto")
    conn.execute("UPDATE clients SET external_ref=? WHERE id=?", ("party-3", ana))
    conn.execute("UPDATE clients SET external_ref=? WHERE id=?", ("party-1", beto))
    conn.commit()
    _id_seq["sales"] = 0
    _venta_a_party(conn, party_id=3, monto=1000.0, numero="V-ANA")
    _venta_a_party(conn, party_id=1, monto=500.0, numero="V-BETO")
    # party 2: sin cliente enlazado, no debe sumarle a nadie.
    _venta_a_party(conn, party_id=2, monto=999.0, numero="V-PROVEEDOR")

    resultado = {
        "ana": cuenta_corriente.get_cc_saldo(ana, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF),
        "beto": cuenta_corriente.get_cc_saldo(beto, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF),
        "deudores": sorted(
            (d["id"], d["saldo"])
            for d in cuenta_corriente.get_clientes_con_saldo_cc(
                VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF
            )
        ),
    }
    conn.close()
    core._db_path = None
    core._database_url = None
    return resultado


@pytest.fixture
def resultado_sqlite(tmp_path):
    return _correr_escenario(str(tmp_path / "external_ref_pg.db"))


@pytest.fixture
def resultado_postgres():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    try:
        return _correr_escenario(url, limpiar_schema=True)
    finally:
        core._db_path = None
        core._database_url = None


def test_el_cruce_por_external_ref_da_lo_mismo_en_los_dos_motores(
    resultado_sqlite, resultado_postgres
):
    assert resultado_sqlite == resultado_postgres


def test_el_cruce_por_external_ref_anda_de_verdad_en_postgres(resultado_postgres):
    """El defecto en sí, dicho sin depender de la comparación entre motores:
    si `'party-' || customer_party_id` no fuera válido en PostgreSQL, este
    test fallaría con un error de SQL, no con un valor distinto."""
    assert resultado_postgres == {
        "ana": 1000.0,
        "beto": 500.0,
        "deudores": [(1, 1000.0), (2, 500.0)],
    }
