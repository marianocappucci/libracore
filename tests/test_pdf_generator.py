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


def test_generate_pdf_resumen_cc(tmp_path, monkeypatch):
    pg2 = _set_data_dir(tmp_path, monkeypatch)
    cliente = {
        "id": 7, "name": "Cliente Test", "cuit_dni": "20111111112",
        "address": "Calle 1", "email": "cliente@test.com", "phone": "",
    }
    periodo = {
        "desde": "2026-07-01", "hasta": "2026-07-31", "emitido": "2026-07-31",
        "saldo_anterior": 500.0,
        "movimientos": [
            {"fecha": "2026-07-05", "tipo": "debito", "concepto": "Venta #12",
             "monto": 1000.0, "referencia": ""},
            {"fecha": "2026-07-20", "tipo": "credito", "concepto": "Pago a cuenta",
             "monto": 300.0, "referencia": "transf. 44"},
        ],
        "total_debitos": 1000.0, "total_creditos": 300.0, "saldo_final": 1200.0,
    }
    path = pg2.generate_pdf_resumen_cc(cliente, periodo, output_dir=str(tmp_path))
    _assert_valid_pdf_file(path)


def test_generate_pdf_resumen_cc_sin_movimientos(tmp_path, monkeypatch):
    pg2 = _set_data_dir(tmp_path, monkeypatch)
    cliente = {"id": 8, "name": "Cliente Sin Movimientos", "cuit_dni": "",
               "address": "", "email": "", "phone": ""}
    periodo = {
        "desde": "2026-07-01", "hasta": "2026-07-31", "emitido": "2026-07-31",
        "saldo_anterior": 0.0, "movimientos": [],
        "total_debitos": 0.0, "total_creditos": 0.0, "saldo_final": 0.0,
    }
    path = pg2.generate_pdf_resumen_cc(cliente, periodo, output_dir=str(tmp_path))
    _assert_valid_pdf_file(path)


def _spy_precios(pg2, monkeypatch):
    """Espía show_prices de _draw_items_table y si se dibujó la caja de totales."""
    reg = {}
    orig_items = pg2._draw_items_table

    def spy_items(pdf, items, **kw):
        reg["show_prices"] = kw.get("show_prices")
        return orig_items(pdf, items, **kw)

    monkeypatch.setattr(pg2, "_draw_items_table", spy_items)
    orig_tot = pg2._draw_totals_and_notes

    def spy_tot(*a, **kw):
        reg["totales"] = True
        return orig_tot(*a, **kw)

    monkeypatch.setattr(pg2, "_draw_totals_and_notes", spy_tot)
    return reg


def test_generate_pdf_remito_valorizado(tmp_path, monkeypatch):
    pg2 = _set_data_dir(tmp_path, monkeypatch)
    reg = _spy_precios(pg2, monkeypatch)
    remito = {
        "number": "0001-00000002", "date": "2026-09-04",
        "client_name": "Distribuidora Test", "client_cuit": "", "client_address": "",
        "client_email": "", "client_phone": "",
        "items": [{"description": "Prod A", "qty": 2, "unit_price": 100, "subtotal": 200}],
        "subtotal": 200, "tax_rate": 0.21, "tax_amount": 42, "total": 242,
        "observations": "entrega lunes",
    }
    path = pg2.generate_pdf(remito, output_dir=str(tmp_path), show_prices=True)
    _assert_valid_pdf_file(path)
    assert reg["show_prices"] is True     # muestra precios
    assert reg.get("totales") is True     # dibujó la caja de totales


def test_generate_pdf_remito_pelado_es_el_default(tmp_path, monkeypatch):
    pg2 = _set_data_dir(tmp_path, monkeypatch)
    reg = _spy_precios(pg2, monkeypatch)
    remito = {
        "number": "0001-00000003", "date": "2026-09-04",
        "client_name": "Cliente Test", "client_cuit": "", "client_address": "",
        "client_email": "", "client_phone": "",
        "items": [{"description": "Item", "qty": 3}],
        "observations": "",
    }
    path = pg2.generate_pdf(remito, output_dir=str(tmp_path))   # sin show_prices
    _assert_valid_pdf_file(path)
    assert reg["show_prices"] is False    # NO muestra precios
    assert "totales" not in reg           # NO dibuja la caja de totales
