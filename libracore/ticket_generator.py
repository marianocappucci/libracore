"""
Generador de tickets de impresión para ticketeadoras térmicas.
Produce un PDF angosto (58 mm ó 80 mm) que puede imprimirse directamente
en impresoras de rollo tipo Epson TM, Star, Bixolon, etc.

Extraído de Contalibra el 2026-07-28. Restolibra tenía una copia **idéntica
de las 385 líneas** más su comanda de cocina propia, así que el mismo
generador existía dos veces — tercer caso de esta clase, después de
`facturas_borrador` y `cuenta_corriente`.

Un producto que necesite un ticket que acá no está (la comanda de cocina de
Restolibra, por ejemplo) puede construirlo sobre las piezas públicas de este
módulo — `TicketPDF`, `cfg_ticket()`, `recortar_a_contenido()`,
`fmt_fecha()` — en vez de copiar el archivo entero.
"""
import base64
import json
import os

from fpdf import FPDF  # noqa: F401  (lo usa _TextoSeguroPDF)

from libracore import config_manager, medios_pago

from .pdf_generator import _TextoSeguroPDF

try:
    import qrcode as _qrlib
    _HAS_QR = True
except ImportError:
    _HAS_QR = False


def _ar(value, decimals=2):
    """Formato monetario argentino: punto miles, coma decimal."""
    try:
        s = f"{float(value):,.{decimals}f}"
        return s.replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return str(value)


# ── Constantes ─────────────────────────────────────────────────────────────────

_MM_TO_PT = 2.8346

_ANCHOS = {
    "58": 58,
    "80": 80,
}

# 🔴 Acá había un `_MEDIOS_LABEL` propio con cinco medios —**sin
# `cuenta_corriente`**, así que un ticket de una venta a crédito imprimía el
# slug crudo—. Y `pdf_generator` tenía otro con el MISMO nombre, en el mismo
# repo, con contenido distinto ("Billetera" contra "Billetera Virtual",
# "Mercado Pago" contra "MercadoPago"): el mismo cobro salía escrito de dos
# formas según si el cliente pedía el ticket o el recibo.
#
# `medios_pago.label()` es la única etiqueta, y nunca devuelve vacío.

_TIPO_FACTURA = {
    1:  "FACTURA A",   2:  "NOTA DÉBITO A",   3:  "NOTA CRÉDITO A",
    6:  "FACTURA B",   7:  "NOTA DÉBITO B",   8:  "NOTA CRÉDITO B",
    11: "FACTURA C",   12: "NOTA DÉBITO C",   13: "NOTA CRÉDITO C",
}


# ── PDF base ───────────────────────────────────────────────────────────────────

# Misma base que los comprobantes: un caracter fuera de cp1252 no puede
# tumbar la impresion de un ticket. Ver .
class TicketPDF(_TextoSeguroPDF):
    def __init__(self, ancho_mm: int, fuente_size: int):
        # Márgenes laterales 2 mm; altura de página dinámica (se extiende sola)
        super().__init__(orientation="P", unit="mm", format=(ancho_mm, 2000))
        self.set_margins(2, 2, 2)
        self.set_auto_page_break(auto=True, margin=2)
        self._ancho = ancho_mm
        self._fs = fuente_size          # tamaño base
        self._w = ancho_mm - 4         # ancho útil
        self.add_page()
        self.set_font("Courier", size=fuente_size)

    # helpers
    def _line_h(self):
        return self._fs * 0.35 + 0.5   # aprox mm por línea de texto

    def _separador(self, char="-"):
        cols = int(self._w / (self._fs * 0.21))  # caracteres en el ancho
        self.set_font("Courier", size=self._fs)
        self.cell(self._w, self._line_h(), char * cols, ln=True)

    def _row(self, izq: str, der: str, bold_izq=False, bold_der=False):
        lh = self._line_h()
        ancho_der = self._w * 0.38
        ancho_izq = self._w - ancho_der
        self.set_font("Courier", "B" if bold_izq else "", self._fs)
        self.cell(ancho_izq, lh, izq, ln=False)
        self.set_font("Courier", "B" if bold_der else "", self._fs)
        self.cell(ancho_der, lh, der, align="R", ln=True)

    def _centrado(self, txt: str, size: int = 0, bold: bool = False):
        s = size or self._fs
        self.set_font("Courier", "B" if bold else "", s)
        self.multi_cell(self._w, s * 0.35 + 0.5, txt, align="C")
        # fpdf2 deja el cursor X en el borde derecho tras multi_cell; resetear
        # al margen izquierdo para que el siguiente elemento no arranque pegado
        # a la derecha y se corte (dejaba caracteres sueltos como "V" o "-").
        self.set_x(self.l_margin)

    def _texto(self, txt: str, bold: bool = False):
        self.set_font("Courier", "B" if bold else "", self._fs)
        self.multi_cell(self._w, self._line_h(), txt)
        self.set_x(self.l_margin)


def cfg_ticket():
    cfg = config_manager.load()
    ancho_mm = int(_ANCHOS.get(str(cfg.get("ticket_ancho_mm", "80")), 80))
    fuente   = max(6, min(14, int(cfg.get("ticket_fuente_size", "9") or "9")))
    logo     = str(cfg.get("ticket_mostrar_logo", "0")) == "1"
    corte    = str(cfg.get("ticket_linea_corte", "1")) == "1"
    pie      = str(cfg.get("ticket_pie", "")).strip()
    return ancho_mm, fuente, logo, corte, pie, cfg


def _empresa_header(pdf: TicketPDF, cfg: dict, logo: bool):
    nombre = cfg.get("empresa_nombre", "") or ""
    dir_   = cfg.get("empresa_direccion", "") or ""
    cuit   = cfg.get("empresa_cuit", "") or ""
    tel    = cfg.get("empresa_telefono", "") or ""

    logo_drawn = False
    if logo:
        logo_path = config_manager.resolve_logo_path(cfg)
        if logo_path and os.path.exists(logo_path):
            logo_w = min(pdf._w * 0.5, 30)
            pdf.image(logo_path, x=(pdf._ancho - logo_w) / 2, w=logo_w)
            pdf.ln(1)
            logo_drawn = True

    iva_cond = cfg.get("empresa_iva_condition", "") or ""

    # Con logo dibujado, el nombre en texto es redundante (mismo criterio que el
    # PDF A4). Si el logo no se pudo dibujar, se cae al nombre como antes.
    if nombre and not logo_drawn:
        pdf._centrado(nombre[:40], bold=True)
    if dir_:
        pdf._centrado(dir_[:48])
    if cuit:
        pdf._centrado(f"CUIT: {cuit}")
    if iva_cond:
        pdf._centrado(iva_cond[:40])
    if tel:
        pdf._centrado(f"Tel: {tel}")


def _pie_ticket(pdf: TicketPDF, pie: str, corte: bool):
    pdf.ln(2)
    if pie:
        pdf._separador()
        pdf._centrado(pie[:80])
    pdf.ln(2)
    if corte:
        pdf._separador("=")
        pdf._centrado("- - - - CORTE - - - -")
        pdf._separador("=")
    pdf.ln(1)


def recortar_a_contenido(pdf: TicketPDF) -> bytes:
    """Recorta la altura del PDF al contenido real generado."""
    alto_real = pdf.get_y() + 5
    page     = pdf.pages[1]
    h_old_pt = page._height_pt
    h_new_pt = alto_real * _MM_TO_PT
    # Ajustar coordenadas: el contenido fue dibujado asumiendo h_old_pt de altura;
    # prepend una traslación para que quede visible en la nueva página más chica.
    ty = h_new_pt - h_old_pt  # negativo → desplaza contenido hacia abajo en PDF
    page.set_dimensions(page._width_pt, h_new_pt)
    transform = f"q 1 0 0 1 0 {ty:.3f} cm\n".encode()
    page.contents = bytearray(transform) + page.contents + bytearray(b"\nQ")
    return bytes(pdf.output())


# ── QR helpers ────────────────────────────────────────────────────────────────

def _afip_qr_url(factura: dict, empresa_cuit: str) -> str:
    cuit_rec = (factura.get("cliente_cuit") or "").replace("-", "").strip()
    tipo_doc = 80 if (len(cuit_rec) == 11 and cuit_rec.isdigit()) else 99
    nro_doc  = int(cuit_rec) if tipo_doc == 80 else 0
    cae_s    = (factura.get("cae") or "").strip()
    cae_int  = int(cae_s) if cae_s.isdigit() else 0
    cuit_e   = empresa_cuit.replace("-", "").strip()
    d = {"ver": 1, "fecha": factura.get("fecha", ""),
         "cuit": int(cuit_e) if cuit_e.isdigit() else 0,
         "ptoVta": int(factura.get("punto_venta", 1)),
         "tipoCmp": int(factura.get("tipo", 11)),
         "nroCmp": int(factura.get("numero", 1)),
         "importe": round(float(factura.get("total", 0)), 2),
         "moneda": "PES", "ctz": 1,
         "tipoDocRec": tipo_doc, "nroDocRec": nro_doc,
         "tipoCodAut": "E", "codAut": cae_int}
    enc = base64.b64encode(json.dumps(d, separators=(",", ":")).encode()).decode()
    return f"https://www.afip.gob.ar/fe/qr/?p={enc}"


def _draw_qr_ticket(pdf: TicketPDF, url: str):
    if not _HAS_QR:
        pdf._centrado("[QR ARCA no disponible]")
        return
    try:
        qr = _qrlib.QRCode(version=None,
                            error_correction=_qrlib.constants.ERROR_CORRECT_M,
                            box_size=1, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        n    = len(matrix)
        size = min(pdf._w, 30)
        x0   = (pdf._ancho - size) / 2
        y0   = pdf.get_y()
        cell = size / n
        pdf.set_fill_color(0, 0, 0)
        for ri, row in enumerate(matrix):
            for ci, dark in enumerate(row):
                if dark:
                    pdf.rect(x0 + ci * cell, y0 + ri * cell, cell, cell, style="F")
        pdf.set_fill_color(255, 255, 255)
        pdf.set_y(y0 + size + 1)
    except Exception:
        pdf._centrado("[QR no disponible]")


# ── Ticket de VENTA ────────────────────────────────────────────────────────────

def fmt_fecha(s: str) -> str:
    """Convierte a 'dd-mm-aaaa'. Acepta 'YYYY-MM-DD[ HH:MM...]' (preservando la hora)
    y el formato ARCA 'AAAAMMDD' (ej: vencimiento de CAE)."""
    s = s or ""
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        d = f"{s[8:10]}-{s[5:7]}-{s[0:4]}"
        resto = s[10:].strip()
        return f"{d} {resto}" if resto else d
    if len(s) == 8 and s.isdigit():
        return f"{s[6:8]}-{s[4:6]}-{s[0:4]}"
    return s


def generar_ticket_venta(venta: dict) -> bytes:
    ancho_mm, fuente, logo, corte, pie, cfg = cfg_ticket()
    pdf = TicketPDF(ancho_mm, fuente)
    # Se corta el papel, se traba la impresora: pedir el ticket de nuevo tiene
    # que dar el mismo archivo. Ver `_TextoSeguroPDF.fijar_fecha_documento()`.
    pdf.fijar_fecha_documento(venta.get("fecha"))

    _empresa_header(pdf, cfg, logo)
    pdf._separador()

    # Encabezado de la venta
    fecha = fmt_fecha((venta.get("fecha") or "")[:16])
    pdf._centrado("TICKET DE VENTA", bold=True)
    pdf._centrado(f"N° {venta.get('id', '')}")
    pdf._centrado(fecha)
    pdf._separador()

    cliente = venta.get("cliente_nombre") or "Consumidor final"
    if cliente and cliente != "Consumidor final":
        pdf._texto(f"Cliente: {cliente[:36]}")
        cuit_c = venta.get("cliente_cuit") or ""
        if cuit_c:
            pdf._texto(f"CUIT: {cuit_c}")
        pdf._separador("-")

    # Ítems
    pdf._row("PRODUCTO", "TOTAL", bold_izq=True, bold_der=True)
    pdf._separador()
    items = venta.get("items", [])
    for it in items:
        nombre = str(it.get("nombre", ""))[:28]
        cant   = float(it.get("cantidad", 0))
        precio = float(it.get("precio_unitario", 0))
        subtot = cant * precio
        cant_s = f"{cant:g}" if cant != int(cant) else str(int(cant))
        pdf._texto(f"{nombre}")
        pdf._row(f"  {cant_s} x $" + _ar(precio), "$" + _ar(subtot))

    pdf._separador()
    descuento = float(venta.get("descuento", 0) or 0)
    if descuento:
        pdf._row("Descuento:", "-$" + _ar(descuento))
    pdf._row("TOTAL:", "$" + _ar(float(venta.get('total', 0))), bold_der=True)

    # Medios de pago
    pagos = venta.get("pagos", [])
    if pagos:
        pdf._separador("-")
        for p in pagos:
            label = medios_pago.label(p.get("medio", ""))
            pdf._row(label + ":", "$" + _ar(float(p.get('monto', 0))))

    _pie_ticket(pdf, pie, corte)
    return recortar_a_contenido(pdf)


# ── Ticket de CIERRE DIARIO ────────────────────────────────────────────────
#
# Dos funciones nuevas (Fase 1 de "cierre diario con comprobante impreso",
# pedido del humano el 2026-09-13): el arqueo de UN turno y el cierre de UNA
# sucursal. Las dos reciben datos ya resueltos —nunca reabren
# `caja_movimientos`— y por eso reimprimir dos veces da el mismo PDF, incluso
# si después alguien anula un movimiento del turno.
#
# 🔴 **Nombres que el motor no tiene.** `libracore.db` no sabe qué es una
# "sucursal" —vive en la base del producto, ver `cierre_diario.py`— así que
# `sucursal_nombre` viene siempre por parámetro. `caja_nombre` en cambio SÍ lo
# resuelve el motor cuando arma la foto (`cajas.nombre` es de esta base), pero
# la función lo acepta igual por parámetro: así sirve tanto para reimprimir
# desde la foto guardada como para el ticket que un producto imprima al
# cerrar el turno en el momento (antes de que exista ningún cierre diario),
# que es Fase 2 y arma el diccionario con lo que tenga a mano.


def _signo_diferencia(valor: float) -> tuple[str, str]:
    """`(signo, leyenda)` de una diferencia de arqueo. Positiva es plata de
    más (sobrante); negativa, de menos (faltante). Cero no lleva leyenda."""
    if valor > 0.0009:
        return "+", "sobrante"
    if valor < -0.0009:
        return "-", "faltante"
    return "", ""


def _fila_medio(pdf: TicketPDF, medio: dict):
    label = medios_pago.label(medio.get("medio_pago", ""))
    neto = float(medio.get("neto", 0))
    signo, _ = _signo_diferencia(neto)
    pdf._row(f"{label}:", f"{signo}${_ar(abs(neto))}")


def generar_ticket_cierre_turno(arqueo: dict) -> bytes:
    """Arqueo de UN turno: caja, cajero, apertura/cierre, inicial, esperado,
    declarado, diferencia (con signo y leyenda) y desglose por medio.

    `arqueo` (todas las claves opcionales salvo las de montos):
    `turno_id`, `cajero_nombre`, `caja_nombre`, `sucursal_nombre`,
    `apertura`, `cierre` (`'YYYY-MM-DD HH:MM[:SS]'`), `monto_inicial`,
    `monto_esperado`, `monto_declarado`, `diferencia`,
    `medios` (lista de `{medio_pago, ingresos, egresos, neto}`).

    Es la misma forma que devuelve `cierre_diario.get_cierre_turno()`, más
    `sucursal_nombre`, que ese diccionario no trae — lo agrega quien arma el
    ticket (ver `caja_router.build_cierre_diario_router`).
    """
    ancho_mm, fuente, logo, corte, pie, cfg = cfg_ticket()
    pdf = TicketPDF(ancho_mm, fuente)
    pdf.fijar_fecha_documento((arqueo.get("cierre") or arqueo.get("apertura") or "")[:16])

    _empresa_header(pdf, cfg, logo)
    pdf._separador()

    pdf._centrado("CIERRE DE TURNO", bold=True)
    if arqueo.get("turno_id"):
        pdf._centrado(f"Turno N° {arqueo['turno_id']}")
    if arqueo.get("sucursal_nombre"):
        pdf._centrado(str(arqueo["sucursal_nombre"])[:40])
    if arqueo.get("caja_nombre"):
        pdf._centrado(str(arqueo["caja_nombre"])[:40])
    pdf._separador()

    if arqueo.get("cajero_nombre"):
        pdf._texto(f"Cajero: {arqueo['cajero_nombre']}")
    if arqueo.get("apertura"):
        pdf._row("Apertura:", fmt_fecha(str(arqueo["apertura"])[:16]))
    if arqueo.get("cierre"):
        pdf._row("Cierre:", fmt_fecha(str(arqueo["cierre"])[:16]))
    pdf._separador("-")

    pdf._row("Monto inicial:", "$" + _ar(arqueo.get("monto_inicial", 0)))
    pdf._row("Esperado:", "$" + _ar(arqueo.get("monto_esperado", 0)))
    pdf._row("Declarado:", "$" + _ar(arqueo.get("monto_declarado", 0)))

    medios = arqueo.get("medios") or []
    if medios:
        pdf._separador("-")
        pdf._row("MEDIO", "NETO", bold_izq=True, bold_der=True)
        for medio in medios:
            _fila_medio(pdf, medio)

    pdf._separador()
    diferencia = float(arqueo.get("diferencia", 0))
    signo, leyenda = _signo_diferencia(diferencia)
    etiqueta = f"DIFERENCIA{f' ({leyenda})' if leyenda else ''}:"
    pdf._row(etiqueta, f"{signo}${_ar(abs(diferencia))}", bold_izq=True, bold_der=True)

    _pie_ticket(pdf, pie, corte)
    return recortar_a_contenido(pdf)


def generar_ticket_cierre_diario(cierre: dict, sucursal_nombre: str = "") -> bytes:
    """Cierre de UNA sucursal: número, sucursal, fecha, quién y cuándo cerró,
    una línea por turno agrupada por caja (con subtotal de caja), desglose
    por medio de la sucursal y totales con la diferencia final.

    `cierre` es la forma que devuelve `cierre_diario.get_cierre()`: la
    cabecera (`numero`, `fecha`, `usuario_id`, `cerrado_en`/`created_at`,
    `monto_esperado_total`, `monto_declarado_total`, `diferencia_total`) más
    `cajas` (cada una con `caja_nombre` y sus `turnos`) y `medios` (el
    desglose de la sucursal). `sucursal_nombre` no está en `cierre`: el motor
    no conoce sucursales, así que la resuelve el producto y se la pasa acá
    (ver `caja_router.build_cierre_diario_router`).

    `cierre.get("cerrado_por_nombre")` es opcional: si el llamador ya resolvió
    el nombre de `usuario_id`, lo muestra; si no, muestra sólo la fecha.
    """
    ancho_mm, fuente, logo, corte, pie, cfg = cfg_ticket()
    pdf = TicketPDF(ancho_mm, fuente)
    pdf.fijar_fecha_documento((cierre.get("created_at") or cierre.get("fecha") or "")[:16])

    _empresa_header(pdf, cfg, logo)
    pdf._separador()

    pdf._centrado("CIERRE DIARIO", bold=True)
    pdf._centrado(f"N° {cierre.get('numero', '')}")
    if sucursal_nombre:
        pdf._centrado(str(sucursal_nombre)[:40])
    pdf._centrado(fmt_fecha(str(cierre.get("fecha", ""))[:10]))
    quien = cierre.get("cerrado_por_nombre")
    cuando = fmt_fecha(str(cierre.get("created_at", ""))[:16]) if cierre.get("created_at") else ""
    if quien or cuando:
        pdf._centrado(" — ".join(p for p in (quien, cuando) if p))
    pdf._separador()

    for caja in cierre.get("cajas", []):
        nombre_caja = caja.get("caja_nombre") or "Sin caja"
        pdf._texto(nombre_caja, bold=True)
        for turno in caja.get("turnos", []):
            pdf._row(
                f"  T#{turno.get('turno_id', turno.get('id',''))} {str(turno.get('cajero_nombre',''))[:18]}",
                "$" + _ar(turno.get("monto_declarado", 0)),
            )
            diferencia_t = float(turno.get("diferencia", 0))
            signo_t, leyenda_t = _signo_diferencia(diferencia_t)
            if leyenda_t:
                pdf._row(f"    dif. ({leyenda_t}):", f"{signo_t}${_ar(abs(diferencia_t))}")
        medios_caja = caja.get("medios") or []
        if medios_caja:
            for medio in medios_caja:
                _fila_medio(pdf, medio)
        pdf._separador("-")

    medios_sucursal = cierre.get("medios") or []
    if medios_sucursal:
        pdf._row("MEDIO (total)", "NETO", bold_izq=True, bold_der=True)
        for medio in medios_sucursal:
            _fila_medio(pdf, medio)
        pdf._separador()

    pdf._row("Esperado:", "$" + _ar(cierre.get("monto_esperado_total", 0)))
    pdf._row("Declarado:", "$" + _ar(cierre.get("monto_declarado_total", 0)))
    diferencia = float(cierre.get("diferencia_total", 0))
    signo, leyenda = _signo_diferencia(diferencia)
    etiqueta = f"DIFERENCIA{f' ({leyenda})' if leyenda else ''}:"
    pdf._row(etiqueta, f"{signo}${_ar(abs(diferencia))}", bold_izq=True, bold_der=True)

    _pie_ticket(pdf, pie, corte)
    return recortar_a_contenido(pdf)


# ── Ticket de FACTURA ELECTRÓNICA ──────────────────────────────────────────────

def generar_ticket_factura(factura: dict) -> bytes:
    ancho_mm, fuente, logo, corte, pie, cfg = cfg_ticket()
    pdf = TicketPDF(ancho_mm, fuente)
    pdf.fijar_fecha_documento(factura.get("fecha"))

    _empresa_header(pdf, cfg, logo)
    pdf._separador()

    tipo_label = _TIPO_FACTURA.get(int(factura.get("tipo", 11)), "COMPROBANTE")
    pv   = str(factura.get("punto_venta", "")).zfill(4)
    num  = str(factura.get("numero", "")).zfill(8)
    fecha = fmt_fecha((factura.get("fecha") or "")[:10])

    pdf._centrado(tipo_label, bold=True)
    pdf._centrado(f"N° {pv}-{num}")
    pdf._centrado(fecha)
    pdf._separador()

    razon = factura.get("cliente_razon") or "Consumidor final"
    cuit  = factura.get("cliente_cuit") or ""
    if razon:
        pdf._texto(f"Cliente: {razon[:36]}")
    if cuit:
        pdf._texto(f"CUIT: {cuit}")
    if razon or cuit:
        pdf._separador("-")

    # Ítems
    pdf._row("PRODUCTO", "TOTAL", bold_izq=True, bold_der=True)
    pdf._separador()
    items = factura.get("items", [])
    if isinstance(items, str):
        items = json.loads(items)
    for it in items:
        # Los ítems de factura se guardan con las claves description/qty/unit_price
        # (ver form_helper.extract_items_from_form). Se contemplan también las
        # variantes descripcion/cantidad/precio_unitario por compatibilidad.
        raw_desc = str(it.get("description") or it.get("descripcion") or it.get("nombre", ""))
        nombre = raw_desc.split("\n", 1)[0][:28]   # primera línea; el detalle no va al ticket
        cant   = float(it.get("qty", it.get("cantidad", 1)) or 1)
        precio = float(it.get("unit_price", it.get("precio_unitario", it.get("precio", 0))) or 0)
        subtot = it.get("subtotal")
        subtot = float(subtot) if subtot is not None else cant * precio
        cant_s = f"{cant:g}" if cant != int(cant) else str(int(cant))
        pdf._texto(f"{nombre}")
        pdf._row(f"  {cant_s} x $" + _ar(precio), "$" + _ar(subtot))

    pdf._separador()
    subtotal = float(factura.get("subtotal", 0))
    iva      = float(factura.get("iva_amount", 0))
    total    = float(factura.get("total", 0))
    if iva:
        pdf._row("Neto:", "$" + _ar(subtotal))
        # Agrupar IVA por alícuota para mostrar cada tasa por separado
        iva_por_pct: dict = {}
        for it in items:
            pct = float(it.get("iva_pct", 0) or 0)
            if pct:
                cant_i   = float(it.get("qty", it.get("cantidad", 1)) or 1)
                precio_i = float(it.get("unit_price", it.get("precio_unitario", it.get("precio", 0))) or 0)
                neto_i   = cant_i * precio_i / (1 + pct / 100)
                iva_por_pct[pct] = iva_por_pct.get(pct, 0.0) + neto_i * pct / 100
        if iva_por_pct:
            for pct, monto_iva in sorted(iva_por_pct.items()):
                pdf._row(f"IVA {pct:.0f}%:", "$" + _ar(monto_iva))
        else:
            # Fallback: calcular alícuota desde totales si no hay iva_pct en ítems
            if subtotal > 0:
                pct_calc = round(iva / subtotal * 100)
                pdf._row(f"IVA {pct_calc:.0f}%:", "$" + _ar(iva))
            else:
                pdf._row("IVA:", "$" + _ar(iva))
    pdf._row("TOTAL:", "$" + _ar(total), bold_der=True)

    # Condición de venta
    cond_venta = (factura.get("condicion_venta") or "Contado").strip()
    pdf._centrado(f"Cond. venta: {cond_venta}")

    # CAE + QR
    cae     = factura.get("cae") or ""
    cae_vto = factura.get("cae_vto") or ""
    if cae:
        pdf._separador("-")
        pdf._texto(f"CAE: {cae}")
        if cae_vto:
            pdf._texto(f"Vto CAE: {fmt_fecha(cae_vto)}")
        empresa_cuit = cfg.get("empresa_cuit", "") or ""
        if empresa_cuit:
            pdf.ln(2)
            _draw_qr_ticket(pdf, _afip_qr_url(factura, empresa_cuit))
            pdf._centrado("Comprobante autorizado por ARCA")

    _pie_ticket(pdf, pie, corte)
    return recortar_a_contenido(pdf)
