"""El PDF de un comprobante es el mismo archivo cada vez que se lo emite.

fpdf2 sella cada PDF con `datetime.now()` al construir el objeto y lo escribe
como `/CreationDate (D:AAAAMMDDHHMMSS)`, con resolución de segundo. Eso alcanza
para que reimprimir el mismo comprobante devuelva bytes distintos: mismo largo,
un dígito de diferencia.

Comparar dos generaciones seguidas **no** prueba nada por sí solo — es
exactamente el test que VentaLibra tenía, y que pasaba sólo porque las dos
requests entraban en el mismo segundo (falló el 2026-08-12 en la pata de
PostgreSQL del CI, más lenta). Así que acá lo que se afirma es lo que se puede
afirmar sin depender del reloj: que el `/CreationDate` emitido es **la fecha
del documento**, que se fija de antemano y no es la de ahora. La igualdad byte
a byte va detrás, como la consecuencia que se busca.
"""
import os
import re
from datetime import UTC, datetime, timezone

import pytest

from libracore import config_manager, ticket_generator
from libracore import pdf_generator as pg


def _creation_date(pdf: bytes) -> str:
    """El `/CreationDate` del PDF, tal cual quedó escrito."""
    m = re.search(rb"/CreationDate\s*\(([^)]*)\)", pdf)
    assert m, "el PDF no trae /CreationDate"
    return m.group(1).decode("latin-1")


def _bytes_del_archivo(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ── El parser de fechas ───────────────────────────────────────────────────────

@pytest.mark.parametrize("valor, esperado", [
    ("2026-07-28",                    datetime(2026, 7, 28, tzinfo=UTC)),
    ("2026-07-28 14:30",              datetime(2026, 7, 28, 14, 30, tzinfo=UTC)),
    ("2026-07-28 14:30:09",           datetime(2026, 7, 28, 14, 30, 9, tzinfo=UTC)),
    ("2026-07-28T14:30:09Z",          datetime(2026, 7, 28, 14, 30, 9, tzinfo=UTC)),
    ("20260728",                      datetime(2026, 7, 28, tzinfo=UTC)),  # ARCA
    ("2026-07-28 14:30 hs",           datetime(2026, 7, 28, 14, 30, tzinfo=UTC)),
    (datetime(2026, 7, 28, 14, 30),   datetime(2026, 7, 28, 14, 30, tzinfo=UTC)),
])
def test_la_fecha_del_documento_se_interpreta_como_utc(valor, esperado):
    """Sin zona horaria se asume UTC: si se asumiera la local, el mismo
    comprobante daría PDFs distintos según el huso del servidor."""
    assert pg.fecha_de_documento(valor) == esperado


@pytest.mark.parametrize("valor", ["", None, "sin fecha", "13/07/2026"])
def test_una_fecha_que_no_se_entiende_no_rompe_nada(valor):
    assert pg.fecha_de_documento(valor) is None


def test_una_fecha_con_zona_se_respeta():
    from datetime import timedelta
    arg = timezone(timedelta(hours=-3))
    assert pg.fecha_de_documento(datetime(2026, 7, 28, 14, 30, tzinfo=arg)) == \
        datetime(2026, 7, 28, 17, 30, tzinfo=UTC)


# ── Tickets térmicos ──────────────────────────────────────────────────────────

@pytest.fixture
def _config_ticket(tmp_path, monkeypatch):
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(tmp_path / "config.json"))
    config_manager.save({
        "empresa_nombre": "Despensa La Esquina",
        "empresa_cuit": "20-11111111-2",
        "ticket_ancho_mm": "80",
    })


def test_el_ticket_de_venta_se_fecha_con_la_venta(_config_ticket):
    venta = {
        "id": 42,
        "fecha": "2026-07-28 14:30",
        "items": [{"nombre": "Yerba 1kg", "cantidad": 1, "precio_unitario": 1500.0}],
        "total": 1500.0,
    }
    primero = ticket_generator.generar_ticket_venta(venta)
    assert _creation_date(primero) == "D:20260728143000Z"
    assert ticket_generator.generar_ticket_venta(venta) == primero


def test_el_ticket_de_factura_se_fecha_con_la_factura(_config_ticket):
    factura = {
        "tipo": 6, "punto_venta": 1, "numero": 7, "fecha": "2026-07-28",
        "items": [{"description": "Item", "qty": 1, "unit_price": 1000, "subtotal": 1000}],
        "subtotal": 1000, "iva_amount": 0, "total": 1000,
    }
    primero = ticket_generator.generar_ticket_factura(factura)
    assert _creation_date(primero) == "D:20260728000000Z"
    assert ticket_generator.generar_ticket_factura(factura) == primero


def test_un_ticket_sin_fecha_igual_sale(_config_ticket):
    """Sin fecha del documento no hay nada determinista que poner, así que
    queda la de fpdf2. Lo que no puede pasar es que el ticket no salga."""
    salida = ticket_generator.generar_ticket_venta({"id": 1, "items": [], "total": 0})
    assert salida.startswith(b"%PDF-")


# ── Comprobantes A4 ───────────────────────────────────────────────────────────

@pytest.fixture
def pg2(tmp_path, monkeypatch):
    """Mismo aislamiento que `test_pdf_generator`: `DATA_DIR` propio."""
    import importlib

    from libracore import config_manager as cm
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    importlib.reload(cm)
    return importlib.reload(pg)


def _remito(**cambios):
    r = {
        "number": "0001-00000001", "date": "2026-07-14",
        "client_name": "Cliente Test", "client_cuit": "20111111112",
        "client_address": "", "client_email": "", "client_phone": "",
        "items": [{"description": "Item", "qty": 2, "unit_price": 100, "subtotal": 200}],
        "observations": "",
    }
    r.update(cambios)
    return r


def _presupuesto(**cambios):
    p = {
        "number": "0001-00000001", "date": "2026-07-14", "valid_until": "2026-07-21",
        "client_name": "Cliente Test", "client_cuit": "20111111112",
        "client_address": "", "client_email": "", "client_phone": "",
        "items": [{"description": "Item", "qty": 1, "unit_price": 500, "subtotal": 500}],
        "subtotal": 500, "tax_amount": 105, "total": 605, "tax_rate": 0.21,
        "observations": "",
    }
    p.update(cambios)
    return p


def _factura(**cambios):
    f = {
        "tipo": 6, "punto_venta": 1, "numero": 1, "fecha": "2026-07-14",
        "cliente_razon": "Cliente Test", "cliente_cuit": "20111111112",
        "cliente_iva_cond": 5, "cliente_domicilio": "", "condicion_venta": "Contado",
        "items": [{"description": "Item", "qty": 1, "unit_price": 1000, "subtotal": 1000}],
        "subtotal": 1000, "iva_amount": 0, "total": 1000, "concepto": 1,
        "cae": "", "cae_vto": "", "observaciones": "",
    }
    f.update(cambios)
    return f


def test_el_remito_se_fecha_con_el_remito(pg2, tmp_path):
    path = pg2.generate_pdf(_remito(), output_dir=str(tmp_path))
    primero = _bytes_del_archivo(path)
    assert _creation_date(primero) == "D:20260714000000Z"
    os.remove(path)
    assert _bytes_del_archivo(
        pg2.generate_pdf(_remito(), output_dir=str(tmp_path))) == primero


def test_el_presupuesto_se_fecha_con_el_presupuesto(pg2, tmp_path):
    path = pg2.generate_pdf_presupuesto(_presupuesto(), output_dir=str(tmp_path))
    primero = _bytes_del_archivo(path)
    assert _creation_date(primero) == "D:20260714000000Z"
    os.remove(path)
    assert _bytes_del_archivo(
        pg2.generate_pdf_presupuesto(_presupuesto(), output_dir=str(tmp_path))) == primero


def test_la_factura_se_fecha_con_la_factura(pg2, tmp_path):
    path = pg2.generate_pdf_factura(_factura(), output_dir=str(tmp_path))
    primero = _bytes_del_archivo(path)
    assert _creation_date(primero) == "D:20260714000000Z"
    os.remove(path)
    assert _bytes_del_archivo(
        pg2.generate_pdf_factura(_factura(), output_dir=str(tmp_path))) == primero


def test_el_recibo_se_fecha_con_el_comprobante_que_cancela(pg2):
    factura = _factura(total=1000)
    cobros = [{"fecha": "2026-07-14", "medio_pago": "efectivo",
               "referencia": "", "monto": 1000}]
    primero = pg2.generate_pdf_recibo(factura, cobros)
    assert _creation_date(primero) == "D:20260714000000Z"
    assert pg2.generate_pdf_recibo(factura, cobros) == primero


def test_el_recibo_emitido_se_fecha_con_su_propia_fecha(pg2):
    """El recibo numerado es el caso donde la reimpresión tiene que dar lo
    mismo aunque después se hayan cobrado otras cuotas de la misma factura."""
    recibo = {
        "punto_venta": 1, "numero": 3, "fecha": "2026-07-20",
        "cliente_razon": "Cliente Test", "cliente_cuit": "20111111112",
        "concepto": "Pago parcial de Factura B 0001-00000001",
        "pagos": [{"fecha": "2026-07-20", "medio_pago": "efectivo", "monto": 400}],
        "total": 400.0,
    }
    primero = pg2.generate_pdf_recibo_doc(recibo)
    assert _creation_date(primero) == "D:20260720000000Z"
    assert pg2.generate_pdf_recibo_doc(recibo) == primero


def test_el_resumen_de_cuenta_se_fecha_con_la_emision(pg2, tmp_path):
    cliente = {"id": 1, "name": "Cliente Test", "cuit_dni": "20111111112",
               "address": "", "email": "", "phone": ""}
    periodo = {
        "desde": "2026-07-01", "hasta": "2026-07-31", "emitido": "2026-08-01",
        "saldo_anterior": 0.0, "saldo_final": 1000.0,
        "total_debitos": 1000.0, "total_creditos": 0.0,
        "movimientos": [{"fecha": "2026-07-14", "concepto": "Factura B 0001-00000001",
                         "tipo": "debito", "monto": 1000.0, "referencia": ""}],
    }
    path = pg2.generate_pdf_resumen_cc(cliente, periodo, output_dir=str(tmp_path))
    primero = _bytes_del_archivo(path)
    assert _creation_date(primero) == "D:20260801000000Z"
    os.remove(path)
    assert _bytes_del_archivo(
        pg2.generate_pdf_resumen_cc(cliente, periodo, output_dir=str(tmp_path))) == primero
