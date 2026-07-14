"""
Cliente WSPadron A13 de ARCA/AFIP.
Consulta datos de contribuyentes por CUIT usando credenciales WSAA.
"""

import ssl
import xml.etree.ElementTree as ET

import httpx

WSPADRON_URL = {
    "homologacion": "https://awshomo.afip.gov.ar/sr-padron/webservices/personaServiceA13",
    "produccion":   "https://aws.afip.gov.ar/sr-padron/webservices/personaServiceA13",
}

_NS = "http://a13.soap.ws.server.puc.sr/"


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    return ctx


async def consultar_persona(
    cuit_empresa: str,
    cuit_consultar: str,
    token: str,
    sign: str,
    ambiente: str = "produccion",
) -> dict:
    url    = WSPADRON_URL.get(ambiente, WSPADRON_URL["produccion"])
    cuit_e = cuit_empresa.replace("-", "").strip()
    cuit_c = cuit_consultar.replace("-", "").strip()

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<SOAP-ENV:Envelope '
        'xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:ns="{_NS}">'
        "<SOAP-ENV:Body>"
        "<ns:getPersona>"
        f"<token>{token}</token>"
        f"<sign>{sign}</sign>"
        f"<cuitRepresentada>{cuit_e}</cuitRepresentada>"
        f"<idPersona>{cuit_c}</idPersona>"
        "</ns:getPersona>"
        "</SOAP-ENV:Body>"
        "</SOAP-ENV:Envelope>"
    )

    async with httpx.AsyncClient(verify=_ssl_ctx(), timeout=15) as client:
        resp = await client.post(
            url,
            content=body.encode(),
            headers={
                "Content-Type": "text/xml; charset=UTF-8",
                "SOAPAction": '""',
            },
        )

    root = ET.fromstring(resp.text)

    # SOAP fault check
    fault = next((e.text for e in root.iter() if e.tag.endswith("faultstring")), None)
    if fault:
        if "no encontrado" in fault.lower() or "not found" in fault.lower() or "inexistente" in fault.lower():
            raise RuntimeError("CUIT no encontrado en el padrón de ARCA.")
        raise RuntimeError(f"WSPadron: {fault}")

    # A13: <personaReturn><persona>...</persona></personaReturn>
    ret = next((e for e in root.iter() if e.tag.endswith("personaReturn")), None)
    if ret is None:
        raise RuntimeError("WSPadron: respuesta inesperada (sin personaReturn)")

    persona = next((e for e in ret if e.tag.endswith("persona")), ret)

    def text(tag, node=persona):
        el = next((e for e in node.iter() if e.tag.endswith(tag)), None)
        return (el.text or "").strip() if el is not None else ""

    tipo_persona = text("tipoPersona")
    estado       = text("estadoClave")

    if tipo_persona == "JURIDICA":
        nombre = text("razonSocial") or text("nombre")
    else:
        apellido = text("apellido")
        nombre_p = text("nombre")
        nombre   = f"{apellido}, {nombre_p}".strip(", ") if apellido else nombre_p

    # Domicilio fiscal: <domicilio> con <tipoDomicilio>FISCAL</tipoDomicilio>
    domicilio = ""
    for dom_el in persona.iter():
        if not dom_el.tag.endswith("domicilio"):
            continue
        tipo = next((e.text or "" for e in dom_el if e.tag.endswith("tipoDomicilio")), "")
        if "FISCAL" not in tipo.upper():
            continue
        calle     = next((e.text or "" for e in dom_el if e.tag.endswith("calle")),     "").strip()
        numero    = next((e.text or "" for e in dom_el if e.tag.endswith("numero")),    "").strip()
        localidad = next((e.text or "" for e in dom_el if e.tag.endswith("localidad")), "").strip()
        provincia = next((e.text or "" for e in dom_el if e.tag.endswith("descripcionProvincia")), "").strip()
        parts = [p for p in [f"{calle} {numero}".strip(), localidad, provincia] if p]
        domicilio = ", ".join(parts)
        break

    iva_condition = _detectar_iva(persona)

    return {
        "cuit":          cuit_c,
        "nombre":        nombre,
        "domicilio":     domicilio,
        "iva_condition": iva_condition,
        "estado":        estado,
    }


def _detectar_iva(persona: ET.Element) -> str:
    # A13: monotributo aparece como <categoriaMonotributo> o <monotributo>
    mono = next((e for e in persona.iter() if e.tag.endswith("categoriaMonotributo")), None)
    if mono is None:
        mono = next((e for e in persona.iter() if e.tag.endswith("monotributo")), None)
    if mono is not None:
        return "Monotributista"

    # Impuestos: mismos códigos que A4 (32=RI, 33=NR, 34=Exento)
    for imp in persona.iter():
        if not imp.tag.endswith("impuesto"):
            continue
        id_imp     = next((e.text for e in imp if e.tag.endswith("idImpuesto")), "") or ""
        estado_imp = next((e.text for e in imp if e.tag.endswith("estado")), "") or ""
        if estado_imp.upper() != "ACTIVO":
            continue
        if id_imp == "32":
            return "Responsable Inscripto"
        if id_imp == "34":
            return "IVA Exento"
        if id_imp == "33":
            return "IVA No Responsable"

    return "Consumidor Final"
