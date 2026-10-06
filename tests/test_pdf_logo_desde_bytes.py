"""El membrete del PDF lleva el logo que viene como contenido (`logo_bytes`), no sólo como archivo.

Lo pidió el humano el 2026-10-06: los PDF de LibraCargo salían con el cuadrito de iniciales aunque
la instancia tenía el logo cargado, porque LibraCargo lo guarda en su base y el motor sólo sabía
leerlo de un archivo en disco.
"""
import io

from PIL import Image

from libracore.pdf_generator import generate_pdf_pre_factura

PRE_FACTURA = {
    "id": 1, "numero_interno": "PF-0001", "fecha_sugerida": "2026-10-06", "cliente_razon": "Acopio Sur SA",
    "cliente_cuit": "30-12345678-1", "tipo_comprobante": 1, "items": [
        {"description": "Flete", "qty": 1, "unit_price": 1000, "iva_rate": 0.21}],
    "total": 1210,
}


def _png(modo="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(modo, (300, 120), (0, 90, 160, 255) if modo == "RGBA" else (0, 90, 160)).save(buf, format="PNG")
    return buf.getvalue()


def _imagenes(pdf: bytes) -> int:
    return pdf.count(b"/Subtype /Image")


def test_sin_logo_no_hay_imagen_y_van_las_iniciales():
    pdf = generate_pdf_pre_factura(PRE_FACTURA, empresa={"nombre": "Transportes Demo SRL", "logo_path": ""})
    assert _imagenes(pdf) == 0


def test_el_logo_como_contenido_se_dibuja():
    pdf = generate_pdf_pre_factura(PRE_FACTURA, empresa={"nombre": "Transportes Demo SRL", "logo_bytes": _png()})
    assert _imagenes(pdf) == 1


def test_un_logo_con_transparencia_como_contenido_tambien():
    pdf = generate_pdf_pre_factura(PRE_FACTURA, empresa={"nombre": "Transportes Demo SRL",
                                                         "logo_bytes": _png("RGBA")})
    assert _imagenes(pdf) == 1


def test_el_contenido_gana_sobre_un_archivo_que_no_existe():
    pdf = generate_pdf_pre_factura(PRE_FACTURA, empresa={"nombre": "Transportes Demo SRL",
                                                         "logo_path": "/no/existe.png", "logo_bytes": _png()})
    assert _imagenes(pdf) == 1


def test_el_logo_como_archivo_sigue_andando(tmp_path):
    ruta = tmp_path / "logo.png"
    ruta.write_bytes(_png())
    pdf = generate_pdf_pre_factura(PRE_FACTURA, empresa={"nombre": "Transportes Demo SRL", "logo_path": str(ruta)})
    assert _imagenes(pdf) == 1
