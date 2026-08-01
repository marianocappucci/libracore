import os
import json
import base64
from fpdf import FPDF  # fpdf2 >= 2.8
from fpdf.enums import RenderStyle as _RS, Corner as _Cor
from . import config_manager


def _ar(value, decimals=2):
    """Formato monetario argentino: punto miles, coma decimal."""
    try:
        s = f"{float(value):,.{decimals}f}"
        return s.replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return str(value)

# Alias de corners para uso interno (la API de fpdf2 2.8.x usa nombres
# que corresponden a la posición VISUAL inversa al eje x)
_C_ALL  = (_Cor.TOP_RIGHT, _Cor.TOP_LEFT, _Cor.BOTTOM_RIGHT, _Cor.BOTTOM_LEFT)
_C_TL   = (_Cor.TOP_RIGHT,)                        # solo esquina superior izquierda visual
_C_BOT  = (_Cor.BOTTOM_RIGHT, _Cor.BOTTOM_LEFT)    # ambas esquinas inferiores visuales


def _wrap_text(pdf, txt: str, max_w: float) -> list[str]:
    """Divide txt en líneas que caben en max_w con la fuente activa."""
    lines, cur = [], ""
    for word in txt.split():
        candidate = (cur + " " + word).strip()
        if pdf.get_string_width(candidate) <= max_w:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def _rrect(pdf, x, y, w, h, r=None, corners=None, style="DF"):
    """Wrapper limpio para _draw_rounded_rect de fpdf2."""
    _r = r if r is not None else _CR
    _c = corners if corners is not None else _C_ALL
    _s = {"F": _RS.F, "D": _RS.D, "FD": _RS.DF, "DF": _RS.DF}.get(style.upper(), _RS.DF)
    pdf._draw_rounded_rect(x, y, w, h, _s, _c, r=_r)

_DATA_DIR            = os.environ.get("DATA_DIR", os.path.dirname(__file__))
PDF_DIR              = os.path.join(_DATA_DIR, "remitos_pdf")
PRESUPUESTOS_PDF_DIR = os.path.join(_DATA_DIR, "presupuestos_pdf")
FACTURAS_PDF_DIR     = os.path.join(_DATA_DIR, "facturas_pdf")
RESUMENES_CC_PDF_DIR = os.path.join(_DATA_DIR, "resumenes_cc_pdf")

_TIPO_LABELS     = {1:"FACTURA A",       6:"FACTURA B",       11:"FACTURA C",
                    3:"NOTA CREDITO A", 8:"NOTA CREDITO B", 13:"NOTA CREDITO C",
                    2:"NOTA DEBITO A",  7:"NOTA DEBITO B",  12:"NOTA DEBITO C"}
_CONCEPTO_LABELS = {1:"Productos", 2:"Servicios", 3:"Productos y Servicios"}
_TIPO_LETRA      = {1:"A", 6:"B", 11:"C", 3:"A", 8:"B", 13:"C", 2:"A", 7:"B", 12:"C"}
_TIPO_COD        = {1:"001", 6:"006", 11:"011", 3:"003", 8:"008", 13:"013",
                    2:"002", 7:"007", 12:"012"}
_TIPO_NOMBRE_DOC = {1:"Factura",         6:"Factura",         11:"Factura",
                    3:"Nota de Crédito", 8:"Nota de Crédito", 13:"Nota de Crédito",
                    2:"Nota de Débito",  7:"Nota de Débito",  12:"Nota de Débito"}
_IVA_LABELS      = {1:"Responsable Inscripto", 6:"Monotributista", 4:"IVA Exento",
                    5:"Consumidor Final", 3:"No Alcanzado"}
_IVA_EMISOR_LABEL = {"Monotributista":        "Responsable Monotributo",
                     "Responsable Inscripto": "IVA Responsable Inscripto",
                     "IVA Exento":            "IVA Exento"}
_TIPOS_C = {11, 12, 13}

# ── Paleta (de la plantilla HTML) ────────────────────────────────────────────
_INK         = (40,  37,  29)    # --ink:          #28251d
_MUTED       = (111, 107, 98)    # --muted:        #6f6b62
_LINE        = (216, 211, 201)   # --line:         #d8d3c9
_ACCENT      = (1,   105, 111)   # --accent:       #01696f
_ACCENT_SOFT = (230, 241, 242)   # --accent-soft:  #e6f1f2
_ACCENT_DARK = (23,  75,  79)    # notes text:     #174b4f
_WHITE       = (255, 255, 255)
_WARNING     = (150, 66,  25)    # --warning:      #964219

# ── Layout A4 18 mm márgenes ─────────────────────────────────────────────────
_LX = 18        # margen izquierdo
_RX = 192       # margen derecho
_CW = 174       # ancho de contenido

# Header columnas  1.2fr : 0.8fr
_LEFT_W  = 100
_GAP_COL = 8
_RIGHT_X = _LX + _LEFT_W + _GAP_COL   # 126
_RIGHT_W = _RX - _RIGHT_X              # 66

# Voucher box
_LETTER_W  = 22
_LETTER_RH = 20
_META_RH   = 5.5
_CR        = 3.5   # border-radius

# Cards
_CARD_GAP = 10
_CARD_W   = int((_CW - _CARD_GAP) / 2)  # 82

# Summary
_TOTALS_W = 78
_SUM_GAP  = 8
_NOTES_W  = _CW - _TOTALS_W - _SUM_GAP  # 88

# ── QR / PIL ─────────────────────────────────────────────────────────────────
try:
    import qrcode as _qrlib
    _HAS_QR = True
except ImportError:
    _HAS_QR = False

try:
    from PIL import Image as _PILImage
    import io as _io
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


def _logo_fit_dims(path: str, max_w: float, max_h: float):
    """Devuelve (w, h) para encajar el logo en max_w×max_h manteniendo proporción."""
    if not _HAS_PIL:
        return 0, max_h   # fpdf2 escala el ancho automáticamente con h fija
    try:
        img = _PILImage.open(path)
        iw, ih = img.size
        img.close()
        scale = min(max_w / iw, max_h / ih)
        return round(iw * scale, 2), round(ih * scale, 2)
    except Exception:
        return 0, max_h


def _prepare_logo(path: str):
    if not _HAS_PIL:
        return path
    try:
        img = _PILImage.open(path)
        if img.mode != "RGBA":
            return path
        _, _, _, alpha = img.split()
        opaque = [(img.getpixel((x, y))[:3])
                  for x in range(0, img.size[0], 15)
                  for y in range(0, img.size[1], 15)
                  if img.getpixel((x, y))[3] > 128]
        is_white = bool(opaque) and sum(r+g+b for r,g,b in opaque)/(len(opaque)*3) > 240
        bg = _PILImage.new("RGB", img.size, (255, 255, 255))
        if is_white:
            dark = _PILImage.new("RGB", img.size, (30, 30, 30))
            bg.paste(dark, mask=alpha)
        else:
            bg.paste(img, mask=alpha)
        buf = _io.BytesIO()
        bg.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception:
        return path


def _empresa():
    cfg = config_manager.load()
    return {
        "nombre":             cfg.get("empresa_nombre",            ""),
        "direccion":          cfg.get("empresa_direccion",         ""),
        "cuit":               cfg.get("empresa_cuit",              ""),
        "telefono":           cfg.get("empresa_telefono",          ""),
        "email":              cfg.get("empresa_email",             ""),
        "logo_path":          config_manager.resolve_logo_path(cfg),
        "iibb":               cfg.get("empresa_iibb",              ""),
        "iva_condition":      cfg.get("empresa_iva_condition",     "Monotributista"),
        "inicio_actividades": cfg.get("empresa_inicio_actividades",""),
    }


def _fmt_fecha(s: str) -> str:
    if not s or len(s) < 10:
        return s or ""
    return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"


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
    enc = base64.b64encode(json.dumps(d, separators=(",",":")).encode()).decode()
    return f"https://www.afip.gob.ar/fe/qr/?p={enc}"


def _draw_qr(pdf, url: str, x: float, y: float, size: float):
    if not _HAS_QR:
        return
    try:
        qr = _qrlib.QRCode(version=None,
                            error_correction=_qrlib.constants.ERROR_CORRECT_M,
                            box_size=1, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        n    = len(matrix)
        cell = size / n
        pdf.set_fill_color(0, 0, 0)
        for ri, row in enumerate(matrix):
            for ci, dark in enumerate(row):
                if dark:
                    pdf.rect(x + ci*cell, y + ri*cell, cell, cell, style="F")
        pdf.set_fill_color(*_WHITE)
    except Exception:
        pass


def _dashed_line(pdf, x1, y1, x2, y2, dash=2.5, gap=1.5):
    pdf.dashed_line(x1, y1, x2, y2, dash_length=dash, space_length=gap)


# ── Header block ──────────────────────────────────────────────────────────────

def _draw_header_block(pdf, letra, titulo, codigo, info_fields, empresa):
    y0 = _LX   # top margin 18 mm

    # Calcular altura del voucher box primero (para que el logo sea proporcional)
    meta_h = len(info_fields) * _META_RH + 5
    vh     = _LETTER_RH + meta_h

    # ── Izquierda: logo + título ──────────────────────────────────────────
    logo_path = empresa.get("logo_path", "")
    has_logo  = bool(logo_path and os.path.exists(logo_path))
    logo_sz   = 14   # fallback para cuadrado de iniciales

    if has_logo:
        lw, lh = _logo_fit_dims(logo_path, _CW * 0.45, vh)
        # Centrar verticalmente respecto al recuadro derecho
        ly = y0 + (vh - lh) / 2
        pdf.image(_prepare_logo(logo_path), x=_LX, y=ly, w=lw, h=lh)
    else:
        # Cuadrado teal con iniciales
        pdf.set_fill_color(*_ACCENT)
        _rrect(pdf, _LX, y0, logo_sz, logo_sz, r=2.5, style="F")
        ini = "".join(w[0].upper() for w in empresa.get("nombre","?").split()[:2]) or "?"
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_text_color(*_WHITE)
        pdf.set_xy(_LX, y0 + 3.5)
        pdf.cell(logo_sz, logo_sz - 7, ini[:3], align="C", ln=False)

    tx = _LX + logo_sz + 4
    tw = _LEFT_W - logo_sz - 4

    # Sin logo → nombre de empresa como texto; con logo → nada de texto
    if not has_logo:
        nombre = empresa.get("nombre", "")
        if nombre:
            pdf.set_font("Helvetica", "B", 14)
            pdf.set_text_color(*_INK)
            pdf.set_xy(tx, y0 + 1)
            pdf.multi_cell(tw, 6, nombre[:52], align="L")

    # ── Derecha: voucher box ──────────────────────────────────────────────
    vx = _RIGHT_X
    vy = y0
    vw = _RIGHT_W

    # Fondo blanco redondeado
    pdf.set_fill_color(*_WHITE)
    _rrect(pdf, vx, vy, vw, vh, style="F")

    # Celda de letra (fondo oscuro) — solo esquina visual sup-izq redondeada
    pdf.set_fill_color(*_INK)
    _rrect(pdf, vx, vy, _LETTER_W, _LETTER_RH, corners=_C_TL, style="F")

    # Letra
    pdf.set_font("Helvetica", "B", 26)
    pdf.set_text_color(*_WHITE)
    pdf.set_xy(vx, vy + 1)
    pdf.cell(_LETTER_W, _LETTER_RH - 2, letra, align="C", ln=False)

    # Tipo + código en fila de letra (lado derecho)
    tx2 = vx + _LETTER_W + 3
    tw2 = vw - _LETTER_W - 6
    pdf.set_text_color(*_INK)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_xy(tx2, vy + 4)
    pdf.cell(tw2, 6, titulo.title(), ln=False)
    if codigo:
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*_MUTED)
        pdf.set_xy(tx2, vy + 11)
        pdf.cell(tw2, 5, f"Código {codigo} · Original", ln=False)

    # Borde exterior redondeado (tinta oscura, sobre todo lo anterior)
    pdf.set_draw_color(*_INK)
    pdf.set_line_width(0.5)
    _rrect(pdf, vx, vy, vw, vh, style="D")

    # Separadores internos
    pdf.set_draw_color(*_LINE)
    pdf.set_line_width(0.3)
    # Línea horizontal (fila letra / filas meta)
    pdf.line(vx + 1, vy + _LETTER_RH, vx + vw - 1, vy + _LETTER_RH)
    # Línea vertical en fila letra (celda letra | datos tipo)
    pdf.line(vx + _LETTER_W, vy + 1, vx + _LETTER_W, vy + _LETTER_RH - 1)

    # Filas meta (PV / N° / Fecha)
    for i, (lbl, val) in enumerate(info_fields):
        ry = vy + _LETTER_RH + 2.5 + i * _META_RH
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*_MUTED)
        pdf.set_xy(vx + 3, ry)
        pdf.cell(32, _META_RH, lbl, ln=False)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_text_color(*_INK)
        pdf.cell(vw - 35, _META_RH, str(val or ""), ln=False)
        # Separador entre filas meta
        if i < len(info_fields) - 1:
            pdf.set_draw_color(*_LINE)
            pdf.set_line_width(0.2)
            pdf.line(vx + 3, ry + _META_RH, vx + vw - 3, ry + _META_RH)

    # Línea separadora del header (2 px, tinta oscura)
    sep_y = vy + vh + 5
    pdf.set_draw_color(*_INK)
    pdf.set_line_width(0.7)
    pdf.line(_LX, sep_y, _RX, sep_y)
    pdf.set_text_color(*_INK)
    return sep_y + 5


# ── Cards EMISOR / CLIENTE ────────────────────────────────────────────────────

def _card_field_lines(pdf, val_w, val_str, row_h=6):
    """Número exacto de líneas que ocupará val_str en multi_cell."""
    if not val_str:
        return 0
    pdf.set_font("Helvetica", "B", 7.5)
    lines = pdf.multi_cell(val_w, row_h, val_str, split_only=True)
    return max(1, len(lines))


def _measure_card_h(pdf, w, fields, row_h=6):
    lbl_w = w * 0.42
    val_w = w - lbl_w - 8
    total = sum(_card_field_lines(pdf, val_w, str(v), row_h) for _, v in fields if v)
    return 13 + total * row_h + 4


def _draw_card(pdf, x, y, w, h, title, fields):
    pdf.set_fill_color(*_WHITE)
    pdf.set_draw_color(*_LINE)
    pdf.set_line_width(0.3)
    _rrect(pdf, x, y, w, h, style="DF")

    pdf.set_font("Helvetica", "B", 7)
    pdf.set_text_color(*_ACCENT)
    pdf.set_xy(x + 4, y + 4)
    pdf.cell(w - 8, 5, title.upper(), ln=False)

    # Línea bajo el encabezado
    pdf.set_draw_color(*_LINE)
    pdf.set_line_width(0.2)
    pdf.line(x + 2, y + 11, x + w - 2, y + 11)

    fy    = y + 13
    row_h = 6
    lbl_w = w * 0.42
    val_w = w - lbl_w - 8

    for lbl, val in fields:
        if not val:
            continue
        val_str  = str(val)
        n_lines  = _card_field_lines(pdf, val_w, val_str, row_h)
        h_row    = n_lines * row_h

        pdf.set_xy(x + 4, fy)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*_MUTED)
        pdf.cell(lbl_w, h_row, lbl, align="L", ln=False)

        pdf.set_xy(x + 4 + lbl_w, fy)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(*_INK)
        pdf.multi_cell(val_w, row_h, val_str, align="L",
                       new_x="LEFT", new_y="NEXT")
        fy += h_row


def _draw_emisor_cliente(pdf, empresa, client_fields):
    y = pdf.get_y()

    iva_cond = empresa.get("iva_condition", "Monotributista")
    iva_lbl  = _IVA_EMISOR_LABEL.get(iva_cond, iva_cond)
    emisor_fields = [
        ("Razón social",       empresa.get("nombre", "")),
        ("CUIT",               empresa.get("cuit", "")),
        ("Condición IVA",      iva_lbl),
        ("Domicilio",          empresa.get("direccion", "")),
        ("Ingresos Brutos",    empresa.get("iibb", "")),
        ("Inicio actividades", _fmt_fecha(empresa.get("inicio_actividades", ""))),
    ]
    emisor_fields  = [(l, v) for l, v in emisor_fields if v]
    cliente_fields = [(l, v) for l, v in client_fields if v]

    h_emisor  = _measure_card_h(pdf, _CARD_W, emisor_fields)
    h_cliente = _measure_card_h(pdf, _CARD_W, cliente_fields)
    box_h     = max(h_emisor, h_cliente)

    _draw_card(pdf, _LX,                         y, _CARD_W, box_h, "Emisor",  emisor_fields)
    _draw_card(pdf, _LX + _CARD_W + _CARD_GAP,   y, _CARD_W, box_h, "Cliente", cliente_fields)

    pdf.set_text_color(*_INK)
    pdf.set_y(y + box_h + 6)


# ── Tabla de ítems ────────────────────────────────────────────────────────────

def _draw_items_table(pdf, items, show_iva_col=False, show_prices=True):
    if not show_prices:
        widths  = [154, 20]
        headers = ["DESCRIPCIÓN", "CANTIDAD"]
        aligns  = ["L", "C"]
    elif show_iva_col:
        widths  = [80, 18, 30, 16, 30]
        headers = ["DESCRIPCIÓN", "CANTIDAD", "PRECIO UNIT.", "IVA", "IMPORTE"]
        aligns  = ["L", "C", "R", "C", "R"]
    else:
        widths  = [97, 20, 30, 27]
        headers = ["DESCRIPCIÓN", "CANTIDAD", "PRECIO UNIT.", "IMPORTE"]
        aligns  = ["L", "C", "R", "R"]

    th_h   = 8
    LINE_H = 5

    def draw_header():
        yh = pdf.get_y()
        hx = _LX
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*_MUTED)
        for h, w, a in zip(headers, widths, aligns):
            pdf.set_xy(hx, yh)
            pad = "  " if a == "L" else ""
            pdf.cell(w, th_h, pad + h, border=0, align=a)
            hx += w
        # Línea inferior gruesa (2 px → 0.7 mm, color tinta)
        pdf.set_draw_color(*_INK)
        pdf.set_line_width(0.7)
        pdf.line(_LX, yh + th_h, _RX, yh + th_h)
        pdf.set_line_width(0.3)
        pdf.set_y(yh + th_h + 1)
        pdf.set_text_color(*_INK)

    draw_header()

    for item in items:
        raw_desc   = str(item.get("description", ""))
        parts      = raw_desc.split("\n", 1)
        title_txt  = parts[0].strip()
        detail_txt = parts[1].strip() if len(parts) > 1 else item.get("detalle", "")
        has_detail = bool(detail_txt)

        qty   = item.get("qty", 1)
        price = item.get("unit_price", 0)
        sub   = item.get("subtotal", 0)
        desc_w = widths[0] - 4

        # Calcular líneas reales para determinar la altura de la fila
        pdf.set_font("Helvetica", "B", 8)
        title_lines = _wrap_text(pdf, title_txt, desc_w)
        detail_lines: list[str] = []
        if has_detail:
            pdf.set_font("Helvetica", "I", 7)
            detail_lines = _wrap_text(pdf, detail_txt, desc_w)

        n_lines = len(title_lines) + len(detail_lines)
        row_h   = n_lines * LINE_H + 5

        if pdf.get_y() + row_h > pdf.h - 52:
            pdf.add_page()
            draw_header()

        y_row = pdf.get_y()

        # Descripción (título en negrita, wrap)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*_INK)
        ty = y_row + 2
        for ln_txt in title_lines:
            pdf.set_xy(_LX + 2, ty)
            pdf.cell(desc_w, LINE_H, ln_txt, ln=False)
            ty += LINE_H

        # Detalle en itálica debajo del título
        if has_detail:
            pdf.set_font("Helvetica", "I", 7)
            pdf.set_text_color(*_MUTED)
            for ln_txt in detail_lines:
                pdf.set_xy(_LX + 2, ty)
                pdf.cell(desc_w, LINE_H, ln_txt, ln=False)
                ty += LINE_H

        # Celdas numéricas (centradas verticalmente en la fila)
        vc = y_row + (row_h - LINE_H) / 2
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*_INK)
        cx = _LX + widths[0]

        pdf.set_xy(cx, vc); pdf.cell(widths[1], LINE_H, f"{qty:g}", align="C", ln=False)
        cx += widths[1]
        if show_prices:
            pdf.set_xy(cx, vc); pdf.cell(widths[2], LINE_H, "$ " + _ar(price), align="R", ln=False)
            cx += widths[2]
            if show_iva_col:
                iva_pct = item.get("iva_pct", 0)
                pdf.set_xy(cx, vc); pdf.cell(widths[3], LINE_H, f"{iva_pct:.0f}%", align="C", ln=False)
                cx += widths[3]
            pdf.set_xy(cx, vc); pdf.cell(widths[-1], LINE_H, "$ " + _ar(sub), align="R", ln=False)

        # Separador de fila
        pdf.set_draw_color(*_LINE)
        pdf.set_line_width(0.25)
        pdf.line(_LX, y_row + row_h, _RX, y_row + row_h)
        pdf.set_y(y_row + row_h)

    pdf.ln(5)


# ── Totales + Notas ───────────────────────────────────────────────────────────

def _draw_totals_and_notes(pdf, sub, iva_amount, otros, total, tax_pct,
                           observations=None, condicion_venta=None):
    y     = pdf.get_y()
    row_h = 8
    tot_h = row_h * 4
    box_h = max(tot_h, 32)

    # Caja de notas (accent-soft, sin borde)
    pdf.set_fill_color(*_ACCENT_SOFT)
    _rrect(pdf, _LX, y, _NOTES_W, box_h, style="F")

    cond_label = f"Condición de venta: {condicion_venta}" if condicion_venta else "Condición de venta: Contado"
    pdf.set_xy(_LX + 4, y + 5)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*_ACCENT_DARK)
    pdf.cell(_NOTES_W - 8, 5, cond_label, ln=True)
    if observations:
        pdf.set_x(_LX + 4)
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(_NOTES_W - 8, 5, "Notas:", ln=True)
        pdf.set_x(_LX + 4)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.multi_cell(_NOTES_W - 8, 4.5, str(observations)[:400])

    # Caja de totales (borde _LINE, redondeada)
    tx = _LX + _NOTES_W + _SUM_GAP
    rows_data = [
        ("Subtotal",              "$ " + _ar(sub),        False),
        (f"IVA {tax_pct:.0f}%",  "$ " + _ar(iva_amount), False),
        ("Otros tributos",        "$ " + _ar(otros),      False),
        ("Total",                 "$ " + _ar(total),       True),
    ]

    pdf.set_fill_color(*_WHITE)
    pdf.set_draw_color(*_LINE)
    pdf.set_line_width(0.3)
    _rrect(pdf, tx, y, _TOTALS_W, tot_h, style="DF")

    for i, (lbl, val, is_total) in enumerate(rows_data):
        ry = y + i * row_h
        if is_total:
            pdf.set_fill_color(*_INK)
            _rrect(pdf, tx, ry, _TOTALS_W, row_h, corners=_C_BOT, style="F")
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(*_WHITE)
        else:
            if i > 0:
                pdf.set_draw_color(*_LINE)
                pdf.set_line_width(0.25)
                pdf.line(tx + 2, ry, tx + _TOTALS_W - 2, ry)
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(*_INK)

        lw = _TOTALS_W * 0.52
        vy = ry + (row_h - 5) / 2
        pdf.set_xy(tx + 4, vy); pdf.cell(lw - 4, 5, lbl, ln=False)
        pdf.set_xy(tx + lw, vy); pdf.cell(_TOTALS_W - lw - 4, 5, val, align="R", ln=False)

    pdf.set_text_color(*_INK)
    pdf.set_y(y + box_h + 6)


# ── Marca no fiscal (borde punteado) ─────────────────────────────────────────

def _draw_no_fiscal_notice(pdf, text="DOCUMENTO NO VÁLIDO COMO FACTURA"):
    y = pdf.get_y() + 4
    h = 10
    pdf.set_draw_color(*_WARNING)
    pdf.set_line_width(0.5)
    _dashed_line(pdf, _LX, y,     _RX, y)
    _dashed_line(pdf, _LX, y + h, _RX, y + h)
    _dashed_line(pdf, _LX, y,     _LX, y + h)
    _dashed_line(pdf, _RX, y,     _RX, y + h)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_WARNING)
    pdf.set_xy(_LX, y + 2.5)
    pdf.cell(_CW, h - 5, text.upper(), align="C", ln=False)
    pdf.set_text_color(*_INK)
    pdf.set_y(y + h + 4)


# ── Footer CAE + QR ───────────────────────────────────────────────────────────

def _draw_factura_footer(pdf, factura, empresa):
    cae     = factura.get("cae") or ""
    cae_vto = factura.get("cae_vto") or ""
    if cae_vto and len(cae_vto) == 8:
        cae_vto = f"{cae_vto[6:8]}/{cae_vto[4:6]}/{cae_vto[0:4]}"

    fy = pdf.h - 44

    # Línea separadora
    pdf.set_draw_color(*_LINE)
    pdf.set_line_width(0.4)
    pdf.line(_LX, fy, _RX, fy)
    fy += 4

    # QR box (30×30 mm, redondeado)
    qr_sz = 30
    qr_x  = _RX - qr_sz
    qr_y  = fy

    pdf.set_fill_color(*_WHITE)
    pdf.set_draw_color(*_LINE)
    pdf.set_line_width(0.3)
    _rrect(pdf, qr_x, qr_y, qr_sz, qr_sz, r=2, style="DF")

    if _HAS_QR and cae and empresa.get("cuit"):
        try:
            _draw_qr(pdf, _afip_qr_url(factura, empresa["cuit"]),
                     qr_x + 2, qr_y + 2, qr_sz - 4)
        except Exception:
            pass
    else:
        pdf.set_font("Helvetica", "", 6.5)
        pdf.set_text_color(*_MUTED)
        pdf.set_xy(qr_x, qr_y + 8);  pdf.cell(qr_sz, 5, "QR fiscal", align="C")
        pdf.set_xy(qr_x, qr_y + 14); pdf.cell(qr_sz, 5, "ARCA / AFIP", align="C")

    # Datos CAE
    info_w = qr_x - _LX - 5
    cy     = fy + 2

    # Logo ARCA tipográfico
    _ARCA_DARK = (74, 74, 74)
    _ARCA_SUB  = (110, 110, 110)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*_ARCA_DARK)
    pdf.set_xy(_LX, cy)
    pdf.cell(40, 5, "ARCA", ln=False)
    cy += 5
    pdf.set_font("Helvetica", "", 5)
    pdf.set_text_color(*_ARCA_SUB)
    pdf.set_xy(_LX, cy)
    pdf.cell(60, 3.5, "AGENCIA DE RECAUDACI\xd3N Y CONTROL ADUANERO", ln=False)
    cy += 4.5
    pdf.set_text_color(*_INK)

    if cae:
        es_dev = os.environ.get("ENV", "") == "development"
        pdf.set_xy(_LX, cy)
        pdf.set_font("Helvetica", "B", 8); pdf.set_text_color(*_INK)
        pdf.cell(22, 5, "CAE/CAI:", ln=False)
        pdf.set_font("Helvetica", "", 8)
        cae_display = f"{cae}  [DEV - SIMULADO]" if es_dev else cae
        pdf.cell(info_w - 22, 5, cae_display, ln=True)
        pdf.set_xy(_LX, pdf.get_y())
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(40, 5, "Vencimiento CAE/CAI:", ln=False)
        pdf.set_font("Helvetica", "", 8)
        pdf.cell(info_w - 40, 5, cae_vto, ln=True)
    else:
        pdf.set_xy(_LX, cy)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(180, 0, 0)
        pdf.cell(info_w, 5, "Pendiente de autorización ARCA", ln=True)

    pdf.set_xy(_LX, pdf.get_y() + 1)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*_MUTED)
    pdf.cell(info_w, 5, "Moneda: Pesos argentinos · Tipo de cambio: no aplica", ln=False)

    pdf.set_font("Helvetica", "", 7)
    pdf.set_xy(_LX, pdf.h - 10)
    pdf.cell(_CW, 4, f"Pág. {pdf.page_no()}/{{nb}}", align="R")
    pdf.set_text_color(*_INK)


# ── Clases PDF ────────────────────────────────────────────────────────────────

class FacturaPDF(FPDF):
    def __init__(self, factura):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.factura = factura
        self._emp    = None
        self.set_margins(_LX, _LX, _LX)
        self.set_auto_page_break(auto=True, margin=46)
        self.alias_nb_pages()

    def header(self):
        f     = self.factura
        emp   = self._emp or _empresa()
        tipo  = f.get("tipo", 11)
        letra = _TIPO_LETRA.get(tipo, "C")
        cod   = _TIPO_COD.get(tipo, "011")
        titulo = _TIPO_NOMBRE_DOC.get(tipo, "Factura")
        pv    = str(f.get("punto_venta", 1)).zfill(4)
        num   = str(f.get("numero", 1)).zfill(8)
        fecha = _fmt_fecha(f.get("fecha", ""))
        info_fields = [
            ("Punto de venta:",    pv),
            ("Comprobante N\xb0:", f"{letra}-{pv}-{num}"),
            ("Fecha de emisión:",  fecha),
        ]
        self.set_y(_draw_header_block(self, letra, titulo, cod, info_fields, emp))

    def footer(self):
        _draw_factura_footer(self, self.factura, self._emp or _empresa())


class RemitoPDF(FPDF):
    def __init__(self, remito):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.remito = remito
        self._emp   = None
        self.set_margins(_LX, _LX, _LX)
        self.set_auto_page_break(auto=True, margin=22)

    def header(self):
        r   = self.remito
        emp = self._emp or _empresa()
        info_fields = [
            ("N° Remito:", r["number"]),
            ("Fecha:",     _fmt_fecha(r["date"]) or r["date"]),
        ]
        self.set_y(_draw_header_block(self, "R", "Remito", "", info_fields, emp))

    def footer(self):
        pass


class PresupuestoPDF(FPDF):
    def __init__(self, presupuesto):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.presupuesto = presupuesto
        self._emp        = None
        self.set_margins(_LX, _LX, _LX)
        self.set_auto_page_break(auto=True, margin=22)

    def header(self):
        p   = self.presupuesto
        emp = self._emp or _empresa()
        info_fields = [
            ("N° Presupuesto:", p["number"]),
            ("Fecha:",          _fmt_fecha(p["date"]) or p["date"]),
            ("Válido hasta:",   _fmt_fecha(p.get("valid_until","")) or p.get("valid_until","")),
        ]
        self.set_y(_draw_header_block(self, "P", "Presupuesto", "", info_fields, emp))

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*_MUTED)
        self.cell(0, 5,
            f"Presupuesto válido hasta: {_fmt_fecha(self.presupuesto.get('valid_until','')) or self.presupuesto.get('valid_until','')}",
            align="C")
        self.set_text_color(*_INK)


# ── Funciones públicas de generación ─────────────────────────────────────────

def generate_pdf(remito, output_dir=None):
    os.makedirs(output_dir or PDF_DIR, exist_ok=True)
    safe = remito["number"].replace("/", "-")
    filepath = os.path.join(output_dir or PDF_DIR,
                            f"remito_{safe}_{remito['date']}.pdf")
    emp = _empresa()
    pdf = RemitoPDF(remito)
    pdf._emp = emp
    pdf.add_page()

    client_fields = [
        ("Nombre",    remito.get("client_name", "")),
        ("CUIT/DNI",  remito.get("client_cuit", "")),
        ("Domicilio", remito.get("client_address", "")),
        ("Email",     remito.get("client_email", "")),
        ("Teléfono",  remito.get("client_phone", "")),
    ]
    _draw_emisor_cliente(pdf, emp, client_fields)
    _draw_items_table(pdf, remito["items"], show_prices=False)
    if remito.get("observations"):
        obs_w = _RX - _LX
        pdf.ln(3)
        y_obs = pdf.get_y()
        pdf.set_fill_color(*_ACCENT_SOFT)
        _rrect(pdf, _LX, y_obs, obs_w, 28, style="F")
        pdf.set_xy(_LX + 4, y_obs + 4)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*_ACCENT_DARK)
        pdf.cell(obs_w - 8, 5, "Observaciones:", ln=True)
        pdf.set_x(_LX + 4)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*_INK)
        pdf.multi_cell(obs_w - 8, 4.5, str(remito["observations"])[:400])
        pdf.ln(2)

    # Anclar aviso al pie (aviso 18mm + margen 22mm)
    target_y = pdf.h - 40
    if pdf.get_y() > target_y:
        pdf.add_page()
        target_y = pdf.h - 40
    pdf.set_y(target_y)
    _draw_no_fiscal_notice(pdf)
    pdf.output(filepath)
    return os.path.abspath(filepath)


def generate_pdf_presupuesto(presupuesto, output_dir=None):
    os.makedirs(output_dir or PRESUPUESTOS_PDF_DIR, exist_ok=True)
    safe = presupuesto["number"].replace("/", "-")
    filepath = os.path.join(output_dir or PRESUPUESTOS_PDF_DIR,
                            f"presupuesto_{safe}_{presupuesto['date']}.pdf")
    emp = _empresa()
    pdf = PresupuestoPDF(presupuesto)
    pdf._emp = emp
    pdf.add_page()

    client_fields = [
        ("Nombre",    presupuesto.get("client_name", "")),
        ("CUIT/DNI",  presupuesto.get("client_cuit", "")),
        ("Domicilio", presupuesto.get("client_address", "")),
        ("Email",     presupuesto.get("client_email", "")),
        ("Teléfono",  presupuesto.get("client_phone", "")),
    ]
    _draw_emisor_cliente(pdf, emp, client_fields)
    _draw_items_table(pdf, presupuesto["items"], show_iva_col=False)

    sub = presupuesto.get("subtotal", 0)
    tax = presupuesto.get("tax_amount", 0)
    tot = presupuesto.get("total", 0)
    pct = round(presupuesto.get("tax_rate", 0) * 100)

    # Anclar totales + aviso al pie (totales 38mm + aviso 18mm + footer 14mm + margen)
    _BOTTOM_BLOCK_H = 80
    target_y = pdf.h - _BOTTOM_BLOCK_H
    if pdf.get_y() > target_y:
        pdf.add_page()
        target_y = pdf.h - _BOTTOM_BLOCK_H
    pdf.set_y(target_y)

    _draw_totals_and_notes(pdf, sub, tax, 0, tot, pct,
                           presupuesto.get("observations", ""))
    _draw_no_fiscal_notice(
        pdf, "Para presupuesto/proforma: Documento no válido como factura")
    pdf.output(filepath)
    return os.path.abspath(filepath)


def generate_pdf_factura(factura, output_dir=None):
    os.makedirs(output_dir or FACTURAS_PDF_DIR, exist_ok=True)
    pv  = str(factura["punto_venta"]).zfill(4)
    num = str(factura["numero"]).zfill(8)
    filepath = os.path.join(output_dir or FACTURAS_PDF_DIR,
                            f"factura_{pv}_{num}.pdf")

    emp = _empresa()
    pdf = FacturaPDF(factura)
    pdf._emp = emp
    pdf.add_page()

    iva_rec = _IVA_LABELS.get(factura.get("cliente_iva_cond") or 0, "Consumidor Final")
    client_fields = [
        ("Nombre",          factura.get("cliente_razon", "")),
        ("CUIT/DNI",        factura.get("cliente_cuit", "")),
        ("Condición IVA",   iva_rec),
        ("Domicilio",       factura.get("cliente_domicilio", "")),
        ("Condición venta", factura.get("condicion_venta", "")),
    ]
    _draw_emisor_cliente(pdf, emp, client_fields)

    concepto = factura.get("concepto", 1)
    if concepto in (2, 3):
        desde = _fmt_fecha(factura.get("fch_serv_desde", ""))
        hasta = _fmt_fecha(factura.get("fch_serv_hasta", ""))
        vto   = _fmt_fecha(factura.get("fch_vto_pago", ""))
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*_MUTED)
        pdf.set_x(_LX)
        txt = f"Per. facturado: {desde} al {hasta}"
        if vto:
            txt += f"  ·  Vto. pago: {vto}"
        pdf.cell(_CW, 5, txt, ln=True)
        pdf.set_text_color(*_INK)
        pdf.ln(2)

    tipo     = factura.get("tipo", 11)
    show_iva = tipo not in _TIPOS_C
    _draw_items_table(pdf, factura["items"], show_iva_col=show_iva)

    sub = factura.get("subtotal", 0)
    iva = factura.get("iva_amount", 0)
    tot = factura.get("total", 0)
    if sub > 0 and iva > 0:
        tax_pct = round(iva / sub * 100)
    elif tipo not in _TIPOS_C:
        tax_pct = 21
    else:
        tax_pct = 0

    # Anclar totales al pie: siempre arriba del footer, sin importar cuántos ítems haya
    _TOTALS_SECTION_H = 38   # box 32mm + gap 6mm
    target_y = pdf.h - 44 - _TOTALS_SECTION_H
    if pdf.get_y() > target_y:
        pdf.add_page()
        target_y = pdf.h - 44 - _TOTALS_SECTION_H
    pdf.set_y(target_y)

    _draw_totals_and_notes(pdf, sub, iva, 0, tot, tax_pct,
                           factura.get("observaciones", ""),
                           condicion_venta=factura.get("condicion_venta", ""))
    pdf.output(filepath)
    return os.path.abspath(filepath)


# ── Recibo de pago ────────────────────────────────────────────────────────────

_MEDIOS_LABEL = {
    "efectivo":      "Efectivo",
    "transferencia": "Transferencia",
    "mercadopago":   "MercadoPago",
    "cuenta_dni":    "Cuenta DNI",
    "billetera":     "Billetera Virtual",
    "cheque":        "Cheque",
    "tarjeta":       "Tarjeta",
}


def generate_pdf_recibo(factura: dict, cobros: list[dict]) -> bytes:
    """
    Genera un recibo de pago A4 en memoria y devuelve los bytes del PDF.

    factura – dict con los campos de la factura (tipo, punto_venta, numero,
              fecha, cliente_razon, cliente_cuit, total, …)
    cobros  – lista de dicts de caja_movimientos (fecha, medio_pago,
              referencia, monto)
    """
    emp = _empresa()
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    lx, rx, cw = _LX, _RX, _CW

    # Calcular ref_line y fecha antes de dibujar el header
    es_venta  = factura.get("_es_venta", False)
    fecha_fac = _fmt_fecha((factura.get("fecha") or "")[:10])
    if es_venta:
        ref_line     = f"Venta N\xb0 {factura.get('_venta_numero', factura.get('numero', ''))}"
    else:
        pv           = str(factura.get("punto_venta", 0)).zfill(4)
        num          = str(factura.get("numero", 0)).zfill(8)
        tipo_nombre  = _TIPO_NOMBRE_DOC.get(int(factura.get("tipo", 11)), "Comprobante")
        ref_line     = f"{tipo_nombre} {pv}-{num}"

    info_fields = [
        ("Comprobante:", ref_line),
        ("Emisi\xf3n:",  fecha_fac),
    ]

    # ── Encabezado estilo factura ─────────────────────────────────────────────
    sep_y = _draw_header_block(pdf, "R", "Recibo", "", info_fields, emp)

    # ── Recibido de ───────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_text_color(*_ACCENT)
    pdf.set_xy(lx, sep_y)
    pdf.cell(cw, 5, "RECIBIDO DE", ln=True)

    pdf.set_draw_color(*_LINE)
    pdf.set_line_width(0.2)
    pdf.line(lx, sep_y + 5, rx, sep_y + 5)

    fy = sep_y + 7
    row_h = 6
    lbl_w = 42

    def _field(lbl, val):
        nonlocal fy
        if not val:
            return
        pdf.set_xy(lx, fy)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*_MUTED)
        pdf.cell(lbl_w, row_h, lbl)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(*_INK)
        val_w = cw - lbl_w
        lines = pdf.multi_cell(val_w, row_h, str(val), split_only=True)
        n = max(1, len(lines))
        pdf.set_xy(lx + lbl_w, fy)
        pdf.multi_cell(val_w, row_h, str(val), align="L", new_x="LEFT", new_y="NEXT")
        fy += n * row_h

    _field("Razón social", factura.get("cliente_razon") or "Consumidor Final")
    _field("CUIT / DNI",   factura.get("cliente_cuit") or "")
    _field("Domicilio",    factura.get("cliente_domicilio") or "")

    fy += 4

    # ── Monto destacado ───────────────────────────────────────────────────────
    total_cobrado = sum(float(c.get("monto", 0)) for c in cobros)
    pdf.set_fill_color(*_ACCENT_SOFT)
    _rrect(pdf, lx, fy, cw, 16, r=3, style="F")

    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*_ACCENT_DARK)
    pdf.set_xy(lx + 4, fy + 3)
    pdf.cell(60, 6, "LA SUMA DE:")

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(*_ACCENT)
    pdf.set_xy(lx + 60, fy + 2)
    pdf.cell(cw - 64, 12, f"$ {_ar(total_cobrado)}", align="R")

    fy += 22

    # ── En concepto de ────────────────────────────────────────────────────────
    parcial = total_cobrado < float(factura.get("total", 0)) - 0.005
    concepto = "Pago parcial de" if parcial else "Cancelación de"
    concepto += f" {ref_line.replace('De: ', '')} del {fecha_fac}"

    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*_MUTED)
    pdf.set_xy(lx, fy)
    pdf.cell(25, 5, "En concepto de:")
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_INK)
    pdf.set_xy(lx + 25, fy)
    pdf.multi_cell(cw - 25, 5, concepto, align="L")
    fy = pdf.get_y() + 6

    # ── Detalle de cobros ─────────────────────────────────────────────────────
    if cobros:
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_text_color(*_ACCENT)
        pdf.set_xy(lx, fy)
        pdf.cell(cw, 5, "DETALLE DE PAGOS")
        fy += 5

        pdf.set_draw_color(*_LINE)
        pdf.set_line_width(0.2)
        pdf.line(lx, fy, rx, fy)
        fy += 2

        # Encabezado
        cols_w = [38, 44, 50, 42]
        headers = ["FECHA", "MEDIO", "REFERENCIA", "MONTO"]
        hx = lx
        pdf.set_font("Helvetica", "B", 6.5)
        pdf.set_text_color(*_MUTED)
        for h_txt, w in zip(headers, cols_w):
            pdf.set_xy(hx, fy)
            align = "R" if h_txt == "MONTO" else "L"
            pdf.cell(w, 5, h_txt, align=align)
            hx += w
        fy += 5
        pdf.line(lx, fy, rx, fy)
        fy += 1

        # Filas
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*_INK)
        for c in cobros:
            hx = lx
            vals = [
                _fmt_fecha((c.get("fecha") or "")[:10]),
                _MEDIOS_LABEL.get(c.get("medio_pago", ""), c.get("medio_pago", "") or "-"),
                c.get("referencia") or "-",
                f"$ {_ar(float(c.get('monto', 0)))}",
            ]
            aligns = ["L", "L", "L", "R"]
            for val, w, al in zip(vals, cols_w, aligns):
                pdf.set_xy(hx, fy)
                pdf.cell(w, 6, str(val), align=al)
                hx += w
            fy += 6

        pdf.set_draw_color(*_LINE)
        pdf.line(lx, fy, rx, fy)
        fy += 3

        # Total
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*_INK)
        pdf.set_xy(lx, fy)
        pdf.cell(cw - cols_w[-1], 7, "Total recibido:", align="R")
        pdf.set_text_color(*_ACCENT)
        pdf.cell(cols_w[-1], 7, f"$ {_ar(total_cobrado)}", align="R")
        fy += 14

    # ── Firma y sello ─────────────────────────────────────────────────────────
    firma_y = max(fy, pdf.h - 55)
    pdf.set_draw_color(*_LINE)
    pdf.set_line_width(0.3)
    pdf.line(lx, firma_y + 20, lx + 70, firma_y + 20)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*_MUTED)
    pdf.set_xy(lx, firma_y + 21)
    pdf.cell(70, 5, "Firma y sello", align="C")

    # Pie (desactivar auto_break para poder posicionarlo en el borde inferior)
    pdf.set_auto_page_break(False)
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(*_MUTED)
    pdf.set_xy(lx, pdf.h - 14)
    pdf.cell(cw, 5,
             f"{emp.get('nombre', '')}  ·  CUIT: {emp.get('cuit', '')}  ·  "
             f"Documento no válido como factura", align="C")

    return bytes(pdf.output())


# ── Resumen de cuenta corriente ──────────────────────────────────────────────

class ResumenCCPDF(FPDF):
    def __init__(self, cliente, periodo):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.cliente = cliente
        self.periodo = periodo
        self._emp    = None
        self.set_margins(_LX, _LX, _LX)
        self.set_auto_page_break(auto=True, margin=22)

    def header(self):
        emp = self._emp or _empresa()
        info_fields = [
            ("Período desde:", _fmt_fecha(self.periodo["desde"])),
            ("Período hasta:",  _fmt_fecha(self.periodo["hasta"])),
            ("Emitido:",        _fmt_fecha(self.periodo["emitido"])),
        ]
        # `_draw_header_block` aplica .title() al título: usar dos palabras que
        # queden bien capitalizadas ("Resumen de cuenta" daría "Resumen De…").
        self.set_y(_draw_header_block(
            self, "CC", "Cuenta corriente", "", info_fields, emp))

    def footer(self):
        pass


def _draw_movimientos_cc(pdf, periodo):
    """Tabla de movimientos del resumen: mismo lenguaje visual que
    `_draw_items_table` pero con las columnas de una cuenta corriente
    (débito/crédito/saldo acumulado) en vez de las de un comprobante."""
    widths  = [26, 78, 24, 24, 22]
    headers = ["FECHA", "CONCEPTO", "DEBE", "HABER", "SALDO"]
    aligns  = ["L", "L", "R", "R", "R"]
    th_h, LINE_H = 8, 5

    def draw_header():
        yh = pdf.get_y()
        hx = _LX
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*_MUTED)
        for h, w, a in zip(headers, widths, aligns):
            pdf.set_xy(hx, yh)
            pad = "  " if a == "L" else ""
            pdf.cell(w, th_h, pad + h, border=0, align=a)
            hx += w
        pdf.set_draw_color(*_INK)
        pdf.set_line_width(0.7)
        pdf.line(_LX, yh + th_h, _RX, yh + th_h)
        pdf.set_line_width(0.3)
        pdf.set_y(yh + th_h + 1)
        pdf.set_text_color(*_INK)

    def draw_row(fecha, concepto, debe, haber, saldo, bold=False, muted=False):
        row_h = LINE_H + 3
        if pdf.get_y() + row_h > pdf.h - 60:
            pdf.add_page()
            draw_header()
        y_row = pdf.get_y()
        pdf.set_font("Helvetica", "B" if bold else "", 8)
        pdf.set_text_color(*(_MUTED if muted else _INK))
        cx = _LX
        for val, w, a in zip([fecha, concepto, debe, haber, saldo], widths, aligns):
            pdf.set_xy(cx + (2 if a == "L" else 0), y_row + 1.5)
            txt = val
            if a == "L":
                txt = "".join(_wrap_text(pdf, str(val), w - 4)[:1])
                if pdf.get_string_width(str(val)) > w - 4:
                    txt = txt[: max(0, len(txt) - 1)] + "…"
            pdf.cell(w - (2 if a == "L" else 0), LINE_H, txt, align=a, ln=False)
            cx += w
        pdf.set_draw_color(*_LINE)
        pdf.set_line_width(0.25)
        pdf.line(_LX, y_row + row_h, _RX, y_row + row_h)
        pdf.set_y(y_row + row_h)

    draw_header()
    saldo = float(periodo["saldo_anterior"])
    draw_row("", "Saldo anterior", "", "", "$ " + _ar(saldo), bold=True, muted=True)

    for m in periodo["movimientos"]:
        monto = float(m["monto"])
        es_debito = m["tipo"] == "debito"
        saldo += monto if es_debito else -monto
        concepto = m["concepto"] or ""
        if m.get("referencia"):
            concepto = f"{concepto} ({m['referencia']})"
        draw_row(
            _fmt_fecha(m["fecha"]) or m["fecha"],
            concepto,
            "$ " + _ar(monto) if es_debito else "",
            "" if es_debito else "$ " + _ar(monto),
            "$ " + _ar(saldo),
        )

    if not periodo["movimientos"]:
        draw_row("", "Sin movimientos en el período", "", "", "", muted=True)

    draw_row(
        "", "Totales del período",
        "$ " + _ar(periodo["total_debitos"]),
        "$ " + _ar(periodo["total_creditos"]),
        "$ " + _ar(periodo["saldo_final"]),
        bold=True,
    )
    pdf.ln(4)


def generate_pdf_resumen_cc(cliente: dict, periodo: dict, output_dir=None) -> str:
    """Resumen de cuenta corriente de un cliente para un período.

    `periodo` es lo que devuelve `libracore.db.cuenta_corriente
    .get_cc_movimientos_periodo()` más la clave `emitido` (fecha de emisión).
    No es un comprobante fiscal: lleva la misma leyenda que remitos y
    presupuestos.
    """
    out_dir = output_dir or RESUMENES_CC_PDF_DIR
    os.makedirs(out_dir, exist_ok=True)
    periodo = {**periodo, "emitido": periodo.get("emitido") or periodo["hasta"]}
    filepath = os.path.join(
        out_dir, f"resumen_cc_{cliente['id']}_{periodo['desde']}_{periodo['hasta']}.pdf")

    emp = _empresa()
    pdf = ResumenCCPDF(cliente, periodo)
    pdf._emp = emp
    pdf.add_page()

    client_fields = [
        ("Nombre",    cliente.get("name", "")),
        ("CUIT/DNI",  cliente.get("cuit_dni", "")),
        ("Domicilio", cliente.get("address", "")),
        ("Email",     cliente.get("email", "")),
        ("Teléfono",  cliente.get("phone", "")),
    ]
    _draw_emisor_cliente(pdf, emp, client_fields)
    _draw_movimientos_cc(pdf, periodo)

    # Recuadro de saldo final
    saldo = float(periodo["saldo_final"])
    box_h = 16
    if pdf.get_y() + box_h > pdf.h - 46:
        pdf.add_page()
    y_box = pdf.get_y()
    box_w = _TOTALS_W
    pdf.set_fill_color(*_ACCENT_SOFT)
    _rrect(pdf, _RX - box_w, y_box, box_w, box_h, style="F")
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_ACCENT_DARK)
    pdf.set_xy(_RX - box_w + 4, y_box + 2)
    pdf.cell(box_w - 8, 5, "SALDO AL " + (_fmt_fecha(periodo["hasta"]) or ""), ln=False)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*_INK)
    pdf.set_xy(_RX - box_w + 4, y_box + 8)
    pdf.cell(box_w - 8, 6, "$ " + _ar(saldo), align="R", ln=False)
    pdf.set_y(y_box + box_h)

    target_y = pdf.h - 40
    if pdf.get_y() > target_y:
        pdf.add_page()
        target_y = pdf.h - 40
    pdf.set_y(target_y)
    _draw_no_fiscal_notice(pdf, "Resumen informativo: documento no válido como factura")
    pdf.output(filepath)
    return os.path.abspath(filepath)
