"""El catálogo de servicios de ARCA que el motor sabe configurar (ADR-032).

Hasta acá la pantalla de ARCA conocía uno solo, la facturación (`wsfe`). Cada
servicio de ARCA se autentica por WSAA **con su propio nombre** (`servicio=` de
`arca_wsaa.autenticar`) y, según el caso, con su propio certificado: el de CTG y
Carta de Porte de LibraCargo se emitió con un alias aparte
(`libracargowscpehomo` / `libracargowscpeprod`) y a nombre de **la persona** que
representa a la empresa, no de la razón social.

Este módulo es sólo datos y dos funciones puras de ARCA: el catálogo, el chequeo
`dummy` de un servicio y la traducción de los errores de WSAA. El router los usa;
nada acá toca la base ni el disco.

## `dummy`: ¿está arriba el servicio?

Los servicios SOAP de ARCA exponen `dummy`, que **no pide autenticación** y
contesta el estado de sus tres servidores (`appserver`, `authserver`,
`dbserver`). Sirve para separar «el servicio está caído» de «mi certificado no
anda». Sólo lo tienen los servicios con `endpoints` en el catálogo (`wscpe`); la
facturación ya tiene el suyo en `arca_wsfe` y no se duplica.
"""
from __future__ import annotations

import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

AMBIENTES = ("homologacion", "produccion")

#: El nombre que WSAA espera en el `<service>` del pedido de acceso.
SERVICIO_FACTURACION = "wsfe"


@dataclass(frozen=True)
class Servicio:
    """Un servicio de ARCA configurable desde la pantalla."""

    id: str
    etiqueta: str
    #: Cómo se llama el servicio ante WSAA (`<service>` del TRA).
    wsaa: str
    #: Una línea que la pantalla muestra bajo el título del bloque.
    ayuda: str = ""
    #: `ambiente -> url SOAP` para el `dummy`. Vacío: el servicio no lo expone acá.
    endpoints: dict[str, str] = field(default_factory=dict)
    #: Espacio de nombres SOAP del servicio, para armar el `dummy`.
    namespace: str = ""


CATALOGO: dict[str, Servicio] = {
    "wsfe": Servicio(
        id="wsfe",
        etiqueta="Facturación electrónica",
        wsaa="wsfe",
    ),
    "wscpe": Servicio(
        id="wscpe",
        etiqueta="CTG y Carta de Porte",
        wsaa="wscpe",
        ayuda="El certificado puede estar a nombre de la persona que representa a la empresa.",
        endpoints={
            "homologacion": "https://cpea-ws-qaext.afip.gob.ar/wscpe/services/soap",
            "produccion": "https://cpea-ws.afip.gob.ar/wscpe/services/soap",
        },
        # 🔑 `https`, no `http`: el manual mezcla los dos y el WSDL real usa éste.
        namespace="https://serviciosjava.afip.gob.ar/wscpe/",
    ),
}


class ServicioDesconocido(KeyError):
    """Se pidió un servicio que no está en el catálogo."""


def servicio(id_: str) -> Servicio:
    try:
        return CATALOGO[(id_ or "").strip().lower()]
    except KeyError:
        raise ServicioDesconocido(id_) from None


# ── dummy ───────────────────────────────────────────────────────────────────


def _ssl_ctx():
    # Los servidores de ARCA usan parámetros DH viejos (igual que arca_wspadron).
    ctx = ssl.create_default_context()
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    return ctx


def dummy(id_: str, ambiente: str, *, timeout: float = 10) -> dict | None:
    """El estado que contesta el `dummy` del servicio, o `None` si no tiene.

    `{"appserver": "OK", "authserver": "OK", "dbserver": "OK"}`. Levanta si ARCA
    no contesta o contesta algo que no es un `dummy`: quien lo llama decide si
    eso es un error o un dato más (el router lo trata como dato).
    """
    svc = servicio(id_)
    url = svc.endpoints.get(ambiente)
    if not url:
        return None
    import httpx

    sobre = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:wsc="{svc.namespace}"><soapenv:Header/><soapenv:Body/></soapenv:Envelope>'
    )
    with httpx.Client(verify=_ssl_ctx(), timeout=timeout) as c:
        resp = c.post(
            url, content=sobre.encode(),
            headers={"Content-Type": "text/xml; charset=UTF-8",
                     "SOAPAction": f'"{svc.namespace}dummy"'},
        )
    raiz = ET.fromstring(resp.text)
    estado = {
        e.tag.split("}")[-1]: (e.text or "").strip()
        for e in raiz.iter()
        if e.tag.split("}")[-1] in ("appserver", "authserver", "dbserver")
    }
    if not estado:
        raise RuntimeError(f"el servicio no contestó un dummy (HTTP {resp.status_code})")
    return estado


# ── Errores de WSAA, en castellano ──────────────────────────────────────────

#: `(fragmento del texto de ARCA, explicación)`. El primero que aparezca gana.
#: Se busca por **código** (`coe.notAuthorized`) y no por la frase, que ARCA
#: cambia de redacción; la frase va igual, tal cual, al final del mensaje.
_TRADUCCIONES = (
    ("coe.notAuthorized",
     "el certificado no está autorizado para este servicio: asociá el servicio en el "
     "Administrador de Relaciones de ARCA al alias del certificado."),
    ("cms.cert.untrusted",
     "ARCA no reconoce este certificado para el ambiente elegido: un certificado de "
     "homologación no sirve en producción, ni al revés. Revisá que sea el del ambiente "
     "correcto y que lo haya emitido ARCA."),
    ("cms.cert.expired",
     "el certificado está vencido: generá uno nuevo en ARCA y cargalo de nuevo."),
    ("cms.cert.notyetvalid",
     "el certificado todavía no es válido (su fecha de inicio es futura). Revisá la hora "
     "del servidor."),
    ("coe.alreadyAuthenticated",
     "ARCA ya emitió un ticket vigente para este certificado y servicio, y esta instancia "
     "no lo tiene guardado. Se puede volver a intentar cuando venza (hasta 12 horas)."),
    ("xml.bad.generationTime",
     "la hora de este servidor no coincide con la de ARCA. Sincronizá el reloj."),
    ("xml.bad.expirationTime",
     "la hora de este servidor no coincide con la de ARCA. Sincronizá el reloj."),
)


def traducir_error_wsaa(texto: str) -> str:
    """El error de WSAA explicado, **con el texto de ARCA al final**.

    Si no se reconoce el código, devuelve el texto de ARCA solo: es el que dice
    si el problema es el certificado, la relación con el servicio o la hora.
    """
    crudo = (texto or "").strip()
    for fragmento, explicacion in _TRADUCCIONES:
        if fragmento.lower() in crudo.lower():
            return f"{explicacion[0].upper()}{explicacion[1:]} (ARCA dijo: {crudo})"
    return crudo
