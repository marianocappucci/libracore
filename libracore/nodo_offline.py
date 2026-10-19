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

MENSAJE_FACTURA_PENDIENTE = (
    "Sin conexión: la venta quedó con su ticket (no válido como factura) y se factura sola al volver la conexión."
)
MENSAJE_QR_SIN_CONEXION = (
    "Sin conexión: el cobro con QR de MercadoPago no está disponible. Cobrá con otro medio."
)
MENSAJE_SIN_CONEXION = "Sin conexión con el central: esta operación necesita internet."


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


#: El central y una instalación común: siempre en línea, nada cambia.
FUERA_DE_NODO = ConexionDelNodo()


def conexion_desde_entorno(*, nodo: Callable[[], str | None] | None = None, ruta_estado: str | None = None,
                           vigencia: timedelta = VIGENCIA) -> ConexionDelNodo:
    """La conexión de esta instancia según su entorno: `LIBRAEDGE_NODE_ID` y `LIBRAEDGE_ESTADO`.

    Un nodo sin `LIBRAEDGE_ESTADO` está **siempre sin conexión**: sin el archivo no hay cómo saber, y ante la duda no se
    sale a ARCA. `nodo` reemplaza la lectura de la identidad (p. ej. `lambda: nodo_actual("ventalibra")`)."""
    ruta = ruta_estado if ruta_estado is not None else (os.environ.get(VARIABLE_ESTADO) or "").strip() or None
    return ConexionDelNodo(nodo=nodo or nodo_desde_entorno(), en_linea=en_linea_segun_estado(ruta, vigencia))


# ── Facturar con internet sin la clave en el nodo: el ticket de acceso prestado ─────────────────


async def ta_para_el_nodo(emisor_id=None, *, servicio: str = "wsfe") -> dict:
    """**En el central.** El ticket de acceso (WSAA) vigente del emisor, para prestárselo a un nodo.

    Devuelve `{"token", "sign", "expiracion", "ambiente", "cuit", "emisor_id"}`. Es el **mismo** ticket que usa el
    central (`arca_wsaa.autenticar` lo reusa de su caché): ARCA no da un segundo ticket para el mismo certificado y
    servicio mientras el primero vale, así que el nodo no puede autenticarse por su cuenta con el certificado del
    central aunque tuviera la clave. El ticket dura 12 horas como máximo; la clave privada nunca sale del central.

    🔴 **Se sirve sólo a un nodo autenticado** (el secreto de LibraEdge del nodo), por HTTPS, y nunca se registra en un
    log. Levanta `NodoSinArca` si el central no tiene ARCA configurada para ese emisor, y `RuntimeError` si ARCA no
    autentica."""
    from libracore import arca_credenciales, arca_wsaa
    from libracore.db import arca_config as db_arca

    arca = db_arca.config_del_emisor(emisor_id)
    if not arca:
        raise NodoSinArca("El central no tiene la configuración de ARCA de este emisor.")
    cert, clave = arca_credenciales.paths_en_disco(arca)
    if not (cert and clave):
        raise NodoSinArca("El central no tiene el certificado y la clave de ARCA de este emisor.")
    ta = await arca_wsaa.autenticar(cert, clave, arca["ambiente"], servicio)
    return {"token": ta["token"], "sign": ta["sign"], "expiracion": ta.get("expiracion"),
            "ambiente": arca["ambiente"], "cuit": arca["cuit"], "emisor_id": arca.get("id")}


ObtenerTa = Callable[[dict], Awaitable[dict]]


def numerador_con_ta(obtener_ta: ObtenerTa, emisor_id=None) -> Callable[[int, int], Awaitable[tuple]]:
    """**En el nodo.** Un `PuertoDeVentas.numerar_comprobante` que pide el número a ARCA con un ticket prestado.

    `obtener_ta(arca)` trae el ticket del central (lo arma el producto sobre el transporte de LibraEdge; ver
    `ta_para_el_nodo`) y devuelve un dict con `token` y `sign`. Devuelve `(numero, ta, arca)` como
    `arca_facturacion.get_next_numero_with_arca`, **sin caer nunca a la numeración local**: en un nodo, un número local
    choca con el de ARCA y con el del central. Si el ticket o ARCA fallan, `SinConexion`; si el nodo no tiene la
    `arca_config` espejada, `NodoSinArca`."""
    from libracore import arca_wsfe
    from libracore.db import arca_config as db_arca

    async def numerar(punto_venta: int, tipo: int) -> tuple:
        arca = db_arca.config_del_emisor(emisor_id)
        if not arca:
            raise NodoSinArca("Este equipo no tiene la configuración de ARCA del central (falta espejar arca_config).")
        try:
            ta = await obtener_ta(arca)
            ultimo = await arca_wsfe.ultimo_numero_autorizado(
                punto_venta, tipo, arca["cuit"], ta["token"], ta["sign"], arca["ambiente"])
        except SinConexion:
            raise
        except Exception as e:
            # Sin el texto del error en el mensaje: puede traer el token.
            logger.error("Nodo: no se pudo pedir el número a ARCA (PV=%s tipo=%s): %s", punto_venta, tipo,
                         type(e).__name__)
            raise SinConexion("ARCA o el central no respondieron: la venta se factura al volver la conexión.") from e
        return ultimo + 1, ta, arca

    return numerar
