"""Las lecturas de la cuenta de clientes salen del libro (etapas B3 y B4, ADR-028 y ADR-029).

Lo que se fija acá, contra los dos motores y los tres orígenes de ventas, en un escenario con
las cuatro fuentes, pagos y débitos borrados, un movimiento y una factura anulados, un pago
cuyo importe cambió y ventas fiadas:
- las cuatro lecturas (`get_cc_saldo`, `get_cc_movimientos`, `get_cc_movimientos_periodo` y
  `get_clientes_con_saldo_cc`) dan lo esperado, con importes y órdenes escritos a mano (ya no
  hay un cálculo contra el cual compararlas);
- un hecho revertido no se muestra, y cada movimiento trae los ids y textos que usa la pantalla;
- lo cargado con SQL propio no se ve hasta `reconstruir` (ADR-029);
- `origen` ya no decide el saldo: sólo completa el número de las ventas.
"""
import os

import pytest

from libracore.db import caja, clients, core, cuenta_corriente, facturas, ventas
from libracore.db import libro_de_clientes as lc
from libracore.db.cuenta_corriente import (
    VENTAS_LIBRACOMMERCE,
    VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF,
    VENTAS_LIBRACORE,
)
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
        core.configure(db_path=str(tmp_path / "cc_desde_el_libro.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.commit()
    yield request.param
    lc.registrar_origen_de_ventas(VENTAS_LIBRACORE)
    core._db_path = None
    core._database_url = None


def _factura(cuit, total, fecha, numero):
    with core.get_connection() as c:
        cur = c.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, "
            "items, subtotal, iva_amount, total) VALUES (11, 1, ?, ?, ?, 'X', '[]', ?, 0, ?)",
            (numero, fecha, cuit, total, total))
        return cur.lastrowid


class _Ventas:
    """Las ventas fiadas, en la tabla donde las tenga el origen que se prueba."""

    def __init__(self, origen):
        self.origen = origen
        self._ultimo = 0
        if origen is not VENTAS_LIBRACORE:
            with core.get_connection() as c:
                c.execute("CREATE TABLE sales (id INTEGER PRIMARY KEY, number TEXT, occurred_on TEXT, "
                          "customer_party_id INTEGER)")
        lc.registrar_origen_de_ventas(origen)

    def party_de(self, cliente_id):
        """Con `external_ref` el party no tiene el id del cliente (VentaLibra)."""
        return cliente_id + 100 if self.origen is VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF else cliente_id

    def enlazar(self, cliente_id):
        if self.origen is VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF:
            with core.get_connection() as c:
                c.execute("UPDATE clients SET external_ref = ? WHERE id = ?",
                          (f"party-{self.party_de(cliente_id)}", cliente_id))

    def fiada(self, cliente_id, numero, fecha, monto, efectivo=0):
        self._ultimo += 1
        venta_id = self._ultimo
        with core.get_connection() as c:
            if self.origen is VENTAS_LIBRACORE:
                c.execute("INSERT INTO ventas (id, numero, fecha, cliente_id, items, total) "
                          "VALUES (?,?,?,?,?,?)", (venta_id, numero, fecha, cliente_id, "[]", monto))
            else:
                # `ventas_pagos.venta_id` tiene FK a `ventas`: la fila existe sin cliente y la
                # venta de verdad es la de `sales`.
                c.execute("INSERT INTO ventas (id, numero, fecha, items, total) VALUES (?,?,?,?,?)",
                          (venta_id, f"x-{venta_id}", fecha, "[]", monto))
                c.execute("INSERT INTO sales VALUES (?,?,?,?)",
                          (venta_id, numero, fecha, self.party_de(cliente_id)))
        ventas.add_venta_pago(venta_id, "cuenta_corriente", monto)
        if efectivo:
            ventas.add_venta_pago(venta_id, "efectivo", efectivo)
        return venta_id


def _escenario(origen):
    """Las cuatro fuentes y todo lo que las saca de la cuenta. Devuelve los ids que importan.

    Los importes son exactos en binario (`.5`, `.25`, `.75`): la comparación es de igualdad.
    Dentro de la lista de ventas y de la de facturas las fechas son distintas, porque el
    cálculo no ordena una fuente contra sí misma; entre fuentes sí hay empates.
    """
    with core.get_connection() as c:
        c.execute("INSERT INTO usuarios (id, username, nombre, password_hash, role) "
                  "VALUES (7, 'ana', 'Ana', 'x', 'admin')")
    v = _Ventas(origen)
    a = clients.create_client("Acopio Sur", cuit_dni="20-11111111-2")
    b = clients.create_client("Barraca Norte", cuit_dni="20222222223")
    c_ = clients.create_client("Cliente que no queda")
    d = clients.create_client("Sin nada")
    e = clients.create_client("Acopio a favor")
    for cliente in (a, b, c_, d, e):
        v.enlazar(cliente)

    # Ventas fiadas (una con pago mixto: el efectivo no es deuda).
    v.fiada(a, "V-1", "2026-09-10 11:30", 1000.5)
    v.fiada(a, "V-2", "2026-09-20", 200.25, efectivo=50)
    v.fiada(b, "V-3", "2026-10-01", 100)

    # Facturas cobradas a cuenta corriente, con y sin guiones en el CUIT.
    f1 = _factura("20-11111111-2", 500, "2026-09-15", 1)
    caja.create_caja_movimiento("2026-09-15", "ingreso", "Factura 1", 500, referencia="ref-f1",
                                factura_id=f1, medio_pago="Cuenta Corriente", usuario_id=7)
    f2 = _factura("20111111112", 90.5, "2026-09-25", 2)
    caja.create_caja_movimiento("2026-09-25", "ingreso", "Factura 2", 90.5, factura_id=f2,
                                medio_pago="cuenta_corriente")
    # Una factura anulada y un movimiento anulado: no cuentan, y el libro los revirtió.
    f3 = _factura("20111111112", 700, "2026-09-26", 3)
    caja.create_caja_movimiento("2026-09-26", "ingreso", "Factura 3", 700, factura_id=f3,
                                medio_pago="cuenta_corriente")
    facturas.anular_factura(f3, usuario_id=None, motivo="error")
    f4 = _factura("20111111112", 60, "2026-09-27", 4)
    mov = caja.create_caja_movimiento("2026-09-27", "ingreso", "Factura 4", 60, factura_id=f4,
                                      medio_pago="cuenta_corriente")
    caja.anular_caja_movimiento(mov)
    # Un cobro en efectivo de una factura no es cuenta corriente.
    f5 = _factura("20111111112", 30, "2026-09-28", 5)
    caja.create_caja_movimiento("2026-09-28", "ingreso", "Factura 5", 30, factura_id=f5,
                                medio_pago="efectivo")

    # Débitos y pagos directos, uno de cada uno borrado.
    cuenta_corriente.create_cc_debito(a, 250, "2026-09-15", "Reserva", "res-1", usuario_id=7)
    cuenta_corriente.create_cc_debito(a, 33, "2026-09-16", "", "")
    borrado = cuenta_corriente.create_cc_debito(a, 999, "2026-09-17", "Se borra", "res-2")
    cuenta_corriente.delete_cc_debito(borrado)
    cuenta_corriente.create_cc_pago(a, 300, "2026-09-15", "Pago", "rec-1", "transferencia", None, 7)
    cuenta_corriente.create_cc_pago(a, 100.75, "2026-10-02", "", "", "efectivo", None, None)
    pago_borrado = cuenta_corriente.create_cc_pago(a, 800, "2026-10-03", "Se borra", "", "efectivo",
                                                   None, None)
    cuenta_corriente.delete_cc_pago(pago_borrado)
    # Un pago cuyo importe cambió: el libro revierte el asiento y vuelve a asentar.
    cambiado = cuenta_corriente.create_cc_pago(b, 40, "2026-10-04", "Pago de B", "", "efectivo",
                                               None, None)
    with core.get_connection() as conn:
        conn.execute("UPDATE cc_pagos SET monto = 45.5 WHERE id = ?", (cambiado,))
    lc.sincronizar(f"cc_pago:{cambiado}")
    # C: un pago que se borró entero. E: sólo un pago (saldo a favor).
    sin_rastro = cuenta_corriente.create_cc_pago(c_, 10, "2026-10-01", "x", "", "efectivo", None, None)
    cuenta_corriente.delete_cc_pago(sin_rastro)
    cuenta_corriente.create_cc_pago(e, 70, "2026-10-01", "Adelanto", "", "efectivo", None, None)

    # Los escritores dejaron todo en el libro: no hay nada que reconstruir.
    assert lc.reconstruir(origen) == 0
    return {"a": a, "b": b, "c": c_, "d": d, "e": e, "cambiado": cambiado}


def _lecturas(ids, origen):
    """Las cuatro lecturas, de todos los clientes y de varios períodos."""
    clientes = [ids[k] for k in "abcde"]
    return {
        "saldos": {c: cuenta_corriente.get_cc_saldo(c, origen) for c in clientes},
        "movimientos": {c: cuenta_corriente.get_cc_movimientos(c, origen) for c in clientes},
        "periodos": {(c, desde, hasta): cuenta_corriente.get_cc_movimientos_periodo(c, desde, hasta, origen)
                     for c in clientes
                     for desde, hasta in (("2026-09-01", "2026-12-31"), ("2026-09-16", "2026-09-30"),
                                          ("2026-10-01", "2026-10-31"), ("2027-01-01", "2027-01-31"))},
        "clientes": cuenta_corriente.get_clientes_con_saldo_cc(origen),
    }


@pytest.fixture(params=["libracore", "libracommerce", "external_ref"])
def origen(request):
    return {"libracore": VENTAS_LIBRACORE, "libracommerce": VENTAS_LIBRACOMMERCE,
            "external_ref": VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF}[request.param]


def test_las_cuatro_lecturas_dan_lo_esperado(base, origen):
    ids = _escenario(origen)
    a, b, c_, d, e = (ids[k] for k in "abcde")
    lecturas = _lecturas(ids, origen)

    assert lecturas["saldos"] == {a: 1000.5 + 200.25 + 500 + 90.5 + 250 + 33 - 300 - 100.75,
                                  b: 100 - 45.5, c_: 0, d: 0, e: -70}
    assert all(isinstance(s, float) for s in lecturas["saldos"].values())
    assert [(m["fecha"], m["concepto"], m["tipo"], m["monto"]) for m in lecturas["movimientos"][a]] == [
        ("2026-09-10", "Venta #V-1", "debito", 1000.5),
        ("2026-09-15", "FACTURA C 0001-00000001", "debito", 500),
        ("2026-09-15", "Reserva", "debito", 250),
        ("2026-09-15", "Pago", "credito", 300),
        ("2026-09-16", "Venta a cuenta corriente", "debito", 33),
        ("2026-09-20", "Venta #V-2", "debito", 200.25),
        ("2026-09-25", "FACTURA C 0001-00000002", "debito", 90.5),
        ("2026-10-02", "Pago a cuenta", "credito", 100.75)]
    assert [(m["concepto"], m["monto"]) for m in lecturas["movimientos"][b]] == [
        ("Venta #V-3", 100), ("Pago de B", 45.5)]
    assert lecturas["movimientos"][c_] == lecturas["movimientos"][d] == []
    assert [m["concepto"] for m in lecturas["movimientos"][e]] == ["Adelanto"]
    assert [(c["id"], c["name"], c["saldo"]) for c in lecturas["clientes"]] == [
        (a, "Acopio Sur", 1673.5), (b, "Barraca Norte", 54.5), (e, "Acopio a favor", -70)]

    def _periodo(cliente, desde, hasta):
        p = lecturas["periodos"][(cliente, desde, hasta)]
        return (p["saldo_anterior"], len(p["movimientos"]), p["total_debitos"], p["total_creditos"],
                p["saldo_final"])

    assert _periodo(a, "2026-09-01", "2026-12-31") == (0, 8, 2074.25, 400.75, 1673.5)
    assert _periodo(a, "2026-09-16", "2026-09-30") == (1450.5, 3, 323.75, 0, 1774.25)
    assert _periodo(a, "2026-10-01", "2026-10-31") == (1774.25, 1, 0, 100.75, 1673.5)
    assert _periodo(a, "2027-01-01", "2027-01-31") == (1673.5, 0, 0, 0, 1673.5)
    assert _periodo(b, "2026-10-01", "2026-10-31") == (0, 2, 100, 45.5, 54.5)
    assert _periodo(c_, "2026-09-01", "2026-12-31") == (0, 0, 0, 0, 0)
    # Un cliente sin movimientos en el período, pero con saldo, entra con ese saldo.
    assert _periodo(e, "2027-01-01", "2027-01-31") == (-70, 0, 0, 0, -70)


def test_la_lista_no_muestra_lo_revertido(base):
    """Un hecho revertido no se muestra: ni el original ni su contrapartida (ADR-028)."""
    ids = _escenario(VENTAS_LIBRACORE)

    movs = cuenta_corriente.get_cc_movimientos(ids["a"])
    assert [(m["fecha"], m["tipo"], m["monto"]) for m in movs] == [
        ("2026-09-10", "debito", 1000.5), ("2026-09-15", "debito", 500), ("2026-09-15", "debito", 250),
        ("2026-09-15", "credito", 300), ("2026-09-16", "debito", 33), ("2026-09-20", "debito", 200.25),
        ("2026-09-25", "debito", 90.5), ("2026-10-02", "credito", 100.75)]
    # Los asientos de lo revertido siguen en el libro, y la lista no los trae.
    with core.get_connection() as c:
        revertidos = c.execute("SELECT COUNT(*) FROM cc_asientos WHERE contrapartida_de IS NOT NULL"
                               ).fetchone()[0]
    # El débito y el pago borrados, la factura y el movimiento anulados, el pago de B que
    # cambió de importe y el pago de C que se borró entero.
    assert revertidos == 6
    assert all(m["monto"] not in (999, 800, 700, 60) for m in movs)


def test_la_forma_de_cada_movimiento(base):
    """Los ids y los textos que usa la pantalla (borrar un pago, enlazar la factura)."""
    ids = _escenario(VENTAS_LIBRACORE)
    movs = cuenta_corriente.get_cc_movimientos(ids["a"])

    por_concepto = {m["concepto"]: m for m in movs}
    assert por_concepto["Pago"]["cc_pago_id"] is not None
    assert por_concepto["Pago"]["usuario_nombre"] == "Ana"
    assert por_concepto["Pago"]["medio"] == "transferencia"
    assert por_concepto["Pago a cuenta"]["medio"] == "efectivo"
    assert por_concepto["Reserva"]["cc_debito_id"] is not None
    assert por_concepto["Reserva"]["referencia"] == "res-1"
    assert por_concepto["FACTURA C 0001-00000001"]["factura_id"] is not None
    assert por_concepto["FACTURA C 0001-00000001"]["referencia"] == "ref-f1"
    assert por_concepto["FACTURA C 0001-00000001"]["usuario_nombre"] == "Ana"
    assert por_concepto["Venta #V-1"]["venta_id"] == 1
    assert "cc_debito_id" not in por_concepto["Pago"]
    claves = {"fecha", "tipo", "concepto", "monto", "referencia", "medio", "venta_id", "factura_id",
              "cc_pago_id", "usuario_nombre"}
    assert all(claves <= set(m) for m in movs)


def test_lo_cargado_con_sql_propio_no_se_ve_hasta_reconstruir(base):
    """ADR-029: el libro es la única lectura, y se llena por los escritores del motor.
    Un pago cargado con SQL propio no está en el libro; `reconstruir` (lo que hace un
    deploy) lo pone."""
    cid = clients.create_client("Acopio Sur", cuit_dni="20111111112")
    cuenta_corriente.create_cc_debito(cid, 500, "2026-10-01", "Reserva")
    with core.get_connection() as c:
        c.execute("INSERT INTO cc_pagos (cliente_id, monto, fecha, concepto) VALUES (?, 120, '2026-10-02', 'p')",
                  (cid,))

    assert cuenta_corriente.get_cc_saldo(cid) == 500
    assert [m["concepto"] for m in cuenta_corriente.get_cc_movimientos(cid)] == ["Reserva"]

    assert lc.reconstruir() == 1
    assert cuenta_corriente.get_cc_saldo(cid) == 380
    assert [m["concepto"] for m in cuenta_corriente.get_cc_movimientos(cid)] == ["Reserva", "p"]
    assert cuenta_corriente.get_cc_movimientos_periodo(cid, "2026-10-02", "2026-10-31")["saldo_final"] == 380
    assert [(c["id"], c["saldo"]) for c in cuenta_corriente.get_clientes_con_saldo_cc()] == [(cid, 380)]


def test_el_origen_no_decide_el_saldo(base):
    """`origen` se acepta para no cambiarle la firma a los productos, pero el saldo es el del
    libro: con cualquier origen, incluso uno cuya tabla de ventas no existe, da lo mismo."""
    ids = _escenario(VENTAS_LIBRACORE)
    a = ids["a"]
    saldo = cuenta_corriente.get_cc_saldo(a)

    for otro in (VENTAS_LIBRACOMMERCE, VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF):
        assert cuenta_corriente.get_cc_saldo(a, otro) == saldo == 1673.5
        assert cuenta_corriente.get_clientes_con_saldo_cc(otro) == cuenta_corriente.get_clientes_con_saldo_cc()
        movs = cuenta_corriente.get_cc_movimientos(a, otro)
        assert [m["monto"] for m in movs] == [m["monto"] for m in cuenta_corriente.get_cc_movimientos(a)]
        # Sin la tabla de ventas del origen, la venta sale con el concepto del asiento y sin su id.
        venta = next(m for m in movs if m["monto"] == 1000.5)
        assert (venta["concepto"], venta["venta_id"]) == ("Venta V-1", None)


def test_un_debito_o_un_pago_negativo_conserva_su_tipo_y_su_signo(base):
    """LibraDesk registra la anulación de un remito como un `cc_debito` de −18150 (La Grace,
    producción). El libro lo asienta al haber, pero el movimiento sigue siendo lo que mostraba
    el cálculo: un débito de monto negativo, no un crédito de 18150. Igual un pago negativo:
    un crédito de monto negativo. El saldo da lo mismo de las dos formas; la lista y los
    totales del período no."""
    cid = clients.create_client("Cliente de remitos")
    cuenta_corriente.create_cc_debito(cid, 1000, "2026-10-01", "Remito 1", "rem-1")
    cuenta_corriente.create_cc_debito(cid, -18150, "2026-10-02", "Anulación remito 1", "rem-1-anul")
    cuenta_corriente.create_cc_pago(cid, 200, "2026-10-03", "Pago", "", "efectivo", None, None)
    cuenta_corriente.create_cc_pago(cid, -50, "2026-10-04", "Pago devuelto", "", "efectivo", None, None)

    movs = cuenta_corriente.get_cc_movimientos(cid)
    assert [(m["fecha"], m["tipo"], m["concepto"], m["monto"]) for m in movs] == [
        ("2026-10-01", "debito", "Remito 1", 1000.0),
        ("2026-10-02", "debito", "Anulación remito 1", -18150.0),
        ("2026-10-03", "credito", "Pago", 200.0),
        ("2026-10-04", "credito", "Pago devuelto", -50.0)]
    assert movs[1]["cc_debito_id"] is not None and movs[3]["cc_pago_id"] is not None

    assert cuenta_corriente.get_cc_saldo(cid) == 1000 - 18150 - 200 + 50 == -17300
    assert [(c["id"], c["saldo"]) for c in cuenta_corriente.get_clientes_con_saldo_cc()] == [(cid, -17300)]

    entero = cuenta_corriente.get_cc_movimientos_periodo(cid, "2026-10-01", "2026-10-31")
    assert (entero["saldo_anterior"], len(entero["movimientos"]), entero["total_debitos"],
            entero["total_creditos"], entero["saldo_final"]) == (0, 4, -17150, 150, -17300)
    # El saldo de apertura de un período posterior arrastra los dos con su signo.
    tarde = cuenta_corriente.get_cc_movimientos_periodo(cid, "2026-10-03", "2026-10-31")
    assert (tarde["saldo_anterior"], tarde["total_debitos"], tarde["total_creditos"],
            tarde["saldo_final"]) == (-17150, 0, 150, -17300)

    # Si la fila del hecho ya no está (borrada con SQL propio y sin sincronizar), el movimiento
    # sigue con lo que dice el asiento: el débito negativo, que se asentó al haber, sale crédito.
    with core.get_connection() as c:
        c.execute("DELETE FROM cc_debitos WHERE referencia = 'rem-1-anul'")
    huerfano = next(m for m in cuenta_corriente.get_cc_movimientos(cid) if m["fecha"] == "2026-10-02")
    assert (huerfano["tipo"], huerfano["monto"], huerfano["cc_debito_id"]) == ("credito", 18150.0, None)
