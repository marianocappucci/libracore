"""Ningún carácter puede tumbar la generación de un PDF (2026-08-03).

El caso que originó esto es real y está reproducido tal cual en
`test_el_caso_de_libradesk_dev_nombre_de_empresa_con_guion_largo`: en
`libradesk-dev` el nombre de empresa era ``"Compulibra — Soporte IT"``, de ahí
salían las iniciales ``"C—"`` del recuadro del encabezado, y **todo** PDF de
presupuesto devolvía 500 con
``FPDFUnicodeEncodingException: Character "—" ... "helveticaB"``.

Los tests que ya existían no lo agarraban porque **todos usan texto ASCII**:
"Cliente Test", "Item de prueba". Con datos así, la codificación latin-1 nunca
se queda corta.
"""
import pytest

from libracore import pdf_generator as pg
from libracore.pdf_generator import _TextoSeguroPDF


def _set_data_dir(tmp_path, monkeypatch, **cfg):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from libracore import config_manager as cm
    importlib.reload(cm)
    importlib.reload(pg)
    if cfg:
        cm.save(cfg)
    return pg


def _es_pdf(path):
    with open(path, "rb") as f:
        assert f.read(5) == b"%PDF-"


# ── El caso real ──────────────────────────────────────────────────────────────

def test_el_caso_de_libradesk_dev_nombre_de_empresa_con_guion_largo(tmp_path, monkeypatch):
    """Reproduce el 500 del 2026-08-03, con el mismo dato que lo causó."""
    pg2 = _set_data_dir(tmp_path, monkeypatch, empresa_nombre="Compulibra — Soporte IT")
    presupuesto = {
        "number": "PRES-00000001", "date": "2026-08-03", "valid_until": "2026-09-03",
        "client_name": "Cliente Test", "client_cuit": "", "client_address": "",
        "client_email": "", "client_phone": "",
        "items": [{"description": "Mano de obra", "qty": 1, "unit_price": 500, "subtotal": 500}],
        "subtotal": 500, "tax_amount": 105, "total": 605, "tax_rate": 0.21,
        "observations": "",
    }
    _es_pdf(pg2.generate_pdf_presupuesto(presupuesto, output_dir=str(tmp_path)))


# ── Los cuatro comprobantes, con el texto que la gente tipea de verdad ────────

# Guión largo, guión medio, comillas curvas, puntos suspensivos y € — todos
# fuera de latin-1 y todos **dentro** de cp1252, así que se dibujan bien.
TIPOGRAFICOS = "Cambio — repuesto “original” … 15 € · s/n – rev. ‘A’"


def test_presupuesto_con_tipografia_de_procesador_de_texto(tmp_path, monkeypatch):
    pg2 = _set_data_dir(tmp_path, monkeypatch, empresa_nombre=TIPOGRAFICOS)
    presupuesto = {
        "number": "PRES-1", "date": "2026-08-03", "valid_until": "2026-09-03",
        "client_name": TIPOGRAFICOS, "client_cuit": "", "client_address": TIPOGRAFICOS,
        "client_email": "", "client_phone": "",
        "items": [{"description": TIPOGRAFICOS, "qty": 1, "unit_price": 500, "subtotal": 500}],
        "subtotal": 500, "tax_amount": 105, "total": 605, "tax_rate": 0.21,
        "observations": TIPOGRAFICOS,
    }
    _es_pdf(pg2.generate_pdf_presupuesto(presupuesto, output_dir=str(tmp_path)))


def test_remito_con_tipografia_de_procesador_de_texto(tmp_path, monkeypatch):
    pg2 = _set_data_dir(tmp_path, monkeypatch, empresa_nombre=TIPOGRAFICOS)
    remito = {
        "number": "0001-1", "date": "2026-08-03", "client_name": TIPOGRAFICOS,
        "client_cuit": "", "client_address": TIPOGRAFICOS, "client_email": "",
        "client_phone": "",
        "items": [{"description": TIPOGRAFICOS, "qty": 2, "unit_price": 100, "subtotal": 200}],
        "observations": TIPOGRAFICOS,
    }
    _es_pdf(pg2.generate_pdf(remito, output_dir=str(tmp_path)))


def test_factura_con_tipografia_de_procesador_de_texto(tmp_path, monkeypatch):
    pg2 = _set_data_dir(tmp_path, monkeypatch, empresa_nombre=TIPOGRAFICOS)
    factura = {
        "tipo": 6, "punto_venta": 1, "numero": 1, "fecha": "2026-08-03",
        "cliente_razon": TIPOGRAFICOS, "cliente_cuit": "20111111112",
        "cliente_iva_cond": 5, "cliente_domicilio": TIPOGRAFICOS,
        "condicion_venta": "Contado",
        "items": [{"description": TIPOGRAFICOS, "qty": 1, "unit_price": 1000, "subtotal": 1000}],
        "subtotal": 1000, "iva_amount": 0, "total": 1000, "concepto": 1,
        "cae": "", "cae_vto": "", "observaciones": TIPOGRAFICOS,
    }
    _es_pdf(pg2.generate_pdf_factura(factura, output_dir=str(tmp_path)))


def test_resumen_cc_con_tipografia_de_procesador_de_texto(tmp_path, monkeypatch):
    pg2 = _set_data_dir(tmp_path, monkeypatch, empresa_nombre=TIPOGRAFICOS)
    cliente = {"id": 1, "name": TIPOGRAFICOS, "cuit_dni": "", "address": TIPOGRAFICOS,
               "email": "", "phone": ""}
    periodo = {
        "desde": "2026-08-01", "hasta": "2026-08-31", "emitido": "2026-08-31",
        "saldo_anterior": 0.0,
        "movimientos": [{"fecha": "2026-08-05", "tipo": "debito",
                         "concepto": TIPOGRAFICOS, "monto": 100.0,
                         "referencia": TIPOGRAFICOS}],
        "total_debitos": 100.0, "total_creditos": 0.0, "saldo_final": 100.0,
    }
    _es_pdf(pg2.generate_pdf_resumen_cc(cliente, periodo, output_dir=str(tmp_path)))


def test_ticket_con_tipografia_de_procesador_de_texto(tmp_path, monkeypatch):
    """`TicketPDF` vive en otro módulo y heredaba de `FPDF` pelado."""
    _set_data_dir(tmp_path, monkeypatch, empresa_nombre=TIPOGRAFICOS)
    import importlib
    from libracore import ticket_generator as tg
    importlib.reload(tg)
    pdf = tg.TicketPDF(ancho_mm=58, fuente_size=8)
    pdf.set_font("Courier", size=8)
    pdf.multi_cell(50, 4, TIPOGRAFICOS)
    assert bytes(pdf.output())[:5] == b"%PDF-"


# ── Lo que cp1252 tampoco cubre: se degrada, no revienta ─────────────────────

@pytest.mark.parametrize("texto", [
    "Producto ≈ equivalente",      # matemático, fuera de cp1252
    "Reparación 🔧 urgente",        # emoji
    "Cliente Ādams",               # latina extendida
    "Опис товару",                 # cirílico, sin equivalente ASCII
])
def test_lo_que_no_entra_en_cp1252_no_tumba_el_pdf(tmp_path, monkeypatch, texto):
    pg2 = _set_data_dir(tmp_path, monkeypatch, empresa_nombre=texto)
    presupuesto = {
        "number": "PRES-1", "date": "2026-08-03", "valid_until": "2026-09-03",
        "client_name": texto, "client_cuit": "", "client_address": "",
        "client_email": "", "client_phone": "",
        "items": [{"description": texto, "qty": 1, "unit_price": 500, "subtotal": 500}],
        "subtotal": 500, "tax_amount": 105, "total": 605, "tax_rate": 0.21,
        "observations": texto,
    }
    _es_pdf(pg2.generate_pdf_presupuesto(presupuesto, output_dir=str(tmp_path)))


# ── La transliteración, unidad por unidad ────────────────────────────────────

def test_el_acento_castellano_no_se_toca():
    """Lo que ya andaba tiene que seguir andando. á é í ó ú ñ ü ¿ ¡ ° son
    latin-1 válidos: si la transliteración los pisara, todos los PDF del
    ecosistema empeorarían para arreglar un caso de borde."""
    assert _TextoSeguroPDF._a_cp1252("Reparación ñandú ¿está? ¡sí! 20°") == \
        "Reparación ñandú ¿está? ¡sí! 20°"


def test_lo_de_cp1252_pasa_entero():
    """No se transliteran: los dibuja la fuente. Es la razón de la capa 1."""
    assert _TextoSeguroPDF._a_cp1252(TIPOGRAFICOS) == TIPOGRAFICOS


@pytest.mark.parametrize("entrada,esperado", [
    ("≈", "~"),
    ("≤", "<="),
    ("→", "->"),
    ("Ādams", "Adams"),      # NFKD: se cae el diacrítico, queda la letra
    ("🔧", "?"),             # sin equivalente: marcador, no vacío
])
def test_transliteracion(entrada, esperado):
    assert _TextoSeguroPDF._a_cp1252(entrada) == esperado


def test_el_guion_largo_se_dibuja_no_se_translitera(tmp_path, monkeypatch):
    """**Capa 1, y no la cubre ningún otro test.**

    Sin `core_fonts_encoding = "cp1252"` los PDF igual saldrían —la
    transliteración de la capa 2 los salvaría— pero el guión largo se
    degradaría a `-`, las comillas curvas a rectas y el € a `EUR`, en **todos**
    los comprobantes del ecosistema. O sea: la capa 2 sola esconde la pérdida
    de calidad en vez de mostrarla.

    `0x97` es el guión largo en WinAnsi/cp1252, que es la codificación que el
    PDF ya venía declarando. Un `-` acá significaría que se perdió la capa 1.
    """
    _set_data_dir(tmp_path, monkeypatch)
    pdf = pg.PresupuestoPDF({"number": "x", "date": "2026-08-03"})
    assert pdf.core_fonts_encoding == "cp1252"
    assert pdf.normalize_text("—") == "\x97"
    assert pdf.normalize_text("“…”") == "\x93\x85\x94"


def test_el_resultado_de_la_transliteracion_siempre_encodea():
    """La invariante de la que depende todo lo demás: pase lo que pase, lo que
    sale de `_a_cp1252` lo puede escribir una fuente core."""
    ruidoso = "".join(chr(c) for c in range(0x2000, 0x2200)) + "🔧🚀日本語"
    _TextoSeguroPDF._a_cp1252(ruidoso).encode("cp1252")
