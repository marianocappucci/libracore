import os

from libracore import pdf_generator as pg


def _set_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from libracore import config_manager as cm
    importlib.reload(cm)
    importlib.reload(pg)
    return pg


def _assert_valid_pdf_file(path):
    assert os.path.exists(path)
    with open(path, "rb") as f:
        header = f.read(5)
    assert header == b"%PDF-"


def test_generate_pdf_remito(tmp_path, monkeypatch):
    pg2 = _set_data_dir(tmp_path, monkeypatch)
    remito = {
        "number": "0001-00000001",
        "date": "2026-07-14",
        "client_name": "Cliente Test",
        "client_cuit": "20111111112",
        "client_address": "",
        "client_email": "",
        "client_phone": "",
        "items": [{"description": "Item de prueba", "qty": 2, "unit_price": 100, "subtotal": 200}],
        "observations": "",
    }
    path = pg2.generate_pdf(remito, output_dir=str(tmp_path))
    _assert_valid_pdf_file(path)


def test_generate_pdf_presupuesto(tmp_path, monkeypatch):
    pg2 = _set_data_dir(tmp_path, monkeypatch)
    presupuesto = {
        "number": "0001-00000001",
        "date": "2026-07-14",
        "valid_until": "2026-07-21",
        "client_name": "Cliente Test",
        "client_cuit": "20111111112",
        "client_address": "", "client_email": "", "client_phone": "",
        "items": [{"description": "Item de prueba", "qty": 1, "unit_price": 500, "subtotal": 500}],
        "subtotal": 500, "tax_amount": 105, "total": 605, "tax_rate": 0.21,
        "observations": "",
    }
    path = pg2.generate_pdf_presupuesto(presupuesto, output_dir=str(tmp_path))
    _assert_valid_pdf_file(path)


def test_generate_pdf_factura(tmp_path, monkeypatch):
    pg2 = _set_data_dir(tmp_path, monkeypatch)
    factura = {
        "tipo": 6, "punto_venta": 1, "numero": 1, "fecha": "2026-07-14",
        "cliente_razon": "Cliente Test", "cliente_cuit": "20111111112",
        "cliente_iva_cond": 5, "cliente_domicilio": "", "condicion_venta": "Contado",
        "items": [{"description": "Item de prueba", "qty": 1, "unit_price": 1000, "subtotal": 1000}],
        "subtotal": 1000, "iva_amount": 0, "total": 1000, "concepto": 1,
        "cae": "", "cae_vto": "", "observaciones": "",
    }
    path = pg2.generate_pdf_factura(factura, output_dir=str(tmp_path))
    _assert_valid_pdf_file(path)


def test_generate_pdf_recibo_returns_pdf_bytes(tmp_path, monkeypatch):
    pg2 = _set_data_dir(tmp_path, monkeypatch)
    factura = {
        "tipo": 6, "punto_venta": 1, "numero": 1, "fecha": "2026-07-14",
        "cliente_razon": "Cliente Test", "cliente_cuit": "20111111112", "total": 1000,
    }
    cobros = [{"fecha": "2026-07-14", "medio_pago": "efectivo", "referencia": "", "monto": 1000}]
    pdf_bytes = pg2.generate_pdf_recibo(factura, cobros)
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes[:5] == b"%PDF-"
