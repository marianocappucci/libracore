"""
Cliente WSFE (Facturación Electrónica v1) de ARCA/AFIP.
Solicita CAE y consulta el último comprobante autorizado.
"""

import re
import ssl
import xml.etree.ElementTree as ET

import httpx

from libracore import tipos_comprobante as tipos

WSFE_URL = {
    "homologacion": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx",
    "produccion":   "https://servicios1.afip.gov.ar/wsfev1/service.asmx",
}

_NS = "http://ar.gov.afip.dif.FEV1/"

# Mapeo porcentaje IVA → ID alicuota WSFE
_IVA_ID = {0: 3, 10: 4, 10.5: 4, 21: 5, 27: 6}


#: `cliente_iva_cond` —el código con que la familia guarda la condición del
#: receptor— → `CondicionIVAReceptorId` de ARCA (RG 5616).
#:
#: 🔴 **No son la misma tabla.** El `3` de la base es «IVA No Responsable» y
#: **ARCA ya no lo acepta como receptor**: «IVA No Alcanzado» es el `15`. Pasar
#: el código de la base derecho manda un id que ARCA rechaza.
_RECEPTOR_POR_COD = {1: 1, 3: 15, 4: 4, 5: 5, 6: 6}
#: Ids que ARCA reconoce y que la base nunca guardó: se aceptan tal cual.
_RECEPTOR_ARCA = {1, 4, 5, 6, 7, 8, 9, 10, 13, 15, 16}
#: Los comprobantes A (y FCE A): sólo se le emiten a inscriptos y monotributistas.
_TIPOS_A = {1, 2, 3, 201, 202, 203}


def condicion_iva_receptor_id(factura: dict) -> int:
    """El `CondicionIVAReceptorId` que ARCA exige en cada comprobante.

    🔴 **Desde la RG 5616 el WSFE rechaza el comprobante sin este dato.** Antes
    no se mandaba y alcanzaba.

    Sin condición guardada sólo se infiere donde no hay duda: un comprobante
    **sin CUIT del receptor** (DocTipo 99) es consumidor final. Con CUIT, o con
    un A, **no se adivina** —podría ser inscripto, monotributista, exento— y
    falla acá con un mensaje que dice qué hacer, en vez de mandar un dato
    inventado o dejar que ARCA conteste con un código. No hay valor por
    defecto silencioso.
    """
    cod = int(factura.get("cliente_iva_cond") or 0)
    if cod in _RECEPTOR_POR_COD:
        return _RECEPTOR_POR_COD[cod]
    if cod in _RECEPTOR_ARCA:
        return cod
    cuit = (factura.get("cliente_cuit") or "").replace("-", "").replace(" ", "")
    tiene_cuit = len(cuit) == 11 and cuit.isdigit()
    if not tiene_cuit and int(factura.get("tipo") or 0) not in _TIPOS_A:
        return 5
    raise RuntimeError(
        "WSFE: falta la condición de IVA del cliente, que ARCA exige en cada "
        "comprobante (RG 5616). Cargala en la ficha del cliente."
    )


def cuit_del_receptor(factura: dict) -> str:
    """El CUIT del receptor **sólo en dígitos**, o `""`.

    Se quitan guiones, puntos, espacios y todo lo que no sea un dígito: un CUIT cargado como
    `30.70933285.2` llegaba antes como «no es un CUIT» y el comprobante salía a consumidor final
    (o, en una clase A, rebotaba en ARCA con un error que no explica nada).
    """
    return re.sub(r"\D", "", str(factura.get("cliente_cuit") or ""))


def cuit_con_verificador_valido(digitos: str) -> bool:
    """¿Son 11 dígitos con el dígito verificador de AFIP/ARCA bien calculado?

    Es la regla pública del CUIT/CUIL (módulo 11, pesos 5-4-3-2-7-6-5-4-3-2). Que cierre **no**
    prueba que la CUIT exista en el padrón de ARCA; que no cierre prueba que no existe.
    """
    if len(digitos) != 11 or not digitos.isdigit():
        return False
    pesos = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    resto = sum(int(d) * p for d, p in zip(digitos[:10], pesos, strict=True)) % 11
    verificador = (11 - resto) % 11
    return int(digitos[10]) == (9 if verificador == 10 else verificador)


def problema_del_receptor(factura: dict) -> str | None:
    """Por qué el receptor de esta factura no sirve para emitirla por ARCA, o `None`.

    Es **la** guarda del CUIT de la familia: la usa `solicitar_cae` y la puede llamar un producto
    **antes** de pedirle el número a ARCA, para contestar con un 422 que dice qué cliente y qué
    cargar. Un producto no escribe su propia versión (`reglas/producto.md` del wiki).

    - Con **11 dígitos**, el verificador tiene que cerrar, en cualquier clase.
    - Las **clases A** y toda **FCE** exigen CUIT de 11 dígitos (DocTipo 80). Una B o una C sin
      CUIT, o con uno que no es de 11 dígitos, es un consumidor final y sale bien.

    Medido en homologación el 2026-10-03:
    - un CUIT `1` en una Factura A vuelve `[10013] DocTipo debe ser igual a 80` y
      `[10015] DocNro invalido`;
    - un CUIT de 11 dígitos con el verificador mal vuelve `[10015] … no se encuentra registrado en
      los padrones` en una **B**, pero en una **A ARCA autoriza con CAE** y sólo avisa (`10238`:
      «La CUIT receptora que ingresaste no existe. Tenes que emitir una Nota de Credito o anular
      la operacion»). Por eso la guarda **bloquea también la A**: una factura a un receptor que no
      existe se emite para anularla después.
    """
    crudo = str(factura.get("cliente_cuit") or "").strip()
    digitos = cuit_del_receptor(factura)
    razon = str(factura.get("cliente_razon") or "").strip()
    cliente = f"del cliente {razon!r}" if razon else "del cliente"
    if len(digitos) == 11:
        if cuit_con_verificador_valido(digitos):
            return None
        return (f"el CUIT {crudo!r} {cliente} no es válido (el dígito verificador no cierra): "
                "revisalo en la ficha del cliente")
    tipo = int(factura.get("tipo") or 0)
    if tipo not in _TIPOS_A and tipo not in tipos.FCE:
        return None
    que = "una FCE" if tipo in tipos.FCE else f"un comprobante clase {tipos.LETRA.get(tipo, 'A')}"
    cargado = f"tiene {crudo!r}" if crudo else "no tiene CUIT cargado"
    cliente_sujeto = f"el cliente {razon!r}" if razon else "el cliente"
    return (f"{cliente_sujeto} {cargado}, y {que} se emite a un receptor con CUIT de 11 dígitos: "
            "cargalo en la ficha del cliente antes de emitir por ARCA")


# Los tipos viven en `tipos_comprobante`; acá sólo se les da el nombre que usa este módulo.
TIPOS_FCE_FACTURA = tipos.FCE_FACTURA
TIPOS_FCE_NOTA = tipos.FCE_NOTA
TIPOS_FCE = tipos.FCE
_TIPOS_C = tipos.C   # C y FCE C: todo el importe va como neto, sin alícuotas de IVA


def _opcionales_fce(factura: dict, tipo: int) -> str:
    """El bloque `<Opcionales>` de una FCE (`""` si el tipo no es FCE).

    Una **factura** FCE lleva el CBU del emisor (id 2101, 22 dígitos) y la
    modalidad de transmisión (id 27: `SCA` o `ADC`). Una **nota** lleva **sólo**
    el id 22 —`S` si anula la factura, `N` si no—: con cualquiera de los otros
    ARCA contesta 10172. Medido en homologación el 2026-10-02.
    """
    if tipo in TIPOS_FCE_FACTURA:
        cbu = str(factura.get("fce_cbu") or "").strip()
        if not (len(cbu) == 22 and cbu.isdigit()):
            raise RuntimeError(
                "WSFE: la factura de crédito electrónica (FCE) exige el CBU del "
                "emisor, de 22 dígitos. Cargalo en la configuración de ARCA.")
        trans = str(factura.get("fce_transmision") or "").strip().upper()
        if trans not in ("SCA", "ADC"):
            raise RuntimeError(
                "WSFE: la FCE exige la modalidad de transmisión: SCA "
                "(circulación abierta) o ADC (agente de depósito colectivo).")
        return (
            "<Opcionales>"
            f"<Opcional><Id>2101</Id><Valor>{cbu}</Valor></Opcional>"
            f"<Opcional><Id>27</Id><Valor>{trans}</Valor></Opcional>"
            "</Opcionales>"
        )
    if tipo in TIPOS_FCE_NOTA:
        anula = str(factura.get("fce_anulacion") or "").strip().upper()
        if anula not in ("S", "N"):
            raise RuntimeError(
                "WSFE: una nota de una FCE exige indicar si anula la factura (S o N).")
        return f"<Opcionales><Opcional><Id>22</Id><Valor>{anula}</Valor></Opcional></Opcionales>"
    return ""


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
    # La fecha del comprobante asociado la exige una nota de FCE (10158).
    fecha = (factura.get("cbte_asoc_fecha") or "").replace("-", "")
    return (
        "<CbtesAsoc><CbteAsoc>"
        f"<Tipo>{asoc_tipo}</Tipo>"
        f"<PtoVta>{asoc_pv}</PtoVta>"
        f"<Nro>{asoc_nro}</Nro>"
        f"<Cuit>{cuit}</Cuit>"
        + (f"<CbteFch>{fecha}</CbteFch>" if fecha else "")
        + "</CbteAsoc></CbtesAsoc>"
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

    # Comprobantes tipo C (11=FC, 12=ND-C, 13=NC-C, y FCE C): todo el importe va como ImpNeto,
    # ImpOpEx debe ser 0, sin bloque de alícuotas de IVA
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

    # Receptor. La guarda va **antes de cualquier llamada a ARCA** y es la misma que usa un
    # producto para contestar antes de pedir el número.
    problema = problema_del_receptor(factura)
    if problema:
        raise RuntimeError(f"WSFE: {problema}")
    cuit_cli = cuit_del_receptor(factura)
    doc_tipo, doc_nro = (80, cuit_cli) if len(cuit_cli) == 11 else (99, 0)

    # 🔴 La FCE exige la fecha de vencimiento de pago **aunque el concepto sea
    # Productos** (sin ella, 10163), que es cuando el resto de los comprobantes
    # no la manda.
    if tipo in TIPOS_FCE_FACTURA and not (factura.get("fch_vto_pago") or "").strip():
        raise RuntimeError("WSFE: la FCE exige la fecha de vencimiento de pago.")

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
           f"<FchVtoPago>{fch_vto}</FchVtoPago>" if concepto_n in (2, 3)
           else f"<FchVtoPago>{fch_vto}</FchVtoPago>" if tipo in TIPOS_FCE_FACTURA else "")
        + f"<ImpTotal>{total:.2f}</ImpTotal>"
        + "<ImpTotConc>0.00</ImpTotConc>"
        + f"<ImpNeto>{imp_neto}</ImpNeto>"
        + f"<ImpOpEx>{imp_opex}</ImpOpEx>"
        + f"<ImpIVA>{imp_iva}</ImpIVA>"
        + "<ImpTrib>0.00</ImpTrib>"
        + "<MonId>PES</MonId><MonCotiz>1</MonCotiz>"
        + f"<CondicionIVAReceptorId>{condicion_iva_receptor_id(factura)}</CondicionIVAReceptorId>"
        + iva_block
        + _cbte_asoc_block(factura, empresa_cuit)
        + _opcionales_fce(factura, tipo)
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
