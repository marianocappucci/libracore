"""**Nada se dibuja fuera del marco del papel.**

El marco lo definen `_LX` y `_RX`: entre esos dos milímetros dibujan el
membrete, las reglas de sección, las tarjetas de emisor/cliente y el pie. El
cuerpo, en cambio, se escribe con el flujo de fpdf2 (`cell` + `ln`), que arranca
en el margen del documento y **no recorta ni envuelve nada**. Las dos formas de
salirse, las dos medidas acá:

1. **El documento no fija su margen** y queda el de fpdf2 —10 mm—: el cuerpo
   entero sale 8 mm a la izquierda del marco que su propia cabecera dibujó.
   Pasó en LibraDesk, en la orden de trabajo y en los dos comprobantes de
   ingreso; por eso el margen ahora lo pone `_TextoSeguroPDF`.
2. **Un texto más ancho que su celda.** `cell` lo dibuja igual: si al lado hay
   otra columna la pisa, y si no hay nada, se va del papel. Medido: 536 mm de
   borde derecho en una hoja de 210.

Los tests **miden el PDF terminado**, no las constantes: se parsea el content
stream y se ubica cada texto y cada línea dibujada. Un `assert` sobre `_LX`
pasaría igual con el defecto entero puesto, porque el defecto nunca estuvo en
el valor de `_LX` sino en quién lo usa.
"""
import importlib
from io import BytesIO

import pytest
from fpdf.fonts import CORE_FONTS_CHARWIDTHS
from pypdf import PdfReader
from pypdf.generic import ByteStringObject, ContentStream

from libracore import pdf_generator as pg

MM = 72 / 25.4

#: Una "palabra" sin espacios más ancha que cualquier renglón: un código
#: pegado, una URL, un serial. Es el caso que el corte por palabra no puede
#: resolver solo.
MONSTRUO = "X" * 220

EMPRESA = {
    "empresa_nombre": "Adolfo Lagrace Comunicaciones S.R.L.",
    "empresa_cuit": "30-65903401-4",
    "empresa_direccion": "Av. Carlos Gardel 172, Suipacha, Buenos Aires",
    "empresa_iibb": "902-677083-3",
    "empresa_inicio_actividades": "1993-06-25",
    "empresa_iva_condition": "Responsable Inscripto",
}

_FUENTES = {
    "/Helvetica": "helvetica", "/Helvetica-Bold": "helveticaB",
    "/Helvetica-Oblique": "helveticaI", "/Helvetica-BoldOblique": "helveticaBI",
    "/Courier": "courier", "/Times-Roman": "times",
}


@pytest.fixture
def pgen(tmp_path, monkeypatch):
    """`pdf_generator` con un DATA_DIR propio y la empresa cargada.

    El `reload` no es adorno: `config_manager` y `pdf_generator` congelan sus
    rutas al importarse.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from libracore import config_manager as cm
    importlib.reload(cm)
    importlib.reload(pg)
    cm.save(dict(EMPRESA))
    return pg


# ── La medición ────────────────────────────────────────────────────

def _ancho(txt, fuente, size):
    """El ancho del texto en mm, con las métricas de la fuente core que el PDF
    declara. Se usan las tablas de fpdf2 y no un promedio: un promedio hace que
    la medición no pueda distinguir "entra justo" de "se pasa por poco"."""
    cw = CORE_FONTS_CHARWIDTHS.get(fuente) or CORE_FONTS_CHARWIDTHS["helvetica"]
    return sum(cw.get(c, 500) for c in txt) * 0.001 * size / MM


def _dibujado(page):
    """[(que, x_izq_mm, x_der_mm, texto)] de todo lo que la página dibuja.

    Se parsea el content stream —`Tm`/`Td`/`TD`/`T*`/`Tj`/`TJ` para el texto,
    `m`/`l`/`re` para las líneas— en vez de usar `extract_text`, que no informa
    la coordenada de arranque: una medición hecha sobre `extract_text` compara
    `0 + ancho` contra el borde y pasa con el defecto entero puesto.
    """
    fuentes = {
        k: _FUENTES.get(str(v.get_object().get("/BaseFont")), "helvetica")
        for k, v in (page.get("/Resources", {}).get("/Font", {}) or {}).items()
    }
    tm = lm = [1, 0, 0, 1, 0, 0]
    size, fuente, leading = 0.0, "helvetica", 0.0
    x_traz = 0.0
    out = []
    for ops, op in ContentStream(page.get_contents(), page.pdf).operations:
        o = op.decode() if isinstance(op, bytes) else op
        if o == "BT":
            tm = lm = [1, 0, 0, 1, 0, 0]
        elif o == "Tf":
            fuente, size = fuentes.get(str(ops[0]), "helvetica"), float(ops[1])
        elif o == "TL":
            leading = float(ops[0])
        elif o == "Tm":
            tm = lm = [float(v) for v in ops]
        elif o in ("Td", "TD"):
            if o == "TD":
                leading = -float(ops[1])
            lm = lm[:4] + [lm[4] + float(ops[0]), lm[5] + float(ops[1])]
            tm = list(lm)
        elif o == "T*":
            lm = lm[:5] + [lm[5] - leading]
            tm = list(lm)
        elif o in ("Tj", "TJ"):
            partes = [ops[0]] if o == "Tj" else [
                e for e in ops[0] if not isinstance(e, (int, float))]
            txt = "".join(
                p.decode("cp1252", "replace")
                if isinstance(p, (bytes, ByteStringObject)) else str(p)
                for p in partes)
            w = _ancho(txt, fuente, size)
            if txt.strip():
                out.append(("texto", tm[4] / MM, tm[4] / MM + w, txt))
            tm = tm[:4] + [tm[4] + w * MM, tm[5]]
        elif o == "m":
            x_traz = float(ops[0])
        elif o == "l":
            x = float(ops[0])
            out.append(("linea", min(x_traz, x) / MM, max(x_traz, x) / MM, ""))
            x_traz = x
        elif o == "re":
            x, w = float(ops[0]), float(ops[2])
            out.append(("recuadro", min(x, x + w) / MM, max(x, x + w) / MM, ""))
    return out


def _desbordes(pdf, tolerancia=0.6):
    """Lo que se dibuja afuera de [`_LX`, `_RX`]. `pdf` es una ruta o bytes."""
    lector = PdfReader(BytesIO(pdf) if isinstance(pdf, bytes) else pdf)
    fuera = []
    for n, page in enumerate(lector.pages, 1):
        for que, izq, der, txt in _dibujado(page):
            if izq < pg._LX - tolerancia or der > pg._RX + tolerancia:
                fuera.append(f"p{n} {que} {izq:.1f}..{der:.1f}mm {txt[:50]!r}")
    return fuera


# ── Los documentos ─────────────────────────────────────────────────

CLIENTE = {
    "client_name": "Establecimiento Frigorifico Arre Beef S.A.",
    "client_cuit": "30-71234567-9",
    "client_address": "Ruta Nacional 7 km 78,5, Perez Millan, Buenos Aires",
    "client_email": "compras@arrebeef.com.ar", "client_phone": "02346-49-1200",
}
DESCRIPCION = ("Tendido de red de voz y datos categoria 6 para el edificio "
               "nuevo de administracion, con certificacion de los 48 puestos")


def _items(descripcion):
    return [{"description": descripcion, "qty": 12, "unit_price": 148500.75,
             "subtotal": 1782009.0, "iva_pct": 21}]


def _remito(pgen, tmp_path, texto):
    return pgen.generate_pdf(
        {"number": "0001-00041989", "date": "2026-08-13", **CLIENTE,
         "client_name": texto, "client_address": texto,
         "items": _items(texto), "observations": texto},
        output_dir=str(tmp_path))


def _presupuesto(pgen, tmp_path, texto):
    return pgen.generate_pdf_presupuesto(
        {"number": "0001-00041989", "date": "2026-08-13",
         "valid_until": "2026-08-31", **CLIENTE, "client_name": texto,
         "client_address": texto, "items": _items(texto), "observations": texto,
         "subtotal": 1782009.0, "tax_amount": 374221.9, "total": 2156230.9,
         "tax_rate": 0.21},
        output_dir=str(tmp_path))


def _factura_dict(texto):
    return {
        "tipo": 1, "punto_venta": 1, "numero": 41989, "fecha": "2026-08-13",
        "cliente_razon": texto, "cliente_cuit": "30-71234567-9",
        "cliente_iva_cond": 1, "cliente_domicilio": texto,
        "condicion_venta": texto, "items": _items(texto),
        "subtotal": 1782009.0, "iva_amount": 374221.9, "total": 2156230.9,
        "concepto": 3, "cae": "75123456789012", "cae_vto": "2026-08-23",
        "observaciones": texto,
    }


def _factura(pgen, tmp_path, texto):
    return pgen.generate_pdf_factura(_factura_dict(texto), output_dir=str(tmp_path))


def _recibo(pgen, tmp_path, texto):
    return pgen.generate_pdf_recibo(
        _factura_dict(texto),
        [{"fecha": "2026-08-13", "medio_pago": texto, "referencia": texto,
          "monto": 2156230.9}])


def _resumen_cc(pgen, tmp_path, texto):
    return pgen.generate_pdf_resumen_cc(
        {"id": 7, "name": texto, "cuit_dni": "30-71234567-9", "address": texto,
         "email": texto, "phone": "02346-49-1200"},
        {"desde": "2026-07-01", "hasta": "2026-07-31", "emitido": "2026-08-01",
         "saldo_anterior": 1250000.0,
         "movimientos": [{"fecha": "2026-07-05", "tipo": "debito",
                          "concepto": texto, "monto": 2156230.9,
                          "referencia": texto}],
         "total_debitos": 2156230.9, "total_creditos": 0.0,
         "saldo_final": 3406230.9},
        output_dir=str(tmp_path))


DOCUMENTOS = [
    ("remito", _remito), ("presupuesto", _presupuesto), ("factura", _factura),
    ("recibo", _recibo), ("resumen de cuenta", _resumen_cc),
]


@pytest.mark.parametrize("nombre,generar", DOCUMENTOS, ids=[d[0] for d in DOCUMENTOS])
def test_ningun_comprobante_se_sale_del_marco(pgen, tmp_path, nombre, generar):
    """Con datos reales: razones sociales largas, descripciones de dos
    renglones e importes de siete cifras. Con "Cliente Test" y "Item de prueba"
    no se sale nada — y eso era lo único que los tests probaban."""
    fuera = _desbordes(generar(pgen, tmp_path, DESCRIPCION))
    assert not fuera, f"{nombre} dibuja fuera del marco:\n" + "\n".join(fuera)


@pytest.mark.parametrize("nombre,generar", DOCUMENTOS, ids=[d[0] for d in DOCUMENTOS])
def test_ningun_comprobante_se_sale_con_un_texto_sin_espacios(
        pgen, tmp_path, nombre, generar):
    """El caso que el corte por palabra no puede resolver: un solo "término"
    más ancho que el renglón. Llega de verdad —una URL pegada, un serial, un
    código de orden de compra— y antes se iba del papel."""
    fuera = _desbordes(generar(pgen, tmp_path, MONSTRUO))
    assert not fuera, f"{nombre} dibuja fuera del marco:\n" + "\n".join(fuera)


# ── Las dos piezas, por separado ───────────────────────────────────

def test_la_base_deja_el_documento_con_el_margen_del_marco():
    """Lo que hace que un documento nuevo no pueda repetir el defecto de
    LibraDesk: el margen ya no depende de que cada clase se acuerde."""
    doc = pg._TextoSeguroPDF(format="A4", unit="mm")
    assert (doc.l_margin, doc.r_margin, doc.t_margin) == (pg._LX, pg._LX, pg._LX)


def test_un_documento_con_otra_geometria_pisa_el_margen_de_la_base():
    """El ticket térmico son 80 mm de papel: con 18 mm de margen no entraría
    nada. La base pone un default, no una imposición."""
    from libracore.ticket_generator import TicketPDF
    assert TicketPDF(ancho_mm=80, fuente_size=8).l_margin == 2


def test_una_palabra_mas_ancha_que_el_renglon_se_parte():
    doc = pg._TextoSeguroPDF(format="A4", unit="mm")
    doc.add_page()
    doc.set_font("Helvetica", "", 8)

    lineas = pg._wrap_text(doc, MONSTRUO, 50)

    assert len(lineas) > 1, "la palabra salió entera y se va del papel"
    assert "".join(lineas) == MONSTRUO, "el corte por carácter perdió texto"
    for linea in lineas:
        assert doc.get_string_width(linea) <= 50


def test_el_wrap_no_toca_lo_que_ya_entraba():
    """La contracara: el corte nuevo no puede cambiar dónde se parte un texto
    normal, o cada comprobante ya emitido saldría distinto."""
    doc = pg._TextoSeguroPDF(format="A4", unit="mm")
    doc.add_page()
    doc.set_font("Helvetica", "", 8)

    assert pg._wrap_text(doc, "uno dos tres", 100) == ["uno dos tres"]


def test_el_recorte_mete_el_texto_en_la_celda_y_avisa_con_elipsis():
    doc = pg._TextoSeguroPDF(format="A4", unit="mm")
    doc.add_page()
    doc.set_font("Helvetica", "", 8)

    recorte = pg._recortar(doc, MONSTRUO, 40)

    assert recorte.endswith("…"), "sin elipsis, el texto cortado se lee como completo"
    assert doc.get_string_width(recorte) <= 38     # 40 menos el aire de `cell`
    assert pg._recortar(doc, "corto", 40) == "corto"
