"""El título del membrete envuelve a dos líneas en vez de salirse (2026-08-05).

**El defecto.** `_draw_header_block` dibujaba el título con un `cell()`, que
**no envuelve**: un título más ancho que su celda —38 mm— se sale del recuadro y
queda pisando el borde. Con los títulos cortos de facturación no se notaba
(Factura 10,8 mm; Remito 10,2; Presupuesto 18,2), y por eso convivió sin que
nadie lo viera. Lo destapó LibraDesk con *"Comprobante de recepción de equipo"*:
**55,8 mm, o sea 17,8 de más**.

---

## 🔴 Estos tests miden el PDF, no la aritmética

La primera versión de este archivo llamaba a `_wrap_text()` con su propio cálculo
del ancho y afirmaba sobre el resultado. **Pasaba con el defecto entero
presente**: revertir el dibujo al `cell()` de una línea la dejaba en verde,
porque nunca miraba lo que `_draw_header_block` dibuja. Lo agarró la
verificación por rotura — 3 de 4 roturas quedaron sin detectar.

Ahora se **renderiza el membrete y se leen las posiciones reales del texto** en
el PDF, comparándolas contra el borde del recuadro. Es más caro y es la única
forma de que el test hable del defecto que se reportó: *"el título se sale del
rectángulo"*.
"""
import re
import zlib
from io import BytesIO

import pytest
from fpdf import FPDF
from pypdf import PdfReader

from libracore import pdf_generator as pg

# Los seis títulos que la familia usa hoy. Ninguno llega a 38 mm.
CORTOS = [
    "Presupuesto", "Remito", "Factura",
    "Orden de trabajo", "Informe de servicio", "Nota de crédito",
]
# Los de LibraDesk (pedido 43), que son los que rompían.
LARGOS = [
    "Comprobante de recepción de equipo",
    "Comprobante de entrega de equipo",
]

INFO = [("Comprobante:", "REC-00000001"), ("Fecha:", "05/08/2026 11:34")]
EMPRESA = {"nombre": "Compulibra - Soporte IT"}

# El borde interno derecho del recuadro, en mm desde el margen izquierdo de la
# hoja. Lo que se dibuje más allá de acá está fuera de la caja.
BORDE_DERECHO = pg._RIGHT_X + pg._RIGHT_W


class _PDF(pg._TextoSeguroPDF, FPDF):
    pass


def _render(titulo, codigo=""):
    pdf = _PDF()
    pdf.add_page()
    pg._draw_header_block(pdf, "A", titulo, codigo, INFO, EMPRESA)
    return bytes(pdf.output())


def _pt_a_mm(v):
    return v / 72 * 25.4


def _textos_dibujados(pdf_bytes):
    """Cada cacho de texto con su x/y reales, leídos del PDF ya generado.

    Devuelve `(texto, x_mm, y_mm, alto_pt)`. `y` se convierte a "desde arriba",
    que es como razona fpdf y como se leen las constantes del módulo.
    """
    encontrados = []
    alto_hoja = None

    def visitar(texto, cm, tm, fuente, tam):
        nonlocal alto_hoja
        if texto and texto.strip():
            encontrados.append((texto, _pt_a_mm(tm[4]), _pt_a_mm(tm[5]), tam))

    lector = PdfReader(BytesIO(pdf_bytes))
    pagina = lector.pages[0]
    alto_hoja = _pt_a_mm(float(pagina.mediabox.height))
    pagina.extract_text(visitor_text=visitar)
    return [(t, x, alto_hoja - y, tam) for t, x, y, tam in encontrados]


def _ancho_dibujado(texto, tam_pt):
    """Cuánto mide ese texto en mm, con la fuente del título."""
    medidor = FPDF()
    medidor.add_page()
    medidor.set_font("Helvetica", "B", tam_pt or 8.5)
    return medidor.get_string_width(texto)


def _lineas_del_titulo(pdf_bytes, titulo):
    """Los cachos de texto que pertenecen al título, y sólo ésos.

    Se filtran por contenido —las palabras del título— en vez de por posición:
    filtrar por posición asumiría la maqueta que este test justamente vigila.
    """
    palabras = set(titulo.title().split())
    return [
        (t.strip(), x, y, tam)
        for t, x, y, tam in _textos_dibujados(pdf_bytes)
        if t.strip() and set(t.strip().split()) & palabras
    ]


# --- 🔴 el defecto reportado: el título se sale del recuadro -----------------

@pytest.mark.parametrize("titulo", LARGOS + CORTOS)
def test_el_titulo_dibujado_no_se_sale_del_recuadro(titulo):
    """El test que describe el reporte del usuario, medido sobre el PDF."""
    lineas = _lineas_del_titulo(_render(titulo), titulo)
    assert lineas, f"no se dibujó nada del título {titulo!r}"

    for texto, x, _y, tam in lineas:
        derecha = x + _ancho_dibujado(texto, tam)
        assert derecha <= BORDE_DERECHO, (
            f"{titulo!r}: la línea {texto!r} termina en {derecha:.1f} mm y el "
            f"recuadro cierra en {BORDE_DERECHO:.1f} — se sale "
            f"{derecha - BORDE_DERECHO:.1f} mm."
        )


@pytest.mark.parametrize("titulo", LARGOS)
def test_un_titulo_largo_se_dibuja_en_dos_lineas(titulo):
    """La contraprueba del anterior. Sin esto, un título **recortado** también
    entraría en la caja y el test de arriba pasaría igual — perdiendo texto."""
    lineas = _lineas_del_titulo(_render(titulo), titulo)
    alturas = sorted({round(y, 1) for _t, _x, y, _tam in lineas})
    assert len(alturas) == 2, (
        f"{titulo!r} se dibujó en {len(alturas)} renglón(es): {alturas}"
    )
    # Y las dos líneas están separadas de verdad, no encimadas.
    assert alturas[1] - alturas[0] >= 3.5, f"renglones a {alturas} mm: se pisan"


@pytest.mark.parametrize("titulo", LARGOS)
def test_no_se_pierde_ni_una_palabra_del_titulo(titulo):
    """Recortar el título entraría en la caja y sería peor que salirse: el papel
    diría otra cosa."""
    texto = PdfReader(BytesIO(_render(titulo))).pages[0].extract_text()
    for palabra in titulo.title().split():
        assert palabra in texto, f"falta {palabra!r} en el membrete"


@pytest.mark.parametrize("titulo", CORTOS)
def test_un_titulo_corto_sigue_en_un_solo_renglon(titulo):
    lineas = _lineas_del_titulo(_render(titulo), titulo)
    alturas = {round(y, 1) for _t, _x, y, _tam in lineas}
    assert len(alturas) == 1, f"{titulo!r} usó {len(alturas)} renglones"


# --- 🔴 y lo que ya existía no se movió --------------------------------------

def _y_de_la_fila_de_datos(titulo, codigo=""):
    """Dónde arranca la primera fila de datos del recuadro.

    Es lo que se mueve si la caja crece, así que sirve de medida del alto sin
    tener que parsear el rectángulo. **Se compara entre corridas, nunca contra
    un número escrito a mano**: pypdf devuelve la *línea base* del texto y fpdf
    posiciona el *tope* de la celda, así que la posición absoluta lleva un
    desfasaje de ~3 mm que dependería de la fuente. Calzar ese número sería
    fijar un accidente de la medición, no la maqueta.
    """
    return next(
        y for t, _x, y, _tam in _textos_dibujados(_render(titulo, codigo))
        if t.strip() == "Comprobante:"
    )


@pytest.mark.parametrize("titulo", CORTOS)
@pytest.mark.parametrize("codigo", ["", "01"])
def test_con_titulo_corto_la_caja_queda_donde_estaba(titulo, codigo):
    """La mitad que permite publicar sin revisar a mano los comprobantes de los
    cinco productos que ya consumen el motor en producción.

    Todos los títulos de una línea tienen que dejar la fila de datos **en la
    misma altura**, con y sin código. Si el crecimiento se colara en el camino
    común, todos se moverían juntos — para eso está la contraprueba de abajo,
    que exige que el largo SÍ se mueva.
    """
    assert _y_de_la_fila_de_datos(titulo, codigo) == pytest.approx(
        _y_de_la_fila_de_datos("Remito"), abs=0.01
    ), (
        f"{titulo!r} (código {codigo or '-'}) dejó la fila de datos en otra "
        f"altura que 'Remito': la caja cambió para un título de una línea, y "
        f"eso mueve los comprobantes de todos los productos."
    )


def test_con_titulo_largo_la_caja_crece_exactamente_un_renglon():
    """Contraprueba del anterior, y la medida del crecimiento.

    Sin esto, el test de arriba pasaría aunque la caja **nunca** se adaptara
    —todos los títulos quedarían igual, incluido el que se sale—. Y se exige que
    crezca `_TITULO_LH`, no "algo": si creciera de menos, las dos líneas se
    pisan; de más, queda un hueco.
    """
    for largo in LARGOS:
        crecimiento = _y_de_la_fila_de_datos(largo) - _y_de_la_fila_de_datos("Remito")
        assert crecimiento == pytest.approx(pg._TITULO_LH, abs=0.01), (
            f"{largo!r} corrió la fila {crecimiento:.2f} mm y el renglón mide "
            f"{pg._TITULO_LH}"
        )


def test_el_titulo_nunca_pasa_de_dos_renglones():
    """Una tercera línea empujaría las filas de datos fuera del recuadro. Un
    título así de largo es un problema de redacción, no de maqueta."""
    kilometrico = "Comprobante de recepción y entrega de equipamiento informático"
    lineas = _lineas_del_titulo(_render(kilometrico), kilometrico)
    alturas = {round(y, 1) for _t, _x, y, _tam in lineas}
    assert len(alturas) <= 2, f"usó {len(alturas)} renglones: {sorted(alturas)}"
