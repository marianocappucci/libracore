"""La cuenta corriente, con las ventas viniendo de donde vengan.

El criterio de cálculo es uno solo — débitos por venta + débitos por
factura + débitos directos − abonos — pero la tabla de ventas cambia según
el producto, y hasta el 2026-07-28 eso se resolvía con una copia entera del
módulo en Contalibra y otra en Restolibra. Estos tests fijan que la versión
parametrizada da lo mismo por los dos caminos, y que `cc_debitos` (el caso
de las ventas que ni siquiera están en esta base) suma igual.
"""
import pytest

from libracore.db import clients, core, cuenta_corriente
from libracore.db.cuenta_corriente import VENTAS_LIBRACOMMERCE, VENTAS_LIBRACORE
from libracore.db.schema import init_core_schema


@pytest.fixture(autouse=True)
def _reset_ids():
    _id_seq.update({"ventas": 0, "sales": 1000})


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "cc_origenes.db"))
    c = core.get_connection()
    init_core_schema(c)
    # `sales` no es del schema de LibraCore: la crean Contalibra/Restolibra al
    # migrar sus ventas a LibraCommerce, en esta misma base. Acá se declara el
    # mínimo que la cuenta corriente le pide.
    c.execute("""
        CREATE TABLE sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT,
            occurred_on TEXT,
            customer_party_id INTEGER
        )
    """)
    # `ventas_pagos` se recrea SIN foreign key a propósito. En el schema de
    # LibraCore apunta a `ventas`, pero la instancia real de Contalibra la
    # tiene apuntando a `sales` -- la migración P7 la reconstruyó, y
    # `CREATE TABLE IF NOT EXISTS` no lo pisa (verificado el 2026-07-28 sobre
    # la base de producción). O sea que a qué tabla apunta la FK depende del
    # producto, y acá conviven las dos: lo que se prueba es el cálculo del
    # saldo, no la integridad referencial de ninguna de las dos formas.
    c.execute("DROP TABLE ventas_pagos")
    c.execute("""
        CREATE TABLE ventas_pagos (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            venta_id   INTEGER NOT NULL,
            medio      TEXT NOT NULL,
            monto      REAL NOT NULL,
            referencia TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.commit()
    yield c
    c.close()
    core._db_path = None


_id_seq = {"ventas": 0, "sales": 1000}


def _venta_fiada(conn, cliente_id, monto, fecha="2026-07-20", numero="V-1",
                 tabla="ventas"):
    """Una venta cobrada a cuenta corriente, en la tabla que corresponda.

    Los ids se piden explícitos y en rangos separados porque acá conviven las
    dos tablas, cosa que no pasa en ningún producto real (cada uno tiene sus
    ventas en una sola). Con AUTOINCREMENT las dos arrancarían en 1 y
    `ventas_pagos.venta_id`, que no dice de qué tabla viene, haría matchear la
    misma fila por los dos caminos.
    """
    _id_seq[tabla] += 1
    venta_id = _id_seq[tabla]
    if tabla == "ventas":
        conn.execute(
            "INSERT INTO ventas (id, numero, fecha, cliente_id, items, total) VALUES (?,?,?,?,?,?)",
            (venta_id, numero, fecha, cliente_id, "[]", monto),
        )
    else:
        conn.execute(
            "INSERT INTO sales (id, number, occurred_on, customer_party_id) VALUES (?,?,?,?)",
            (venta_id, numero, fecha, cliente_id),
        )
    conn.execute(
        "INSERT INTO ventas_pagos (venta_id, medio, monto) VALUES (?,?,?)",
        (venta_id, "cuenta_corriente", monto),
    )
    conn.commit()


def test_el_saldo_es_el_mismo_por_los_dos_origenes(conn):
    """La misma deuda, cargada en `ventas` o en `sales`, tiene que dar igual.

    Es la prueba de que reemplazar las copias de Contalibra/Restolibra por
    esta versión no mueve un peso.
    """
    viejo = clients.create_client("Cliente Legacy")
    nuevo = clients.create_client("Cliente Migrado")
    _venta_fiada(conn, viejo, 1500.0, tabla="ventas")
    _venta_fiada(conn, nuevo, 1500.0, tabla="sales")

    assert cuenta_corriente.get_cc_saldo(viejo, VENTAS_LIBRACORE) == 1500.0
    assert cuenta_corriente.get_cc_saldo(nuevo, VENTAS_LIBRACOMMERCE) == 1500.0


def test_el_default_sigue_siendo_el_origen_de_libracore(conn):
    """Ningún caller viejo pasa `origen`: el que no lo pasa tiene que seguir
    leyendo `ventas`, o Contalibra rompe en silencio."""
    cid = clients.create_client("Cliente")
    _venta_fiada(conn, cid, 800.0, tabla="ventas")

    assert cuenta_corriente.get_cc_saldo(cid) == 800.0


def test_cada_origen_ignora_las_ventas_del_otro(conn):
    cid = clients.create_client("Cliente")
    _venta_fiada(conn, cid, 800.0, tabla="ventas", numero="V-1")
    _venta_fiada(conn, cid, 300.0, tabla="sales", numero="S-1")

    assert cuenta_corriente.get_cc_saldo(cid, VENTAS_LIBRACORE) == 800.0
    assert cuenta_corriente.get_cc_saldo(cid, VENTAS_LIBRACOMMERCE) == 300.0


def test_los_movimientos_traen_el_numero_de_venta_de_cada_origen(conn):
    cid = clients.create_client("Cliente")
    _venta_fiada(conn, cid, 500.0, fecha="2026-07-10", numero="POS-42", tabla="sales")

    movs = cuenta_corriente.get_cc_movimientos(cid, VENTAS_LIBRACOMMERCE)
    assert len(movs) == 1
    assert movs[0]["concepto"] == "Venta #POS-42"
    assert movs[0]["fecha"] == "2026-07-10"
    assert movs[0]["tipo"] == "debito"


def test_el_saldo_combina_las_cuatro_fuentes(conn):
    """Venta fiada + factura a cuenta corriente + débito directo − pago.

    Vale la pena tenerlo explícito: la verificación contra la base real de
    Contalibra (2026-07-28) encontró un solo cliente con saldo distinto de
    cero, así que la mezcla de fuentes no está cubierta por datos reales.
    """
    cid = clients.create_client("Cliente Completo", cuit_dni="20-11111111-2")
    _venta_fiada(conn, cid, 1000.0, tabla="sales")
    conn.execute(
        """INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit,
                                 cliente_razon, items, subtotal, iva_amount, total)
           VALUES (6, 1, 1, '2026-07-21', '20-11111111-2', 'Cliente Completo',
                   '[]', 413.22, 86.78, 500.0)"""
    )
    conn.execute(
        """INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, medio_pago, factura_id)
           VALUES ('2026-07-21', 'ingreso', 'Factura B 0001-00000001', 500.0,
                   'cuenta_corriente', 1)"""
    )
    conn.commit()
    cuenta_corriente.create_cc_debito(cid, 250.0, "2026-07-22", "Venta POS-9", "sale-9")
    cuenta_corriente.create_cc_pago(cid, 300.0, "2026-07-23", "Pago", "", "efectivo", None, None)

    # 1000 + 500 + 250 − 300
    assert cuenta_corriente.get_cc_saldo(cid, VENTAS_LIBRACOMMERCE) == 1450.0

    movs = cuenta_corriente.get_cc_movimientos(cid, VENTAS_LIBRACOMMERCE)
    assert len(movs) == 4
    assert sum(1 for m in movs if m["tipo"] == "debito") == 3
    # El saldo del período completo tiene que cerrar con el saldo de la cuenta:
    # es lo que evita que el resumen que se manda por mail diga otra cosa que
    # la pantalla.
    periodo = cuenta_corriente.get_cc_movimientos_periodo(
        cid, "2026-07-01", "2026-07-31", VENTAS_LIBRACOMMERCE
    )
    assert periodo["saldo_final"] == 1450.0


# ── Débitos directos: las ventas que no están en esta base ───────────────────

def test_un_debito_directo_suma_al_saldo(conn):
    cid = clients.create_client("Cliente")
    cuenta_corriente.create_cc_debito(cid, 1200.0, "2026-07-20", "Venta POS-7", "sale-7")

    assert cuenta_corriente.get_cc_saldo(cid) == 1200.0


def test_el_debito_directo_se_cancela_con_un_pago(conn):
    cid = clients.create_client("Cliente")
    cuenta_corriente.create_cc_debito(cid, 1000.0, "2026-07-20", "Venta POS-7", "sale-7")
    cuenta_corriente.create_cc_pago(cid, 400.0, "2026-07-25", "Pago", "", "efectivo", None, None)

    assert cuenta_corriente.get_cc_saldo(cid) == 600.0


def test_el_debito_directo_aparece_en_los_movimientos(conn):
    cid = clients.create_client("Cliente")
    cuenta_corriente.create_cc_debito(cid, 1000.0, "2026-07-20", "Venta POS-7", "sale-7")
    cuenta_corriente.create_cc_pago(cid, 400.0, "2026-07-25", "Pago", "", "efectivo", None, None)

    movs = cuenta_corriente.get_cc_movimientos(cid)
    assert [(m["tipo"], m["monto"]) for m in movs] == [("debito", 1000.0), ("credito", 400.0)]
    assert movs[0]["concepto"] == "Venta POS-7"


def test_repetir_la_referencia_no_fia_dos_veces(conn):
    """Un reintento del cobro no puede duplicar la deuda del cliente."""
    cid = clients.create_client("Cliente")
    primero = cuenta_corriente.create_cc_debito(cid, 1000.0, "2026-07-20", "Venta", "sale-7")
    segundo = cuenta_corriente.create_cc_debito(cid, 1000.0, "2026-07-20", "Venta", "sale-7")

    assert primero == segundo
    assert cuenta_corriente.get_cc_saldo(cid) == 1000.0


def test_sin_referencia_cada_debito_es_uno_nuevo(conn):
    # Dos fiados a mano el mismo día por el mismo monto son dos deudas, no un
    # duplicado -- por eso la idempotencia es por referencia y no por importe.
    cid = clients.create_client("Cliente")
    cuenta_corriente.create_cc_debito(cid, 500.0, "2026-07-20", "Fiado")
    cuenta_corriente.create_cc_debito(cid, 500.0, "2026-07-20", "Fiado")

    assert cuenta_corriente.get_cc_saldo(cid) == 1000.0


def test_el_listado_de_deudores_incluye_los_debitos_directos(conn):
    cid = clients.create_client("Cliente")
    cuenta_corriente.create_cc_debito(cid, 750.0, "2026-07-20", "Venta POS-3", "sale-3")

    deudores = cuenta_corriente.get_clientes_con_saldo_cc()
    assert [(d["id"], d["saldo"]) for d in deudores] == [(cid, 750.0)]


def test_un_producto_sin_debitos_directos_no_cambia_su_saldo(conn):
    """`cc_debitos` vacía tiene que sumar cero: es lo que garantiza que
    Contalibra y Restolibra no se enteren de que la tabla existe."""
    cid = clients.create_client("Cliente")
    _venta_fiada(conn, cid, 900.0, tabla="ventas")

    assert cuenta_corriente.get_cc_saldo(cid) == 900.0
    assert [d["saldo"] for d in cuenta_corriente.get_clientes_con_saldo_cc()] == [900.0]


def test_borrar_un_debito_lo_saca_del_saldo(conn):
    cid = clients.create_client("Cliente")
    did = cuenta_corriente.create_cc_debito(cid, 300.0, "2026-07-20", "Fiado")
    cuenta_corriente.delete_cc_debito(did)

    assert cuenta_corriente.get_cc_saldo(cid) == 0.0


# ── Clientes de otra base ────────────────────────────────────────────────────

def test_el_cliente_externo_se_crea_una_sola_vez(conn):
    primero = clients.resolver_cliente_externo("party-7", "Vecina del 12")
    segundo = clients.resolver_cliente_externo("party-7", "Vecina del 12")

    assert primero == segundo
    assert len([c for c in clients.get_all_clients() if c["external_ref"] == "party-7"]) == 1


def test_el_cliente_externo_refresca_el_nombre(conn):
    """El nombre que vale en el resumen de cuenta es el actual, no el que
    tenía cuando fió por primera vez."""
    cid = clients.resolver_cliente_externo("party-7", "Vecina del 12")
    clients.resolver_cliente_externo("party-7", "María González")

    assert clients.get_client(cid)["name"] == "María González"


def test_dos_clientes_externos_distintos_no_se_mezclan(conn):
    uno = clients.resolver_cliente_externo("party-7", "Cliente A")
    otro = clients.resolver_cliente_externo("party-8", "Cliente B")
    cuenta_corriente.create_cc_debito(uno, 500.0, "2026-07-20", "Venta", "sale-1")

    assert uno != otro
    assert cuenta_corriente.get_cc_saldo(uno) == 500.0
    assert cuenta_corriente.get_cc_saldo(otro) == 0.0


def test_un_external_ref_vacio_es_un_error(conn):
    # Si entrara, todos los clientes sin referencia colapsarían en uno solo y
    # las deudas se mezclarían entre personas distintas.
    with pytest.raises(ValueError):
        clients.resolver_cliente_externo("", "Cliente")
