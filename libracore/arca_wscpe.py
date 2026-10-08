"""
Cliente WSCPE de ARCA: la **Carta de Porte Electrónica** (CPE) automotor y su CTG. Sólo lectura (ADR-034).

Lo usa [[libracargo]] para traer de ARCA los datos de una CPE —kilos de carga y de descarga, chofer, pagador del flete,
origen, destino, estado y el PDF— a partir de su **CTG**. Emitir (`autorizarCPEAutomotor`) y el ciclo de vida (arribo,
desvío, anulación) quedan para otra etapa: son documentos fiscales y de circulación, y no entran por un módulo de
consultas.

🔑 **Por quién se consulta es un dato de cada llamada, nunca de la configuración.** El certificado puede estar a nombre
de una persona que actúa por la empresa, y la empresa —o un titular de granos que le delegó la emisión— va en
`cuitRepresentada`. ARCA sólo lo acepta si ese CUIT está **relacionado** con el alias del certificado para `wscpe` en
el Administrador de Relaciones; si no, contesta un `soap:Fault` («no esta relacionada con el conjunto {…}») que acá es
`CuitNoRelacionado`. Las relaciones viajan **dentro del ticket de WSAA** (`cuits_habilitados`): una delegación hecha
después de pedir el ticket no se ve hasta el ticket siguiente, y WSAA no da otro mientras el vigente no venza (≈12 h).

🔴 **No hay forma de listar «las CPE donde soy transportista».** El servicio contesta una CPE sólo si se conoce su CTG
o su tipo, sucursal y número de orden; las demás consultas son por planta de destino. Medido sobre el WSDL v2.2.0.

Protocolo (medido en homologación y producción, 2026-10-02 y 2026-10-07): SOAP *document*, el namespace `https://…`
(no `http://`, que es lo que mezcla el manual) va **sólo en el elemento raíz** del pedido y los hijos sin calificar.
El `dummy` (sin autenticación) está en `arca_servicios.dummy("wscpe", ambiente)`.
"""

import base64
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from xml.sax.saxutils import escape

import httpx

from libracore import arca_credenciales, arca_servicios, arca_wsaa

#: El servicio a pedirle a WSAA. Un ticket de `wsfe` no sirve para este.
SERVICIO = "wscpe"

_CATALOGO = arca_servicios.servicio(SERVICIO)
WSCPE_URL = _CATALOGO.endpoints
_NS = _CATALOGO.namespace

#: `tipoCPE` de la carta de porte automotor de granos.
TIPO_AUTOMOTOR = 74

#: ARCA informa las fechas sin zona; son de la Argentina.
_AR = timezone(timedelta(hours=-3))

#: «No existen solicitudes para los parámetros indicados.» Medido el 2026-10-07: un CTG que no existe, o una CPE en la
#: que el CUIT representado no interviene, dan el mismo error.
_NO_ENCONTRADA = {800}

#: Los estados que nombra el manual v2.2.0. ARCA puede contestar otros: el estado viaja siempre tal cual.
ESTADOS = {
    "AC": "Activa",
    "CF": "Activa con confirmación de arribo",
    "CN": "Confirmada",
    "DD": "Descargada",
    "CO": "Con contingencia",
    "AN": "Anulada",
    "RE": "Rechazada",
    "DE": "Desactivada",
}


class ErrorWscpe(RuntimeError):
    """ARCA contestó con errores (`errores/error`). `codigos` trae los pares `(codigo, descripcion)` tal cual."""

    def __init__(self, mensaje: str, codigos: list[tuple[int, str]]):
        super().__init__(mensaje)
        self.codigos = codigos


class CpeNoEncontrada(ErrorWscpe):
    """ARCA no devuelve esa CPE (`800`): el CTG no existe, es de otro ambiente, o el CUIT representado no interviene."""


class CuitNoRelacionado(ErrorWscpe):
    """El CUIT representado no está relacionado con el certificado para `wscpe` (falta la delegación en ARCA)."""


class RespuestaNoSoap(RuntimeError):
    """ARCA (o su balanceador) contestó algo que no es SOAP: un 502, una línea suelta. No se sabe qué pasó adentro."""


class SinCredenciales(RuntimeError):
    """No hay certificado y clave de `wscpe` cargados para esa empresa y ese ambiente."""


@dataclass(frozen=True)
class Origen:
    cuit: str | None
    cod_provincia: int | None
    cod_localidad: int | None
    planta: int | None
    renspa: str


@dataclass(frozen=True)
class Destino:
    cuit: str | None
    cod_provincia: int | None
    cod_localidad: int | None
    planta: int | None
    cuit_destinatario: str | None


@dataclass(frozen=True)
class Carga:
    """Kilos y grano. Los de descarga llegan **después**: hasta el arribo vienen vacíos y hay que volver a consultar."""

    cod_grano: int | None
    cosecha: int | None
    peso_bruto: int | None
    peso_tara: int | None
    peso_bruto_descarga: int | None
    peso_tara_descarga: int | None

    @property
    def peso_neto(self) -> int | None:
        return _neto(self.peso_bruto, self.peso_tara)

    @property
    def peso_neto_descarga(self) -> int | None:
        return _neto(self.peso_bruto_descarga, self.peso_tara_descarga)


@dataclass(frozen=True)
class Transporte:
    cuit_transportista: str | None
    dominios: tuple[str, ...]
    fecha_hora_partida: datetime | None
    km: int | None
    cuit_chofer: str | None
    tarifa_referencia: Decimal | None
    tarifa: Decimal | None
    cuit_pagador_flete: str | None
    cuit_intermediario_flete: str | None
    mercaderia_fumigada: bool | None


@dataclass(frozen=True)
class CartaDePorte:
    """Una CPE automotor como la devuelve ARCA, con lo que la familia usa tipado y el resto en `respuesta_xml`."""

    nro_ctg: int
    tipo_cpe: int | None
    sucursal: int | None
    nro_orden: int | None
    estado: str
    fecha_emision: datetime | None
    fecha_inicio_estado: datetime | None
    fecha_vencimiento: datetime | None
    observaciones: str
    origen: Origen
    destino: Destino
    carga: Carga
    transporte: Transporte
    #: Los CUIT de la comercialización (remitentes, corredores, mercado a término…), `nombre del campo -> CUIT`.
    intervinientes: dict[str, str]
    pdf: bytes | None
    #: La `respuesta` completa **sin el PDF**, para archivar lo que hoy no se usa.
    respuesta_xml: str

    @property
    def numero(self) -> str:
        """El N.º de CPE como se imprime: `00001-00072413`."""
        if self.sucursal is None or self.nro_orden is None:
            return ""
        return f"{self.sucursal:05d}-{self.nro_orden:08d}"

    @property
    def estado_descripcion(self) -> str:
        return ESTADOS.get(self.estado, self.estado)

    @property
    def tiene_descarga(self) -> bool:
        """Si ARCA ya informa los kilos de descarga (lo que liquida el flete)."""
        return self.carga.peso_bruto_descarga is not None


def _neto(bruto: int | None, tara: int | None) -> int | None:
    return None if bruto is None or tara is None else bruto - tara


# ── El ticket ──────────────────────────────────────────────────────────────

def cuits_habilitados(ticket: dict) -> tuple[str, ...]:
    """Los CUIT por los que el ticket deja operar: las `relation` que WSAA graba en el token.

    Es lo que ARCA compara con `cuitRepresentada`. Sirve para decir **antes** de consultar si la delegación ya está
    (medido en producción el 2026-10-07: el `soap:Fault` de un CUIT no relacionado nombra este mismo conjunto).
    Un token que no se puede leer da `()`: no es un error, es «no se sabe».
    """
    try:
        raiz = ET.fromstring(base64.b64decode(ticket.get("token") or ""))
    except (ValueError, ET.ParseError):
        return ()
    return tuple(dict.fromkeys(
        e.get("key", "") for e in raiz.iter() if e.tag.split("}")[-1] == "relation" and e.get("key")))


# ── El transporte ──────────────────────────────────────────────────────────

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


def _pedido(elemento: str, cuit_representada, token: str, sign: str, campos: dict) -> str:
    cuit = _digitos(cuit_representada)
    if len(cuit) != 11:
        raise ValueError(f"WSCPE: el CUIT representado tiene que tener 11 dígitos, no «{cuit_representada}»")
    auth = {"auth": {"token": token, "sign": sign, "cuitRepresentada": cuit}}
    return f"<wsc:{elemento}>{_xml(auth | campos)}</wsc:{elemento}>"


async def _llamar(operacion: str, cuerpo: str, ambiente: str) -> ET.Element:
    """Manda `cuerpo` y devuelve el elemento `respuesta`. Levanta con los errores de ARCA tipados."""
    url = WSCPE_URL.get(ambiente)
    if not url:
        raise ValueError(f"WSCPE: ambiente desconocido «{ambiente}»")
    sobre = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:wsc="{_NS}"><soapenv:Header/><soapenv:Body>{cuerpo}</soapenv:Body></soapenv:Envelope>'
    )
    async with httpx.AsyncClient(verify=arca_servicios._ssl_ctx(), timeout=60) as client:
        resp = await client.post(url, content=sobre.encode(), headers={
            "Content-Type": "text/xml; charset=UTF-8", "SOAPAction": f'"{_NS}{operacion}"'})
    try:
        raiz = ET.fromstring(resp.text)
    except ET.ParseError:
        raise RespuestaNoSoap(
            f"WSCPE: respuesta que no es SOAP (HTTP {resp.status_code}): {resp.text[:200]}") from None
    fault = next((e.text or "" for e in raiz.iter() if e.tag.split("}")[-1] == "faultstring"), None)
    if fault is not None:
        if "no esta relacionada" in fault.lower() or "no está relacionada" in fault.lower():
            raise CuitNoRelacionado(
                "WSCPE: el CUIT representado no está relacionado con este certificado para el servicio wscpe. "
                "Quien es titular de ese CUIT tiene que delegar «wscpe» al alias del certificado en el Administrador "
                "de Relaciones de ARCA; si lo acaba de hacer, ARCA lo toma con el próximo ticket (hasta 12 horas). "
                f"(ARCA dijo: {fault})", [])
        raise ErrorWscpe(f"WSCPE: {fault}", [])
    respuesta = next((e for e in raiz.iter() if e.tag.split("}")[-1] == "respuesta"), None)
    if respuesta is None:
        raise RuntimeError(f"WSCPE: {operacion} sin respuesta ni errores")
    errores = [
        (_entero(_texto(e, "codigo")) or 0, _texto(e, "descripcion"))
        for e in _hijos(respuesta, "errores")
    ]
    if errores:
        mensaje = "WSCPE: " + "; ".join(f"[{c}] {d}" for c, d in errores)
        clase = CpeNoEncontrada if any(c in _NO_ENCONTRADA for c, _ in errores) else ErrorWscpe
        raise clase(mensaje, errores)
    return respuesta


def _hijo(nodo: ET.Element | None, nombre: str) -> ET.Element | None:
    """El hijo **directo** con ese nombre. Directo: `cuit` aparece en origen, destino y destinatario."""
    if nodo is None:
        return None
    return next((e for e in nodo if e.tag.split("}")[-1] == nombre), None)


def _hijos(nodo: ET.Element | None, nombre: str) -> list[ET.Element]:
    contenedor = _hijo(nodo, nombre)
    return list(contenedor) if contenedor is not None else []


def _texto(nodo: ET.Element | None, nombre: str) -> str:
    hijo = _hijo(nodo, nombre)
    return (hijo.text or "").strip() if hijo is not None else ""


def _entero(texto: str) -> int | None:
    return int(texto) if texto.lstrip("-").isdigit() else None


def _cuit(nodo, nombre) -> str | None:
    """ARCA manda los CUIT como `long`: se devuelven como texto de 11 dígitos."""
    n = _entero(_texto(nodo, nombre))
    return f"{n:011d}" if n else None


def _decimal(nodo, nombre) -> Decimal | None:
    texto = _texto(nodo, nombre)
    return Decimal(texto) if texto else None


def _fecha(nodo, nombre) -> datetime | None:
    texto = _texto(nodo, nombre)
    if not texto:
        return None
    fecha = datetime.fromisoformat(texto)
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=_AR)


def _booleano(nodo, nombre) -> bool | None:
    texto = _texto(nodo, nombre).lower()
    return {"true": True, "false": False}.get(texto)


# ── Las consultas ──────────────────────────────────────────────────────────

async def provincias(cuit_representada, token: str, sign: str, ambiente: str = "produccion") -> dict[int, str]:
    """`codigo -> nombre` de las provincias. Es la llamada autenticada más barata: prueba una representación."""
    respuesta = await _llamar("consultarProvincias", _pedido(
        "ConsultarProvinciasReq", cuit_representada, token, sign, {}), ambiente)
    return {int(_texto(p, "codigo")): _texto(p, "descripcion")
            for p in respuesta if p.tag.split("}")[-1] == "provincia"}


async def consultar_cpe(
    cuit_representada, token: str, sign: str, *, ctg: int | None = None, tipo_cpe: int | None = None,
    sucursal: int | None = None, nro_orden: int | None = None, cuit_solicitante=None, ambiente: str = "produccion",
) -> CartaDePorte:
    """Una CPE automotor por su `ctg`, o por `tipo_cpe` + `sucursal` + `nro_orden` (`consultarCPEAutomotor`).

    `cuit_solicitante` es opcional en el WSDL; sin él ARCA contesta a cualquier interviniente. Levanta
    `CpeNoEncontrada` si ARCA no la tiene para ese CUIT y `CuitNoRelacionado` si falta la delegación.
    """
    if ctg is None and None in (tipo_cpe, sucursal, nro_orden):
        raise ValueError("WSCPE: hace falta el CTG, o el tipo, la sucursal y el número de orden de la CPE")
    solicitud = {
        "cuitSolicitante": _digitos(cuit_solicitante) if cuit_solicitante else None,
        "cartaPorte": (None if ctg is not None else
                       {"tipoCPE": int(tipo_cpe), "sucursal": int(sucursal), "nroOrden": int(nro_orden)}),
        "nroCTG": int(ctg) if ctg is not None else None,
    }
    respuesta = await _llamar("consultarCPEAutomotor", _pedido(
        "ConsultarCPEAutomotorReq", cuit_representada, token, sign, {"solicitud": solicitud}), ambiente)
    return _carta_de_porte(respuesta)


def _carta_de_porte(respuesta: ET.Element) -> CartaDePorte:
    cab = _hijo(respuesta, "cabecera")
    if cab is None or _entero(_texto(cab, "nroCTG")) is None:
        raise RuntimeError("WSCPE: consultarCPEAutomotor sin cabecera ni errores")
    origen, destino = _hijo(respuesta, "origen"), _hijo(respuesta, "destino")
    carga, transporte = _hijo(respuesta, "datosCarga"), _hijo(respuesta, "transporte")
    intervinientes = {
        e.tag.split("}")[-1]: f"{int(e.text):011d}"
        for e in _hijos(respuesta, "intervinientes") if (e.text or "").strip().isdigit()
    }
    pdf_texto = _texto(respuesta, "pdf")
    sin_pdf = ET.Element(respuesta.tag, respuesta.attrib)
    sin_pdf.extend(e for e in respuesta if e.tag.split("}")[-1] != "pdf")
    return CartaDePorte(
        nro_ctg=int(_texto(cab, "nroCTG")),
        tipo_cpe=_entero(_texto(cab, "tipoCartaPorte")),
        sucursal=_entero(_texto(cab, "sucursal")),
        nro_orden=_entero(_texto(cab, "nroOrden")),
        estado=_texto(cab, "estado"),
        fecha_emision=_fecha(cab, "fechaEmision"),
        fecha_inicio_estado=_fecha(cab, "fechaInicioEstado"),
        fecha_vencimiento=_fecha(cab, "fechaVencimiento"),
        observaciones=_texto(cab, "observaciones"),
        origen=Origen(
            cuit=_cuit(origen, "cuit"), cod_provincia=_entero(_texto(origen, "codProvincia")),
            cod_localidad=_entero(_texto(origen, "codLocalidad")), planta=_entero(_texto(origen, "planta")),
            renspa=_texto(origen, "nroRenspa"),
        ),
        destino=Destino(
            cuit=_cuit(destino, "cuit"), cod_provincia=_entero(_texto(destino, "codProvincia")),
            cod_localidad=_entero(_texto(destino, "codLocalidad")), planta=_entero(_texto(destino, "planta")),
            cuit_destinatario=_cuit(_hijo(respuesta, "destinatario"), "cuit"),
        ),
        carga=Carga(
            cod_grano=_entero(_texto(carga, "codGrano")), cosecha=_entero(_texto(carga, "cosecha")),
            peso_bruto=_entero(_texto(carga, "pesoBruto")), peso_tara=_entero(_texto(carga, "pesoTara")),
            peso_bruto_descarga=_entero(_texto(carga, "pesoBrutoDescarga")),
            peso_tara_descarga=_entero(_texto(carga, "pesoTaraDescarga")),
        ),
        transporte=Transporte(
            cuit_transportista=_cuit(transporte, "cuitTransportista"),
            dominios=tuple((e.text or "").strip() for e in (transporte if transporte is not None else [])
                           if e.tag.split("}")[-1] == "dominio" and (e.text or "").strip()),
            fecha_hora_partida=_fecha(transporte, "fechaHoraPartida"),
            km=_entero(_texto(transporte, "kmRecorrer")),
            cuit_chofer=_cuit(transporte, "cuitChofer"),
            tarifa_referencia=_decimal(transporte, "tarifaReferencia"),
            tarifa=_decimal(transporte, "tarifa"),
            cuit_pagador_flete=_cuit(transporte, "cuitPagadorFlete"),
            cuit_intermediario_flete=_cuit(transporte, "cuitIntermediarioFlete"),
            mercaderia_fumigada=_booleano(transporte, "mercaderiaFumigada"),
        ),
        intervinientes=intervinientes,
        pdf=base64.b64decode(pdf_texto) if pdf_texto else None,
        respuesta_xml=ET.tostring(sin_pdf, encoding="unicode"),
    )


# ── De punta a punta, con las credenciales del producto (ADR-032) ──────────

async def autenticar(empresa: str, ambiente: str) -> dict:
    """El ticket de WSAA para `wscpe` con el par de `arca_credenciales_servicio`. Reusa la caché de disco."""
    cert_path, clave_path = arca_credenciales.paths_en_disco_de_servicio(empresa, SERVICIO, ambiente)
    if not cert_path or not clave_path:
        raise SinCredenciales(
            f"No hay certificado y clave de «CTG y Carta de Porte» cargados para {ambiente}: "
            "cargalos en Configuración / ARCA.")
    try:
        return await arca_wsaa.autenticar(cert_path, clave_path, ambiente, servicio=SERVICIO)
    except RuntimeError as e:
        raise RuntimeError(arca_servicios.traducir_error_wsaa(str(e))) from e


async def consultar_por_ctg(empresa: str, ambiente: str, *, cuit_representada, ctg: int) -> CartaDePorte:
    """La CPE `ctg`, consultada **por `cuit_representada`**, que se pasa siempre explícito: no tiene valor por defecto.

    Con dos CUIT posibles (la persona del certificado y la empresa, más cada titular que delegue) un default es
    consultar o, el día que se emita, firmar con el contribuyente equivocado.
    """
    ticket = await autenticar(empresa, ambiente)
    return await consultar_cpe(cuit_representada, ticket["token"], ticket["sign"], ctg=ctg, ambiente=ambiente)


# ── Catálogos de ARCA ──────────────────────────────────────────────────────
#
# Los códigos que pide la emisión (grano, localidad, planta) son los **de ARCA**, no los de los maestros de un
# producto. Se consultan acá y el producto los guarda junto a los suyos.

def _codigos(respuesta: ET.Element, nombre: str) -> dict[int, str]:
    return {int(_texto(e, "codigo")): _texto(e, "descripcion")
            for e in respuesta if e.tag.split("}")[-1] == nombre and _entero(_texto(e, "codigo")) is not None}


async def tipos_grano(cuit_representada, token: str, sign: str, ambiente: str = "produccion") -> dict[int, str]:
    """`código -> nombre` de los granos (`consultarTiposGrano`). Medido: 39 en homologación (Soja = 23)."""
    respuesta = await _llamar("consultarTiposGrano", _pedido(
        "ConsultarTiposGranoReq", cuit_representada, token, sign, {}), ambiente)
    return _codigos(respuesta, "grano")


async def localidades(cuit_representada, token: str, sign: str, *, cod_provincia: int,
                      ambiente: str = "produccion") -> dict[int, str]:
    """`código -> nombre` de las localidades de una provincia de ARCA (`consultarLocalidadesPorProvincia`)."""
    respuesta = await _llamar("consultarLocalidadesPorProvincia", _pedido(
        "ConsultarLocalidadesPorProvinciaReq", cuit_representada, token, sign,
        {"solicitud": {"codProvincia": int(cod_provincia)}}), ambiente)
    return _codigos(respuesta, "localidad")


@dataclass(frozen=True)
class Planta:
    numero: int
    cod_provincia: int | None
    cod_localidad: int | None


async def plantas(cuit_representada, token: str, sign: str, *, cuit, ambiente: str = "produccion") -> list[Planta]:
    """Las plantas inscriptas de `cuit` (`consultarPlantas`). Sin plantas, ARCA contesta `800`: acá es `[]`."""
    try:
        respuesta = await _llamar("consultarPlantas", _pedido(
            "ConsultarPlantasReq", cuit_representada, token, sign, {"solicitud": {"cuit": _digitos(cuit)}}), ambiente)
    except CpeNoEncontrada:
        return []
    return [Planta(numero=int(_texto(p, "nroPlanta")), cod_provincia=_entero(_texto(p, "codProvincia")),
                   cod_localidad=_entero(_texto(p, "codLocalidad")))
            for p in respuesta if p.tag.split("}")[-1] == "planta" and _entero(_texto(p, "nroPlanta")) is not None]


async def ultimo_nro_orden(cuit_representada, token: str, sign: str, *, sucursal: int,
                           tipo_cpe: int = TIPO_AUTOMOTOR, ambiente: str = "produccion") -> int:
    """El último número de orden autorizado para (sucursal, tipo) de `cuit_representada` (`consultarUltNroOrden`).

    La numeración la lleva ARCA por solicitante, sucursal y tipo. Medido en homologación: `0` sin ninguna emitida.
    """
    respuesta = await _llamar("consultarUltNroOrden", _pedido(
        "ConsultarUltNroOrdenReq", cuit_representada, token, sign,
        {"solicitud": {"sucursal": int(sucursal), "tipoCPE": int(tipo_cpe)}}), ambiente)
    numero = _entero(_texto(respuesta, "nroOrden"))
    if numero is None:
        raise RuntimeError("WSCPE: consultarUltNroOrden sin nroOrden ni errores")
    return numero


# ── Emitir (ADR-035) ───────────────────────────────────────────────────────

#: El orden de los intervinientes en el esquema (`IntervinientesSolicitud` es `sequence`).
_ORDEN_INTERVINIENTES = (
    "cuitRemitenteComercialVentaPrimaria", "cuitRemitenteComercialVentaSecundaria",
    "cuitRemitenteComercialVentaSecundaria2", "cuitMercadoATermino", "cuitCorredorVentaPrimaria",
    "cuitCorredorVentaSecundaria", "cuitRepresentanteEntregador", "cuitRepresentanteRecibidor",
)


@dataclass(frozen=True)
class OrigenPlanta:
    """La carga sale de una planta inscripta (acopio, por ejemplo) del solicitante."""

    cod_provincia: int
    cod_localidad: int
    planta: int


@dataclass(frozen=True)
class OrigenCampo:
    """La carga sale de un campo. ARCA exige que el solicitante tenga actividad de productor (error `1015`)."""

    cod_provincia: int
    cod_localidad: int
    renspa: str | None = None


@dataclass(frozen=True)
class TransporteSolicitud:
    cuit_transportista: str
    dominios: tuple[str, ...]
    fecha_hora_partida: datetime
    km: int
    cuit_chofer: str
    cuit_pagador_flete: str
    mercaderia_fumigada: bool = False
    tarifa: Decimal | None = None
    cuit_intermediario_flete: str | None = None
    codigo_turno: str | None = None


@dataclass(frozen=True)
class DestinoSolicitud:
    cuit: str
    cod_provincia: int
    cod_localidad: int
    planta: int | None = None
    es_campo: bool = False


@dataclass(frozen=True)
class SolicitudCpe:
    """Todo lo que pide `autorizarCPEAutomotor` menos el número de orden, que lo pone `emitir_cpe`.

    Los campos y su orden siguen el WSDL v2.2.0 (`AutorizarAutomotorSolicitud`). Los rangos del esquema se validan
    acá (`problemas`) para que un dato fuera de rango no viaje a ARCA.
    """

    cuit_solicitante: str
    sucursal: int
    origen: OrigenPlanta | OrigenCampo
    cod_grano: int
    cosecha: int
    peso_bruto: int
    peso_tara: int
    destino: DestinoSolicitud
    cuit_destinatario: str
    transporte: TransporteSolicitud
    #: Cuando el productor retira: el CUIT del remitente comercial productor.
    cuit_remitente_comercial_productor: str | None = None
    #: `cuitRemitenteComercialVentaPrimaria`, `cuitCorredorVentaPrimaria`… con el nombre del WSDL.
    intervinientes: dict[str, str] | None = None
    observaciones: str | None = None
    tipo_cpe: int = TIPO_AUTOMOTOR

    def problemas(self) -> list[str]:
        """Lo que ARCA rechazaría por el esquema, dicho antes de llamar. Vacío si está todo en rango."""
        p = []
        cuits = {"del solicitante": self.cuit_solicitante, "del destino": self.destino.cuit,
                 "del destinatario": self.cuit_destinatario, "del transportista": self.transporte.cuit_transportista,
                 "del chofer": self.transporte.cuit_chofer, "del pagador del flete": self.transporte.cuit_pagador_flete}
        for nombre, cuit in cuits.items():
            if len(_digitos(cuit)) != 11:
                p.append(f"el CUIT {nombre} tiene que tener 11 dígitos")
        for nombre, kg in (("bruto", self.peso_bruto), ("tara", self.peso_tara)):
            if not 1 <= kg <= 88000:
                p.append(f"el peso {nombre} tiene que estar entre 1 y 88.000 kg")
        if self.peso_tara >= self.peso_bruto:
            p.append("la tara tiene que ser menor que el peso bruto")
        if not 0 <= self.cosecha <= 9999:
            p.append("la cosecha es un número de 4 cifras (2526 = 2025/2026)")
        if not 1 <= self.transporte.km <= 99999:
            p.append("los kilómetros a recorrer tienen que estar entre 1 y 99.999")
        if not 1 <= len(self.transporte.dominios) <= 3:
            p.append("hacen falta entre 1 y 3 dominios (chasis y acoplados)")
        for d in self.transporte.dominios:
            if not 6 <= len(d) <= 7:
                p.append(f"el dominio «{d}» tiene que tener 6 o 7 caracteres")
        t = self.transporte.tarifa
        if t is not None and not Decimal(0) <= t <= Decimal("99999.99"):
            p.append("la tarifa tiene que estar entre 0 y 99.999,99")
        if self.observaciones and len(self.observaciones) > 2000:
            p.append("las observaciones tienen hasta 2.000 caracteres")
        return p

    def _campos(self, nro_orden: int) -> dict:
        o, d, t = self.origen, self.destino, self.transporte
        if isinstance(o, OrigenPlanta):
            origen = {"operador": {"codProvincia": o.cod_provincia, "codLocalidad": o.cod_localidad,
                                   "planta": o.planta}}
        else:
            origen = {"productor": {"codProvincia": o.cod_provincia, "codLocalidad": o.cod_localidad,
                                    "nroRenspa": o.renspa}}
        retira = self.cuit_remitente_comercial_productor is not None
        dados = self.intervinientes or {}
        desconocidos = set(dados) - set(_ORDEN_INTERVINIENTES)
        if desconocidos:
            raise ValueError(f"WSCPE: intervinientes que el esquema no tiene: {sorted(desconocidos)}")
        intervinientes = {k: _digitos(dados[k]) for k in _ORDEN_INTERVINIENTES if dados.get(k)}
        return {
            "cabecera": {"tipoCP": self.tipo_cpe, "cuitSolicitante": _digitos(self.cuit_solicitante),
                         "sucursal": self.sucursal, "nroOrden": nro_orden},
            "origen": origen,
            "correspondeRetiroProductor": "true" if retira else "false",
            # 🔑 No es una elección: sale del origen. Medido en homologación (2026-10-08): con origen en campo y
            # `false`, ARCA contesta 949 («el campo productor no se debe informar»); con `true` llega a validar
            # que el solicitante sea productor (1015). Con planta va en `false`.
            "esSolicitanteCampo": "true" if isinstance(o, OrigenCampo) else "false",
            "retiroProductor": ({"cuitRemitenteComercialProductor": _digitos(self.cuit_remitente_comercial_productor)}
                                if retira else None),
            "intervinientes": intervinientes or None,
            "datosCarga": {"codGrano": self.cod_grano, "cosecha": self.cosecha,
                           "pesoBruto": self.peso_bruto, "pesoTara": self.peso_tara},
            "destino": {"cuit": _digitos(d.cuit), "esDestinoCampo": "true" if d.es_campo else "false",
                        "codProvincia": d.cod_provincia, "codLocalidad": d.cod_localidad, "planta": d.planta},
            "destinatario": {"cuit": _digitos(self.cuit_destinatario)},
            "transporte": {
                "cuitTransportista": _digitos(t.cuit_transportista),
                # `dominio` se repite: va aparte, ver `_xml_transporte`.
                "fechaHoraPartida": t.fecha_hora_partida.astimezone(_AR).strftime("%Y-%m-%dT%H:%M:%S"),
                "kmRecorrer": t.km, "codigoTurno": t.codigo_turno, "cuitChofer": _digitos(t.cuit_chofer),
                "tarifa": f"{t.tarifa:.2f}" if t.tarifa is not None else None,
                "cuitPagadorFlete": _digitos(t.cuit_pagador_flete),
                "cuitIntermediarioFlete": _digitos(t.cuit_intermediario_flete) if t.cuit_intermediario_flete else None,
                "mercaderiaFumigada": "true" if t.mercaderia_fumigada else "false",
            },
            "observaciones": self.observaciones or None,
        }

    def xml(self, nro_orden: int) -> str:
        """El `<solicitud>` en el orden del esquema, con los `dominio` repetidos después del transportista."""
        campos = self._campos(nro_orden)
        transporte = campos.pop("transporte")
        dominios = "".join(f"<dominio>{escape(d.upper())}</dominio>" for d in self.transporte.dominios)
        cuerpo_transporte = (f"<cuitTransportista>{transporte.pop('cuitTransportista')}</cuitTransportista>"
                             f"{dominios}{_xml(transporte)}")
        antes = {k: v for k, v in campos.items() if k != "observaciones"}
        return (f"<solicitud>{_xml(antes)}<transporte>{cuerpo_transporte}</transporte>"
                f"{_xml({'observaciones': campos['observaciones']})}</solicitud>")


class EmisionIncierta(RuntimeError):
    """ARCA no contestó al autorizar y tampoco se pudo saber si la carta quedó emitida.

    🔴 **No se reintenta**: reintentar con el mismo número puede chocar con una carta emitida, y con el siguiente
    puede dejar dos cartas para el mismo viaje. Hay que consultar (`consultar_cpe` por tipo, sucursal y número)
    antes de volver a emitir. `nro_orden` es el número que se pidió.
    """

    def __init__(self, mensaje: str, *, sucursal: int, nro_orden: int, tipo_cpe: int):
        super().__init__(mensaje)
        self.sucursal, self.nro_orden, self.tipo_cpe = sucursal, nro_orden, tipo_cpe


class SolicitudInvalida(ValueError):
    """La solicitud tiene datos fuera de lo que admite el esquema de ARCA. `problemas` dice cuáles."""

    def __init__(self, problemas: list[str]):
        super().__init__("; ".join(problemas))
        self.problemas = problemas


async def autorizar_cpe(cuit_representada, token: str, sign: str, solicitud: SolicitudCpe, *, nro_orden: int,
                        ambiente: str = "produccion") -> CartaDePorte:
    """`autorizarCPEAutomotor` con un número de orden dado. **Sin guardas**: el camino normal es `emitir_cpe`."""
    cuerpo = (f"<wsc:AutorizarCPEAutomotorReq>"
              f"{_xml({'auth': {'token': token, 'sign': sign, 'cuitRepresentada': _exigir_once(cuit_representada)}})}"
              f"{solicitud.xml(nro_orden)}</wsc:AutorizarCPEAutomotorReq>")
    return _carta_de_porte(await _llamar("autorizarCPEAutomotor", cuerpo, ambiente))


def _exigir_once(cuit) -> str:
    digitos = _digitos(cuit)
    if len(digitos) != 11:
        raise ValueError(f"WSCPE: el CUIT representado tiene que tener 11 dígitos, no «{cuit}»")
    return digitos


def _ruta_del_cerrojo(cuit: str, sucursal: int, tipo_cpe: int, ambiente: str) -> str:
    import os
    directorio = arca_wsaa._dir_de_tickets()
    os.makedirs(directorio, mode=0o700, exist_ok=True)
    return os.path.join(directorio, f"cpe-{ambiente}-{cuit}-{sucursal}-{tipo_cpe}")


async def emitir_cpe(cuit_representada, token: str, sign: str, solicitud: SolicitudCpe, *,
                     ambiente: str = "produccion") -> CartaDePorte:
    """Emite la carta: pide el último número, autoriza con el siguiente y devuelve la CPE con su CTG y su PDF.

    - 🔑 **El CUIT representado tiene que ser el solicitante**: se emite *en nombre de* quien delegó. Si no
      coinciden se rechaza antes de llamar (con dos CUIT posibles, firmar por otro es el error caro).
    - **Un cerrojo entre procesos por (solicitante, sucursal, tipo, ambiente)**: dos emisiones a la vez pedirían
      el mismo número.
    - 🔴 **Si ARCA no contesta al autorizar, no se reintenta.** Se consulta por el número pedido: si la carta
      está, se devuelve; si ARCA dice que no existe, se informa que no se emitió (y se puede volver a intentar);
      si tampoco contesta, `EmisionIncierta`. Es la regla del manual (sección 1.3) y la misma lección del CAE.
    """
    cuit = _exigir_once(cuit_representada)
    if _digitos(solicitud.cuit_solicitante) != cuit:
        raise ValueError("WSCPE: se emite en nombre del solicitante: el CUIT representado tiene que ser el "
                         f"del solicitante ({_digitos(solicitud.cuit_solicitante)}), no {cuit}")
    problemas = solicitud.problemas()
    if problemas:
        raise SolicitudInvalida(problemas)
    ruta = _ruta_del_cerrojo(cuit, solicitud.sucursal, solicitud.tipo_cpe, ambiente)
    async with arca_wsaa._cerrojo(ruta):
        nro = await ultimo_nro_orden(cuit, token, sign, sucursal=solicitud.sucursal, tipo_cpe=solicitud.tipo_cpe,
                                     ambiente=ambiente) + 1
        try:
            return await autorizar_cpe(cuit, token, sign, solicitud, nro_orden=nro, ambiente=ambiente)
        except (httpx.TransportError, RespuestaNoSoap) as falla:
            try:
                return await consultar_cpe(cuit, token, sign, tipo_cpe=solicitud.tipo_cpe,
                                           sucursal=solicitud.sucursal, nro_orden=nro, ambiente=ambiente)
            except CpeNoEncontrada:
                raise RuntimeError(
                    f"WSCPE: ARCA no contestó al autorizar ({type(falla).__name__}) y la carta "
                    f"{solicitud.sucursal:05d}-{nro:08d} no figura: no se emitió. Se puede volver a intentar."
                ) from falla
            except Exception as consulta:
                raise EmisionIncierta(
                    f"WSCPE: ARCA no contestó al autorizar la carta {solicitud.sucursal:05d}-{nro:08d} y tampoco "
                    "se pudo consultar si quedó emitida. No reintentar: consultala primero.",
                    sucursal=solicitud.sucursal, nro_orden=nro, tipo_cpe=solicitud.tipo_cpe,
                ) from consulta


async def anular_cpe(cuit_representada, token: str, sign: str, *, sucursal: int, nro_orden: int,
                     tipo_cpe: int = TIPO_AUTOMOTOR, motivo: int | None = None, observaciones: str | None = None,
                     ambiente: str = "produccion") -> str:
    """Anula una carta emitida (`anularCPE`) y devuelve el estado que informa ARCA (`AN`).

    ARCA pone sus condiciones (plazo, estado, topes de anuladas del titular); cualquier rechazo llega como
    `ErrorWscpe` con sus códigos.
    """
    if observaciones is not None and not 1 <= len(observaciones) <= 100:
        raise ValueError("WSCPE: las observaciones de la anulación tienen entre 1 y 100 caracteres")
    respuesta = await _llamar("anularCPE", _pedido(
        "AnularCPEReq", cuit_representada, token, sign,
        {"solicitud": {"cartaPorte": {"tipoCPE": int(tipo_cpe), "sucursal": int(sucursal), "nroOrden": int(nro_orden)},
                       "anulacionMotivo": motivo, "anulacionObservaciones": observaciones}}), ambiente)
    return _texto(_hijo(respuesta, "cabecera"), "estado")
