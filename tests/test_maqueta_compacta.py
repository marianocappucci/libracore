"""La maqueta comprimida (2026-08-03): que entren más ítems en la primera hoja.

**El problema, medido antes de tocar nada.** Sobre una carilla A4 de 297 mm, un
presupuesto gastaba 51,5 mm de membrete y 65 mm en las tarjetas Emisor/Cliente
—más que el membrete— y reservaba 80 mm al pie. A los ítems les quedaba el
**24% de la hoja**: entraban 6 con descripciones de una línea y sólo 4 con
descripciones reales. Un presupuesto de 5 ítems ya se iba a dos hojas.

**Qué se cambió y qué NO.** Sólo alturas y aires: alto de la caja de la letra,
del renglón de la cajita de datos, de la fila de las tarjetas y de la fila de
totales. **No se eliminó ningún campo, no se achicó ninguna tipografía y no se
tocaron ni la tabla de ítems ni el pie con CAE/QR.** La distinción no es
estética: las facturas son documentos fiscales y los campos del emisor y del
receptor son obligatorios — comprimir espaciado es seguro, sacar datos no.
`test_la_factura_conserva_todos_los_campos_fiscales` es la guarda de eso.

Estos tests fijan el **resultado** (cuántos ítems entran), no las constantes:
una constante se puede cambiar por una razón válida, pero si al hacerlo entran
menos ítems, la compresión se perdió y nadie se entera.
"""
import importlib
from io import BytesIO

import pytest
from pypdf import PdfReader

from libracore import pdf_generator as pg

EMPRESA = {
    "empresa_nombre": "Compulibra - Soporte IT",
    "empresa_cuit": "20-31234567-8",
    "empresa_direccion": "Av. Villarino 1200, Chivilcoy, Buenos Aires",
    # Distinto del CUIT a propósito: con el mismo número, sacar la fila de
    # Ingresos Brutos no cambiaba el texto extraído y la guarda fiscal pasaba
    # igual. Pasó — lo encontró la verificación por falla forzada.
    "empresa_iibb": "902-654321-7",
    "empresa_inicio_actividades": "2018-03-01",
    "empresa_iva_condition": "Responsable Inscripto",
}

CLIENTE = {
    "client_name": "Clinica del Sol S.A.",
    "client_cuit": "30-65432198-2",
    "client_address": "Rivadavia 1290, Chivilcoy",
    "client_email": "sistemas@clinicadelsol.com.ar",
    "client_phone": "2346-430077",
}

CLIENTE_CARD = [
    ("Nombre", CLIENTE["client_name"]),
    ("CUIT/DNI", CLIENTE["client_cuit"]),
    ("Domicilio", CLIENTE["client_address"]),
    ("Email", CLIENTE["client_email"]),
    ("Teléfono", CLIENTE["client_phone"]),
]


@pytest.fixture
def pgen(tmp_path, monkeypatch):
    """`pdf_generator` con un DATA_DIR propio y la empresa cargada.

    El `reload` no es adorno: `config_manager` y `pdf_generator` congelan sus
    rutas al importarse, así que sin recargarlos leerían el DATA_DIR de otro
    test.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from libracore import config_manager as cm
    importlib.reload(cm)
    importlib.reload(pg)
    cm.save(dict(EMPRESA))
    return pg


def _items(n, descripcion="Servicio tecnico item {i}"):
    return [{"description": descripcion.format(i=i + 1), "qty": 1,
             "unit_price": 12500, "subtotal": 12500, "iva_pct": 21}
            for i in range(n)]


def _presupuesto(pgen, n, tmp_path, descripcion=None):
    total = 12500 * n
    kwargs = {"descripcion": descripcion} if descripcion else {}
    return pgen.generate_pdf_presupuesto(
        {"number": "0001-00000001", "date": "2026-08-03",
         "valid_until": "2026-08-17", "observations": "", **CLIENTE,
         "items": _items(n, **kwargs), "subtotal": total,
         "tax_amount": round(total * 0.21), "total": round(total * 1.21),
         "tax_rate": 0.21}, output_dir=str(tmp_path))


def _remito(pgen, n, tmp_path):
    return pgen.generate_pdf(
        {"number": "0001-00000001", "date": "2026-08-03", "observations": "",
         **CLIENTE, "items": _items(n)}, output_dir=str(tmp_path))


def _paginas(ruta):
    return len(PdfReader(ruta).pages)


def _texto(ruta):
    return "\n".join(p.extract_text() for p in PdfReader(ruta).pages)


# ── El alto de cada bloque ─────────────────────────────────────────

def test_el_membrete_entra_en_42mm(pgen):
    """Antes medía 51,5 mm en un presupuesto (tres campos en la cajita)."""
    pdf = pgen.PresupuestoPDF({"number": "0001-00000001", "date": "2026-08-03",
                               "valid_until": "2026-08-17"})
    pdf._emp = pgen._empresa()
    pdf.add_page()

    alto = pdf.get_y() - 18          # 18 mm es el margen superior
    assert alto <= 42, f"el membrete creció a {alto:.1f} mm"


def test_las_tarjetas_entran_en_52mm(pgen):
    """Antes medían 65 mm — el bloque más grande de la carilla."""
    pdf = pgen.PresupuestoPDF({"number": "0001-00000001", "date": "2026-08-03",
                               "valid_until": "2026-08-17"})
    pdf._emp = pgen._empresa()
    pdf.add_page()
    arriba = pdf.get_y()
    pgen._draw_emisor_cliente(pdf, pdf._emp, CLIENTE_CARD)

    alto = pdf.get_y() - arriba
    assert alto <= 52, f"las tarjetas crecieron a {alto:.1f} mm"


# ── Lo que de verdad importa: cuántos ítems entran ─────────────────

def test_un_presupuesto_de_8_items_entra_en_una_hoja(pgen, tmp_path):
    """Antes entraban 6."""
    assert _paginas(_presupuesto(pgen, 8, tmp_path)) == 1


def test_un_presupuesto_de_10_items_entra_en_una_hoja(pgen, tmp_path):
    """La capacidad medida de la maqueta comprimida, al borde.

    Está a propósito pegado al límite: es el único test que se rompe si se
    revierte **cualquiera** de los tres bloques, incluida la reserva del pie.
    Con 8 ítems sobra margen — se verificó que devolver `_BOTTOM_BLOCK_H` a 80
    dejaba pasar el test de 8 y sólo éste lo agarra.
    """
    assert _paginas(_presupuesto(pgen, 10, tmp_path)) == 1


def test_un_presupuesto_de_6_items_con_descripciones_largas_entra_en_una_hoja(
        pgen, tmp_path):
    """El caso realista: descripciones de dos renglones, como las que se
    tipean de verdad. Antes de comprimir entraban 4."""
    largo = ("Servicio tecnico de mantenimiento preventivo mensual sobre el "
             "equipamiento del sector, item {i}")
    assert _paginas(_presupuesto(pgen, 6, tmp_path, descripcion=largo)) == 1


def test_un_remito_de_12_items_entra_en_una_hoja(pgen, tmp_path):
    """Antes entraban 10."""
    assert _paginas(_remito(pgen, 12, tmp_path)) == 1


def test_los_totales_siguen_anclados_al_pie(pgen, tmp_path):
    """Decisión explícita al comprimir: el bloque de totales **no** sigue a los
    ítems, se queda al pie. Un comprobante se lee igual siempre y el ojo sabe
    dónde buscar el total. Con 1 ítem el total tiene que estar abajo, no
    pegado al ítem.
    """
    ruta = _presupuesto(pgen, 1, tmp_path)
    pagina = PdfReader(ruta).pages[0]
    alto_pt = float(pagina.mediabox.height)
    posiciones = {}

    def visitor(text, cm, tm, font_dict, font_size):
        limpio = text.strip()
        if limpio in ("Total", "Servicio tecnico item 1"):
            posiciones.setdefault(limpio, (alto_pt - tm[5]) / 2.8346)

    pagina.extract_text(visitor_text=visitor)

    assert "Total" in posiciones and "Servicio tecnico item 1" in posiciones
    separacion = posiciones["Total"] - posiciones["Servicio tecnico item 1"]
    assert separacion > 60, (
        f"el total quedó a {separacion:.0f} mm del único ítem: parece que dejó "
        "de estar anclado al pie")


@pytest.mark.parametrize("nombre,esperado", [
    ("Compulibra — Soporte IT", "CS"),    # la raya contaba como palabra: daba "C—"
    ("Perez & Asoc.", "PA"),
    ("Compulibra", "C"),
    ("— —", "?"),                          # sin ninguna palabra utilizable
    ("", "?"),
])
def test_las_iniciales_del_logo_saltean_los_simbolos(pgen, tmp_path, nombre, esperado):
    """El cuadrito que reemplaza al logo cuando no hay archivo cargado.

    Tomaba la primera letra de las dos primeras palabras sin mirar qué eran,
    así que "Compulibra — Soporte IT" —el nombre real de `libradesk-dev`—
    dibujaba **"C—"**: un guión donde va una inicial.
    """
    from libracore import config_manager as cm
    cm.save({**EMPRESA, "empresa_nombre": nombre, "logo_path": ""})

    pdf = pgen.PresupuestoPDF({"number": "0001-00000001", "date": "2026-08-03",
                               "valid_until": "2026-08-17"})
    pdf._emp = pgen._empresa()
    pdf.add_page()
    ruta = tmp_path / "iniciales.pdf"
    pdf.output(str(ruta))

    texto = _texto(str(ruta))
    assert esperado in texto, f"no se dibujaron las iniciales {esperado!r}"


def test_el_aviso_no_fiscal_no_pisa_las_observaciones_del_remito(pgen, tmp_path):
    """🔴 Defecto que la compresión destapó.

    La caja de observaciones medía 28 mm fijos, pero el cursor quedaba donde
    terminaba el **texto**, no donde terminaba la **caja**. El aviso de "no
    válido como factura" se ancla al pie, así que con la hoja llena se dibujaba
    encima del recuadro. Antes no se veía porque con esta cantidad de ítems el
    remito ni siquiera entraba en una hoja.

    **12 ítems no es un número al azar**: se midió con el código defectuoso y
    es donde se reproduce —una sola carilla, con el aviso a 38 pt del texto de
    la observación, o sea adentro de un recuadro de 28 mm—. Con 10 sobraba
    lugar y el test pasaba con el defecto puesto.

    El invariante se verifica **por página**: si los dos textos caen en la
    misma, tiene que haber aire suficiente; si el aviso se fue a la siguiente,
    no hay nada que pisar. Las dos aserciones de existencia evitan que el test
    pase por no encontrar nada.
    """
    ruta = pgen.generate_pdf(
        {"number": "0001-00000001", "date": "2026-08-03",
         "observations": "Entrega en mano - reviso el area de sistemas.",
         **CLIENTE, "items": _items(12)}, output_dir=str(tmp_path))

    vistos = {"observacion": False, "aviso": False}
    for numero, pagina in enumerate(PdfReader(ruta).pages, start=1):
        posiciones = {}

        def visitor(text, cm, tm, font_dict, font_size, _p=posiciones):
            limpio = text.strip()
            if limpio.startswith("Entrega en mano"):
                _p.setdefault("observacion", tm[5])
            elif "NO VÁLIDO COMO FACTURA" in limpio.upper():
                _p.setdefault("aviso", tm[5])

        pagina.extract_text(visitor_text=visitor)
        for clave in posiciones:
            vistos[clave] = True

        if len(posiciones) == 2:
            # En coordenadas PDF el origen está abajo: el aviso queda por
            # debajo de la observación. El recuadro baja ~20 mm (57 pt) desde
            # el texto, así que menos que eso es superposición.
            separacion = posiciones["observacion"] - posiciones["aviso"]
            assert separacion > 57, (
                f"en la página {numero} el aviso quedó a {separacion:.1f} pt "
                "de la observación: se está dibujando encima del recuadro")

    assert all(vistos.values()), f"no se dibujaron los dos textos: {vistos}"


# ── La guarda fiscal ───────────────────────────────────────────────

def test_la_factura_conserva_todos_los_campos_fiscales(pgen, tmp_path):
    """🔴 La compresión toca alturas, **nunca** datos.

    Los campos del emisor y del receptor son obligatorios en una factura. Que
    los tests midan "entran más ítems" sin verificar esto dejaría la puerta
    abierta a ganar espacio sacando campos, que es exactamente lo que no se
    puede hacer.
    """
    total = 12500 * 3
    ruta = pgen.generate_pdf_factura(
        {"punto_venta": 1, "numero": 1, "tipo": 6, "concepto": 1,
         "fecha": "2026-08-03", "cae": "75123456789012",
         "cae_vto": "2026-08-13", "condicion_venta": "Contado",
         "cliente_razon": CLIENTE["client_name"],
         "cliente_cuit": CLIENTE["client_cuit"],
         "cliente_domicilio": CLIENTE["client_address"],
         "cliente_iva_cond": 1, "observaciones": "",
         "items": _items(3), "subtotal": total,
         "iva_amount": round(total * 0.21), "total": round(total * 1.21)},
        output_dir=str(tmp_path))

    texto = _texto(ruta)
    for esperado in (
        # Emisor
        "Compulibra - Soporte IT", "20-31234567-8", "Responsable Inscripto",
        "Av. Villarino 1200", "01-03-2018", "902-654321-7",
        # Receptor
        "Clinica del Sol S.A.", "30-65432198-2", "Rivadavia 1290",
        # Fiscales del comprobante
        "75123456789012", "Subtotal", "Total",
    ):
        assert esperado in texto, f"la factura perdió {esperado!r}"


def test_la_etiqueta_se_alinea_con_el_primer_renglon_del_valor(pgen, tmp_path):
    """Defecto que existía desde antes y que comprimir volvió evidente.

    La etiqueta se centraba sobre el alto **total** del campo, así que con un
    domicilio de dos renglones quedaba al lado del segundo: "Domicilio"
    terminaba rotulando la ciudad y el primer renglón quedaba sin etiqueta.

    Se ejercita `_draw_card` **directamente**, con una etiqueta que no existe
    en ningún otro lugar del comprobante: en un presupuesto real hay dos
    "Domicilio" -el del emisor y el del cliente- y la primera versión de este
    test agarraba el que no era, comparando posiciones de tarjetas distintas.

    **Se mide contra el SEGUNDO renglón del valor, no contra el primero.**
    `pypdf` reporta `tm[5] = 0` para el primer fragmento de un `multi_cell`
    (posicionamiento relativo dentro del mismo objeto de texto), así que la
    posición del primer renglón no es utilizable. El invariante equivalente:
    si la etiqueta está alineada con el primer renglón, queda **un alto de
    fila completo** por encima del segundo; si vuelve a centrarse sobre el
    campo entero, queda a la mitad de eso.
    """
    pdf = pgen.PresupuestoPDF({"number": "0001-00000001", "date": "2026-08-03",
                               "valid_until": "2026-08-17"})
    pdf._emp = pgen._empresa()
    pdf.add_page()
    campos = [("EtiquetaUnica",
               "Una direccion suficientemente larga como para partirse en dos")]
    alto = pgen._measure_card_h(pdf, pgen._CARD_W, campos)
    pgen._draw_card(pdf, pgen._LX, 60, pgen._CARD_W, alto, "PRUEBA", campos)

    ruta = tmp_path / "tarjeta.pdf"
    pdf.output(str(ruta))

    pagina = PdfReader(str(ruta)).pages[0]
    posiciones = {}

    def visitor(text, cm, tm, font_dict, font_size):
        limpio = text.strip()
        if limpio == "EtiquetaUnica":
            posiciones.setdefault("etiqueta", tm[5])
        elif limpio.startswith("suficientemente"):
            posiciones.setdefault("segundo_renglon", tm[5])

    pagina.extract_text(visitor_text=visitor)
    assert set(posiciones) == {"etiqueta", "segundo_renglon"}, \
        f"no se ubicaron los dos textos: {posiciones}"

    fila_pt = pgen._CARD_ROW_H * 72 / 25.4          # mm -> pt
    salto = posiciones["etiqueta"] - posiciones["segundo_renglon"]
    assert salto > fila_pt * 0.8, (
        f"la etiqueta quedó a {salto:.1f} pt del segundo renglón, y con un "
        f"alto de fila de {fila_pt:.1f} pt debería estar a uno entero: volvió "
        "a centrarse sobre el alto completo del campo")
