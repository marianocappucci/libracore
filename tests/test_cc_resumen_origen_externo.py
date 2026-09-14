"""`cc_resumen` hilando `origen` — el gap que el hilado de `origen` por sí solo
no probaba.

`libracore/cc_resumen.py` recibió un parámetro `origen` en `calcular_periodo`,
`enviar_resumen` y `enviar_resumenes_pendientes` para que un producto con
`VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF` pueda usarlo. `tests/test_cc_resumen.py`
sólo corre con el default (`VENTAS_LIBRACORE`), así que un `calcular_periodo`
que IGNORARA `origen` —por ejemplo, llamando a `get_cc_movimientos_periodo` sin
pasarlo— seguiría con esa suite en verde. Este archivo cierra ese hueco:
ejecuta el mismo escenario de VentaLibra de
`tests/db/test_cuenta_corriente_external_ref.py` (parties cuyo id no coincide
con el `clients.id`, enlazados por `external_ref`) a través de `cc_resumen`.
"""
import datetime

import pytest

from libracore import cc_resumen, config_manager
from libracore.db import clients, core
from libracore.db.cuenta_corriente import VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF

HOY = datetime.date(2026, 8, 3)

_id_seq = {"sales": 0}


@pytest.fixture(autouse=True)
def _reset_ids():
    _id_seq["sales"] = 0


def _crear_sales(conn):
    """`sales` no es del schema de LibraCore -- ver
    `tests/db/test_cuenta_corriente_external_ref.py`, de donde sale este mismo
    helper (duplicado porque `tests/` no es un paquete y no se puede importar
    entre subcarpetas)."""
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


def _venta_a_party(conn, party_id, monto, numero="S-1", fecha="2026-07-20"):
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
def conn(tmp_path, monkeypatch, crear_schema):
    core.configure(db_path=str(tmp_path / "cc_resumen_external_ref.db"))
    c = core.get_connection()
    crear_schema(c)
    _crear_sales(c)
    c.commit()
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(tmp_path / "config.json"))
    yield c
    c.close()
    core._db_path = None


@pytest.fixture
def enviados(monkeypatch, tmp_path):
    """Mismo doble que `tests/test_cc_resumen.py`: nunca debería llegar a
    usarse acá porque los dos tests de este archivo van por `dry_run=True` o
    llaman a `calcular_periodo` directo -- si algo lo llena, algo no estaba
    aislado como se esperaba."""
    sent = []

    def _fake_pdf(cliente, periodo, output_dir=None):
        path = tmp_path / f"resumen_{cliente['id']}.pdf"
        path.write_bytes(b"%PDF-1.4 fake")
        return str(path)

    def _fake_send(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(cc_resumen.pdf_generator, "generate_pdf_resumen_cc", _fake_pdf)
    monkeypatch.setattr(cc_resumen.email_sender, "enviar_documento", _fake_send)
    return sent


def _cfg(**over):
    base = {
        "cc_resumen_habilitado": "1",
        "cc_resumen_dia_mes": "1",
        "cc_resumen_dia_semana": "1",
        "cc_resumen_solo_con_saldo": "1",
        "email_smtp_host": "smtp.test",
        "email_smtp_user": "user@test",
        "email_smtp_password": "x",
        "empresa_nombre": "Empresa Test",
    }
    base.update(over)
    config_manager.save(base)
    return config_manager.load()


def _escenario(conn):
    """El mismo caso de VentaLibra que `test_cuenta_corriente_external_ref.py`:
    Ana es client 1 pero corresponde al party 3; Beto es client 2 pero
    corresponde al party 1."""
    ana = clients.create_client("Ana", email="ana@test.com")
    beto = clients.create_client("Beto", email="beto@test.com")
    conn.execute("UPDATE clients SET external_ref=? WHERE id=?", ("party-3", ana))
    conn.execute("UPDATE clients SET external_ref=? WHERE id=?", ("party-1", beto))
    conn.commit()
    return ana, beto


# ── calcular_periodo ─────────────────────────────────────────────────────────

def test_calcular_periodo_trae_la_venta_del_cliente_correcto_por_external_ref(conn):
    ana, beto = _escenario(conn)
    _venta_a_party(conn, party_id=3, monto=1000.0, numero="V-ANA")
    _venta_a_party(conn, party_id=1, monto=500.0, numero="V-BETO")

    periodo_ana = cc_resumen.calcular_periodo(
        clients.get_client(ana), HOY, origen=VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF
    )
    assert periodo_ana["saldo_final"] == 1000.0
    assert [m["concepto"] for m in periodo_ana["movimientos"]] == ["Venta #V-ANA"]

    periodo_beto = cc_resumen.calcular_periodo(
        clients.get_client(beto), HOY, origen=VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF
    )
    assert periodo_beto["saldo_final"] == 500.0
    assert [m["concepto"] for m in periodo_beto["movimientos"]] == ["Venta #V-BETO"]


def test_calcular_periodo_sin_pasar_origen_no_ve_la_venta_de_libracommerce(conn):
    """El caso contrario, documentado: con el default (`VENTAS_LIBRACORE`)
    `calcular_periodo` sigue leyendo `ventas` -- vacía en este escenario, así
    que la venta de LibraCommerce queda invisible y el saldo da otra cosa
    (cero en vez de 1000)."""
    ana, _ = _escenario(conn)
    _venta_a_party(conn, party_id=3, monto=1000.0, numero="V-ANA")

    periodo = cc_resumen.calcular_periodo(clients.get_client(ana), HOY)
    assert periodo["saldo_final"] == 0.0
    assert periodo["movimientos"] == []


# ── enviar_resumenes_pendientes ──────────────────────────────────────────────

def test_envio_masivo_dry_run_trae_el_saldo_correcto_por_external_ref(conn, enviados):
    """`dry_run=True` corta ANTES de `enviar_resumen` (no hace falta SMTP ni
    PDF -- mismo camino que usa `test_dry_run_no_envia_ni_marca_la_base` en
    `tests/test_cc_resumen.py`), pero SÍ pasa por `calcular_periodo`: es la
    superficie mínima para probar que `origen` llega hasta ahí sin depender de
    dobles de mail."""
    _cfg()
    ana, beto = _escenario(conn)
    clients.update_client(ana, cc_resumen_auto=1)
    clients.update_client(beto, cc_resumen_auto=1)
    _venta_a_party(conn, party_id=3, monto=1000.0, numero="V-ANA")
    _venta_a_party(conn, party_id=1, monto=500.0, numero="V-BETO")

    r = cc_resumen.enviar_resumenes_pendientes(
        hoy=HOY, dry_run=True, forzar=True, origen=VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF
    )

    por_cliente = {e["cliente"]: e["saldo"] for e in r["enviados"]}
    assert por_cliente == {"Ana": 1000.0, "Beto": 500.0}
    assert enviados == []  # dry_run: ningún mail ni PDF se generó de verdad


def test_envio_masivo_sin_pasar_origen_no_encuentra_saldo_y_omite(conn, enviados):
    """Mismo escenario que arriba, pero llamando SIN `origen`: con el default
    ninguno de los dos clientes tiene saldo (la venta vive en `sales`, no en
    `ventas`), así que `solo_con_saldo` los omite a los dos en vez de
    enviarles el resumen."""
    _cfg()
    ana, beto = _escenario(conn)
    clients.update_client(ana, cc_resumen_auto=1)
    clients.update_client(beto, cc_resumen_auto=1)
    _venta_a_party(conn, party_id=3, monto=1000.0, numero="V-ANA")
    _venta_a_party(conn, party_id=1, monto=500.0, numero="V-BETO")

    r = cc_resumen.enviar_resumenes_pendientes(hoy=HOY, dry_run=True, forzar=True)

    assert r["enviados"] == []
    assert {o["motivo"] for o in r["omitidos"]} == {"sin_saldo"}
    assert enviados == []
