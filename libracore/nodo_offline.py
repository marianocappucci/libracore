"""La «conexión» del motor en un nodo offline de LibraEdge (ADR-074): si esta instancia es un nodo y si está en línea.

Un nodo es la instancia del producto que corre en la PC del local y sincroniza con el central por LibraEdge. El motor
no depende de `libraedge` en runtime: lo que necesita saber de él son dos datos, y los dos tienen un contrato simple
que se lee sin importarlo.

- **Si es un nodo:** `LIBRAEDGE_NODE_ID` (lo escribe el instalador; `libraedge.identidad.nodo_actual()`). Un producto
  que además lee su variable vieja (`RESTOLIBRA_EDGE_NODE_ID`) inyecta la función de libraedge:
  `ConexionDelNodo(nodo=lambda: nodo_actual("restolibra"), ...)`.
- **Si está en línea:** el archivo de estado que escribe el proceso del nodo (`libraedge-nodo`, `LIBRAEDGE_ESTADO`;
  `libraedge.nodo.escribir_estado`). Es JSON con, entre otras, `en_linea` (bool) y `ultima_sincronizacion_ok` (ISO).
  En línea es: el último ciclo sincronizó **y** la última sincronización buena es de hace menos de `vigencia`. Lo
  segundo cubre el proceso del nodo caído, cuyo archivo se queda diciendo `en_linea: true` para siempre. Sin archivo,
  ilegible, sin fecha o viejo: **sin conexión**. Ante la duda, no se sale a ARCA ni a MercadoPago.

🔑 **«En línea» es «ve al central», no «tiene internet».** Son casi lo mismo y la diferencia corre del lado seguro: con
internet y el central caído, el nodo emite el ticket y factura al volver; nunca al revés. Además la facturación con
internet usa el ticket de acceso que presta el central (`ta_para_el_nodo`), así que sin central no hay con qué firmar.

`en_linea_segun_estado` es **la** implementación: la de `libracommerce.erp.offline` (ADR-067 de ese motor) hace lo
mismo y pasa a importarla de acá. El motor de abajo es el que no puede depender del de arriba.

Fuera de un nodo (el central, una instalación común) `FUERA_DE_NODO` dice siempre «en línea» y nada cambia.

`solo_en_el_central` (ADR-076) es la guarda de lo que el nodo no tiene: 409 `solo_en_el_central` en un nodo.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

#: La identidad del nodo; la misma variable que `libraedge.identidad.VARIABLE`.
VARIABLE_NODO = "LIBRAEDGE_NODE_ID"
#: Dónde escribe su estado el proceso del nodo; la misma que lee `libraedge-nodo` (`cli.py`).
VARIABLE_ESTADO = "LIBRAEDGE_ESTADO"
#: Cuánto vale la última sincronización buena. El proceso del nodo sincroniza cada minuto.
VIGENCIA = timedelta(minutes=5)

#: `detail.codigo` de un 409 de la API: la operación necesita conexión y no la hay (QR, MercadoPago).
CODIGO_SIN_CONEXION = "sin_conexion"
#: `detail.codigo` de un 409 de `POST /api/ventas/{id}/facturar` en un nodo sin conexión: no hay factura todavía, la
#: venta quedó con su ticket y en la bandeja para facturarse al volver.
CODIGO_FACTURA_PENDIENTE = "factura_pendiente"
#: `detail.codigo` de un 422: el nodo no tiene con qué facturar (sin `arca_config` espejada o sin credenciales).
CODIGO_NODO_SIN_ARCA = "nodo_sin_arca"
#: `detail.codigo` de un 409: la función no existe en un nodo (ADR-076), sólo en el central.
CODIGO_SOLO_EN_EL_CENTRAL = "solo_en_el_central"

MENSAJE_FACTURA_PENDIENTE = (
    "Sin conexión: la venta quedó con su ticket (no válido como factura) y se factura sola al volver la conexión."
)
MENSAJE_QR_SIN_CONEXION = (
    "Sin conexión: el cobro con QR de MercadoPago no está disponible. Cobrá con otro medio."
)
MENSAJE_SIN_CONEXION = "Sin conexión con el central: esta operación necesita internet."
MENSAJE_SOLO_EN_EL_CENTRAL = "Esta función sólo está disponible en el central, no en el equipo del local."


class SinConexion(Exception):
    """La operación necesita conexión y el nodo no la tiene. La API la contesta 409 con `detalle()`."""

    codigo = CODIGO_SIN_CONEXION

    def __init__(self, mensaje: str = MENSAJE_SIN_CONEXION):
        super().__init__(mensaje)
        self.mensaje = mensaje

    def detalle(self) -> dict:
        """El `detail` de la respuesta: `{"codigo", "mensaje"}` (la pantalla muestra `mensaje`)."""
        return {"codigo": self.codigo, "mensaje": self.mensaje}


class FacturaPendiente(SinConexion):
    """No se facturó: la venta quedó en la bandeja «a facturar» (`comprobante_pendiente_id`) y se factura al volver.

    No es un error de la venta, que está cobrada y tiene su ticket: es que el comprobante fiscal todavía no existe."""

    codigo = CODIGO_FACTURA_PENDIENTE

    def __init__(self, mensaje: str = MENSAJE_FACTURA_PENDIENTE, *, comprobante_pendiente_id: int | None = None,
                 motivo: str = ""):
        super().__init__(mensaje)
        self.comprobante_pendiente_id = comprobante_pendiente_id
        self.motivo = motivo

    def detalle(self) -> dict:
        return {**super().detalle(), "comprobante_pendiente_id": self.comprobante_pendiente_id,
                "motivo": self.motivo}


class NodoSinArca(Exception):
    """El nodo no tiene con qué facturar: falta la `arca_config` espejada o la forma de autenticarse. 422.

    No va a la bandeja como si fuera un corte: no se arregla solo al volver la conexión, lo tiene que mirar alguien."""

    codigo = CODIGO_NODO_SIN_ARCA

    def detalle(self) -> dict:
        return {"codigo": self.codigo, "mensaje": str(self)}


class RechazoDelCentral(NodoSinArca):
    """El central **contestó** y no presta el ticket: nodo revocado o sin credencial (401), el central sin ARCA (422),
    servicio que no presta (501). No es un corte: reintentar al volver no lo arregla. Termina en el 422 `nodo_sin_arca`.

    El producto traduce a esto (o a `NodoSinArca`) el rechazo de su cliente del ticket —en libraedge,
    `TicketRechazado`—. También cuenta como rechazo cualquier excepción con el atributo `rechazo_del_central = True`."""


def _es_rechazo(error: BaseException) -> bool:
    return isinstance(error, NodoSinArca) or getattr(error, "rechazo_del_central", False) is True


def _es_de_red(error: BaseException) -> bool:
    """Un corte: la red (conexión, DNS, tiempo agotado) o un 5xx del central. Se reintenta al volver."""
    if isinstance(error, (SinConexion, OSError, TimeoutError)):  # ConnectionError es un OSError
        return True
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx viene con el motor
        httpx = None
    if httpx is not None:
        if isinstance(error, httpx.TransportError):
            return True
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code >= 500
    status = getattr(error, "status_code", None)
    return isinstance(status, int) and status >= 500


def es_rechazo(error: BaseException) -> bool:
    """¿El central **contestó** y no presta el ticket (`NodoSinArca`, `RechazoDelCentral` o `rechazo_del_central = True`)?"""
    return _es_rechazo(error)


def es_corte(error: BaseException) -> bool:
    """¿Es un corte —la red, un tiempo agotado, un 5xx del central— y por lo tanto se reintenta al volver?"""
    return _es_de_red(error)


# ── El estado del nodo ──────────────────────────────────────────────────────


def _fecha(valor) -> datetime | None:
    if not isinstance(valor, str) or not valor.strip():
        return None
    try:
        fecha = datetime.fromisoformat(valor.strip())
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=UTC)


def esta_en_linea(ruta: str | None, *, vigencia: timedelta = VIGENCIA, ahora: datetime | None = None) -> bool:
    """¿El archivo de estado del nodo dice que está sincronizando? Sin archivo, ilegible, sin fecha o viejo: `False`."""
    if not ruta:
        return False
    try:
        with open(ruta, encoding="utf-8") as archivo:
            estado = json.load(archivo)
    except (OSError, ValueError):
        return False
    if not isinstance(estado, dict) or estado.get("en_linea") is not True:
        return False
    ultima = _fecha(estado.get("ultima_sincronizacion_ok"))
    if ultima is None:
        return False
    ahora = ahora or datetime.now(UTC)
    # Una fecha del futuro (reloj de la PC corrido) no se toma como «recién»: es tan dudosa como una vieja.
    return -timedelta(minutes=1) <= ahora - ultima <= vigencia


def en_linea_segun_estado(ruta: str | None, vigencia: timedelta = VIGENCIA) -> Callable[[], bool]:
    """Un `en_linea` que lee el archivo de estado del nodo **en cada llamada** (es un archivo chico)."""

    def en_linea() -> bool:
        return esta_en_linea(ruta, vigencia=vigencia)

    return en_linea


def nodo_desde_entorno(variable: str = VARIABLE_NODO) -> Callable[[], str | None]:
    """Un `nodo` que lee la identidad del entorno en cada llamada. Vacío o ausente: no es un nodo (`None`)."""

    def nodo() -> str | None:
        return (os.environ.get(variable) or "").strip() or None

    return nodo


def _ningun_nodo() -> str | None:
    return None


def _siempre_en_linea() -> bool:
    return True


@dataclass(frozen=True)
class ConexionDelNodo:
    """Lo que el motor pregunta antes de salir a ARCA o a MercadoPago.

    - `nodo`: el id de este nodo, o `None` si la instancia no es un nodo. Default: no es un nodo.
    - `en_linea`: si el nodo está sincronizando con el central. Sólo se consulta si es un nodo.
    """

    nodo: Callable[[], str | None] = _ningun_nodo
    en_linea: Callable[[], bool] = _siempre_en_linea

    def node_id(self) -> str | None:
        return self.nodo()

    def es_nodo(self) -> bool:
        return self.nodo() is not None

    def sin_conexion(self) -> bool:
        """`True` sólo en un nodo que no está en línea. Fuera de un nodo, nunca."""
        if not self.es_nodo():
            return False
        try:
            return not self.en_linea()
        except Exception:
            logger.exception("No se pudo saber si el nodo está en línea: se toma como sin conexión")
            return True

    def exigir(self, mensaje: str = MENSAJE_SIN_CONEXION) -> None:
        """Levanta `SinConexion(mensaje)` si es un nodo sin conexión."""
        if self.sin_conexion():
            raise SinConexion(mensaje)

    def estado(self, a_facturar: int = 0) -> dict:
        """Lo que contesta `GET /api/nodo/conexion` y lee el cartel «sin conexión» de libra-ui (ADR-077 del kit):
        `{"es_nodo", "en_linea", "node_id", "a_facturar"}`. Fuera de un nodo `en_linea` es `True` y `node_id` `None`.

        `a_facturar` lo pone quien sabe qué espera salir (la bandeja de ventas del `PuertoDeVentas`); una instancia sin
        bandeja de ventas deja el 0."""
        return {"es_nodo": self.es_nodo(), "en_linea": not self.sin_conexion(), "node_id": self.node_id(),
                "a_facturar": a_facturar}


#: El central y una instalación común: siempre en línea, nada cambia.
FUERA_DE_NODO = ConexionDelNodo()


def conexion_desde_entorno(*, nodo: Callable[[], str | None] | None = None, ruta_estado: str | None = None,
                           vigencia: timedelta = VIGENCIA) -> ConexionDelNodo:
    """La conexión de esta instancia según su entorno: `LIBRAEDGE_NODE_ID` y `LIBRAEDGE_ESTADO`.

    Un nodo sin `LIBRAEDGE_ESTADO` está **siempre sin conexión**: sin el archivo no hay cómo saber, y ante la duda no se
    sale a ARCA. `nodo` reemplaza la lectura de la identidad (p. ej. `lambda: nodo_actual("ventalibra")`)."""
    ruta = ruta_estado if ruta_estado is not None else (os.environ.get(VARIABLE_ESTADO) or "").strip() or None
    return ConexionDelNodo(nodo=nodo or nodo_desde_entorno(), en_linea=en_linea_segun_estado(ruta, vigencia))


# ── Lo que sólo se hace en el central ───────────────────────────────────────────────────────────


class SoloEnElCentral(Exception):
    """La función no existe en un nodo: se hace en el central. La API la contesta 409 con `detalle()`."""

    codigo = CODIGO_SOLO_EN_EL_CENTRAL

    def __init__(self, mensaje: str = MENSAJE_SOLO_EN_EL_CENTRAL):
        super().__init__(mensaje)
        self.mensaje = mensaje

    def detalle(self) -> dict:
        """El `detail` de la respuesta: `{"codigo", "mensaje"}` (la pantalla muestra `mensaje`)."""
        return {"codigo": self.codigo, "mensaje": self.mensaje}


def guarda_solo_en_el_central(*, nodo: Callable[[], str | None] | None = None,
                              mensaje: str = MENSAJE_SOLO_EN_EL_CENTRAL) -> Callable[[], None]:
    """Una dependencia de FastAPI que contesta **409** `{"codigo": "solo_en_el_central", "mensaje"}` si el proceso
    corre en un nodo, y no hace nada en el central ni en una instalación común (ADR-076).

    Es la costura para lo que el nodo no puede hacer porque su verdad vive en el central (la agenda de MedLibra y
    GestioLibra, que varios puestos comparten y que no se reconcilia sin red)::

        @router.post("/turnos", dependencies=[Depends(solo_en_el_central)])

    A diferencia de `SinConexion` (409 `sin_conexion`: «probá cuando vuelva internet»), esto no se arregla
    esperando: el nodo **no tiene** esa función, esté en línea o no. La pantalla lo muestra distinto.

    `nodo` es la lectura de la identidad (default: `LIBRAEDGE_NODE_ID`); un producto que además lee su variable
    vieja pasa la suya, como en `conexion_desde_entorno`. Se lee **en cada pedido**, no al montar la ruta."""
    es_nodo = nodo or nodo_desde_entorno()

    def solo_en_el_central() -> None:
        if es_nodo() is not None:
            from fastapi import HTTPException

            raise HTTPException(409, SoloEnElCentral(mensaje).detalle())

    return solo_en_el_central


#: La guarda con la lectura de siempre (`LIBRAEDGE_NODE_ID`): `Depends(solo_en_el_central)`.
solo_en_el_central = guarda_solo_en_el_central()


# ── Facturar con internet sin la clave en el nodo: el ticket de acceso prestado ─────────────────

#: Los servicios de ARCA cuyo ticket de acceso (WSAA) el central le presta a un nodo. Una lista cerrada a propósito: un
#: servicio nuevo del catálogo (`arca_servicios.CATALOGO`) no se presta solo, porque un nodo robado pediría con él
#: tickets de lo que nadie decidió prestar.
SERVICIOS_PRESTABLES = ("wsfe", "wscpe")

#: Los ambientes en el orden en que se busca el par de un servicio que no es la facturación: producción si está cargado,
#: si no homologación (la misma regla que LibraCargo usa para `wscpe`).
_AMBIENTES = ("produccion", "homologacion")


async def ta_para_el_nodo(emisor_id=None, *, servicio: str = "wsfe", empresa: str | None = None,
                          ambiente: str | None = None) -> dict:
    """**En el central.** El ticket de acceso (WSAA) vigente de un servicio, para prestárselo a un nodo.

    Devuelve `{"token", "sign", "expiracion", "ambiente", "servicio", "cuit", "emisor_id"}`. Es el **mismo** ticket que usa
    el central (`arca_wsaa.autenticar` lo reusa de su caché): ARCA no da un segundo ticket para el mismo certificado y
    servicio mientras el primero vale, así que el nodo no puede autenticarse por su cuenta con el certificado del
    central aunque tuviera la clave. El ticket dura 12 horas como máximo; la clave privada nunca sale del central.

    - **`servicio="wsfe"`** (el de siempre): el par es el de `arca_config` del `emisor_id`, y `cuit` es el del emisor.
    - **`servicio="wscpe"`** (la carta de porte): el par no está en `arca_config` sino en `arca_credenciales_servicio`
      (ADR-032), una fila por `(empresa, servicio, ambiente)`. Por eso hace falta **`empresa`** (el nombre con el que el
      producto cargó el par: `"agencia"` en LibraCargo). Sin `ambiente` se usa producción si el par está cargado y, si
      no, homologación. `cuit` y `emisor_id` son `None`: **el CUIT representado de una CPE es el de su titular**, se elige
      en cada emisión y ARCA lo valida contra las relaciones que trae el propio token (`arca_wscpe.cuits_habilitados`).

    🔴 **Se sirve sólo a un nodo autenticado** (el secreto de LibraEdge del nodo), por HTTPS, y nunca se registra en un
    log. Levanta `NodoSinArca` si el central no tiene ese par cargado, `ValueError` si el servicio no se presta (o falta
    `empresa` para uno que la necesita: es un error de quien arma la ruta, no del nodo) y `RuntimeError` si ARCA no
    autentica."""
    from libracore import arca_credenciales, arca_wsaa
    from libracore.db import arca_config as db_arca

    servicio = (servicio or "").strip().lower()
    if servicio not in SERVICIOS_PRESTABLES:
        raise ValueError(f"El servicio {servicio!r} no se presta a un nodo (se prestan {SERVICIOS_PRESTABLES}).")
    if servicio == "wsfe":
        arca = db_arca.config_del_emisor(emisor_id)
        if not arca:
            raise NodoSinArca("El central no tiene la configuración de ARCA de este emisor.")
        cert, clave = arca_credenciales.paths_en_disco(arca)
        if not (cert and clave):
            raise NodoSinArca("El central no tiene el certificado y la clave de ARCA de este emisor.")
        ta = await arca_wsaa.autenticar(cert, clave, arca["ambiente"], servicio)
        return {"token": ta["token"], "sign": ta["sign"], "expiracion": ta.get("expiracion"),
                "ambiente": arca["ambiente"], "servicio": servicio, "cuit": arca["cuit"],
                "emisor_id": arca.get("id")}

    if not (empresa or "").strip():
        raise ValueError(f"ta_para_el_nodo({servicio!r}) necesita la `empresa` con la que está cargado el par.")
    for amb in ((ambiente,) if ambiente else _AMBIENTES):
        cert, clave = arca_credenciales.paths_en_disco_de_servicio(empresa, servicio, amb)
        if cert and clave:
            break
    else:
        raise NodoSinArca(f"El central no tiene el certificado y la clave de {servicio} cargados.")
    ta = await arca_wsaa.autenticar(cert, clave, amb, servicio)
    return {"token": ta["token"], "sign": ta["sign"], "expiracion": ta.get("expiracion"), "ambiente": amb,
            "servicio": servicio, "cuit": None, "emisor_id": None}


ObtenerTa = Callable[..., Awaitable[dict]]


async def _pedir_ta(obtener_ta: ObtenerTa, arca: dict, servicio: str) -> dict:
    """**En el nodo.** Trae el ticket del central y clasifica lo que pase: corte, rechazo u otro error (ver
    `numerador_con_ta`). `obtener_ta(arca)` para `wsfe` (como siempre) y `obtener_ta(arca, servicio=...)` para los demás.

    Y comprueba **qué ticket volvió**: uno sin el campo `servicio` es de `wsfe` (un LibraEdge viejo no lo manda). Si hace
    falta el de otro servicio y el central devolvió el de facturación, no se sigue: ARCA contestaría con un error de
    autenticación que no explica nada. Es un rechazo, no un corte: actualizar LibraEdge es lo que lo arregla."""
    try:
        ta = await (obtener_ta(arca) if servicio == "wsfe" else obtener_ta(arca, servicio=servicio))
    except Exception as e:
        if _es_rechazo(e):
            # El central dijo que no: no es un corte, no va a la bandeja (422 `nodo_sin_arca`).
            if isinstance(e, NodoSinArca):
                raise
            raise RechazoDelCentral(
                "El central no presta el ticket de ARCA a este equipo (nodo revocado o sin credencial).") from e
        if _es_de_red(e):
            logger.error("Nodo: el central no respondió al pedir el ticket de ARCA: %s", type(e).__name__)
            raise SinConexion(
                "El central no respondió: la venta se factura al volver la conexión.") from e
        raise  # cualquier otra cosa es un error, no un corte: que se vea
    dado = str((ta or {}).get("servicio") or "wsfe").strip().lower()
    if dado != servicio:
        raise RechazoDelCentral(
            f"El central devolvió un ticket de {dado} y hace falta uno de {servicio}: actualizá LibraEdge.")
    return ta


def ticket_prestado(obtener_ta: ObtenerTa, emisor_id=None, *, servicio: str = "wsfe"
                    ) -> Callable[[], Awaitable[tuple[dict, dict]]]:
    """**En el nodo.** Una función `async () -> (ta, arca)` que pide el ticket prestado de `servicio` al central.

    `arca` es la `arca_config` espejada del emisor (`emisor_id`). Para `wsfe` es obligatoria: sin ella `NodoSinArca`. Para
    otro servicio puede faltar y se le pasa a `obtener_ta` `{"id": emisor_id}`: lo único que el central necesita para
    saber de quién es. Los errores son los de `numerador_con_ta`."""
    from libracore.db import arca_config as db_arca

    async def ticket() -> tuple[dict, dict]:
        arca = db_arca.config_del_emisor(emisor_id)
        if not arca:
            if servicio == "wsfe":
                raise NodoSinArca(
                    "Este equipo no tiene la configuración de ARCA del central (falta espejar arca_config).")
            arca = {"id": emisor_id}
        return await _pedir_ta(obtener_ta, arca, servicio), arca

    return ticket


def numerador_con_ta(obtener_ta: ObtenerTa, emisor_id=None) -> Callable[[int, int], Awaitable[tuple]]:
    """**En el nodo.** Un `PuertoDeVentas.numerar_comprobante` que pide el número a ARCA con un ticket prestado.

    `obtener_ta(arca)` trae el ticket del central (lo arma el producto sobre el transporte de LibraEdge; ver
    `ta_para_el_nodo`) y devuelve un dict con `token` y `sign`. Devuelve `(numero, ta, arca)` como
    `arca_facturacion.get_next_numero_with_arca`, **sin caer nunca a la numeración local**: en un nodo, un número local
    choca con el de ARCA y con el del central.

    Qué pasa con un error de `obtener_ta`:
    - **Corte** (`SinConexion`, red, tiempo agotado, 5xx del central): `SinConexion` → la venta va a la bandeja.
    - **Rechazo** (`NodoSinArca`, `RechazoDelCentral` o una excepción con `rechazo_del_central = True`; en libraedge,
      `TicketRechazado` traducido por el producto): `NodoSinArca` → 422, no va a la bandeja.
    - Cualquier otra: sale tal cual (es un error, no un corte).

    Si ARCA no da el número, `SinConexion`. Sin `arca_config` espejada, `NodoSinArca`."""
    from libracore import arca_wsfe

    ticket = ticket_prestado(obtener_ta, emisor_id)

    async def numerar(punto_venta: int, tipo: int) -> tuple:
        ta, arca = await ticket()
        try:
            ultimo = await arca_wsfe.ultimo_numero_autorizado(
                punto_venta, tipo, arca["cuit"], ta["token"], ta["sign"], arca["ambiente"])
        except Exception as e:
            # ARCA no contestó o rechazó el pedido del último número: se reintenta al volver. Sin el texto del error
            # en el log: puede traer el token.
            logger.error("Nodo: no se pudo pedir el número a ARCA (PV=%s tipo=%s): %s", punto_venta, tipo,
                         type(e).__name__)
            raise SinConexion("ARCA no respondió: la venta se factura al volver la conexión.") from e
        return ultimo + 1, ta, arca

    return numerar
