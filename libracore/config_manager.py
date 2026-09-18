"""Configuracion persistida en config.json (DATA_DIR) — nombre de empresa,
credenciales de MercadoPago/SMTP, ticket, resolucion de logo/certificados
ARCA con auto-correccion de rutas obsoletas.

Cada producto puede tener defaults propios ademas de los genericos de aca
(ej. cargos automaticos de cubierto/panera en Restolibra) — `load()`/`save()`
aceptan un `extra_defaults` opcional para eso en vez de que este modulo
conozca nada especifico de un producto.

## 🔴 Los tres secretos NO viven en el JSON (desde 2026-09-17)

`mp_access_token`, `mp_webhook_secret` y `email_smtp_password` son credenciales
de terceros, y hasta esta version se escribian en `config.json` **en texto
plano**. No fue una decision: no habia en el codigo ninguna razon tecnica para
que estuvieran ahi y no cifradas, y la pagina del wiki que hace de censo de
secretos en reposo ni siquiera las nombraba. Lo que tapaba el hueco es que
`mp_config_router` enmascara el token en la respuesta HTTP — o sea que el
problema se penso y se resolvio **en la capa en que el dato se mira**, y en la
capa en que se guarda no habia nada.

Ahora van a un **almacen cifrado** que el producto inyecta con
`usar_almacen_de_secretos()`. Hoy el unico que existe es
`libraauth.secretos.SecretosRepository` (AES-GCM con clave derivada del
`SECRET_KEY` de la instancia), pero este modulo **no lo importa ni lo nombra en
el codigo**: le alcanza con que el objeto tenga `get(clave)` y `set(clave,
valor)`.

🔑 **Esa indireccion es el punto, no un adorno.** LibraCore no depende de
libraauth y no va a empezar a depender — lo dice `comprobantes_router` y es el
mismo criterio con el que los gates de rol los pone el producto. El producto ya
tiene los dos paquetes, asi que es el unico lugar donde pueden encontrarse sin
que ningun motor importe al otro.

**Para los ~12 lugares que leen estos campos no cambia nada.** `load()` sigue
devolviendo el secreto en claro bajo la misma clave; lo unico que cambia es de
donde sale. Ver `CLAVES_SECRETAS` y `migrar_secretos_al_almacen()`."""
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


#: Las claves de `DEFAULTS` que son **credenciales de terceros** y por lo tanto
#: no pueden quedar en el JSON.
#:
#: Es una lista corta y explicita, no una heuristica sobre el nombre: un
#: `"token" in clave` clasificaria de mas el dia que aparezca un
#: `mp_token_publico`, y de menos con cualquier secreto que no se llame asi. Si
#: aparece un secreto nuevo, se agrega **aca** y la migracion lo levanta sola.
#:
#: ⚠️ `mp_user_id` y `mp_pos_id` NO estan y no tienen que estar: identifican la
#: cuenta y la caja, no autorizan nada. Meterlos los volveria ilegibles al rotar
#: el `SECRET_KEY` sin ninguna ganancia de seguridad.
CLAVES_SECRETAS = ("mp_access_token", "mp_webhook_secret", "email_smtp_password")

#: El almacen cifrado, o `None`. Estado del proceso, igual que el destino de
#: `libracore.db.core.configure()`.
_almacen = None


def usar_almacen_de_secretos(almacen) -> None:
    """Enchufa el almacen cifrado. Lo llama el producto al arrancar.

    `almacen` es cualquier objeto con `get(clave) -> str` y `set(clave, valor)`.
    En los ocho productos es `libraauth.secretos.SecretosRepository`, construido
    con el mismo `session_factory` que el `UserRepository`.

    `None` lo desconecta, que es lo que hacen los tests que quieren medir el
    camino de solo-JSON.
    """
    global _almacen
    _almacen = almacen


def almacen_de_secretos():
    """El almacen enchufado, o `None`. Para que un producto pueda contestar
    "¿esta instancia guarda sus secretos cifrados?" sin adivinar."""
    return _almacen


def load(extra_defaults: dict | None = None):
    """La config efectiva: el JSON, con los secretos traidos del almacen.

    **Sin almacen enchufado se comporta exactamente como antes.** Eso no es
    tolerancia a un olvido: es lo que hace que `panel_admin`, el provisioning y
    cualquier script que lea la config de una instancia ajena sigan andando sin
    una base al lado.

    Y con almacen enchufado, un secreto que **todavia esta en el JSON y no en el
    almacen** se devuelve igual. Esa es la ventana entre que se despliega el
    codigo nuevo y que corre la migracion: durante esos segundos la instancia
    tiene que seguir cobrando. El almacen gana cuando tiene algo; el JSON es el
    respaldo, no al reves.
    """
    defaults = {**DEFAULTS, **(extra_defaults or {})}
    if not os.path.exists(CONFIG_PATH):
        cfg = defaults.copy()
    else:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            cfg = {**defaults, **data}
        except Exception:
            cfg = defaults.copy()
    if _almacen is not None:
        for clave in CLAVES_SECRETAS:
            guardado = _almacen.get(clave)
            if guardado:
                cfg[clave] = guardado
    return cfg


def save(data, extra_defaults: dict | None = None):
    """Persiste la config. Los secretos van al almacen y **vacios al JSON**.

    🔴 **El orden importa y no es simetrico.** Primero se guarda en el almacen y
    recien despues se escribe el JSON sin el secreto. Al reves —vaciar el JSON y
    despues intentar cifrar— un fallo al guardar dejaria la credencial borrada
    de los dos lados.

    Por eso tampoco se atrapa lo que lance el almacen. `cifrar()` levanta
    `ClaveDeCifradoAusente` cuando la instancia no tiene ni `SECRET_KEY` ni
    `LIBRAAUTH_ENCRYPTION_KEY`, y dejarlo propagar es lo correcto: lo unico que
    podria hacerse en su lugar es escribir el secreto en claro, que es
    exactamente lo que este cambio vino a sacar.

    **Solo se escribe el que cambio.** `config_router` hace load-modificar-save
    para guardar la razon social, asi que sin este cotejo cada edicion de datos
    de empresa recifraria las tres credenciales y les moveria el
    `actualizado_at` — un campo que despues se lee para saber cuando se toco una
    credencial de verdad.
    """
    defaults = {**DEFAULTS, **(extra_defaults or {})}
    merged = {**defaults, **data}
    if _almacen is not None:
        for clave in CLAVES_SECRETAS:
            valor = merged.get(clave) or ""
            if valor != _almacen.get(clave):
                _almacen.set(clave, valor)
            merged[clave] = ""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)


def migrar_secretos_al_almacen() -> dict:
    """Saca del `config.json` los secretos que quedaron en claro. Idempotente.

    La llama el producto en el arranque, despues de `usar_almacen_de_secretos()`.
    Correrla en cada arranque es a proposito: asi la migracion de las instancias
    vivas **es el deploy**, y no una lista de servidores a los que alguien tiene
    que entrar a mano — que es como quedan a medias las migraciones de esta
    familia.

    Devuelve `{"migradas": [...], "ya_estaban": [...], "fallaron": {clave: motivo}}`.
    🔑 **Nombres de claves, nunca valores.** Este informe se loguea, y un
    informe que imprima el secreto lo muda del JSON a los logs.

    Reglas, en orden:

    - Si el almacen **ya tiene** el secreto, el del JSON es una copia vieja: se
      borra del JSON sin tocar el almacen. El almacen es la fuente de verdad
      desde el momento en que tiene algo.
    - Si el almacen no lo tiene y el JSON si, se cifra, se guarda, y **recien
      entonces** se vacia el JSON.
    - Si cifrar falla, **el JSON no se toca**. Una instancia sin clave de
      cifrado tiene que seguir cobrando con la credencial que tiene, no quedarse
      sin ninguna. Queda en `fallaron` para que se vea.
    """
    if _almacen is None:
        raise RuntimeError(
            "No hay almacen de secretos enchufado: llamar primero a "
            "usar_almacen_de_secretos()."
        )
    informe: dict = {"migradas": [], "ya_estaban": [], "fallaron": {}}
    if not os.path.exists(CONFIG_PATH):
        return informe

    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            crudo = json.load(f)
    except Exception as e:
        informe["fallaron"]["config.json"] = f"no se pudo leer: {type(e).__name__}"
        return informe

    a_limpiar = []
    for clave in CLAVES_SECRETAS:
        en_json = (crudo.get(clave) or "").strip()
        if not en_json:
            continue
        if _almacen.get(clave):
            informe["ya_estaban"].append(clave)
            a_limpiar.append(clave)
            continue
        try:
            _almacen.set(clave, en_json)
        except Exception as e:
            informe["fallaron"][clave] = type(e).__name__
            continue
        informe["migradas"].append(clave)
        a_limpiar.append(clave)

    if a_limpiar:
        for clave in a_limpiar:
            crudo[clave] = ""
        # Se reescribe el JSON **crudo**, sin pasar por `save()`: `save` mergea
        # contra DEFAULTS, y toda clave propia del producto que no este ahi
        # volveria a su valor por defecto. Ese merge ya borro el token de
        # MercadoPago una vez (ver el docstring de `mp_config_router`).
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(crudo, f, ensure_ascii=False, indent=2)

    return informe


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


#: Con que nombre se guarda en disco el par de cada ambiente.
#:
#: 🔴 **Los dos pares NO pueden compartir archivo.** Hasta el 2026-09-01 el
#: upload escribia siempre `certificado.crt`, asi que subir el de homologacion
#: **pisaba el de produccion** — medido, no supuesto. Es exactamente la
#: operacion destructiva que separar las columnas venia a evitar.
#:
#: Produccion conserva los nombres historicos: son los que ya estan en el
#: volumen de cada instancia viva, y son los que busca el rescate de abajo.
ARCHIVOS_POR_AMBIENTE = {
    "produccion":   ("certificado.crt", "clave_privada.key"),
    "homologacion": ("certificado_homologacion.crt", "clave_privada_homologacion.key"),
}


def resolve_cert_paths(cert_path="", key_path="", ambiente="produccion"):
    """Devuelve (certificado, clave_privada) ARCA auto-corrigiendo rutas obsoletas.

    Analogo a resolve_logo_path: si un path guardado no apunta a un archivo
    existente, se cae al nombre estandar **del ambiente pedido** dentro de
    CERTS_DIR. Si tampoco existe el fallback, se devuelve el path original tal
    cual para que el llamador reporte el error habitual.

    ## 🔴 Por que el rescate tiene que saber el ambiente

    Cae a un nombre fijo, asi que sin saber el ambiente cae **al de
    produccion**. Eso deshace la garantia de `arca_config.paths_de()`, que
    devuelve ("", "") justamente para NO entregar las credenciales reales
    cuando falta el par de homologacion: se reponian una capa mas abajo.

    Medido el 2026-09-01: una instancia pasada a `homologacion` sin haber
    subido todavia su par terminaba autenticando **con el certificado de
    produccion del cliente**.

    El default es `produccion` por los llamadores que no saben de ambientes —
    los dos productos que consultan el padron—, y para ellos es el valor
    correcto: sin selector, lo que hay es la credencial real. Quien si lo sabe
    lo pasa.
    """
    nombres = ARCHIVOS_POR_AMBIENTE.get((ambiente or "").strip().lower())

    def _resolve(p, filename):
        p = (p or "").strip()
        if not filename:
            # Ambiente desconocido: no hay a que caer. Devolver el path tal cual
            # es lo mismo que hace el final del rescate, y no inventa un archivo.
            return p
        if p and os.path.exists(p):
            return p
        fallback = os.path.join(CERTS_DIR, filename)
        return fallback if os.path.exists(fallback) else p

    return (_resolve(cert_path, nombres[0] if nombres else ""),
            _resolve(key_path, nombres[1] if nombres else ""))
