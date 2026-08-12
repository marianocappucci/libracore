"""Tickets térmicos.

El módulo venía de Contalibra sin tests (Restolibra tenía una copia
idéntica, también sin tests). Estos fijan el comportamiento tal como estaba
al extraerlo, que es lo que permite afirmar que los dos productos siguen
imprimiendo lo mismo.

Un PDF no se puede comparar carácter por carácter, así que se verifica lo
que importa: que salga un PDF válido, que el ancho de página sea el que se
configuró, y que los datos del ticket estén realmente adentro.
"""
import zlib

import pytest

from libracore import config_manager, ticket_generator


@pytest.fixture(autouse=True)
def _config(tmp_path, monkeypatch):
    """Config aislada por test: `config_manager` escribe en disco."""
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(tmp_path / "config.json"))
    config_manager.save({
        "empresa_nombre": "Despensa La Esquina",
        "empresa_cuit": "20-11111111-2",
        "ticket_ancho_mm": "80",
        "ticket_fuente_size": "9",
        "ticket_pie": "Gracias por su compra",
    })


def _texto_del_pdf(pdf: bytes) -> str:
    """Texto plano de un PDF de fpdf2, descomprimiendo los streams.

    No es un parser: alcanza para afirmar que un dato entró al ticket.
    """
    partes = []
    for bloque in pdf.split(b"stream")[1:]:
        crudo = bloque.split(b"endstream")[0].strip(b"\r\n")
        try:
            partes.append(zlib.decompress(crudo).decode("latin-1"))
        except (zlib.error, UnicodeDecodeError):
            partes.append(crudo.decode("latin-1", errors="ignore"))
    return "\n".join(partes)


def _venta(**cambios) -> dict:
    venta = {
        "id": 42,
        "fecha": "2026-07-28 14:30",
        "cliente_nombre": "Consumidor final",
        "items": [
            {"nombre": "Yerba 1kg", "cantidad": 2, "precio_unitario": 1500.0},
            {"nombre": "Fideos 500g", "cantidad": 1, "precio_unitario": 900.0},
        ],
        "total": 3900.0,
        "pagos": [{"medio": "efectivo", "monto": 3900.0}],
    }
    venta.update(cambios)
    return venta


def test_el_ticket_de_venta_es_un_pdf():
    salida = ticket_generator.generar_ticket_venta(_venta())
    assert salida.startswith(b"%PDF-")
    assert len(salida) > 500


def test_el_ancho_del_papel_sale_de_la_configuracion():
    """58 y 80 mm son papeles distintos: si el ancho no se respeta, el ticket
    sale cortado o con media hoja en blanco."""
    ancho_80 = ticket_generator.generar_ticket_venta(_venta())

    config_manager.save({**config_manager.load(), "ticket_ancho_mm": "58"})
    ancho_58 = ticket_generator.generar_ticket_venta(_venta())

    # El MediaBox lleva el ancho en puntos: 80mm ≈ 226.77, 58mm ≈ 164.4
    assert b"226.77" in ancho_80
    assert b"164.4" in ancho_58


def test_el_ticket_lleva_los_productos_y_el_total():
    texto = _texto_del_pdf(ticket_generator.generar_ticket_venta(_venta()))
    assert "Yerba 1kg" in texto
    assert "Fideos 500g" in texto
    assert "3.900,00" in texto  # formato argentino: punto miles, coma decimal


def test_el_ticket_lleva_los_datos_del_comercio_y_el_pie():
    texto = _texto_del_pdf(ticket_generator.generar_ticket_venta(_venta()))
    assert "Despensa La Esquina" in texto
    assert "20-11111111-2" in texto
    assert "Gracias por su compra" in texto


def test_el_cliente_aparece_solo_si_no_es_consumidor_final():
    anonimo = _texto_del_pdf(ticket_generator.generar_ticket_venta(_venta()))
    assert "Cliente:" not in anonimo

    con_nombre = _texto_del_pdf(ticket_generator.generar_ticket_venta(
        _venta(cliente_nombre="Vecina del 12")
    ))
    assert "Vecina del 12" in con_nombre


def test_las_cantidades_fraccionarias_no_se_muestran_como_enteros():
    """Un ticket de fiambrería lleva 0,75 kg, no 1."""
    texto = _texto_del_pdf(ticket_generator.generar_ticket_venta(_venta(
        items=[{"nombre": "Queso", "cantidad": 0.75, "precio_unitario": 8500.0}],
        total=6375.0,
    )))
    assert "0.75 x" in texto


def test_el_descuento_aparece_cuando_lo_hay():
    sin_descuento = _texto_del_pdf(ticket_generator.generar_ticket_venta(_venta()))
    assert "Descuento" not in sin_descuento

    con_descuento = _texto_del_pdf(ticket_generator.generar_ticket_venta(
        _venta(descuento=400.0)
    ))
    assert "Descuento" in con_descuento


def test_los_medios_de_pago_salen_en_el_ticket():
    texto = _texto_del_pdf(ticket_generator.generar_ticket_venta(_venta(
        pagos=[
            {"medio": "efectivo", "monto": 2000.0},
            {"medio": "transferencia", "monto": 1900.0},
        ],
    )))
    assert "Efectivo" in texto
    assert "Transferencia" in texto


def test_una_venta_sin_items_no_rompe():
    # Puede pasar reimprimiendo algo viejo o mal cargado: mejor un ticket
    # pobre que una excepción en la mano del cajero.
    salida = ticket_generator.generar_ticket_venta(_venta(items=[], total=0))
    assert salida.startswith(b"%PDF-")


# ── Ticket de factura ────────────────────────────────────────────────────────

def _factura(**cambios) -> dict:
    factura = {
        "tipo": 6, "punto_venta": 1, "numero": 123, "fecha": "2026-07-28",
        "cliente_razon": "Kiosco SA", "cliente_cuit": "30-22222222-3",
        "items": [{"description": "Servicio", "qty": 1, "unit_price": 1000.0}],
        "subtotal": 826.45, "iva_amount": 173.55, "total": 1000.0,
    }
    factura.update(cambios)
    return factura


def test_el_ticket_de_factura_lleva_tipo_y_numero():
    texto = _texto_del_pdf(ticket_generator.generar_ticket_factura(_factura()))
    assert "FACTURA B" in texto
    assert "0001-00000123" in texto
    assert "Kiosco SA" in texto


def test_el_cae_y_su_vencimiento_salen_impresos():
    texto = _texto_del_pdf(ticket_generator.generar_ticket_factura(
        _factura(cae="71234567890123", cae_vto="20260807")
    ))
    assert "71234567890123" in texto
    # El vencimiento viene de ARCA como AAAAMMDD y se imprime dd-mm-aaaa.
    assert "07-08-2026" in texto


def test_los_items_de_factura_aceptan_json_crudo():
    """`facturas.items` se guarda como texto JSON en la base."""
    texto = _texto_del_pdf(ticket_generator.generar_ticket_factura(_factura(
        items='[{"description": "Consultoria", "qty": 2, "unit_price": 500.0}]'
    )))
    assert "Consultoria" in texto


def test_fmt_fecha_traduce_los_dos_formatos_que_llegan():
    # ISO de la base y AAAAMMDD de ARCA.
    assert ticket_generator.fmt_fecha("2026-07-28") == "28-07-2026"
    assert ticket_generator.fmt_fecha("20260807") == "07-08-2026"
    assert ticket_generator.fmt_fecha("2026-07-28 14:30") == "28-07-2026 14:30"
    assert ticket_generator.fmt_fecha("") == ""


def test_las_piezas_publicas_permiten_armar_un_ticket_propio():
    """Es lo que usa la comanda de cocina de Restolibra en vez de copiar el
    módulo entero."""
    ancho_mm, fuente, _logo, _corte, _pie, _cfg = ticket_generator.cfg_ticket()
    pdf = ticket_generator.TicketPDF(ancho_mm, fuente)
    pdf._centrado("** COCINA **", bold=True)
    pdf._separador("=")
    pdf._texto("2 x Milanesa")

    salida = ticket_generator.recortar_a_contenido(pdf)
    assert salida.startswith(b"%PDF-")
    assert "Milanesa" in _texto_del_pdf(salida)
