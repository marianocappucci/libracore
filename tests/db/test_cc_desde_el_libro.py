"""Las lecturas de la cuenta de clientes desde el libro (etapa B3, ADR-028).

Lo que se fija acá, contra los dos motores y los tres orígenes de ventas:
- con `LIBRACORE_CC_DESDE_EL_LIBRO` apagada y encendida las cuatro lecturas
  (`get_cc_saldo`, `get_cc_movimientos`, `get_cc_movimientos_periodo` y
  `get_clientes_con_saldo_cc`) devuelven **exactamente** lo mismo, en un escenario con
  las cuatro fuentes, pagos y débitos borrados, un movimiento y una factura anulados, un
  pago cuyo importe cambió y ventas fiadas;
- encendida, no pasa por el cálculo (se prueba rompiéndolo);
- encendida en una base sin `cc_asientos`, lee calculado;
- el interruptor entiende los valores que dice el ADR, y apagado es el default.
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

VARIABLE = "LIBRACORE_CC_DESDE_EL_LIBRO"


@pytest.fixture(params=["sqlite", "postgres"])
def base(request, tmp_path, monkeypatch):
    monkeypatch.delenv(VARIABLE, raising=False)
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

    assert lc.comparar(origen) == []
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


def test_apagado_y_encendido_las_cuatro_lecturas_dan_lo_mismo(base, origen, monkeypatch):
    ids = _escenario(origen)

    monkeypatch.delenv(VARIABLE, raising=False)
    assert not lc.lee_del_libro()
    calculado = _lecturas(ids, origen)

    monkeypatch.setenv(VARIABLE, "1")
    assert lc.lee_del_libro()
    # Encendida no pasa por el cálculo: si lo tocara, esto lo rompe.
    def _no_calcular(*_a, **_k):
        raise AssertionError("leyó calculado con el interruptor encendido")
    for nombre in ("get_cc_saldo_calculado", "get_cc_movimientos_calculados",
                   "get_clientes_con_saldo_calculado"):
        monkeypatch.setattr(cuenta_corriente, nombre, _no_calcular)
    del_libro = _lecturas(ids, origen)

    # Que el escenario no sea vacío: de lo contrario la igualdad no prueba nada.
    a, b = ids["a"], ids["b"]
    assert calculado["saldos"] == {a: 1000.5 + 200.25 + 500 + 90.5 + 250 + 33 - 300 - 100.75,
                                   b: 100 - 45.5, ids["c"]: 0, ids["d"]: 0, ids["e"]: -70}
    assert [m["concepto"] for m in calculado["movimientos"][a]] == [
        "Venta #V-1", "FACTURA C 0001-00000001", "Reserva", "Pago", "Venta a cuenta corriente",
        "Venta #V-2", "FACTURA C 0001-00000002", "Pago a cuenta"]
    assert [(c["id"], c["saldo"]) for c in calculado["clientes"]] == [(a, 1673.5), (b, 54.5), (ids["e"], -70)]

    assert del_libro["saldos"] == calculado["saldos"]
    assert del_libro["movimientos"] == calculado["movimientos"]
    assert del_libro["periodos"] == calculado["periodos"]
    assert del_libro["clientes"] == calculado["clientes"]
    assert all(isinstance(s, float) for s in del_libro["saldos"].values())


def test_la_lista_no_muestra_lo_revertido_y_sale_del_libro(base, monkeypatch):
    """Un hecho revertido no se muestra: ni el original ni su contrapartida (ADR-028)."""
    ids = _escenario(VENTAS_LIBRACORE)
    monkeypatch.setenv(VARIABLE, "1")

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


def test_la_forma_de_cada_movimiento_es_la_del_calculo(base, monkeypatch):
    """Los ids y los textos que usa la pantalla (borrar un pago, enlazar la factura)."""
    ids = _escenario(VENTAS_LIBRACORE)
    calculados = cuenta_corriente.get_cc_movimientos(ids["a"])
    monkeypatch.setenv(VARIABLE, "true")
    del_libro = cuenta_corriente.get_cc_movimientos(ids["a"])

    assert del_libro == calculados
    por_concepto = {m["concepto"]: m for m in del_libro}
    assert por_concepto["Pago"]["cc_pago_id"] is not None
    assert por_concepto["Pago"]["usuario_nombre"] == "Ana"
    assert por_concepto["Pago"]["medio"] == "transferencia"
    assert por_concepto["Pago a cuenta"]["medio"] == "efectivo"
    assert por_concepto["Reserva"]["cc_debito_id"] is not None
    assert por_concepto["FACTURA C 0001-00000001"]["factura_id"] is not None
    assert por_concepto["FACTURA C 0001-00000001"]["referencia"] == "ref-f1"
    assert por_concepto["Venta #V-1"]["venta_id"] == 1
    assert "cc_debito_id" not in por_concepto["Pago"]


def test_encendido_lee_lo_que_dice_el_libro(base, monkeypatch):
    """Un pago cargado con SQL propio, sin pasar por el motor, no está en el libro:
    el interruptor decide si se ve. `reconstruir` lo pone y los dos vuelven a coincidir."""
    cid = clients.create_client("Acopio Sur", cuit_dni="20111111112")
    cuenta_corriente.create_cc_debito(cid, 500, "2026-10-01", "Reserva")
    with core.get_connection() as c:
        c.execute("INSERT INTO cc_pagos (cliente_id, monto, fecha, concepto) VALUES (?, 120, '2026-10-02', 'p')",
                  (cid,))

    assert cuenta_corriente.get_cc_saldo(cid) == 380
    monkeypatch.setenv(VARIABLE, "si")
    assert cuenta_corriente.get_cc_saldo(cid) == 500
    assert [m["concepto"] for m in cuenta_corriente.get_cc_movimientos(cid)] == ["Reserva"]

    assert lc.reconstruir() == 1
    assert cuenta_corriente.get_cc_saldo(cid) == 380
    assert [m["concepto"] for m in cuenta_corriente.get_cc_movimientos(cid)] == ["Reserva", "p"]


def test_sin_cc_asientos_lee_calculado_aunque_este_encendido(base, monkeypatch):
    cid = clients.create_client("Acopio Sur", cuit_dni="20111111112")
    cuenta_corriente.create_cc_debito(cid, 500, "2026-10-01", "Reserva")
    cuenta_corriente.create_cc_pago(cid, 120, "2026-10-02", "Pago", "", "efectivo", None, None)
    with core.get_connection() as c:
        c.execute("DROP TABLE cc_asientos")
    monkeypatch.setenv(VARIABLE, "1")

    assert not lc.lee_del_libro()
    assert cuenta_corriente.get_cc_saldo(cid) == 380
    assert [m["concepto"] for m in cuenta_corriente.get_cc_movimientos(cid)] == ["Reserva", "Pago"]
    assert cuenta_corriente.get_cc_movimientos_periodo(cid, "2026-10-02", "2026-10-31")["saldo_final"] == 380
    assert [(c["id"], c["saldo"]) for c in cuenta_corriente.get_clientes_con_saldo_cc()] == [(cid, 380)]


@pytest.mark.parametrize("valor", ["1", "true", "TRUE", "True", "si", "SI", "sí", "Sí", " 1 "])
def test_los_valores_que_encienden(base, monkeypatch, valor):
    monkeypatch.setenv(VARIABLE, valor)
    assert lc.lee_del_libro()


@pytest.mark.parametrize("valor", ["", "0", "no", "false", "off", "yes", "2"])
def test_los_demas_valores_y_la_variable_ausente_apagan(base, monkeypatch, valor):
    monkeypatch.setenv(VARIABLE, valor)
    assert not lc.lee_del_libro()
    monkeypatch.delenv(VARIABLE)
    assert not lc.lee_del_libro()
