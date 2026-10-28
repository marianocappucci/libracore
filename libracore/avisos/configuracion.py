"""La configuración de canales de una instancia (ADR-090): qué hace WhatsApp y con qué se conecta.

## Dónde vive cada dato

| Dato | Dónde | Por qué |
|---|---|---|
| `avisos_whatsapp_modo` (`apagado`, `registro`, `evolution`) | `config.json` | no es un secreto |
| `avisos_evolution_url`, `avisos_evolution_instancia` | `config.json` | no son secretos: dicen a dónde ir, no cómo entrar |
| la `apikey` de Evolution | **el almacén cifrado de secretos** (`config_manager.usar_almacen_de_secretos`), con la clave `avisos_evolution_apikey` | es una credencial |

🔴 **La `apikey` no pasa por `config_manager.load()`.** `load()` devuelve en claro todo lo que está en `CLAVES_SECRETAS`, y hay
productos cuyo `GET /api/config` devuelve ese dict entero (`mp_config_router` lo documenta: hoy filtran el token de MercadoPago y la
contraseña de SMTP en el JSON de una pantalla). Poner acá la `apikey` en esa lista la sumaba a la lista de lo que se puede filtrar
por un descuido de un producto. Se lee y se escribe directo en el almacén, y **sólo `ProveedorEvolution` la recibe**. Hacia afuera
sale `apikey_cargada: true|false`, nunca el valor ni una parte de él.

Sin almacén enchufado **no se guarda la apikey**: la alternativa sería escribirla en claro en `config.json`, que es lo que
`config_manager` vino a sacar. El script de un cron (`docker exec`) tiene que enchufar el almacén antes de despachar, igual que el
cron de MercadoPago (`config_manager.secretos_fuera_de_alcance`).

## Una demo nunca manda

Con `DEMO_MODE=1` el WhatsApp siempre es `registro`, diga lo que diga la configuración, y no se puede guardar `evolution`. Una demo
pública con Evolution conectado le dejaría a cualquiera con un código de demo mandar mensajes a cualquier número.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from libracore import config_manager
from libracore.avisos.mensaje import CANAL_EMAIL, CANAL_WHATSAPP, Proveedor
from libracore.avisos.proveedores import ProveedorApagado, ProveedorEvolution, ProveedorRegistro, ProveedorSmtp

MODO_APAGADO = "apagado"
MODO_REGISTRO = "registro"
MODO_EVOLUTION = "evolution"
MODOS = (MODO_APAGADO, MODO_REGISTRO, MODO_EVOLUTION)

CLAVE_MODO = "avisos_whatsapp_modo"
CLAVE_URL = "avisos_evolution_url"
CLAVE_INSTANCIA = "avisos_evolution_instancia"
#: Con esta clave se guarda la apikey en el almacén de secretos. **No** está en `config_manager.CLAVES_SECRETAS` a propósito.
CLAVE_APIKEY = "avisos_evolution_apikey"

#: Los defaults que `config_manager.DEFAULTS` suma para esta configuración.
DEFAULTS = {CLAVE_MODO: MODO_APAGADO, CLAVE_URL: "", CLAVE_INSTANCIA: ""}

_INSTANCIA = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_LARGO_DE_URL = 300
_LARGO_DE_APIKEY = 500


class ErrorDeConfiguracion(ValueError):
    """La configuración que se quiere guardar no sirve. El mensaje se le muestra a la persona y **no lleva la apikey**."""


@dataclass(frozen=True, slots=True)
class ConfigWhatsApp:
    modo: str
    url: str
    instancia: str
    apikey_cargada: bool


def _almacen():
    return config_manager.almacen_de_secretos()


def _apikey() -> str:
    """La apikey en claro, o vacío. **Sólo para `ProveedorEvolution`**: no sale por ninguna API."""
    almacen = _almacen()
    return (almacen.get(CLAVE_APIKEY) or "") if almacen is not None else ""


def leer() -> ConfigWhatsApp:
    cfg = config_manager.load_sin_demo()
    modo = cfg.get(CLAVE_MODO) or MODO_APAGADO
    return ConfigWhatsApp(
        modo=modo if modo in MODOS else MODO_APAGADO,
        url=(cfg.get(CLAVE_URL) or "").strip(),
        instancia=(cfg.get(CLAVE_INSTANCIA) or "").strip(),
        apikey_cargada=bool(_apikey()),
    )


def validar_url(url: str) -> str:
    """La URL base de Evolution, sin la barra final. Vacía es válida (es «borrar»)."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if len(url) > _LARGO_DE_URL or any(c.isspace() for c in url):
        raise ErrorDeConfiguracion("La URL de Evolution no es válida.")
    partes = urlsplit(url)
    if partes.scheme not in ("http", "https") or not partes.hostname:
        raise ErrorDeConfiguracion("La URL de Evolution tiene que empezar con http:// o https://.")
    if partes.username or partes.password or partes.query or partes.fragment:
        # El usuario y la contraseña de una URL terminarían en los logs de cualquier proxy; la credencial va en la apikey.
        raise ErrorDeConfiguracion("La URL de Evolution no lleva usuario, contraseña, parámetros ni #.")
    return url


def validar_instancia(instancia: str) -> str:
    instancia = (instancia or "").strip()
    if instancia and not _INSTANCIA.fullmatch(instancia):
        raise ErrorDeConfiguracion("El nombre de la instancia sólo lleva letras, números, punto, guión y guión bajo.")
    return instancia


def guardar(*, modo: str | None = None, url: str | None = None, instancia: str | None = None,
            apikey: str | None = None, quitar_apikey: bool = False) -> ConfigWhatsApp:
    """Guarda lo que vino (`None` = no tocar). Devuelve la configuración resultante, **sin la apikey**.

    - `apikey` vacía **no borra** la que está: la pantalla no la muestra, así que mandar el campo vacío tiene que querer decir «no
      la toqués» (como la contraseña de SMTP y el token de MercadoPago). Para sacarla, `quitar_apikey=True`.
    - `modo="evolution"` pide URL, instancia y apikey ya cargadas, y no se puede en una demo.
    - Se guarda el `config.json` **sobre lo que había** (`load_sin_demo`): `save()` mergea contra los defaults, y armarlo de cero
      borraría todo lo demás."""
    if modo is not None and modo not in MODOS:
        raise ErrorDeConfiguracion(f"El modo tiene que ser uno de: {', '.join(MODOS)}.")
    nueva_url = validar_url(url) if url is not None else None
    nueva_instancia = validar_instancia(instancia) if instancia is not None else None
    if apikey is not None and len(apikey) > _LARGO_DE_APIKEY:
        raise ErrorDeConfiguracion("La apikey es demasiado larga.")
    apikey = (apikey or "").strip()

    actual = leer()
    resultante = ConfigWhatsApp(
        modo=modo if modo is not None else actual.modo,
        url=nueva_url if nueva_url is not None else actual.url,
        instancia=nueva_instancia if nueva_instancia is not None else actual.instancia,
        apikey_cargada=(False if quitar_apikey else bool(apikey) or actual.apikey_cargada),
    )
    if resultante.modo == MODO_EVOLUTION:
        if config_manager.es_demo():
            raise ErrorDeConfiguracion("En una demo los avisos por WhatsApp sólo se registran: no se puede conectar Evolution.")
        faltan = [n for n, v in (("la URL", resultante.url), ("el nombre de la instancia", resultante.instancia),
                                 ("la apikey", resultante.apikey_cargada)) if not v]
        if faltan:
            raise ErrorDeConfiguracion(f"Para usar Evolution falta cargar {' y '.join(faltan)}.")

    if apikey or quitar_apikey:
        almacen = _almacen()
        if almacen is None:
            raise ErrorDeConfiguracion(
                "Esta instancia no tiene un almacén de secretos: la apikey no se puede guardar cifrada, y en texto plano "
                "no se guarda.")
        try:
            almacen.set(CLAVE_APIKEY, "" if quitar_apikey and not apikey else apikey)
        except Exception as e:  # noqa: BLE001 - p. ej. una instancia sin clave de cifrado
            raise ErrorDeConfiguracion(f"No se pudo guardar la apikey cifrada ({type(e).__name__}).") from None

    cfg = config_manager.load_sin_demo()
    cfg.update({CLAVE_MODO: resultante.modo, CLAVE_URL: resultante.url, CLAVE_INSTANCIA: resultante.instancia})
    config_manager.save(cfg)
    return leer()


def motivo_de_whatsapp() -> str:
    """Por qué WhatsApp no está disponible, en una frase para la pantalla. Vacío si lo está."""
    cfg = leer()
    if cfg.modo == MODO_APAGADO:
        return "WhatsApp está apagado."
    if cfg.modo == MODO_REGISTRO or config_manager.es_demo():
        return ""
    if _almacen() is None:
        if config_manager.secretos_fuera_de_alcance():
            return ("Este proceso no abrió el almacén de secretos: la apikey de Evolution está guardada pero no se puede leer "
                    "desde acá.")
        return "Esta instancia no tiene un almacén de secretos donde guardar la apikey."
    faltan = [n for n, v in (("la URL", cfg.url), ("el nombre de la instancia", cfg.instancia),
                             ("la apikey", cfg.apikey_cargada)) if not v]
    return f"Falta cargar {' y '.join(faltan)}." if faltan else ""


def proveedor_whatsapp(*, transporte: httpx.BaseTransport | None = None) -> Proveedor:
    """El proveedor de WhatsApp que corresponde a la configuración: apagado, registro o Evolution.

    En una demo, siempre registro. `transporte` es para los tests (`httpx.MockTransport`)."""
    cfg = leer()
    if cfg.modo == MODO_APAGADO:
        return ProveedorApagado(CANAL_WHATSAPP, "WhatsApp está apagado.")
    if cfg.modo == MODO_REGISTRO or config_manager.es_demo():
        return ProveedorRegistro(CANAL_WHATSAPP)
    return ProveedorEvolution(cfg.url, cfg.instancia, _apikey(), transporte=transporte)


def proveedor_email(smtp_config=None) -> ProveedorSmtp:
    """El proveedor de mail: el SMTP que la instancia ya tiene (`smtp_config` es el resolver del producto)."""
    return ProveedorSmtp(smtp_config)


def proveedores(smtp_config=None, *, transporte: httpx.BaseTransport | None = None) -> dict[str, Proveedor]:
    """`{canal: proveedor}` de la instancia. Lo que arma un barrido: `proveedores(...)["whatsapp"]`."""
    return {CANAL_EMAIL: proveedor_email(smtp_config), CANAL_WHATSAPP: proveedor_whatsapp(transporte=transporte)}
