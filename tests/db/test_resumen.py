"""El nucleo del resumen: lo que cualquier producto de la familia puede
contestarle al panel del cliente.

Lo que estos tests fijan, en orden de importancia:

1. 🔴 **"Sin cobrar" es un CONTEO, no una muestra.** `get_dashboard_data`
   devuelve `facturas_sin_cobrar` con `LIMIT 8`; consolidando cinco sucursales,
   sumar muestras daria el tope en vez del dato.
2. 🔴 **La cuenta corriente NO es un cobro.** Es la misma definicion que usa
   `get_cobros_factura` — que es lo que muestra la pantalla de comprobantes—, y
   mirar solo `factura_id IS NULL` la contradice.
3. El periodo filtra lo que tiene que filtrar, y el saldo de caja es historico.
"""
import pytest

from libracore.db import core
from libracore.db.resumen import get_resumen_core


@pytest.fixture(autouse=True)
def _base(tmp_path, crear_schema):
    core._db_path = None
    core.configure(db_path=str(tmp_path / "resumen.db"))
    with core.get_connection() as conn:
        crear_schema(conn)
        conn.commit()
    yield
    core._db_path = None


def _factura(total=100.0, fecha="2026-08-10", numero=1):
    with core.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, "
            "cliente_razon, cliente_iva_cond, items, subtotal, iva_amount, total) "
            "VALUES (11, 1, ?, ?, '', 'Consumidor Final', 5, '[]', ?, 0, ?)",
            (numero, fecha, total, total),
        )
        conn.commit()
        return cur.lastrowid


def _movimiento(monto, *, tipo="ingreso", fecha="2026-08-10", factura_id=None, medio="efectivo"):
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, referencia, "
            "factura_id, medio_pago) VALUES (?, ?, 'prueba', ?, '', ?, ?)",
            (fecha, tipo, monto, factura_id, medio),
        )
        conn.commit()


PERIODO = ("2026-08-01", "2026-08-31")


def test_una_base_vacia_da_ceros_y_no_falla():
    r = get_resumen_core(*PERIODO)
    assert r["facturado"] == 0.0
    assert r["comprobantes"] == 0
    assert r["sin_cobrar"] == {"cantidad": 0, "monto": 0.0}


def test_factura_y_caja_del_periodo():
    _factura(total=1000.0, numero=1)
    _movimiento(1000.0)
    _movimiento(300.0, tipo="egreso")

    r = get_resumen_core(*PERIODO)

    assert r["facturado"] == 1000.0
    assert r["comprobantes"] == 1
    assert r["cobrado"] == 1000.0
    assert r["egresos"] == 300.0
    assert r["saldo_caja"] == 700.0


def test_lo_de_otro_periodo_no_entra():
    _factura(total=500.0, fecha="2026-07-15", numero=1)
    _movimiento(500.0, fecha="2026-07-15")

    r = get_resumen_core(*PERIODO)

    assert r["facturado"] == 0.0
    assert r["cobrado"] == 0.0
    # 🔑 El saldo es HISTORICO y por eso sí lo ve: es cuanta plata hay, no
    # cuanta entro en el periodo.
    assert r["saldo_caja"] == 500.0


def test_sin_cobrar_es_un_conteo_y_no_una_muestra():
    """🔴 Con NUEVE impagas, el conteo tiene que decir 9 y no 8."""
    for n in range(1, 10):
        _factura(total=100.0, numero=n)

    r = get_resumen_core(*PERIODO)

    assert r["sin_cobrar"]["cantidad"] == 9
    assert r["sin_cobrar"]["monto"] == 900.0


def test_sin_cobrar_mira_todas_y_no_solo_las_del_periodo():
    """Una factura de marzo sin cobrar es plata que falta en agosto."""
    _factura(total=100.0, fecha="2026-03-01", numero=1)
    r = get_resumen_core(*PERIODO)
    assert r["sin_cobrar"]["cantidad"] == 1


def test_una_factura_cobrada_no_cuenta_como_impaga():
    fid = _factura(total=100.0, numero=1)
    _movimiento(100.0, factura_id=fid)

    assert get_resumen_core(*PERIODO)["sin_cobrar"]["cantidad"] == 0


def test_la_cuenta_corriente_NO_es_un_cobro():
    """🔴 Plata que no entro.

    Es la definicion de `get_cobros_factura`, que es lo que muestra la pantalla
    de comprobantes. Mirar solo `factura_id IS NULL` la daria por cobrada y el
    panel mostraria menos deuda de la que hay — la direccion peligrosa.
    """
    fid = _factura(total=100.0, numero=1)
    _movimiento(100.0, factura_id=fid, medio="Cuenta Corriente")

    r = get_resumen_core(*PERIODO)

    assert r["sin_cobrar"]["cantidad"] == 1
    assert r["sin_cobrar"]["monto"] == 100.0


def test_un_cobro_parcial_en_efectivo_la_saca_de_impagas():
    """El control de que el test de arriba mide el MEDIO y no el monto."""
    fid = _factura(total=100.0, numero=1)
    _movimiento(40.0, factura_id=fid, medio="efectivo")

    assert get_resumen_core(*PERIODO)["sin_cobrar"]["cantidad"] == 0


def test_las_notas_no_cuentan_como_facturado():
    """Una nota de credito resta por otro lado; sumarla infla el facturado."""
    _factura(total=1000.0, numero=1)
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, "
            "cliente_razon, cliente_iva_cond, items, subtotal, iva_amount, total) "
            "VALUES (13, 1, 1, '2026-08-11', '', 'X', 5, '[]', 200, 0, 200)"
        )
        conn.commit()

    r = get_resumen_core(*PERIODO)

    assert r["facturado"] == 1000.0
    assert r["comprobantes"] == 1
