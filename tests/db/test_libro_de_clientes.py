"""La cuenta de clientes como libro (`libracore.db.libro_de_clientes`, opción B, etapas B1 a B4).

Lo que se fija acá, contra los dos motores:
- cada escritor del motor deja su asiento, en su transacción, y lo revierte con la
  fecha del original cuando el hecho deja de contar (borrar, anular);
- el saldo que se lee (`get_cc_saldo`) es el del libro, y suma las cuatro fuentes;
- `reconstruir` llena el libro desde los hechos, y repetirlo no escribe nada: lo cargado
  con SQL propio no se ve hasta que se corre (ADR-029);
- el cliente de un hecho se fija la primera vez: cambiar el CUIT no mueve la deuda;
- un CUIT de dos clientes no se asienta a ninguno;
- la cuenta de clientes de LibraCargo, que vive en la misma tabla sin origen, no entra;
- sin la tabla del libro, los escritores siguen andando y las lecturas tiran un error claro.
"""
import os

import pytest

from libracore.db import caja, clients, core, cuenta_corriente, facturas, ventas
from libracore.db import libro_de_clientes as lc
from libracore.db import libro_de_terceros as libro
from libracore.db.cuenta_corriente import VENTAS_LIBRACOMMERCE, VENTAS_LIBRACORE
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
        core.configure(db_path=str(tmp_path / "libro_clientes.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.commit()
    yield request.param
    core._db_path = None
    core._database_url = None


def _asientos(origen=None):
    with core.get_connection() as c:
        sql = "SELECT * FROM cc_asientos WHERE origen IS NOT NULL"
        params = ()
        if origen:
            sql, params = sql + " AND origen = ?", (origen,)
        return [dict(f) for f in c.execute(sql + " ORDER BY id", params).fetchall()]


def _factura(cuit, total, fecha="2026-10-01", numero=1):
    with core.get_connection() as c:
        cur = c.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, "
            "items, subtotal, iva_amount, total) VALUES (11, 1, ?, ?, ?, 'X', '[]', ?, 0, ?)",
            (numero, fecha, cuit, total, total))
        return cur.lastrowid


def _venta_fiada(cliente_id, monto, fecha="2026-10-02", numero="V-1"):
    with core.get_connection() as c:
        cur = c.execute("INSERT INTO ventas (numero, fecha, cliente_id, items, total) VALUES (?,?,?,?,?)",
                        (numero, fecha, cliente_id, "[]", monto))
        venta_id = cur.lastrowid
    ventas.add_venta_pago(venta_id, "cuenta_corriente", monto)
    return venta_id


def test_las_cuatro_fuentes_suman_al_saldo_del_libro(base):
    cid = clients.create_client("Acopio Sur", cuit_dni="20-11111111-2")
    _venta_fiada(cid, 1000)
    fid = _factura("20111111112", 500)
    caja.create_caja_movimiento("2026-10-03", "ingreso", "Factura C", 500, factura_id=fid,
                                medio_pago="Cuenta Corriente")
    cuenta_corriente.create_cc_debito(cid, 250, "2026-10-04", "Reserva", "res-1")
    cuenta_corriente.create_cc_pago(cid, 300, "2026-10-05", "Pago", "", "efectivo", None, None)

    assert cuenta_corriente.get_cc_saldo(cid) == 1450
    assert lc.saldos_del_libro() == {cid: 1450}
    assert [a["origen"].split(":")[0] for a in _asientos()] == ["venta_pago", "caja_mov",
                                                                 "cc_debito", "cc_pago"]
    pago = _asientos()[-1]
    assert (pago["fecha"], pago["haber"], pago["rol"], pago["tercero_id"]) == ("2026-10-05", 300, "cliente", cid)


def test_borrar_y_anular_revierten_con_la_fecha_del_original(base):
    cid = clients.create_client("Acopio Sur", cuit_dni="20111111112")
    pago = cuenta_corriente.create_cc_pago(cid, 300, "2026-09-05", "Pago", "", "efectivo", None, None)
    debito = cuenta_corriente.create_cc_debito(cid, 250, "2026-09-04", "Reserva")
    fid = _factura("20111111112", 500)
    mov = caja.create_caja_movimiento("2026-09-03", "ingreso", "Factura C", 500, factura_id=fid,
                                      medio_pago="cuenta_corriente")

    cuenta_corriente.delete_cc_pago(pago)
    cuenta_corriente.delete_cc_debito(debito)
    caja.anular_caja_movimiento(mov)

    for origen, fecha in ((f"cc_pago:{pago}", "2026-09-05"), (f"cc_debito:{debito}", "2026-09-04"),
                          (f"caja_mov:{mov}", "2026-09-03")):
        original, reversion = _asientos(origen)
        assert reversion["contrapartida_de"] == original["id"]
        assert reversion["fecha"] == fecha
    assert cuenta_corriente.get_cc_saldo(cid) == 0
    assert lc.saldos_del_libro() == {cid: 0}
    assert cuenta_corriente.get_cc_movimientos(cid) == []
    # Volver a sincronizar no hace nada: ya está revertido.
    assert lc.reconstruir() == 0


def test_anular_la_factura_o_borrarla_revierte_su_deuda(base):
    cid = clients.create_client("Acopio Sur", cuit_dni="20111111112")
    anulada = _factura("20111111112", 500, numero=1)
    borrada = _factura("20111111112", 700, numero=2)
    caja.create_caja_movimiento("2026-10-03", "ingreso", "F1", 500, factura_id=anulada,
                                medio_pago="cuenta_corriente")
    caja.create_caja_movimiento("2026-10-03", "ingreso", "F2", 700, factura_id=borrada,
                                medio_pago="cuenta_corriente")
    assert lc.saldos_del_libro() == {cid: 1200}

    facturas.anular_factura(anulada, usuario_id=None, motivo="error")
    facturas.delete_factura(borrada)

    assert cuenta_corriente.get_cc_saldo(cid) == 0
    assert lc.saldos_del_libro() == {cid: 0}


def test_dar_de_baja_un_pago_anula_sus_cobros_y_el_libro_los_revierte(base):
    cid = clients.create_client("Acopio Sur", cuit_dni="20111111112")
    fid = _factura("20111111112", 500)
    pago = cuenta_corriente.create_cc_pago(cid, 500, "2026-10-05", "Pago", "", "efectivo", None, None)
    caja.create_caja_movimiento("2026-10-03", "ingreso", "F1", 500, factura_id=fid,
                                medio_pago="cuenta_corriente", cc_pago_id=pago)
    assert lc.saldos_del_libro() == {cid: 0}

    assert caja.anular_movimientos_de_cc_pago(pago) == 1
    cuenta_corriente.delete_cc_pago(pago)

    assert cuenta_corriente.get_cc_saldo(cid) == lc.saldos_del_libro()[cid] == 0


def test_reconstruir_llena_el_libro_desde_los_hechos_y_repetirlo_no_escribe(base):
    cid = clients.create_client("Acopio Sur", cuit_dni="20111111112")
    fid = _factura("20111111112", 500)
    # Hechos cargados por fuera del motor, como estaban antes de esta versión.
    with core.get_connection() as c:
        c.execute("INSERT INTO cc_pagos (cliente_id, monto, fecha, concepto) VALUES (?, 120, '2026-08-01', 'p')",
                  (cid,))
        c.execute("INSERT INTO cc_debitos (cliente_id, monto, fecha, concepto) VALUES (?, 80, '2026-08-02', 'd')",
                  (cid,))
        c.execute("INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, medio_pago, factura_id) "
                  "VALUES ('2026-08-03', 'ingreso', 'F', 500, 'cuenta_corriente', ?)", (fid,))
        c.execute("INSERT INTO ventas (id, numero, fecha, cliente_id, items, total) "
                  "VALUES (50, 'V-50', '2026-08-04 10:30', ?, '[]', 40)", (cid,))
        c.execute("INSERT INTO ventas_pagos (venta_id, medio, monto) VALUES (50, 'cuenta_corriente', 40)")
    assert _asientos() == []
    # Sin pasar por los escritores no hay asientos: la cuenta no los ve (ADR-029).
    assert cuenta_corriente.get_cc_saldo(cid) == 0
    assert cuenta_corriente.get_cc_movimientos(cid) == []

    assert lc.reconstruir() == 4
    assert lc.reconstruir() == 0
    assert lc.saldos_del_libro() == {cid: 500}
    assert cuenta_corriente.get_cc_saldo(cid) == 500
    assert [m["concepto"] for m in cuenta_corriente.get_cc_movimientos(cid)] == [
        "p", "d", "FACTURA C 0001-00000001", "Venta #V-50"]
    assert _asientos("venta_pago:1")[0]["fecha"] == "2026-08-04"

    # Un pago borrado con SQL propio: el libro lo sigue mostrando hasta que `reconstruir`
    # lo revierte.
    with core.get_connection() as c:
        c.execute("DELETE FROM cc_pagos")
    assert cuenta_corriente.get_cc_saldo(cid) == 500
    assert lc.reconstruir() == 1
    assert cuenta_corriente.get_cc_saldo(cid) == 620


def test_el_cliente_de_una_deuda_se_fija_la_primera_vez(base):
    """Decisión del humano, 2026-10-06: cambiar el CUIT no mueve la deuda en el libro."""
    a = clients.create_client("Cliente A", cuit_dni="20111111112")
    fid = _factura("20111111112", 500)
    caja.create_caja_movimiento("2026-10-03", "ingreso", "F1", 500, factura_id=fid,
                                medio_pago="cuenta_corriente")

    clients.update_client(a, cuit_dni="20999999990")

    # El libro la deja donde se asentó (el cálculo de antes se la sacaba), y reconstruir
    # tampoco la mueve.
    assert cuenta_corriente.get_cc_saldo(a) == 500
    assert lc.saldos_del_libro() == {a: 500}
    assert lc.reconstruir() == 0
    assert cuenta_corriente.get_cc_saldo(a) == 500


def test_una_factura_sin_cliente_se_asienta_cuando_su_cuit_tiene_cliente(base):
    fid = _factura("20-11111111-2", 500)
    mov = caja.create_caja_movimiento("2026-10-03", "ingreso", "F1", 500, factura_id=fid,
                                      medio_pago="cuenta_corriente")
    assert _asientos() == []

    cid = clients.create_client("Llegó después", cuit_dni="20111111112")

    assert [a["origen"] for a in _asientos()] == [f"caja_mov:{mov}"]
    assert cuenta_corriente.get_cc_saldo(cid) == 500
    otro = clients.create_client("Otro")
    clients.update_client(otro, cuit_dni="20222222223")
    assert lc.saldos_del_libro() == {cid: 500}


def test_un_cuit_de_dos_clientes_no_se_asienta_a_ninguno(base):
    with core.get_connection() as c:
        for nombre in ("Uno", "Dos"):
            c.execute("INSERT INTO clients (name, cuit_dni) VALUES (?, '20111111112')", (nombre,))
    fid = _factura("20111111112", 500)
    caja.create_caja_movimiento("2026-10-03", "ingreso", "F1", 500, factura_id=fid,
                                medio_pago="cuenta_corriente")
    assert _asientos() == []
    # Elegir uno sería inventar: la deuda no está en la cuenta de ninguno, ni siquiera al reconstruir.
    assert lc.reconstruir() == 0
    assert lc.saldos_del_libro() == {}
    assert cuenta_corriente.get_clientes_con_saldo_cc() == []


def test_las_ventas_fiadas_salen_del_origen_registrado(base):
    """VentaLibra, Contalibra y Restolibra registran su `OrigenVentas` al montar el router."""
    with core.get_connection() as c:
        c.execute("CREATE TABLE sales (id INTEGER PRIMARY KEY, number TEXT, occurred_on TEXT, "
                  "customer_party_id INTEGER)")
    cid = clients.create_client("Party mismo id")
    lc.registrar_origen_de_ventas(VENTAS_LIBRACOMMERCE)
    try:
        with core.get_connection() as c:
            # `ventas_pagos.venta_id` tiene FK a `ventas` en el motor: la fila de `ventas`
            # existe sin cliente, y la venta de verdad es la de `sales`.
            c.execute("INSERT INTO ventas (id, numero, fecha, items, total) VALUES (7, 'x', '2026-10-01', '[]', 0)")
            c.execute("INSERT INTO sales VALUES (7, 'POS-7', '2026-10-02', ?)", (cid,))
        ventas.add_venta_pago(7, "cuenta_corriente", 640)
        ventas.add_venta_pago(7, "efectivo", 10)

        assert [(a["fecha"], a["concepto"], a["debe"]) for a in _asientos()] == [
            ("2026-10-02", "Venta POS-7", 640)]
        # El `origen` que se lee es el que dice dónde están las ventas: completa el número.
        assert cuenta_corriente.get_cc_saldo(cid, VENTAS_LIBRACOMMERCE) == 640
        assert [(m["concepto"], m["venta_id"]) for m in
                cuenta_corriente.get_cc_movimientos(cid, VENTAS_LIBRACOMMERCE)] == [("Venta #POS-7", 7)]
    finally:
        lc.registrar_origen_de_ventas(VENTAS_LIBRACORE)


def test_la_cuenta_de_libracargo_en_la_misma_tabla_no_entra(base):
    """LibraCargo asienta con rol `cliente` y sin origen, sobre sus propios terceros."""
    libro.asentar(1, "cliente", "2026-10-01", "Factura de flete", debe=9000)
    assert lc.saldos_del_libro() == {}
    assert cuenta_corriente.get_cc_saldo(1) == 0
    assert lc.reconstruir() == 0


def test_con_conn_el_asiento_sale_con_la_transaccion_de_quien_llama(base):
    cid = clients.create_client("Acopio Sur")
    with pytest.raises(RuntimeError):
        with core.get_connection() as c:
            cuenta_corriente.create_cc_pago(cid, 300, "2026-10-05", "Pago", "", "efectivo", None, None,
                                            conn=c)
            assert len(_asientos_en(c)) == 1
            raise RuntimeError("falla el documento")
    assert _asientos() == []


def _asientos_en(c):
    return c.execute("SELECT id FROM cc_asientos WHERE origen IS NOT NULL").fetchall()


def test_sin_la_tabla_del_libro_los_escritores_siguen_andando(base):
    """LibraDesk arma a mano las tablas del motor que usa: su base puede no tener el libro."""
    cid = clients.create_client("Acopio Sur", cuit_dni="20111111112")
    with core.get_connection() as c:
        c.execute("DROP TABLE cc_asientos")
    pago = cuenta_corriente.create_cc_pago(cid, 300, "2026-10-05", "Pago", "", "efectivo", None, None)
    cuenta_corriente.create_cc_debito(cid, 500, "2026-10-04", "Reserva")
    cuenta_corriente.delete_cc_pago(pago)
    assert lc.reconstruir() == 0
    assert lc.saldos_del_libro() == {}


def test_sin_la_tabla_del_libro_las_lecturas_dicen_que_falta(base):
    """Leer de un libro que no existe daría saldos en cero, en silencio: mejor un error claro."""
    cid = clients.create_client("Acopio Sur", cuit_dni="20111111112")
    with core.get_connection() as c:
        c.execute("DROP TABLE cc_asientos")
    lecturas = (
        lambda: cuenta_corriente.get_cc_saldo(cid),
        lambda: cuenta_corriente.get_cc_movimientos(cid),
        lambda: cuenta_corriente.get_cc_movimientos_periodo(cid, "2026-10-01", "2026-10-31"),
        lambda: cuenta_corriente.get_clientes_con_saldo_cc(),
    )
    for leer in lecturas:
        with pytest.raises(RuntimeError, match=r"cc_asientos.*0020.*init_core_schema"):
            leer()
