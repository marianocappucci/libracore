import os
import json
import base64
import unicodedata
from datetime import date, datetime, timezone
from fpdf import FPDF  # fpdf2 >= 2.8
from fpdf.enums import RenderStyle as _RS, Corner as _Cor
from . import config_manager


def fecha_de_documento(valor) -> datetime | None:
    """La fecha propia de un comprobante, como `datetime` con zona horaria.

    Acepta lo que traen los dicts de los generadores: un `datetime` o un
    `date` ya armado, el ISO `AAAA-MM-DD` con hora opcional que sale de la
    base, y el `AAAAMMDD` de ARCA (el vencimiento de CAE, por ejemplo). Una
    fecha sin zona se lee como **UTC**, no como hora local: si no, el mismo
    comprobante daría un PDF distinto según el huso del servidor.

    Devuelve `None` cuando no hay nada interpretable.
    """
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    if isinstance(valor, date):
        return datetime(valor.year, valor.month, valor.day, tzinfo=timezone.utc)
    texto = str(valor or "").strip()
    # Recortar de a pedazos aguanta lo que viene con cola ("... 14:30 hs",
    # una fecha completa donde sólo se esperaba el día), quedándose siempre con
    # la mayor precisión que sí se entienda.
    for candidato in (texto, texto[:19], texto[:16], texto[:10]):
        try:
            return fecha_de_documento(datetime.fromisoformat(candidato))
        except ValueError:
            continue
    return None


class _TextoSeguroPDF(FPDF):
    """Base de todos los PDF de LibraCore: **ningún carácter puede tumbar la
    generación**.

    Los 14 tipos estándar de PostScript (Helvetica y compañía, los que usamos)
    no son Unicode. fpdf2 los codifica en **latin-1** por defecto y, ante un
    carácter que no entra, **levanta `FPDFUnicodeEncodingException`**: el
    endpoint devuelve 500 y el comprobante no se puede descargar. Es un fallo
    de datos, no de código, y aparece con caracteres que cualquiera tipea sin
    pensarlo — guión largo, comillas tipográficas, puntos suspensivos.

    > Pasó de verdad: en `libradesk-dev` el nombre de empresa era
    > `"Compulibra — Soporte IT"`, de ahí salían las iniciales `"C—"` del
    > recuadro del encabezado, y **todo** PDF de presupuesto daba 500
    > (2026-08-03).

    Se arregla en dos capas, y las dos hacen falta:

    1. **`core_fonts_encoding = "cp1252"`**. El PDF ya declara
       `/Encoding /WinAnsiEncoding` para las fuentes core —lo emite fpdf2 en
       `output.py`— y WinAnsi *es* cp1252. O sea que latin-1 era la
       codificación equivocada para lo que el archivo ya declaraba: con cp1252
       el guión largo, las comillas curvas, los puntos suspensivos y el € se
       **dibujan bien**, no se reemplazan por nada.
    2. **`normalize_text` que translitera en vez de romper.** cp1252 sigue
       siendo un byte por carácter, así que algo afuera siempre puede llegar
       (una ñ vietnamita pegada de un mail, un emoji). Eso se degrada al ASCII
       más parecido, y lo que no tenga equivalente cae en `?`. Un PDF con un
       `?` es un problema menor; un 500 deja al usuario sin comprobante.
    """

    # Reemplazos donde `unicodedata` no ayuda: no son variantes acentuadas de
    # una letra, así que la descomposición NFKD los deja igual.
    _EQUIVALENTES = {
        "€": "EUR",  # €  (sí entra en cp1252, esto es para el resto)
        "™": "(TM)",
        "≤": "<=",
        "≥": ">=",
        "≠": "!=",
        "≈": "~",
        "×": "x",
        "→": "->",
        "←": "<-",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.core_fonts_encoding = "cp1252"
        # El marco del papel, acá y no en cada documento: `_draw_header_block`,
        # `_draw_emisor_cliente`, las reglas de sección y el pie dibujan todos
        # entre `_LX` y `_RX` con coordenadas absolutas, mientras que el cuerpo
        # se escribe con el flujo de fpdf2 (`cell` + `ln`), que arranca en el
        # margen del documento. Si el documento no fija el suyo queda el de
        # fpdf2 —10 mm— y el cuerpo entero sale 8 mm a la izquierda del marco
        # que su propia cabecera acaba de dibujar.
        #
        # > Pasó de verdad, en la orden de trabajo y en los comprobantes de
        # > recepción/entrega de LibraDesk: las dos clases heredan esta base y
        # > ninguna llamaba a `set_margins`. Sus hermanas —factura, remito,
        # > presupuesto, resumen de cuenta, informe— lo llaman todas, con estos
        # > mismos tres valores. O sea que la convención ya existía y lo único
        # > que la sostenía era que cada documento se acordara de repetirla.
        #
        # Un documento con otra geometría la pisa después de este `super()`,
        # que es lo que hace `TicketPDF` con sus 2 mm de papel térmico.
        self.set_margins(_LX, _LX, _LX)

    def fijar_fecha_documento(self, valor) -> None:
        """Sella el `/CreationDate` con la fecha **del comprobante**, no con la
        del momento en que se lo imprime.

        fpdf2 pone `datetime.now()` al construir el objeto y lo escribe con
        resolución de segundo. Con eso, reimprimir el mismo comprobante
        devuelve bytes distintos si las dos impresiones caen en segundos
        distintos: mismo largo, un dígito de diferencia, nada que se vea en el
        papel — pero rompe todo lo que compara el archivo, y hace que "el
        comprobante es el mismo" no se pueda afirmar sobre el PDF.

        > Pasó de verdad: el test de reimpresión de tickets de VentaLibra
        > pasaba sólo cuando las dos requests entraban en el mismo segundo. En
        > la pata de PostgreSQL del CI, más lenta, la ventana se agrandó y
        > falló (2026-08-12). Nunca había probado la reimpresión determinista:
        > pasaba por suerte de timing.

        Un comprobante no se crea cuando se imprime, se creó cuando se emitió.
        Sellarlo con su propia fecha es más fiel *y* lo vuelve reproducible.

        Si la fecha no se puede interpretar se deja la de fpdf2 (el momento
        actual): sin fecha del documento no hay nada determinista que poner.
        """
        fecha = fecha_de_documento(valor)
        if fecha is not None:
            self.set_creation_date(fecha)

    def normalize_text(self, text: str) -> str:
        try:
            return super().normalize_text(text)
        except Exception:
            # `except Exception` a propósito: la excepción de fpdf2 cambió de
            # nombre y de módulo entre versiones, y este método no puede
            # fallar por perseguir un import.
            return super().normalize_text(self._a_cp1252(text))

    @classmethod
    def _a_cp1252(cls, text: str) -> str:
        salida = []
        for ch in text:
            try:
                ch.encode("cp1252")
                salida.append(ch)
                continue
            except UnicodeEncodeError:
                pass
            reemplazo = cls._EQUIVALENTES.get(ch)
            if reemplazo is None:
                # NFKD parte "ā" en "a" + diacrítico; nos quedamos con lo que
                # sea ASCII imprimible.
                plano = "".join(
                    c for c in unicodedata.normalize("NFKD", ch)
                    if not unicodedata.combining(c)
                )
                try:
                    plano.encode("cp1252")
                    reemplazo = plano
                except UnicodeEncodeError:
                    reemplazo = ""
            salida.append(reemplazo or "?")
        return "".join(salida)


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


def _recortar(pdf, txt: str, cell_w: float) -> str:
    """El texto que entra en una celda de ancho `cell_w`, con elipsis si sobra.

    `cell` **no recorta ni envuelve**: un texto más ancho que su celda se dibuja
    igual, encima de la columna de al lado y, si no hay nada al lado, afuera del
    papel. Va donde el valor lo escribe el usuario y la maqueta no puede crecer
    a lo alto: una condición de venta, el medio de pago de un cobro.

    `cell` deja 1 mm de aire a cada lado, así que se mide contra `cell_w - 2`.
    """
    ancho = cell_w - 2
    if pdf.get_string_width(txt) <= ancho:
        return txt
    elipsis = pdf.get_string_width("…")
    recorte = txt
    while recorte and pdf.get_string_width(recorte) + elipsis > ancho:
        recorte = recorte[:-1]
    return recorte.rstrip() + "…"


def _partir_palabra(pdf, word: str, max_w: float) -> tuple[str, str]:
    """`word` cortada en el último carácter que entra en `max_w`.

    Siempre corta **al menos un carácter**, aunque no entre: devolver la palabra
    entera dejaría al llamador en un bucle infinito, y una `w` sola más ancha
    que el renglón sólo pasa con un ancho absurdo.
    """
    corte = 1
    while corte < len(word) and pdf.get_string_width(word[:corte + 1]) <= max_w:
        corte += 1
    return word[:corte], word[corte:]


def _wrap_text(pdf, txt: str, max_w: float) -> list[str]:
    """Divide txt en líneas que caben en max_w con la fuente activa.

    Corta por palabra y, cuando una palabra sola es más ancha que el renglón,
    **por carácter**. Sin ese segundo corte la línea salía tal cual y `cell` la
    dibujaba entera: un serial pegado, una URL o un texto sin espacios se iban
    del papel, no sólo del margen. Medido en la orden de trabajo de LibraDesk:
    536 mm de borde derecho en una hoja de 210.
    """
    lines, cur = [], ""
    for word in txt.split():
        candidate = (cur + " " + word).strip()
        if pdf.get_string_width(candidate) <= max_w:
            cur = candidate
            continue
        if cur:
            lines.append(cur)
            cur = ""
        while pdf.get_string_width(word) > max_w:
            trozo, word = _partir_palabra(pdf, word, max_w)
            lines.append(trozo)
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
_WARNING_SOFT= (250, 235, 227)   # fondo del mismo tono, para franjas

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
_LETTER_RH = 15
# Cuanto crece la fila de la letra cuando el titulo necesita dos renglones, y
# cuanto separa a los dos. Es el mismo numero a proposito: la segunda linea
# ocupa exactamente lo que la fila creció, asi que la caja no queda holgada.
_TITULO_LH = 4.4
_META_RH   = 4.6
_CR        = 3.5   # border-radius

# Cards
# Alto de renglon dentro de las tarjetas Emisor/Cliente. Bajado de 6 a 4.8 el
# 2026-08-03: las dos tarjetas ocupaban 65 mm de la primera carilla — mas que
# el membrete — y eran el bloque que menos items dejaba entrar.
_CARD_ROW_H = 4.8
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
    return f"{s[8:10]}-{s[5:7]}-{s[0:4]}"


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

    # ── El titulo puede necesitar dos lineas ──────────────────────────────
    #
    # Se dibujaba con un `cell()`, que NO envuelve: un titulo mas ancho que su
    # celda se sale del recuadro y queda pisando el borde. Con los titulos
    # cortos de facturacion (Factura, Remito, Presupuesto: 10-18 mm de 38) no
    # se notaba; lo destapo LibraDesk con "Comprobante de recepcion de equipo",
    # que mide 55,8 mm y se pasaba 17,8.
    #
    # La fila de la letra CRECE solo si hace falta, asi que un titulo de una
    # linea produce exactamente el mismo PDF que antes — que es lo que permite
    # publicar esto sin revisar los comprobantes de los otros cinco productos.
    titulo_txt = (titulo or "").title()
    _ancho_titulo = _RIGHT_W - _LETTER_W - 6
    pdf.set_font("Helvetica", "B", 8.5)
    # Dos lineas como maximo: una tercera no entra sin empujar las filas meta,
    # y un titulo tan largo es un problema de redaccion, no de maqueta.
    titulo_lineas = _wrap_text(pdf, titulo_txt, _ancho_titulo)[:2] or [""]
    # Con `codigo` la segunda linea chocaria contra el, asi que ahi la fila
    # crece igual y el codigo baja con ella.
    letter_rh = _LETTER_RH + (_TITULO_LH if len(titulo_lineas) > 1 else 0)

    # Calcular altura del voucher box primero (para que el logo sea proporcional)
    meta_h = len(info_fields) * _META_RH + 4
    vh     = letter_rh + meta_h

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
        # Iniciales de las dos primeras palabras QUE EMPIECEN CON LETRA O
        # DIGITO. Sin el filtro, "Compulibra — Soporte IT" da "C—", porque la
        # raya cuenta como palabra y el cuadrito del logo termina mostrando un
        # guion donde va una inicial. Lo mismo con "Perez & Asoc." -> "P&".
        _palabras = [w for w in empresa.get("nombre", "").split() if w[:1].isalnum()]
        ini = "".join(w[0].upper() for w in _palabras[:2]) or "?"
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
    _rrect(pdf, vx, vy, _LETTER_W, letter_rh, corners=_C_TL, style="F")

    # Letra
    pdf.set_font("Helvetica", "B", 26)
    pdf.set_text_color(*_WHITE)
    pdf.set_xy(vx, vy + 1)
    pdf.cell(_LETTER_W, letter_rh - 2, letra, align="C", ln=False)

    # Tipo + código en fila de letra (lado derecho)
    tx2 = vx + _LETTER_W + 3
    tw2 = vw - _LETTER_W - 6
    pdf.set_text_color(*_INK)
    pdf.set_font("Helvetica", "B", 8.5)
    # Con una sola linea arranca en vy+4, igual que siempre; con dos sube para
    # que el par quede centrado en la fila y no pegado al borde de arriba.
    ty = vy + 4 if len(titulo_lineas) == 1 else vy + 2.5
    for i, linea in enumerate(titulo_lineas):
        pdf.set_xy(tx2, ty + i * _TITULO_LH)
        pdf.cell(tw2, 6, linea, ln=False)
    if codigo:
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*_MUTED)
        pdf.set_xy(tx2, vy + 11 + (letter_rh - _LETTER_RH))
        pdf.cell(tw2, 5, f"Código {codigo} · Original", ln=False)

    # Borde exterior redondeado (tinta oscura, sobre todo lo anterior)
    pdf.set_draw_color(*_INK)
    pdf.set_line_width(0.5)
    _rrect(pdf, vx, vy, vw, vh, style="D")

    # Separadores internos
    pdf.set_draw_color(*_LINE)
    pdf.set_line_width(0.3)
    # Línea horizontal (fila letra / filas meta)
    pdf.line(vx + 1, vy + letter_rh, vx + vw - 1, vy + letter_rh)
    # Línea vertical en fila letra (celda letra | datos tipo)
    pdf.line(vx + _LETTER_W, vy + 1, vx + _LETTER_W, vy + letter_rh - 1)

    # Filas meta (PV / N° / Fecha)
    for i, (lbl, val) in enumerate(info_fields):
        ry = vy + letter_rh + 2.5 + i * _META_RH
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
    sep_y = vy + vh + 3.5
    pdf.set_draw_color(*_INK)
    pdf.set_line_width(0.7)
    pdf.line(_LX, sep_y, _RX, sep_y)
    pdf.set_text_color(*_INK)
    return sep_y + 4


# ── Cards EMISOR / CLIENTE ────────────────────────────────────────────────────

def _card_field_lines(pdf, val_w, val_str, row_h=_CARD_ROW_H):
    """Número exacto de líneas que ocupará val_str en multi_cell."""
    if not val_str:
        return 0
    pdf.set_font("Helvetica", "B", 7.5)
    lines = pdf.multi_cell(val_w, row_h, val_str, split_only=True)
    return max(1, len(lines))


def _measure_card_h(pdf, w, fields, row_h=_CARD_ROW_H):
    lbl_w = w * 0.42
    val_w = w - lbl_w - 8
    total = sum(_card_field_lines(pdf, val_w, str(v), row_h) for _, v in fields if v)
    return 10 + total * row_h + 3


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
    pdf.line(x + 2, y + 8.5, x + w - 2, y + 8.5)

    fy    = y + 10
    row_h = _CARD_ROW_H
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
        # Alto de UN renglon, no el del campo entero: centrar sobre el alto
        # completo dejaba la etiqueta al lado de la SEGUNDA linea del valor y
        # la primera sin rotular ("Domicilio" terminaba rotulando la ciudad).
        pdf.cell(lbl_w, row_h, lbl, align="L", ln=False)

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
    pdf.set_y(y + box_h + 4)


# ── Tabla de ítems ────────────────────────────────────────────────────────────

def _draw_items_table(pdf, items, show_iva_col=False, show_prices=True,
                      iva_incluido=False, tax_rate=0):
    """Tabla de items del comprobante.

    `iva_incluido=True` **suma el IVA de cada linea al precio unitario y al
    importe que se imprimen**. Es el comprobante de quien no discrimina
    (Consumidor Final, Monotributista): no computa credito fiscal, asi que el
    desglose no le sirve y ver un neto le hace parecer que el precio es otro.
    La alicuota de cada linea sale de `iva_pct`; `tax_rate` (fraccion) es el
    default de las lineas que no la traen, o sea los comprobantes guardados
    antes de que existiera la alicuota por item.

    Los importes del diccionario **no se tocan**: el que manda es el que
    calculo el backend, y lo unico que cambia es como se presentan.
    """
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
        if iva_incluido:
            pct = item.get("iva_pct")
            factor = 1 + (tax_rate if pct is None else pct / 100)
            price = price * factor
            sub = sub * factor
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
                           observations=None, condicion_venta=None,
                           discriminar=True):
    """Caja de notas + caja de totales.

    `discriminar=False` colapsa el desglose a una linea de aviso y el total.
    El **alto de la caja no cambia**: las filas se reparten el mismo espacio,
    asi que el comprobante que discrimina —los cuatro renglones de siempre—
    sale identico a como salia antes de 2026-08-05.
    """
    y     = pdf.get_y()
    tot_h = 26
    box_h = max(tot_h, 26)

    # Caja de notas (accent-soft, sin borde)
    pdf.set_fill_color(*_ACCENT_SOFT)
    _rrect(pdf, _LX, y, _NOTES_W, box_h, style="F")

    cond_label = f"Condición de venta: {condicion_venta}" if condicion_venta else "Condición de venta: Contado"
    pdf.set_xy(_LX + 4, y + 5)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*_ACCENT_DARK)
    pdf.cell(_NOTES_W - 8, 5, _recortar(pdf, cond_label, _NOTES_W - 8), ln=True)
    if observations:
        pdf.set_x(_LX + 4)
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(_NOTES_W - 8, 5, "Notas:", ln=True)
        pdf.set_x(_LX + 4)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.multi_cell(_NOTES_W - 8, 4.5, str(observations)[:400])

    # Caja de totales (borde _LINE, redondeada)
    tx = _LX + _NOTES_W + _SUM_GAP
    # `tax_pct=None` = el comprobante mezcla alicuotas y no hay UN porcentaje
    # que lo describa. Escribir cualquiera seria declarar mal el IVA de las
    # lineas que no la usan; se muestra el monto sin porcentaje, y el detalle
    # por linea queda en la tabla de items (`show_iva_col=True`).
    etiqueta_iva = "IVA" if tax_pct is None else f"IVA {tax_pct:.0f}%"
    if discriminar:
        rows_data = [
            ("Subtotal",              "$ " + _ar(sub),        False),
            (etiqueta_iva,            "$ " + _ar(iva_amount), False),
            ("Otros tributos",        "$ " + _ar(otros),      False),
            ("Total",                 "$ " + _ar(total),       True),
        ]
    else:
        # Sin desglose. El monto del IVA no se omite por conveniencia: quien no
        # computa credito fiscal no tiene que hacer nada con el, y mostrarselo
        # aparte le hace comparar el neto contra el precio final de otro
        # presupuesto que si lo incluye.
        rows_data = [
            ("IVA incluido en los precios", "",                False),
            ("Otros tributos",              "$ " + _ar(otros), False),
            ("Total",                       "$ " + _ar(total),  True),
        ]
    row_h = tot_h / len(rows_data)

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
    pdf.set_y(y + box_h + 4)


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
        cae_vto = f"{cae_vto[6:8]}-{cae_vto[4:6]}-{cae_vto[0:4]}"

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

class FacturaPDF(_TextoSeguroPDF):
    def __init__(self, factura):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.factura = factura
        self._emp    = None
        self.fijar_fecha_documento(factura.get("fecha"))
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


class RemitoPDF(_TextoSeguroPDF):
    def __init__(self, remito):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.remito = remito
        self._emp   = None
        self.fijar_fecha_documento(remito.get("date"))
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


class PresupuestoPDF(_TextoSeguroPDF):
    def __init__(self, presupuesto):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.presupuesto = presupuesto
        self._emp        = None
        self.fijar_fecha_documento(presupuesto.get("date"))
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
        texto_obs = str(remito["observations"])[:400]
        # El alto sale del texto, no de un 28 fijo. Con el fijo pasaban dos
        # cosas: una observacion de un renglon dejaba media caja vacia, y —lo
        # que se veia mal de verdad— el cursor quedaba donde termino el TEXTO
        # y no donde termina la CAJA, asi que el aviso de "no valido como
        # factura", que se ancla al pie, se dibujaba **encima** del recuadro.
        # Solo se notaba con la hoja llena, o sea desde que entran mas items.
        pdf.set_font("Helvetica", "", 7.5)
        lineas_obs = pdf.multi_cell(obs_w - 8, 4.5, texto_obs, split_only=True)
        obs_h = 4 + 5 + max(1, len(lineas_obs)) * 4.5 + 4
        y_obs = pdf.get_y()
        pdf.set_fill_color(*_ACCENT_SOFT)
        _rrect(pdf, _LX, y_obs, obs_w, obs_h, style="F")
        pdf.set_xy(_LX + 4, y_obs + 4)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*_ACCENT_DARK)
        pdf.cell(obs_w - 8, 5, "Observaciones:", ln=True)
        pdf.set_x(_LX + 4)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*_INK)
        pdf.multi_cell(obs_w - 8, 4.5, texto_obs)
        pdf.set_y(y_obs + obs_h)
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


def generate_pdf_presupuesto(presupuesto, output_dir=None, discriminar=True):
    """`discriminar=False` saca el desglose de IVA y muestra los precios ya
    con el impuesto adentro.

    Lo decide la condicion frente al IVA del **receptor**, que es la unica
    consecuencia de esa condicion que aplica a un presupuesto (el tipo de
    comprobante A/B/C no aplica: esto no es una factura). El default es `True`
    para que los productos que todavia no la modelan salgan como siempre.
    """
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

    # Un presupuesto puede mezclar alicuotas cuando cada linea trae la suya
    # (LibraDesk desde 2026-08-05). Si las mezcla, se muestra la columna de IVA
    # por linea y el total va sin porcentaje; si todas coinciden —el caso de
    # siempre, y el unico de Contalibra/Restolibra hoy— el comprobante sale
    # **identico** a como salia antes.
    alicuotas = {
        round(float(i.get("tax_rate", presupuesto.get("tax_rate", 0)) or 0), 4)
        for i in presupuesto["items"]
    }
    mezcla = len(alicuotas) > 1
    # Sin desglose, la columna de IVA por linea sobra: el porcentaje ya esta
    # adentro del precio y ponerlo al lado invita a sumarlo dos veces.
    _draw_items_table(
        pdf, presupuesto["items"],
        show_iva_col=mezcla and discriminar,
        iva_incluido=not discriminar,
        tax_rate=presupuesto.get("tax_rate", 0) or 0,
    )

    sub = presupuesto.get("subtotal", 0)
    tax = presupuesto.get("tax_amount", 0)
    tot = presupuesto.get("total", 0)
    pct = None if mezcla else round(presupuesto.get("tax_rate", 0) * 100)

    # Anclar totales + aviso al pie (totales 38mm + aviso 18mm + footer 14mm + margen)
    _BOTTOM_BLOCK_H = 68
    target_y = pdf.h - _BOTTOM_BLOCK_H
    if pdf.get_y() > target_y:
        pdf.add_page()
        target_y = pdf.h - _BOTTOM_BLOCK_H
    pdf.set_y(target_y)

    _draw_totals_and_notes(pdf, sub, tax, 0, tot, pct,
                           presupuesto.get("observations", ""),
                           discriminar=discriminar)
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
    _TOTALS_SECTION_H = 30   # box 26mm + gap 4mm
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

    **Firma histórica: el recibo que sale por acá no tiene número**, porque se
    arma en el momento y no queda registrado en ningún lado. Se conserva para
    no romper a los productos que todavía la llaman; lo que hay que usar es
    `generate_pdf_recibo_doc()`, que renderiza un recibo ya emitido y
    numerado (ver `libracore.recibos`).
    """
    es_venta  = factura.get("_es_venta", False)
    if es_venta:
        ref_line = f"Venta N\xb0 {factura.get('_venta_numero', factura.get('numero', ''))}"
    else:
        pv          = str(factura.get("punto_venta", 0)).zfill(4)
        num         = str(factura.get("numero", 0)).zfill(8)
        tipo_nombre = _TIPO_NOMBRE_DOC.get(int(factura.get("tipo", 11)), "Comprobante")
        ref_line    = f"{tipo_nombre} {pv}-{num}"

    fecha_fac     = (factura.get("fecha") or "")[:10]
    total_cobrado = sum(float(c.get("monto", 0)) for c in cobros)
    parcial       = total_cobrado < float(factura.get("total", 0)) - 0.005
    concepto      = "Pago parcial de" if parcial else "Cancelación de"
    concepto     += f" {ref_line} del {_fmt_fecha(fecha_fac)}"

    return _render_recibo({
        "numero_label":      "",
        "ref_line":          ref_line,
        "fecha":             fecha_fac,
        "cliente_razon":     factura.get("cliente_razon") or "Consumidor Final",
        "cliente_cuit":      factura.get("cliente_cuit") or "",
        "cliente_domicilio": factura.get("cliente_domicilio") or "",
        "concepto":          concepto,
        "pagos":             cobros,
        "total":             total_cobrado,
        "anulado":           False,
        "anulado_motivo":    "",
    })


def generate_pdf_recibo_doc(recibo: dict) -> bytes:
    """Renderiza un recibo **ya emitido** — una fila de la tabla `recibos`, tal
    como la devuelve `libracore.db.recibos.get_recibo()`.

    Ésta es la entrada buena: el papel sale del registro guardado, así que
    reimprimirlo devuelve exactamente el mismo documento aunque después se
    hayan cobrado otras cuotas de la misma factura. Y lleva el número arriba,
    que es lo que lo vuelve citable.
    """
    pv  = str(recibo.get("punto_venta") or 1).zfill(4)
    num = str(recibo.get("numero") or 0).zfill(8)
    return _render_recibo({
        "numero_label":      f"{pv}-{num}",
        "ref_line":          recibo.get("concepto") or "",
        "fecha":             (recibo.get("fecha") or "")[:10],
        "cliente_razon":     recibo.get("cliente_razon") or "Consumidor Final",
        "cliente_cuit":      recibo.get("cliente_cuit") or "",
        "cliente_domicilio": recibo.get("cliente_domicilio") or "",
        "concepto":          recibo.get("concepto") or "",
        "pagos":             recibo.get("pagos") or [],
        "total":             float(recibo.get("total") or 0),
        "anulado":           bool(recibo.get("anulado")),
        "anulado_motivo":    recibo.get("anulado_motivo") or "",
    })


def _render_recibo(d: dict) -> bytes:
    """Maqueta única de los dos recibos. La diferencia entre el viejo y el
    documento son los datos, no el papel: así el cliente que ya vio uno
    reconoce el otro."""
    emp = _empresa()
    # `_TextoSeguroPDF` y no `FPDF` pelado: hasta acá el recibo era el último
    # comprobante del módulo que instanciaba FPDF directamente, así que se
    # quedó afuera del arreglo de cp1252 de v1.7.0 y un carácter fuera de esa
    # codificación en la razón social lo tumbaba con un 500. Mismo agujero que
    # tenía `TicketPDF`, tapado por el mismo camino.
    pdf = _TextoSeguroPDF(format="A4", unit="mm")
    pdf.fijar_fecha_documento(d["fecha"])
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    lx, rx, cw = _LX, _RX, _CW

    fecha_fac = _fmt_fecha(d["fecha"])
    ref_line  = d["ref_line"]
    cobros    = d["pagos"]

    info_fields = []
    if d["numero_label"]:
        info_fields.append(("Recibo N\xb0:", d["numero_label"]))
    if ref_line:
        info_fields.append(("Comprobante:", ref_line))
    info_fields.append(("Emisi\xf3n:", fecha_fac))

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

    _field("Razón social", d["cliente_razon"])
    _field("CUIT / DNI",   d["cliente_cuit"])
    _field("Domicilio",    d["cliente_domicilio"])

    fy += 4

    # ── Monto destacado ───────────────────────────────────────────────────────
    total_cobrado = d["total"]
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
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*_MUTED)
    pdf.set_xy(lx, fy)
    pdf.cell(25, 5, "En concepto de:")
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_INK)
    pdf.set_xy(lx + 25, fy)
    pdf.multi_cell(cw - 25, 5, d["concepto"], align="L")
    fy = pdf.get_y() + 6

    # ── Anulado ───────────────────────────────────────────────────────────────
    # Franja en vez de marca de agua: el recibo anulado se sigue pudiendo
    # imprimir (el número está consumido y alguien puede pedir ver cuál era),
    # pero tiene que ser imposible confundirlo con uno vigente de un vistazo.
    if d["anulado"]:
        pdf.set_fill_color(*_WARNING_SOFT)
        _rrect(pdf, lx, fy, cw, 10, r=3, style="F")
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*_WARNING)
        pdf.set_xy(lx + 4, fy + 2)
        motivo = d["anulado_motivo"]
        pdf.cell(cw - 8, 6, f"ANULADO{f' — {motivo}' if motivo else ''}")
        fy += 14

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
                # El medio de pago y la referencia los escribe el usuario: sin
                # recorte, una referencia larga se lleva puesta la columna del
                # monto y se va del papel.
                pdf.cell(w, 6, _recortar(pdf, str(val), w), align=al)
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

class ResumenCCPDF(_TextoSeguroPDF):
    def __init__(self, cliente, periodo):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.cliente = cliente
        self.periodo = periodo
        self._emp    = None
        # El resumen no tiene fecha propia: la emisión es lo que lo fecha.
        self.fijar_fecha_documento(periodo.get("emitido") or periodo.get("hasta"))
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
