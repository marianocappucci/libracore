"""Configuracion persistida en config.json (DATA_DIR) — nombre de empresa,
credenciales de MercadoPago/SMTP, ticket, resolucion de logo/certificados
ARCA con auto-correccion de rutas obsoletas.

Cada producto puede tener defaults propios ademas de los genericos de aca
(ej. cargos automaticos de cubierto/panera en Restolibra) — `load()`/`save()`
aceptan un `extra_defaults` opcional para eso en vez de que este modulo
conozca nada especifico de un producto."""
import json
import os

_DATA_DIR   = os.environ.get("DATA_DIR", os.getcwd())
CONFIG_PATH = os.path.join(_DATA_DIR, "config.json")
LOGO_DIR    = os.path.join(_DATA_DIR, "logos")
CERTS_DIR   = os.path.join(_DATA_DIR, "arca_certs")

_LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp")

DEFAULTS = {
    # Estado del servicio (gestionado desde panel_admin.py o config web)
    "servicio_estado":        "activo",   # activo | pausado | suspendido
    "servicio_mensaje":       "",         # mensaje personalizado opcional
    "empresa_nombre":         "",
    "empresa_direccion":      "",
    "empresa_cuit":           "",
    "empresa_telefono":       "",
    "empresa_email":          "",
    "empresa_iibb":           "",
    "empresa_iva_condition":       "Monotributista",
    "empresa_inicio_actividades":  "",
    "logo_path":                   "",
    # MercadoPago
    "mp_access_token":        "",
    "mp_webhook_secret":      "",
    "mp_concepto_descripcion": "Cobro con Mercadopago",
    "mp_iva_rate":            "0",
    # Cobrar una venta presencial por QR y facturarla sola son dos cosas: el
    # cobro entra igual, pero emitir el comprobante sin que nadie lo pida es
    # una decision del negocio. Por omision, no.
    "mp_auto_facturar_ventas": False,
    # MercadoPago QR Dinámico (punto de venta presencial)
    "mp_user_id":             "",   # ID numérico del vendedor en MP
    "mp_pos_id":              "",   # External ID del POS creado en MP
    # De que ambiente es la credencial cargada: "prueba" | "produccion" | "".
    # NO se elige a mano — lo DERIVA el "probar" de la pantalla preguntandole a
    # MercadoPago quien es el dueño del token. Ver `mp_config_router`.
    #
    # La huella es el sha256 recortado del token sobre el que se determino: si
    # el token de al lado cambia por cualquier via —la pantalla, panel_admin
    # escribiendo config.json, restaurar un backup— deja de coincidir y la
    # clasificacion se descarta sola. Sin ella, cambiar de prueba a produccion
    # dejaria el cartel diciendo "prueba" sobre una credencial real.
    "mp_ambiente":            "",
    "mp_ambiente_verificado": "",   # 'YYYY-MM-DD HH:MM:SS' en hora AR
    "mp_ambiente_huella":     "",
    # Email / SMTP
    "email_smtp_host":        "",
    "email_smtp_port":        "587",
    "email_smtp_user":        "",
    "email_smtp_password":    "",
    "email_from":             "",
    "email_from_name":        "",
    # Resumen automático de cuenta corriente por email
    # (el toggle real es por cliente — `clients.cc_resumen_auto` —; esto es la
    # llave maestra del sistema y los parámetros comunes del envío)
    "cc_resumen_habilitado":   "0",   # 0 | 1 — si está en 0 no se envía nada
    "cc_resumen_dia_mes":      "1",   # día del mes para la frecuencia mensual (1-28)
    "cc_resumen_dia_semana":   "1",   # 1=lunes … 7=domingo, para semanal/quincenal
    "cc_resumen_solo_con_saldo": "1",  # 0 | 1 — omitir clientes con saldo <= 0
    "cc_resumen_asunto":       "Resumen de cuenta corriente - {empresa}",
    "cc_resumen_cuerpo":       "",    # vacío = texto por defecto de cc_resumen.py
    # Impresora de tickets (ticketeadora térmica)
    "ticket_ancho_mm":        "80",      # 58 | 80
    "ticket_fuente_size":     "9",       # puntos
    "ticket_mostrar_logo":    "0",       # 0 | 1
    "ticket_linea_corte":     "1",       # imprimir línea de corte al final
    "ticket_pie":             "",        # texto libre al pie (ej: "¡Gracias por su compra!")
}


def load(extra_defaults: dict | None = None):
    defaults = {**DEFAULTS, **(extra_defaults or {})}
    if not os.path.exists(CONFIG_PATH):
        return defaults.copy()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**defaults, **data}
    except Exception:
        return defaults.copy()


def save(data, extra_defaults: dict | None = None):
    defaults = {**DEFAULTS, **(extra_defaults or {})}
    merged = {**defaults, **data}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)


def resolve_logo_path(cfg=None):
    """Devuelve la ruta a un archivo de logo existente, o "" si no hay ninguno.

    El "logo_path" guardado en config es una ruta absoluta que puede quedar
    obsoleta si el DATA_DIR del proceso que la escribio cambio (ej. migracion
    de path). Si el path guardado no existe, se cae al logo mas reciente
    encontrado en LOGO_DIR.
    """
    cfg = cfg if cfg is not None else load()
    p = (cfg.get("logo_path") or "").strip()
    if p and os.path.exists(p):
        return p
    if os.path.isdir(LOGO_DIR):
        cands = [os.path.join(LOGO_DIR, f) for f in os.listdir(LOGO_DIR)
                 if f.lower().startswith("logo") and f.lower().endswith(_LOGO_EXTS)]
        if cands:
            return max(cands, key=os.path.getmtime)
    return ""


def resolve_cert_paths(cert_path="", key_path=""):
    """Devuelve (certificado, clave_privada) ARCA auto-corrigiendo rutas obsoletas.

    Analogo a resolve_logo_path: si un path guardado no apunta a un archivo
    existente, se cae al nombre estandar dentro de CERTS_DIR. Si tampoco
    existe el fallback, se devuelve el path original tal cual para que el
    llamador reporte el error habitual.
    """
    def _resolve(p, filename):
        p = (p or "").strip()
        if p and os.path.exists(p):
            return p
        fallback = os.path.join(CERTS_DIR, filename)
        return fallback if os.path.exists(fallback) else p

    return (_resolve(cert_path, "certificado.crt"),
            _resolve(key_path, "clave_privada.key"))
