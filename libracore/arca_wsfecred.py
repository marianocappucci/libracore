"""
Cliente WSFECRED de ARCA: el **registro de Facturas de Crédito Electrónica** (FCE MiPyME). Sólo consultas (ADR-019).

WSFE **emite** la FCE; este servicio dice lo que pasa **después**, y que WSFE no sabe:

- si al receptor **le corresponde** FCE (`monto_obligado`): ARCA **no lo frena al emitir** —medido el 2026-10-05, WSFE
  autorizó una FCE a un receptor que el registro da como no obligado—, así que la regla la tiene que aplicar quien emite;
- en qué **estado** está la FCE y **cuánto queda** (`estado_de_fce`): ARCA le abre una cuenta corriente a cada FCE y lleva
  su saldo (medido: 1210 − una nota de 121 = 1089, igual que `notas_de_credito.saldo_acreditable`);
- su **historial** de estados (`historial`).

🔑 **Aceptar y rechazar no están acá, a propósito**: `aceptarFECred` y `rechazarFECred` los ejecuta el **comprador** sobre
la cuenta corriente de la FCE que le emitieron. La familia **emite**; lo que necesita es saber si el comprador la rechazó,
porque sólo entonces ARCA deja anularla entera (nota con anulación `S`; sin rechazo, `10154`).

Protocolo (medido en homologación, `WS-2.1.6`): SOAP *document*; el namespace va **sólo en el elemento raíz** del pedido y
los hijos van sin calificar; el orden de los campos importa (`sequence`). La autenticación es WSAA con el servicio
**`wsfecred`** (`arca_wsaa.autenticar(..., servicio=SERVICIO)`), y el certificado tiene que tener ese servicio
**autorizado**: si no, WSAA contesta `coe.notAuthorized` antes de llegar acá. Ver `docs/fce.md`.
"""

import datetime
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal
from xml.sax.saxutils import escape

import httpx

#: El servicio a pedirle a WSAA. Un ticket de `wsfe` no sirve para este.
SERVICIO = "wsfecred"

WSFECRED_URL = {
    "homologacion": "https://fwshomo.afip.gob.ar/wsfecred/FECredService",
    # La del WSDL público de producción (`soap:address`), verificada el 2026-10-05. Sin llamadas medidas todavía.
    "produccion":   "https://serviciosjava.afip.gov.ar/wsfecred/FECredService",
}

_NS = "http://ar.gob.afip.wsfecred/FECredService/"

#: Los errores de ARCA que quieren decir «esa FCE no está en el registro»: la cuenta corriente (`1102`) o el
#: comprobante (`1105`). Medidos el 2026-10-05.
_NO_REGISTRADA = {1102, 1105}


class ErrorWsfecred(RuntimeError):
    """ARCA contestó con errores (`arrayErrores`). `codigos` trae los pares `(codigo, descripcion)` tal cual."""

    def __init__(self, mensaje: str, codigos: list[tuple[int, str]]):
        super().__init__(mensaje)
        self.codigos = codigos


class FceNoRegistrada(ErrorWsfecred):
    """La FCE no está en el registro de ARCA (`1102` o `1105`): un número mal, otro CUIT emisor, u otro ambiente."""


@dataclass(frozen=True)
class MontoObligado:
    """Si el receptor está obligado a recibir FCE en esa fecha y, si lo está, desde qué monto."""

    obligado: bool
    monto_desde: Decimal | None

    def corresponde(self, total) -> bool:
        """Si a una factura de `total` (con IVA) le corresponde ser FCE para este receptor."""
        if not self.obligado:
            return False
        return self.monto_desde is None or Decimal(str(total)) >= self.monto_desde


@dataclass(frozen=True)
class NotaAsociada:
    tipo: int
    punto_venta: int
    numero: int
    total: Decimal
    es_anulacion: bool


@dataclass(frozen=True)
class EstadoFce:
    """La FCE vista por ARCA: su estado, el de su cuenta corriente y la plata."""

    cuenta: int
    estado: str             # PendienteRecepcion | Recepcionado | Aceptado | Rechazado | InformadaAgDpto
    estado_cuenta: str      # Modificable | Aceptada | Rechazada | CanceladaTotal | InformadaAgDpto
    importe_inicial: Decimal
    notas: Decimal          # lo que suman las notas de débito y crédito (negativo si acreditan)
    saldo: Decimal
    saldo_aceptado: Decimal | None
    notas_asociadas: tuple[NotaAsociada, ...]

    @property
    def rechazada(self) -> bool:
        """Si el comprador la rechazó: es lo único que habilita anularla entera (nota con `S`)."""
        return self.estado == "Rechazado" or self.estado_cuenta == "Rechazada"


# ── El transporte ──────────────────────────────────────────────────────────

def _ssl_ctx():
    """El mismo que WSFE: los servidores de ARCA negocian parámetros DH viejos."""
    ctx = ssl.create_default_context()
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    return ctx


def _xml(campos: dict) -> str:
    """Un dict (ordenado: el esquema es `sequence`) a XML **sin namespace** en los hijos. `None` se omite."""
    partes = []
    for nombre, valor in campos.items():
        if valor is None:
            continue
        adentro = _xml(valor) if isinstance(valor, dict) else escape(str(valor))
        partes.append(f"<{nombre}>{adentro}</{nombre}>")
    return "".join(partes)


def _digitos(cuit) -> str:
    return "".join(c for c in str(cuit) if c.isdigit())


async def _llamar(operacion: str, cuerpo: str, ambiente: str) -> ET.Element:
    """Manda `cuerpo` (ya armado, o vacío) y devuelve el elemento `…Return` de la respuesta."""
    url = WSFECRED_URL.get(ambiente, WSFECRED_URL["produccion"])
    envelope = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:fec="{_NS}"><soapenv:Body>{cuerpo}</soapenv:Body></soapenv:Envelope>'
    )
    async with httpx.AsyncClient(verify=_ssl_ctx(), timeout=30) as client:
        resp = await client.post(url, content=envelope.encode(), headers={
            "Content-Type": "text/xml; charset=utf-8", "SOAPAction": f"{_NS}{operacion}"})
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        # Medido: un pedido mal formado no vuelve como SOAP sino como una línea suelta del balanceador (`BL… 500`).
        raise RuntimeError(f"WSFECRED: respuesta que no es SOAP (HTTP {resp.status_code}): {resp.text[:200]}") from None
    fault = next((e.text for e in root.iter() if e.tag.endswith("faultstring")), None)
    if fault:
        raise RuntimeError(f"WSFECRED: {fault}")
    retorno = next((e for e in root.iter() if e.tag.endswith("Return")), None)
    if retorno is None:
        raise RuntimeError(f"WSFECRED: respuesta sin {operacion}Return")
    errores = [
        (int(_texto(cd, "codigo") or 0), _texto(cd, "descripcion"))
        for arr in retorno if arr.tag.endswith("arrayErrores") or arr.tag.endswith("arrayErroresFormato")
        for cd in arr
    ]
    if errores:
        mensaje = "WSFECRED: " + "; ".join(f"[{c}] {d}" for c, d in errores)
        clase = FceNoRegistrada if any(c in _NO_REGISTRADA for c, _ in errores) else ErrorWsfecred
        raise clase(mensaje, errores)
    return retorno


def _pedido(operacion: str, cuit_empresa: str, token: str, sign: str, campos: dict) -> str:
    auth = {"authRequest": {"token": token, "sign": sign, "cuitRepresentada": _digitos(cuit_empresa)}}
    return f"<fec:{operacion}Request>{_xml(auth | campos)}</fec:{operacion}Request>"


def _hijo(nodo: ET.Element | None, nombre: str) -> ET.Element | None:
    """El hijo **directo** con ese nombre (sin mirar el namespace). Directo: `estado` aparece en varios niveles."""
    if nodo is None:
        return None
    return next((e for e in nodo if e.tag.split("}")[-1] == nombre), None)


def _hijos(nodo: ET.Element | None, nombre: str) -> list[ET.Element]:
    """Los hijos del hijo `nombre` (un `array…`), o `[]`. Explícito: el valor de verdad de un `Element` engaña."""
    contenedor = _hijo(nodo, nombre)
    return list(contenedor) if contenedor is not None else []


def _texto(nodo: ET.Element | None, nombre: str) -> str:
    hijo = _hijo(nodo, nombre)
    return (hijo.text or "").strip() if hijo is not None else ""


def _importe(nodo, nombre) -> Decimal | None:
    texto = _texto(nodo, nombre)
    return Decimal(texto) if texto else None


# ── Las consultas ──────────────────────────────────────────────────────────

async def dummy(ambiente: str = "produccion") -> dict:
    """El estado de los tres servidores. Va con el `Body` **vacío**: su pedido no tiene partes en el WSDL."""
    retorno = await _llamar("dummy", "", ambiente)
    return {n: _texto(retorno, n) for n in ("appserver", "authserver", "dbserver")}


async def monto_obligado(
    cuit_empresa: str, cuit_receptor: str, fecha: datetime.date, token: str, sign: str, ambiente: str = "produccion",
) -> MontoObligado:
    """Si `cuit_receptor` está obligado a recibir FCE en `fecha`, y desde qué monto (`consultarMontoObligadoRecepcion`).

    La operación vieja (`consultarObligadoRecepcion`) está deprecada: ARCA la contesta con la observación `9998` y
    pide ésta, porque la obligación depende del monto y de la fecha.
    """
    retorno = await _llamar("consultarMontoObligadoRecepcion", _pedido(
        "consultarMontoObligadoRecepcion", cuit_empresa, token, sign,
        {"cuitConsultada": _digitos(cuit_receptor), "fechaEmision": fecha.isoformat()}), ambiente)
    return MontoObligado(obligado=_texto(retorno, "obligado") == "S", monto_desde=_importe(retorno, "montoDesde"))


def _id_comprobante(cuit_emisor, tipo, punto_venta, numero) -> dict:
    return {"CUITEmisor": _digitos(cuit_emisor), "codTipoCmp": int(tipo), "ptoVta": int(punto_venta),
            "nroCmp": int(numero)}


async def estado_de_fce(
    cuit_empresa: str, tipo: int, punto_venta: int, numero: int, token: str, sign: str, ambiente: str = "produccion",
) -> EstadoFce:
    """La FCE `tipo punto_venta-numero` **emitida por `cuit_empresa`**, vista por ARCA: estado, cuenta corriente, saldo.

    Una sola llamada: `consultarCtaCte` acepta la factura en lugar del código de la cuenta (es un `choice`), y trae la
    factura con su estado, sus notas asociadas y los importes. Levanta `FceNoRegistrada` si ARCA no la tiene.
    """
    retorno = await _llamar("consultarCtaCte", _pedido(
        "consultarCtaCte", cuit_empresa, token, sign,
        {"idCtaCte": {"idFactura": _id_comprobante(cuit_empresa, tipo, punto_venta, numero)}}), ambiente)
    cta = _hijo(retorno, "ctaCte")
    if cta is None:
        raise RuntimeError("WSFECRED: consultarCtaCte sin ctaCte ni errores")
    factura = _hijo(cta, "factura")
    notas = tuple(
        NotaAsociada(
            tipo=int(_texto(c, "codTipoCmp")), punto_venta=int(_texto(c, "ptovta")), numero=int(_texto(c, "nroCmp")),
            total=Decimal(_texto(c, "importeTotal") or "0"), es_anulacion=_texto(c, "esAnulacion") == "S",
        )
        for c in _hijos(cta, "arrayNotasDCAsociadas")
    )
    return EstadoFce(
        cuenta=int(_texto(cta, "codCtaCte")),
        estado=_texto(_hijo(factura, "estado"), "estado"),
        estado_cuenta=_texto(_hijo(cta, "estadoCtaCte"), "estado"),
        importe_inicial=_importe(cta, "importeInicial") or Decimal("0"),
        notas=_importe(cta, "importeTotalNotasDC") or Decimal("0"),
        saldo=_importe(cta, "saldo") or Decimal("0"),
        saldo_aceptado=_importe(cta, "saldoAceptado"),
        notas_asociadas=notas,
    )


async def historial(
    cuit_empresa: str, tipo: int, punto_venta: int, numero: int, token: str, sign: str, ambiente: str = "produccion",
) -> list[tuple[str, str]]:
    """Los estados por los que pasó la FCE, `(estado, fecha_hora)` en el orden en que los da ARCA."""
    retorno = await _llamar("consultarHistorialEstadosComprobante", _pedido(
        "consultarHistorialEstadosComprobante", cuit_empresa, token, sign,
        {"idComprobante": _id_comprobante(cuit_empresa, tipo, punto_venta, numero)}), ambiente)
    return [(_texto(e, "estado"), _texto(e, "fechaHoraEstado"))
            for e in _hijos(retorno, "arrayHistorialEstados")]
