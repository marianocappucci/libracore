"""La cola de comprobantes de un nodo offline de LibraEdge (ADR-079): el producto deja el pedido sin red, el nodo lo emite
solo al volver y el central recibe el resultado.

ADR-074 resolvió las ventas del punto de venta: sin conexión salen con un ticket no fiscal, entran a la bandeja
`comprobantes_pendientes` y `facturar_pendientes_del_nodo` las factura **desde la venta** al volver. Eso no sirve para lo
que no es una venta de `PuertoDeVentas`:

- **`documento`**: una factura que nace de otra entidad (la de LibraCargo desde una orden de carga, la «conjunta» de
  LibraClub con la reserva y sus consumos). Sin red no se numera local (choca con ARCA y con el central).
- **`cpe`**: una carta de porte electrónica (servicio `wscpe`). Un camión no puede viajar sin ella: es un límite legal, y
  hasta que la cola la emite no hay CPE válida (la pantalla lo avisa).

Qué hace el motor y qué el producto::

    producto                                   motor (acá)
    ────────                                   ───────────
    encolar(cola, "cpe", orden_id, datos=…) ─▶ una fila por origen (idempotente), `esperando`
                                               ┊ al volver la conexión (hilo, tanda o botón):
                                               ┊  - más viejos primero
                                               ┊  - candado por pedido: una emisión, aunque corran dos a la vez
    emisor(pedido, ctx)  ◀─────────────────────┘  - `emitiendo` ANTES de llamar al emisor
      arma el comprobante desde su entidad,        - la fecha de la factura (`ctx.fecha_de_la_factura`)
      numera con `ctx.numerar` / pide el ticket    - qué hacer con cada error (abajo)
      con `ctx.ticket("wscpe")`, emite y devuelve
      el resultado (`factura_id`/`cae`, el CTG) ─▶ `emitido`, y avisa a `al_cambiar` para que suba al central

**Qué hace el motor con un error del emisor** (la regla es «ante la duda, no se emite dos veces»):

| El emisor levanta | El pedido queda | Y además |
|---|---|---|
| `SinConexion`, un error de red, un 5xx del central | `esperando`, sin consumir el intento | la tanda se corta: se reintenta al volver |
| `NodoSinArca` / `RechazoDelCentral` (el central no presta el ticket) | `esperando`, con el motivo | se corta **ese tipo** y se informa: no se arregla solo |
| `arca_wscpe.EmisionIncierta` o algo con `emision_incierta = True` | `incierto` | **no se reintenta**: se consulta a ARCA antes (`reintentar` lo pide confirmado) |
| cualquier otra cosa (ARCA rechazó, datos inválidos, un bug) | `a_revisar`, con el mensaje | sigue con el siguiente; una persona corrige y `reintentar` |
| devuelve un `documento` **sin CAE** | `a_revisar` | la factura existe como borrador: se autoriza desde Facturas |

Y si el proceso muere con el pedido `emitiendo`, el próximo en tomar el candado lo pasa a `incierto`: no sabe si ARCA lo
llegó a emitir.

🔴 **Sin red nunca se numera local.** `ctx.numerar` sólo pide el número a ARCA con el ticket prestado del central
(`nodo_offline.numerador_con_ta`) y, si no hay ticket o ARCA no contesta, levanta `SinConexion` / `NodoSinArca`: el emisor
del producto no tiene un camino a `get_next_numero_with_arca`.

Fuera de un nodo nada cambia: `procesar_cola` no hace nada, `arrancar_cola_al_volver` no arranca nada y `encolar` se
rehúsa (una fila encolada fuera de un nodo no la emitiría nadie).
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from libracore import candados, fechas
from libracore.db import cola_arca as db
from libracore.db.core import Conexion, get_connection
from libracore.nodo_offline import (
    FUERA_DE_NODO,
    ConexionDelNodo,
    NodoSinArca,
    ObtenerTa,
    RechazoDelCentral,
    SinConexion,
    es_corte,
    es_rechazo,
    numerador_con_ta,
    ticket_prestado,
)

logger = logging.getLogger(__name__)

#: La versión de la forma de `payload_de_pedido`. Si cambia, sube: el central rechaza lo que no entiende.
VERSION = 1

#: Cada cuánto mira la cola el hilo del nodo. El mismo que el de la bandeja de ventas.
INTERVALO_AL_VOLVER = 60.0

MENSAJE_A_MEDIAS = (
    "Se cortó a mitad de la emisión (el equipo se apagó o el proceso murió) y no se sabe si ARCA llegó a emitirlo. "
    "Consultalo en ARCA antes de volver a emitir."
)
MENSAJE_SIN_RED = "Sin conexión con ARCA o con el central: se emite al volver la conexión."
MENSAJE_SIN_CAE = "ARCA no autorizó la factura (quedó sin CAE): autorizala desde Facturas o descartá el pedido."


class EmisionEnCurso(Exception):
    """Otro proceso (u otro hilo) está emitiendo este pedido ahora. No se emite otro: la API contesta 409."""


class PedidoNoEncontrado(LookupError):
    """El pedido no existe, o es de otro producto o de otro nodo."""


class ConfirmacionRequerida(ValueError):
    """Reabrir un pedido `incierto` pide confirmar que se consultó a ARCA y que no salió."""


def _nada(pedido: dict) -> None:
    return None


@dataclass(frozen=True)
class ColaDeArca:
    """Cómo el motor llega al producto: sus emisores, su conexión y su ticket. Inmutable, con defaults que no hacen nada.

    - `producto`: el nombre del producto (`libracargo`), el `origen_producto` de la cola y del candado.
    - `emisores`: `{origen_tipo: emisor}`. El **emisor** es `async def emisor(pedido, ctx) -> dict`: arma el comprobante
      desde su entidad (`pedido["origen_id"]`, `pedido["datos"]`), lo emite con las herramientas de `ctx` y devuelve el
      resultado. **Tiene que ser idempotente**: si su entidad ya tiene el comprobante (la orden ya tiene factura), lo
      devuelve sin emitir otro. Un tipo sin emisor no se encola ni se emite.
    - `conexion`: si la instancia es un nodo y si está en línea (`nodo_offline`). El default no es un nodo: nada cambia.
    - `obtener_ta`: el cliente del ticket prestado, `obtener_ta(arca)` para `wsfe` y `obtener_ta(arca, servicio=)` para
      los demás (el contrato de `nodo_offline.ticket_prestado`). `emisor_id`: el de `arca_config` (varias razones sociales).
    - `numerar_comprobante`: `(punto_venta, tipo) -> (numero, ta, arca)`. Default: `numerador_con_ta(obtener_ta)`. Existe
      para que el producto lo intercepte (sus tests fijan el ambiente parcheando el numerador).
    - `al_cambiar(pedido)`: el pedido (`payload_de_pedido`) **después de confirmar** cada cambio de estado que el central
      tiene que ver (emitido, a revisar, incierto, descartado, y esperando tras reabrir) y del alta. Es donde el producto
      encola la subida al central (`aplicar_pedido_del_nodo`). Un error suyo se registra y no se propaga: el comprobante
      ya está emitido. La `revision` hace idempotente el reenvío, así que el producto puede reconciliar en `en_cada_vuelta`.
    """

    producto: str
    emisores: Mapping[str, Callable[..., Awaitable[dict] | dict]] = field(default_factory=dict)
    conexion: ConexionDelNodo = FUERA_DE_NODO
    obtener_ta: ObtenerTa | None = None
    emisor_id: int | None = None
    numerar_comprobante: Callable[[int, int], Awaitable[tuple]] | None = None
    al_cambiar: Callable[[dict], None] = _nada

    def __post_init__(self):
        if not str(self.producto or "").strip():
            raise ValueError("producto es obligatorio")
        desconocidos = set(self.emisores) - set(db.ORIGENES)
        if desconocidos:
            raise ValueError(f"emisores para tipos que la cola no tiene: {sorted(desconocidos)} (hay {db.ORIGENES})")

    def instancia(self) -> str:
        """El `origen_instancia` de las filas: el id de este nodo."""
        return self.conexion.node_id() or ""


@dataclass(frozen=True)
class ContextoDeEmision:
    """Lo que el motor le da al emisor del producto para emitir con seguridad."""

    cola: ColaDeArca
    pedido: dict
    usuario_id: int | None
    #: El instante de la emisión, en Argentina.
    ahora: datetime.datetime

    @property
    def hoy(self) -> datetime.date:
        return self.ahora.date()

    async def numerar(self, punto_venta: int, tipo: int) -> tuple:
        """`(numero, ta, arca)` pedido a ARCA con el ticket prestado del central. **Nunca numera local.**

        `SinConexion` si el central o ARCA no contestan; `NodoSinArca` si el equipo no tiene con qué autenticarse."""
        cola = self.cola
        numerar = cola.numerar_comprobante
        if numerar is None:
            if cola.obtener_ta is None:
                raise NodoSinArca(
                    "Este equipo no tiene con qué autenticarse en ARCA: configurá el ticket prestado del central "
                    "(ColaDeArca.obtener_ta).")
            numerar = numerador_con_ta(cola.obtener_ta, cola.emisor_id)
        numero, ta, arca = await numerar(punto_venta, tipo)
        if not ta:
            # El numerador del producto cayó a la numeración local (ARCA no contestó o no hay ARCA): en un nodo
            # ese número choca con el de ARCA y con el del central.
            if not arca:
                raise NodoSinArca("Este equipo no tiene la configuración de ARCA del central: hay que espejar arca_config.")
            raise SinConexion("ARCA no respondió: se emite al volver la conexión.")
        return numero, ta, arca

    async def ticket(self, servicio: str = "wsfe") -> tuple[dict, dict]:
        """`(ta, arca)`: el ticket prestado del central para `servicio` (`wscpe` para una CPE). El `ta` trae el `ambiente`
        con el que hay que llamar. Los errores son los de `nodo_offline.ticket_prestado`."""
        if self.cola.obtener_ta is None:
            raise NodoSinArca("Este equipo no tiene con qué autenticarse en ARCA: configurá el ticket prestado del central "
                              "(ColaDeArca.obtener_ta).")
        return await ticket_prestado(self.cola.obtener_ta, self.cola.emisor_id, servicio=servicio)()

    def fecha_de_la_factura(self, punto_venta: int, tipo: int, ambiente: str, *,
                            emisor_id: int | None = None) -> datetime.date:
        """La fecha con la que sale la factura de este pedido: **la del hecho** (`fecha_ref`) si ARCA todavía la admite según
        el `concepto` del pedido (5 días en productos, 10 en servicios), si no la de hoy, y nunca antes que el último
        comprobante de la serie. Es `venta_facturacion.fecha_de_la_factura` con el piso resuelto (ADR-074, punto 4).

        Una `cpe` no la usa: su fecha es la de partida del camión, la elige quien carga el viaje y ARCA la valida."""
        from libracore.venta_facturacion import fecha_de_la_factura_en_la_serie

        return fecha_de_la_factura_en_la_serie(
            self.pedido.get("fecha_ref"), punto_venta, tipo, ambiente, concepto=int(self.pedido.get("concepto") or 1),
            emisor_id=emisor_id, hoy=self.hoy)


# ── Alta ─────────────────────────────────────────────────────────────────


class FueraDeUnNodo(ValueError):
    """La cola es del nodo: encolar en el central o en una instalación común dejaría una fila que nadie emite."""


def _avisar(cola: ColaDeArca, pedido: dict | None) -> None:
    if pedido is None:
        return
    try:
        cola.al_cambiar(payload_de_pedido(pedido))
    except Exception as e:
        logger.error("Nodo: falló al_cambiar de la cola (pedido %s): %s", pedido.get("id"), type(e).__name__)


def encolar(cola: ColaDeArca, origen_tipo: str, origen_id, *, datos: dict, fecha_ref: str = "", concepto: int = 1,
            conn: Conexion | None = None) -> tuple[dict, bool]:
    """Deja un pedido de emisión en la cola del nodo. Devuelve `(pedido, creado)`.

    Idempotente por origen (ver `db.cola_arca.encolar`): pedir lo mismo dos veces no duplica y actualiza los datos
    mientras esté `esperando`; si ya está en otro estado levanta `db.cola_arca.PedidoYaResuelto`.

    Con `conn`, en la transacción de quien llama y **sin avisar** a `al_cambiar` (no está confirmado todavía): el producto
    encola la subida con el pedido que devuelve, en la misma transacción. Sin `conn`, confirma y avisa.

    - `datos`: JSON que el emisor necesita. Poco: el id de su entidad y lo que no se pueda releer después.
    - `fecha_ref`: la fecha del hecho (ISO) —la de la orden, la de la reserva— o, en una `cpe`, la de partida.
    - `concepto`: el de ARCA de una factura (1 productos, 2 servicios, 3 ambos); manda sobre el plazo de la fecha.

    `FueraDeUnNodo` si la instancia no es un nodo; `ValueError` si el producto no registró un emisor para el tipo."""
    if not cola.conexion.es_nodo():
        raise FueraDeUnNodo("La cola de comprobantes es del nodo: esta instancia no es un nodo de LibraEdge.")
    if origen_tipo not in cola.emisores:
        raise ValueError(f"El producto {cola.producto!r} no registró un emisor para {origen_tipo!r}.")
    pedido_id, creado = db.encolar(cola.producto, cola.instancia(), origen_tipo, origen_id, datos=datos,
                                   fecha_ref=fecha_ref, concepto=concepto, conn=conn)
    pedido = db.get_pedido(pedido_id, conn=conn)
    if conn is None:
        _avisar(cola, pedido)
    return pedido, creado


async def encolar_y_emitir(cola: ColaDeArca, origen_tipo: str, origen_id, *, datos: dict, fecha_ref: str = "",
                           concepto: int = 1, usuario_id: int | None = None) -> dict:
    """`encolar` y, si hay conexión, intenta emitir ya. Devuelve el pedido como quedó.

    Es el camino del producto cuando una persona pide el comprobante: la cola es **siempre** la primera parada (la
    intención queda guardada pase lo que pase) y con red sale en el momento; sin red —o si se corta a mitad— queda
    `esperando` y sale solo al volver. Quien mira `pedido["estado"]` sabe cuál de los dos fue. **No admite `conn`**: el
    emisor lee la entidad del producto, que ya tiene que estar confirmada."""
    pedido, _ = encolar(cola, origen_tipo, origen_id, datos=datos, fecha_ref=fecha_ref, concepto=concepto)
    if cola.conexion.sin_conexion():
        return pedido
    try:
        return await emitir_pedido(cola, pedido["id"], usuario_id=usuario_id)
    except (SinConexion, NodoSinArca, EmisionEnCurso):
        return db.get_pedido(pedido["id"])


# ── Emitir ───────────────────────────────────────────────────────────────


def _es_incierta(error: BaseException) -> bool:
    """¿No se sabe si ARCA lo emitió? `arca_wscpe.EmisionIncierta`, o lo que el producto marque con `emision_incierta`."""
    from libracore.arca_wscpe import EmisionIncierta

    return isinstance(error, EmisionIncierta) or getattr(error, "emision_incierta", False) is True


def _mensaje(error: BaseException) -> str:
    return str(error).strip() or type(error).__name__


def _nombre_del_candado(cola: ColaDeArca, pedido_id: int) -> str:
    return f"emitir-cola:{cola.producto}:{pedido_id}"


def _cambiar(cola: ColaDeArca, pedido_id: int, transicion: Callable[..., bool], *args, **kwargs) -> dict | None:
    """Corre una transición de `db.cola_arca`, la confirma y **después** avisa. `None` si el pedido ya no estaba en un
    estado desde el que valga (lo movió otro)."""
    with get_connection() as conn:
        movido = transicion(pedido_id, *args, conn=conn, **kwargs)
    if not movido:
        return None
    pedido = db.get_pedido(pedido_id)
    _avisar(cola, pedido)
    return pedido


def _validar_resultado(origen_tipo: str, resultado) -> None:
    if not isinstance(resultado, dict) or not resultado:
        raise ValueError(f"El emisor de {origen_tipo!r} tiene que devolver un dict con el resultado, no {resultado!r}.")
    if origen_tipo == db.ORIGEN_DOCUMENTO and not resultado.get("factura_id"):
        raise ValueError("El emisor de un documento tiene que devolver el factura_id de la factura que creó.")


async def emitir_pedido(cola: ColaDeArca, pedido_id: int, *, usuario_id: int | None = None) -> dict:
    """Emite un pedido de la cola con su candado y deja el estado. Devuelve el pedido como quedó.

    Levanta, **después de dejar anotado el motivo** (como `facturar_venta` con `FacturaPendiente`):
    `SinConexion` (el pedido sigue `esperando`), `NodoSinArca` (idem, con el motivo) y `EmisionEnCurso` (otro lo está
    emitiendo). Todo lo demás es un estado: `emitido`, `a_revisar` o `incierto`. Un pedido que ya no está `esperando`
    se devuelve tal cual, salvo uno `emitiendo` que nadie tiene en este momento: quedó a medias y pasa a `incierto`.

    Fuera de un nodo no hace nada y devuelve el pedido."""
    pedido = db.get_pedido(pedido_id)
    if pedido is None or pedido["origen_producto"] != cola.producto or pedido["origen_instancia"] != cola.instancia():
        raise PedidoNoEncontrado(f"No hay un pedido {pedido_id} de {cola.producto} en este nodo.")
    if not cola.conexion.es_nodo():
        return pedido
    emisor = cola.emisores.get(pedido["origen_tipo"])
    if emisor is None:
        raise ValueError(f"El producto {cola.producto!r} no registró un emisor para {pedido['origen_tipo']!r}.")

    with get_connection() as conn, candados.candado_de_base(conn, _nombre_del_candado(cola, pedido_id)) as tomado:
        if not tomado:
            raise EmisionEnCurso(f"El pedido {pedido_id} ya se está emitiendo: esperá unos segundos.")
        return await _emitir_con_candado(cola, pedido_id, emisor, usuario_id)


async def _emitir_con_candado(cola: ColaDeArca, pedido_id: int, emisor, usuario_id: int | None) -> dict:
    pedido = db.get_pedido(pedido_id)   # releído adentro del candado: lo que importa es lo que hay ahora
    if pedido["estado"] == db.ESTADO_EMITIENDO:
        # Con el candado en la mano, nadie lo está emitiendo: el que lo tomó murió. No se sabe si llegó a ARCA.
        logger.error("Nodo: el pedido %s de la cola quedó a medias: pasa a incierto", pedido_id)
        return _cambiar(cola, pedido_id, db.marcar_incierto, MENSAJE_A_MEDIAS) or db.get_pedido(pedido_id)
    if pedido["estado"] != db.ESTADO_ESPERANDO:
        return pedido
    if cola.conexion.sin_conexion():
        raise SinConexion(MENSAJE_SIN_RED)
    if not db.tomar_para_emitir(pedido_id):
        return db.get_pedido(pedido_id)

    pedido = db.get_pedido(pedido_id)
    ctx = ContextoDeEmision(cola=cola, pedido=pedido, usuario_id=usuario_id, ahora=fechas.ahora())
    try:
        resultado = emisor(pedido, ctx)
        if inspect.isawaitable(resultado):
            resultado = await resultado
        _validar_resultado(pedido["origen_tipo"], resultado)
    except Exception as e:
        return _tras_el_error(cola, pedido_id, e)
    return _tras_emitir(cola, pedido, resultado)


def _tras_emitir(cola: ColaDeArca, pedido: dict, resultado: dict) -> dict:
    pedido_id = pedido["id"]
    if pedido["origen_tipo"] == db.ORIGEN_DOCUMENTO and not resultado.get("cae"):
        logger.warning("Nodo: el pedido %s de la cola salió sin CAE: a revisar", pedido_id)
        motivo = str(resultado.get("cae_error") or "").strip() or MENSAJE_SIN_CAE
        return _cambiar(cola, pedido_id, db.marcar_a_revisar, motivo, resultado) or db.get_pedido(pedido_id)
    logger.info("Nodo: el pedido %s de la cola (%s %s) se emitió", pedido_id, pedido["origen_tipo"], pedido["origen_id"])
    return _cambiar(cola, pedido_id, db.marcar_emitido, resultado) or db.get_pedido(pedido_id)


def _tras_el_error(cola: ColaDeArca, pedido_id: int, error: Exception) -> dict:
    """Qué hacer con lo que levantó el emisor. Ver la tabla del docstring del módulo. Sin el texto del error en el log:
    puede traer un token; en la fila sí, que es lo que ve la persona."""
    if _es_incierta(error):
        logger.error("Nodo: el pedido %s de la cola quedó incierto (%s)", pedido_id, type(error).__name__)
        return _cambiar(cola, pedido_id, db.marcar_incierto, _mensaje(error)) or db.get_pedido(pedido_id)
    if isinstance(error, SinConexion) or es_corte(error):
        db.devolver_a_esperando(pedido_id, MENSAJE_SIN_RED)
        if isinstance(error, SinConexion):
            raise error
        raise SinConexion(MENSAJE_SIN_RED) from error
    if es_rechazo(error):
        motivo = _mensaje(error)
        db.devolver_a_esperando(pedido_id, motivo)
        if isinstance(error, NodoSinArca):
            raise error
        raise RechazoDelCentral(motivo) from error
    logger.error("Nodo: el pedido %s de la cola no se pudo emitir: %s", pedido_id, type(error).__name__)
    return _cambiar(cola, pedido_id, db.marcar_a_revisar, _mensaje(error)) or db.get_pedido(pedido_id)


# ── Al volver la conexión ────────────────────────────────────────────────


def pedidos_a_emitir(cola: ColaDeArca) -> int:
    """Cuántos pedidos de este nodo esperan salir (0 fuera de un nodo)."""
    if not cola.conexion.es_nodo():
        return 0
    return db.contar_por_estado(cola.producto, cola.instancia())[db.ESTADO_ESPERANDO]


def resumen(cola: ColaDeArca) -> dict:
    """`{"es_nodo", "en_linea", "node_id", "cuentas": {estado: n}, "a_revisar": n}`: lo que la pantalla avisa."""
    c = cola.conexion
    cuentas = db.contar_por_estado(cola.producto, cola.instancia()) if c.es_nodo() else dict.fromkeys(db.ESTADOS, 0)
    return {"es_nodo": c.es_nodo(), "en_linea": not c.sin_conexion(), "node_id": c.node_id(), "cuentas": cuentas,
            "a_revisar": sum(cuentas[e] for e in db.ESTADOS_DE_REVISION)}


async def procesar_cola(cola: ColaDeArca, *, limite: int = 20, usuario_id: int | None = None) -> dict:
    """Emite los pedidos que el nodo dejó sin red, **los más viejos primero** (por id, sea cual sea el tipo).

    Cada uno con su candado (`emitir_pedido`): la tanda automática y el botón a la vez no emiten dos veces. Un pedido
    `emitiendo` que nadie tiene pasa a `incierto`.

    - Si la conexión se vuelve a cortar a mitad de tanda, se corta ahí: lo que queda, queda para la próxima.
    - `NodoSinArca` (el central no presta el ticket de ese servicio) corta **ese tipo** y se informa; los otros siguen.
    - Un pedido que falla no frena a los demás: ARCA rechazar una CPE no impide emitir las facturas que están detrás.

    Devuelve `{"es_nodo", "en_linea", "emitidos": [{pedido_id, origen_tipo, origen_id, resultado}], "a_revisar",
    "inciertos", "errores": [{pedido_id, error}], "quedan"}`. Fuera de un nodo no hace nada."""
    conexion = cola.conexion
    salida = {"es_nodo": conexion.es_nodo(), "en_linea": not conexion.sin_conexion(), "emitidos": [], "a_revisar": [],
              "inciertos": [], "errores": [], "quedan": 0}
    if not salida["es_nodo"] or not salida["en_linea"]:
        salida["quedan"] = pedidos_a_emitir(cola)
        return salida

    cortados: set[str] = set()
    # Un `limite` por tipo y no uno para todos: si las CPE se traban (el central no presta `wscpe`) y siguen
    # `esperando`, ocuparían el cupo para siempre y las facturas de atrás no saldrían nunca.
    pedidos = sorted((f for tipo in cola.emisores for f in db.listar(
        estados=(db.ESTADO_ESPERANDO, db.ESTADO_EMITIENDO), origen_producto=cola.producto,
        origen_instancia=cola.instancia(), origen_tipo=tipo, limite=limite)), key=lambda f: f["id"])
    for fila in pedidos:
        if fila["origen_tipo"] in cortados:
            continue
        if conexion.sin_conexion():
            salida["en_linea"] = False
            break
        item = {"pedido_id": fila["id"], "origen_tipo": fila["origen_tipo"], "origen_id": fila["origen_id"]}
        try:
            hecho = await emitir_pedido(cola, fila["id"], usuario_id=usuario_id)
        except SinConexion:
            salida["en_linea"] = False
            break
        except EmisionEnCurso:
            continue
        except NodoSinArca as e:
            salida["errores"].append({**item, "error": str(e)})
            cortados.add(fila["origen_tipo"])
            continue
        except Exception as e:
            logger.error("Nodo: no se pudo emitir el pedido %s de la cola: %s", fila["id"], type(e).__name__)
            salida["errores"].append({**item, "error": type(e).__name__})
            continue
        if hecho["estado"] == db.ESTADO_EMITIDO:
            salida["emitidos"].append({**item, "resultado": hecho["resultado"]})
        elif hecho["estado"] == db.ESTADO_A_REVISAR:
            salida["a_revisar"].append({**item, "error": hecho["ultimo_error"]})
        elif hecho["estado"] == db.ESTADO_INCIERTO:
            salida["inciertos"].append({**item, "error": hecho["ultimo_error"]})
    salida["quedan"] = pedidos_a_emitir(cola)
    return salida


def arrancar_cola_al_volver(cola: ColaDeArca, *, intervalo: float = INTERVALO_AL_VOLVER,
                            habilitada: Callable[[], bool] = lambda: True,
                            en_cada_vuelta: Callable[[], object] | None = None) -> threading.Event | None:
    """En un nodo, arranca un hilo que cada `intervalo` segundos emite la cola si hay conexión.

    El hermano de `venta_facturacion.arrancar_facturacion_al_volver` (ADR-074), con el mismo contrato: devuelve el `Event`
    que lo para (`.set()`), o `None` si la instancia no es un nodo; `en_cada_vuelta` se llama una vez al arrancar y en cada
    vuelta **después** de emitir, haya conexión o no (es donde el producto reconcilia lo que `al_cambiar` no llegó a
    subir); un error suyo o al emitir se registra —sólo el tipo, sin datos— y no corta el ciclo. Un producto con
    `PuertoDeVentas` y cola puede usar un solo hilo: `arrancar_facturacion_al_volver(ventas, en_cada_vuelta=lambda:
    asyncio.run(procesar_cola(cola)))`. Con varios workers, cada uno arranca el suyo y el candado por pedido evita la doble
    emisión."""
    if not cola.conexion.es_nodo():
        return None
    parar = threading.Event()

    def _reconciliar() -> None:
        if en_cada_vuelta is None:
            return
        try:
            en_cada_vuelta()
        except Exception as e:
            logger.error("Nodo: falló en_cada_vuelta de la cola de comprobantes: %s", type(e).__name__)

    def _bucle() -> None:
        _reconciliar()
        while not parar.wait(intervalo):
            try:
                if habilitada() and not cola.conexion.sin_conexion():
                    asyncio.run(procesar_cola(cola))
            except Exception as e:
                logger.error("Nodo: falló la emisión automática de la cola de comprobantes: %s", type(e).__name__)
            _reconciliar()

    threading.Thread(target=_bucle, name="libracore-cola-al-volver", daemon=True).start()
    return parar


# ── Lo que decide una persona ────────────────────────────────────────────


def reintentar(cola: ColaDeArca, pedido_id: int, *, datos: dict | None = None,
               confirmo_que_no_salio: bool = False) -> dict:
    """`a_revisar` o `incierto` → `esperando`, con `datos` corregidos si se pasan: sale en la próxima tanda.

    🔴 Un pedido `incierto` exige `confirmo_que_no_salio=True`: quien lo reabre dice que **consultó a ARCA** y la
    factura o la carta no figura. Si sí figura es `marcar_emitido`, no esto. Levanta `PedidoNoEncontrado`, `ConfirmacionRequerida`
    y `ValueError` si el pedido no está en un estado desde el que se pueda reabrir."""
    pedido = _del_nodo(cola, pedido_id)
    if pedido["estado"] == db.ESTADO_INCIERTO and not confirmo_que_no_salio:
        raise ConfirmacionRequerida(
            "No se sabe si ARCA lo emitió: consultalo en ARCA y, si no figura, confirmá que no salió para reintentar.")
    nuevo = _cambiar(cola, pedido_id, db.reabrir, datos=datos)
    if nuevo is None:
        raise ValueError(f"El pedido {pedido_id} está {pedido['estado']}: sólo se reabre uno a revisar o incierto.")
    return nuevo


def descartar(cola: ColaDeArca, pedido_id: int, motivo: str = "") -> dict:
    """Cierra el pedido sin emitirlo (`esperando`, `a_revisar` o `incierto`). `ValueError` si ya salió o se está emitiendo."""
    pedido = _del_nodo(cola, pedido_id)
    nuevo = _cambiar(cola, pedido_id, db.descartar, motivo)
    if nuevo is None:
        raise ValueError(f"El pedido {pedido_id} está {pedido['estado']}: no se puede descartar.")
    return nuevo


def marcar_emitido(cola: ColaDeArca, pedido_id: int, resultado: dict) -> dict:
    """Una persona comprobó que **sí salió** (la CPE figura en ARCA, la factura se autorizó a mano): `incierto` o
    `a_revisar` → `emitido`, con el `resultado` (el CTG, el `factura_id`). `ValueError` si el pedido no está en revisión."""
    pedido = _del_nodo(cola, pedido_id)
    _validar_resultado(pedido["origen_tipo"], resultado)
    if pedido["estado"] not in db.ESTADOS_DE_REVISION:
        raise ValueError(f"El pedido {pedido_id} está {pedido['estado']}: sólo se marca emitido uno a revisar o incierto.")
    nuevo = _cambiar(cola, pedido_id, db.marcar_emitido, resultado)
    if nuevo is None:
        raise ValueError(f"El pedido {pedido_id} cambió de estado mientras tanto.")
    return nuevo


def _del_nodo(cola: ColaDeArca, pedido_id: int) -> dict:
    pedido = db.get_pedido(pedido_id)
    if pedido is None or pedido["origen_producto"] != cola.producto or pedido["origen_instancia"] != cola.instancia():
        raise PedidoNoEncontrado(f"No hay un pedido {pedido_id} de {cola.producto} en este nodo.")
    return pedido


# ── La subida al central ─────────────────────────────────────────────────

_CAMPOS = ("origen_producto", "origen_instancia", "origen_tipo", "origen_id", "estado", "datos", "fecha_ref", "concepto",
           "intentos", "ultimo_error", "resultado", "revision", "resuelto_at", "created_at")


def payload_de_pedido(pedido: dict) -> dict:
    """Lo que viaja al central de un pedido, en JSON. Los ids (`id_en_el_nodo`, el `factura_id` del `resultado`) son **los
    del nodo**. El producto lo encola en `ColaDeArca.al_cambiar` y el central lo aplica con `aplicar_pedido_del_nodo`."""
    datos = {campo: pedido.get(campo) for campo in _CAMPOS}
    datos["id_en_el_nodo"] = pedido.get("id")
    datos["version"] = VERSION
    return datos


def aplicar_pedido_del_nodo(payload: dict, *, conn: Conexion | None = None) -> tuple[int, bool]:
    """**En el central.** Escribe la copia de un pedido del nodo. Devuelve `(id, aplicada)`.

    Idempotente y a prueba de desorden: sólo pisa con una `revision` mayor (el outbox entrega al menos una vez y no
    necesariamente en orden); `aplicada=False` es «ya tenía algo igual o más nuevo». Con `conn`, en la transacción del
    receptor, sin confirmar. La copia es de consulta: el central no emite nada de ella (no es un nodo)."""
    if int(payload.get("version") or 0) != VERSION:
        raise ValueError(f"versión de pedido del nodo no soportada: {payload.get('version')!r}")
    if not str(payload.get("origen_instancia") or "").strip():
        raise ValueError("origen_instancia es obligatorio: sin el id del nodo, dos nodos se pisarían")
    return db.aplicar_copia(payload, conn=conn)
