"""
Cliente WSFE (Facturación Electrónica v1) de ARCA/AFIP.
Solicita CAE y consulta el último comprobante autorizado.
"""

import ssl
import xml.etree.ElementTree as ET

import httpx

WSFE_URL = {
    "homologacion": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx",
    "produccion":   "https://servicios1.afip.gov.ar/wsfev1/service.asmx",
}

_NS = "http://ar.gov.afip.dif.FEV1/"

# Mapeo porcentaje IVA → ID alicuota WSFE
_IVA_ID = {0: 3, 10: 4, 10.5: 4, 21: 5, 27: 6}


def _ssl_ctx():
    """SSL context que acepta los parámetros DH legacy de los servidores ARCA."""
    ctx = ssl.create_default_context()
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    return ctx


def _iva_id(pct: float) -> int:
    return _IVA_ID.get(round(pct, 1), _IVA_ID.get(round(pct), 5))


async def _soap(url: str, action: str, body: str) -> ET.Element:
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<SOAP-ENV:Envelope '
        'xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<SOAP-ENV:Body>" + body + "</SOAP-ENV:Body>"
        "</SOAP-ENV:Envelope>"
    )
    async with httpx.AsyncClient(verify=_ssl_ctx(), timeout=30) as client:
        resp = await client.post(
            url,
            content=envelope.encode(),
            headers={
                "Content-Type": "text/xml; charset=UTF-8",
                "SOAPAction": f'"{_NS}{action}"',
            },
        )
    root = ET.fromstring(resp.text)
    fault = next((e.text for e in root.iter() if e.tag.endswith("faultstring")), None)
    if fault:
        raise RuntimeError(f"WSFE: {fault}")
    return root


def _cbte_asoc_block(factura: dict, empresa_cuit: str) -> str:
    asoc_tipo = int(factura.get("cbte_asoc_tipo") or 0)
    asoc_pv   = int(factura.get("cbte_asoc_pv")   or 0)
    asoc_nro  = int(factura.get("cbte_asoc_nro")  or 0)
    if not asoc_tipo or not asoc_nro:
        return ""
    cuit = empresa_cuit.replace("-", "")
    return (
        "<CbtesAsoc><CbteAsoc>"
        f"<Tipo>{asoc_tipo}</Tipo>"
        f"<PtoVta>{asoc_pv}</PtoVta>"
        f"<Nro>{asoc_nro}</Nro>"
        f"<Cuit>{cuit}</Cuit>"
        "</CbteAsoc></CbtesAsoc>"
    )


def _auth(token: str, sign: str, cuit: str) -> str:
    c = cuit.replace("-", "")
    return f"<Auth><Token>{token}</Token><Sign>{sign}</Sign><Cuit>{c}</Cuit></Auth>"


async def ultimo_numero_autorizado(
    punto_venta: int,
    tipo: int,
    empresa_cuit: str,
    token: str,
    sign: str,
    ambiente: str = "produccion",
) -> int:
    """Último comprobante autorizado por ARCA para tipo+punto_venta."""
    url = WSFE_URL.get(ambiente, WSFE_URL["produccion"])
    body = (
        f'<FECompUltimoAutorizado xmlns="{_NS}">'
        + _auth(token, sign, empresa_cuit)
        + f"<PtoVta>{punto_venta}</PtoVta><CbteTipo>{tipo}</CbteTipo>"
        + "</FECompUltimoAutorizado>"
    )
    root = await _soap(url, "FECompUltimoAutorizado", body)
    cbte = next((e.text for e in root.iter() if e.tag.endswith("CbteNro")), "0")
    return int(cbte or 0)


async def solicitar_cae(
    factura: dict,
    empresa_cuit: str,
    token: str,
    sign: str,
    ambiente: str = "produccion",
) -> dict:
    """
    Solicita CAE para una factura. Devuelve {"cae": ..., "cae_vto": ...}.
    Lanza RuntimeError con mensaje legible ante cualquier falla.
    """
    url = WSFE_URL.get(ambiente, WSFE_URL["produccion"])

    fecha       = (factura.get("fecha") or "").replace("-", "")  # YYYYMMDD
    concepto_n  = int(factura.get("concepto", 1))
    # Fechas de servicio — obligatorias cuando concepto es 2 o 3
    fch_desde   = (factura.get("fch_serv_desde") or fecha).replace("-", "")
    fch_hasta   = (factura.get("fch_serv_hasta") or fecha).replace("-", "")
    fch_vto     = (factura.get("fch_vto_pago")   or fecha).replace("-", "")
    sub      = float(factura.get("subtotal",   0))
    iva      = float(factura.get("iva_amount", 0))
    total    = float(factura.get("total",      0))
    tipo     = int(factura.get("tipo", 6))
    pct      = round(iva / sub * 100, 1) if sub > 0 else 0

    # Comprobantes tipo C (11=FC, 12=ND-C, 13=NC-C): todo el importe va como ImpNeto,
    # ImpOpEx debe ser 0, sin bloque de alícuotas de IVA
    _TIPOS_C = {11, 12, 13}
    if tipo in _TIPOS_C:
        imp_neto = f"{total:.2f}"
        imp_iva  = "0.00"
        imp_opex = "0.00"
    elif pct > 0:
        imp_neto = f"{sub:.2f}"
        imp_iva  = f"{iva:.2f}"
        imp_opex = "0.00"
    else:
        imp_neto = "0.00"
        imp_iva  = "0.00"
        imp_opex = f"{sub:.2f}"

    # Receptor
    cuit_cli = (factura.get("cliente_cuit") or "").replace("-", "").replace(" ", "")
    if len(cuit_cli) == 11 and cuit_cli.isdigit():
        doc_tipo, doc_nro = 80, cuit_cli
    else:
        doc_tipo, doc_nro = 99, 0

    # Bloque IVA — comprobantes C nunca llevan alícuotas
    iva_block = ""
    if tipo not in _TIPOS_C and pct > 0:
        aid = _iva_id(pct)
        iva_block = (
            "<Iva><AlicIva>"
            f"<Id>{aid}</Id>"
            f"<BaseImp>{imp_neto}</BaseImp>"
            f"<Importe>{imp_iva}</Importe>"
            "</AlicIva></Iva>"
        )

    body = (
        f'<FECAESolicitar xmlns="{_NS}">'
        + _auth(token, sign, empresa_cuit)
        + "<FeCAEReq><FeCabReq>"
        + "<CantReg>1</CantReg>"
        + f"<PtoVta>{factura['punto_venta']}</PtoVta>"
        + f"<CbteTipo>{factura['tipo']}</CbteTipo>"
        + "</FeCabReq><FeDetReq><FECAEDetRequest>"
        + f"<Concepto>{factura.get('concepto', 1)}</Concepto>"
        + f"<DocTipo>{doc_tipo}</DocTipo>"
        + f"<DocNro>{doc_nro}</DocNro>"
        + f"<CbteDesde>{factura['numero']}</CbteDesde>"
        + f"<CbteHasta>{factura['numero']}</CbteHasta>"
        + f"<CbteFch>{fecha}</CbteFch>"
        + (f"<FchServDesde>{fch_desde}</FchServDesde>"
           f"<FchServHasta>{fch_hasta}</FchServHasta>"
           f"<FchVtoPago>{fch_vto}</FchVtoPago>" if concepto_n in (2, 3) else "")
        + f"<ImpTotal>{total:.2f}</ImpTotal>"
        + "<ImpTotConc>0.00</ImpTotConc>"
        + f"<ImpNeto>{imp_neto}</ImpNeto>"
        + f"<ImpOpEx>{imp_opex}</ImpOpEx>"
        + f"<ImpIVA>{imp_iva}</ImpIVA>"
        + "<ImpTrib>0.00</ImpTrib>"
        + "<MonId>PES</MonId><MonCotiz>1</MonCotiz>"
        + iva_block
        + _cbte_asoc_block(factura, empresa_cuit)
        + "</FECAEDetRequest></FeDetReq></FeCAEReq>"
        + "</FECAESolicitar>"
    )

    root = await _soap(url, "FECAESolicitar", body)

    det = next((e for e in root.iter() if e.tag.endswith("FECAEDetResponse")), None)
    if det is None:
        raise RuntimeError("WSFE: respuesta sin FECAEDetResponse")

    resultado = next((e.text for e in det.iter() if e.tag.endswith("Resultado")), "") or ""

    if resultado == "A":
        cae     = next((e.text for e in det.iter() if e.tag.endswith("CAE")),       "") or ""
        cae_vto = next((e.text for e in det.iter() if e.tag.endswith("CAEFchVto")), "") or ""
        return {"cae": cae, "cae_vto": cae_vto}

    # Rechazado — recopilar observaciones y errores de todo el XML
    msgs = []
    for elem in root.iter():
        local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if local in ("Obs", "Err"):
            cod = ""
            msg = ""
            for c in elem:
                cl = c.tag.split("}")[-1] if "}" in c.tag else c.tag
                if cl in ("Code", "Codigo"):
                    cod = c.text or ""
                elif cl == "Msg":
                    msg = c.text or ""
            if msg:
                msgs.append(f"[{cod}] {msg}" if cod else msg)

    if not msgs:
        # fallback: include raw XML snippet for debugging
        raw = ET.tostring(root, encoding="unicode")[:800]
        raise RuntimeError(f"WSFE rechazó el comprobante (resultado={resultado}). XML: {raw}")

    raise RuntimeError("WSFE rechazó el comprobante: " + "; ".join(msgs))
