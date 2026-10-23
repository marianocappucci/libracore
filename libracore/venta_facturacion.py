"""La factura de una venta del punto de venta, y lo que pasa cuando el QR se
paga (P9-M3).

Hasta acá esto estaba escrito dos veces —`app/venta_facturacion.py` de
Contalibra y de Restolibra— con 145 líneas de diferencia que eran casi todas
docstrings. Las dos diferencias reales, y cómo quedaron:

1. **La alícuota.** Contalibra lee `ventas_origen_externo`: una venta que llegó
   de otro producto (MedLibra, con prestaciones exentas) trae la suya. Eso es
   el puerto `alicuota_de(venta_id)`; el default es `None` → 21 %.
2. **El punto de venta.** Restolibra resuelve el del mostrador donde se cobró
   (usuario → turno abierto → caja → punto de venta, `resolver_punto_venta`) y
   cae al de la empresa. Contalibra siempre el de la empresa. Queda la cadena de
   Restolibra para los dos: en una instancia de un solo POS da lo mismo.

El módulo no sabe dónde viven las ventas: las lee y las escribe por
`PuertoDeVentas`, que el producto arma sobre `libracommerce.erp.ventas` (o sobre
lo que tenga). Es el mismo criterio que `recibos.emitir_recibo_venta(get_venta=)`.

Dos cosas que **no** hace, a propósito, y que lo distinguen de
`mp_facturacion.generar_factura_mp`:

- **No registra movimiento de caja.** La venta ya registró uno por cada pago
  acreditado, en la misma transacción que la creó. Volver a registrarlo
  contaría el ingreso dos veces; lo que hace es **vincular** los que ya están
  (`vincular_cobros`).
- **No crea un cliente.** Una venta sin cliente se factura a Consumidor Final
  sin persistir nada: un mostrador con 200 ventas por día llenaría `clients` en
  una semana.

## En un nodo offline de LibraEdge (ADR-074)

`PuertoDeVentas.conexion` dice si la instancia es un nodo y si está en línea
(`libracore.nodo_offline`). Fuera de un nodo no cambia nada. En un nodo:

- **Con conexión, se factura como siempre**, con una diferencia: si ARCA no da
  el número, **no** se cae a la numeración local (en un nodo choca con la de
  ARCA y con la del central). La venta va a la bandeja, como sin conexión.
- **Sin conexión, no se sale a ARCA.** La venta ya está cobrada y tiene su
  ticket (no válido como factura); entra a la bandeja de comprobantes
  pendientes (`origen_tipo = "venta"`, `origen_instancia = node_id`) y
  `facturar_venta` levanta `nodo_offline.FacturaPendiente` (la API: 409
  `factura_pendiente`).
- **Al volver**, `facturar_pendientes_del_nodo` (sola, con
  `arrancar_facturacion_al_volver`, o a mano desde
  `POST /api/nodo/facturar-pendientes`) factura cada una **desde la venta** y
  cierra su fila de la bandeja.

**Una sola factura por venta**: `facturar_venta` toma un candado por venta
(`candados.candado_de_base`, entre procesos) y relee la venta adentro. Dos
intentos a la vez (la tanda automática y el botón, el webhook y el poll) no
piden dos CAE: el segundo recibe `FacturacionEnCurso`.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from libracore import arca_credenciales, candados, config_manager, fechas
from libracore import pdf_generator as pdf_gen
from libracore.arca_facturacion import (
    ambiente_de,
    get_next_numero_with_arca,
    solicitar_cae,
    tipo_de_comprobante,
)
from libracore.db import arca_config as db_arca
from libracore.db import clients as db_clients
from libracore.db import comprobantes_pendientes as db_pendientes
from libracore.db import facturas as db_facturas
from libracore.db.caja import resolver_punto_venta
from libracore.db.core import get_connection
from libracore.nodo_offline import (
    FUERA_DE_NODO,
    ConexionDelNodo,
    FacturaPendiente,
    NodoSinArca,
    SinConexion,
)

logger = logging.getLogger(__name__)

# Cliente sintético para la venta sin cliente asignado. No se guarda en
# `clients`: viaja hasta `create_factura`, que snapshotea razón social, CUIT y
# domicilio en la propia factura.
CONSUMIDOR_FINAL = {
    "name": "Consumidor Final",
    "cuit_dni": "",
    "iva_condition": "Consumidor Final",
    "address": "",
    "email": "",
}

_IVA_CODES = {
    "Responsable Inscripto": 1, "IVA Responsable Inscripto": 1,
    "Monotributista": 6, "Responsable Monotributo": 6,
    "IVA Exento": 4, "Consumidor Final": 5,
    "No Alcanzado": 3, "IVA No Responsable": 3,
}
_TIPO_LABEL = {1: "Factura A", 6: "Factura B", 11: "Factura C"}

#: Misma tasa por defecto que el formulario manual (`FacturaPayload.tax_rate`).
IVA_RATE_DEFAULT = 0.21

#: Cuántos días para atrás admite ARCA la fecha de un comprobante (`CbteFch` de wsfe, RG 4291): 5 para productos
#: (concepto 1) y 10 para servicios o productos y servicios (2 y 3). Hacia adelante admite lo mismo; acá no se usa.
DIAS_ATRAS_SEGUN_CONCEPTO = {1: 5, 2: 10, 3: 10}


def fecha_de_la_factura(fecha_venta, *, concepto: int = 1, hoy: datetime.date | None = None,
                        piso: datetime.date | None = None) -> datetime.date:
    """La fecha de la factura de una venta que se factura al volver la conexión (ADR-074, decisión del humano).

    - La **de la venta**, si está dentro de lo que ARCA admite para atrás según el concepto (5 días en productos, 10 en
      servicios), bordes incluidos: una venta de hace exactamente 5 días sale con su fecha.
    - **Hoy**, si pasó más, si la fecha no se lee o si es del futuro.
    - Nunca antes que `piso`: la fecha del último comprobante de ese punto de venta y tipo. 🔴 ARCA rechaza un
      comprobante con fecha anterior a la del último autorizado de la misma serie (la correlatividad es por número y
      por fecha), y al volver la conexión el nodo puede haber facturado ventas nuevas con la fecha de hoy antes de
      vaciar la bandeja.
    """
    hoy = hoy or fechas.hoy()
    dia = fechas.dia_iso(str(fecha_venta or "")[:10])
    dias = DIAS_ATRAS_SEGUN_CONCEPTO.get(int(concepto or 1), 5)
    elegida = dia if dia is not None and hoy - datetime.timedelta(days=dias) <= dia <= hoy else hoy
    if piso is not None and elegida < piso <= hoy:
        elegida = piso
    return elegida


def _ultima_fecha_de_la_serie(punto_venta: int, tipo: int, ambiente: str, emisor_id: int | None = None
                              ) -> datetime.date | None:
    """La fecha del último comprobante de la serie. `emisor_id` es el de `facturas.emisor_id` (`None` o 0: el emisor
    por defecto, que es el de `arca_config` sin id)."""
    with get_connection() as conn:
        fila = conn.execute(
            "SELECT MAX(fecha) FROM facturas WHERE punto_venta=? AND tipo=? AND ambiente=? "
            "AND COALESCE(emisor_id, 0) = ?",
            (punto_venta, tipo, ambiente, int(emisor_id or 0)),
        ).fetchone()
    return fechas.dia_iso(str(fila[0])[:10]) if fila and fila[0] else None


def fecha_de_la_factura_en_la_serie(fecha_hecho, punto_venta: int, tipo: int, ambiente: str, *, concepto: int = 1,
                                    emisor_id: int | None = None, hoy: datetime.date | None = None) -> datetime.date:
    """`fecha_de_la_factura` con el `piso` de la serie `(punto_venta, tipo, ambiente, emisor)` ya resuelto.

    Es lo que necesita quien emite una factura **al volver la conexión** y conoce la serie recién cuando numera: la cola
    de comprobantes del nodo (`libracore.cola_arca`, ADR-079) la usa para los `documento` que no nacen de una venta."""
    return fecha_de_la_factura(fecha_hecho, concepto=concepto, hoy=hoy,
                               piso=_ultima_fecha_de_la_serie(punto_venta, tipo, ambiente, emisor_id))


# Los ids son los de `medios_pago.ELEGIBLES`; los valores, los de
# `CONDICIONES_VENTA` de `facturas_router` (lo que acepta ARCA).
#
# 🔴 **La tarjeta va partida porque ARCA la parte.** Débito y crédito son dos
# condiciones de venta distintas en el comprobante. Las grafías históricas
# entran también: hay ventas viejas con esos medios, y facturarlas después no
# puede caer en "Otra" sólo porque la grafía cambió.
_MEDIO_A_CONDICION = {
    "efectivo": "Contado",
    "transferencia": "Transferencia Bancaria",
    "tarjeta_debito": "Tarjeta de Débito",
    "tarjeta_credito": "Tarjeta de Crédito",
    "cheque": "Cheque",
    "mercadopago": "Otros medios de pago electrónico",
    "cuenta_dni": "Otros medios de pago electrónico",
    "billetera": "Otros medios de pago electrónico",
    "cuenta_corriente": "Cuenta Corriente",
    "tarjeta": "Tarjeta de Crédito",
    "debito": "Tarjeta de Débito",
    "credito": "Tarjeta de Crédito",
    "mercado_pago": "Otros medios de pago electrónico",
    "qr": "Otros medios de pago electrónico",
}


class VentaNoFacturable(Exception):
    """La venta no está en condiciones de emitirse (inexistente, anulada o sin
    ítems)."""


class FacturacionEnCurso(Exception):
    """Otro proceso está facturando esta misma venta ahora. No se emite otra: la API contesta 409."""


def _sin_alicuota_propia(venta_id: int) -> float | None:
    return None


def _nada(*args) -> None:
    return None


#: El `origen_producto` de la bandeja cuando el producto no dice el suyo.
PRODUCTO_POR_DEFECTO = "ventas"


@dataclass(frozen=True)
class PuertoDeVentas:
    """Cómo este módulo y el router del cobro llegan a las ventas del producto.

    Cada callable recibe y devuelve lo que `libracommerce.erp.ventas` ya
    devuelve: `obtener` el dict de la venta (`id`, `numero`, `total`, `items`,
    `pagos`, `estado`, `status`, `factura_id`, `usuario_id`, `mp_payment_id`),
    los demás escriben y commitean por su cuenta.
    """

    obtener: Callable[[int], dict | None]
    vincular_factura: Callable[[int, int], None]
    #: `(numero, factura_id) -> cuántos movimientos de caja vinculó`.
    vincular_cobros: Callable[[str, int], int]
    set_pago_mp: Callable[[int, str], None]
    #: `(venta_id, payment_id, usuario_id) -> acreditó algo`.
    acreditar: Callable[[int, str, int | None], bool]
    sellar_referencia_mp: Callable[[int, str], None]
    set_orden_mp: Callable[[int, str], None] = _nada
    #: La alícuota que trae la venta, o `None` para la default.
    alicuota_de: Callable[[int], float | None] = _sin_alicuota_propia
    #: Quién numera el comprobante: `(punto_venta, tipo) -> (numero, ta, arca)`.
    #: `None` es `arca_facturacion.get_next_numero_with_arca`. Existe para que el
    #: producto lo intercepte —su suite fija el ambiente parcheando el numerador,
    #: porque con `ENV=development` todo sale `produccion` y la marca es invisible—.
    numerar_comprobante: Callable[[int, int], Awaitable[tuple]] | None = None
    #: Si la instancia es un nodo offline y si está en línea (ADR-074). El default no es un nodo: nada cambia.
    conexion: ConexionDelNodo = FUERA_DE_NODO
    #: El nombre del producto (`ventalibra`): el `origen_producto` de la bandeja y del candado por venta.
    producto: str = PRODUCTO_POR_DEFECTO
    #: `(venta, factura) -> None`, después de vincular la factura a la venta. En un nodo es donde el producto encola
    #: la subida de la factura al central (`facturas_del_nodo.payload_factura`). Un error acá se registra y no se
    #: propaga: la factura ya existe y tiene su CAE.
    al_facturar: Callable[[dict, dict], None] = _nada


def _tipo_comprobante(emisor_cond: str, cliente_cond: str) -> int:
    """A/B/C según el emisor y la condición del cliente (ver `tipo_de_comprobante`)."""
    return tipo_de_comprobante(emisor_cond, cliente_cond)


def _armar_items(venta: dict, iva_rate: float) -> tuple[list, float, float, float]:
    """Convierte las líneas de la venta en líneas de factura.

    Los precios de una venta son finales (con IVA adentro); los de una factura
    son netos y el IVA se suma aparte. Con `iva_rate > 0` cada línea se
    desagrega, y el IVA del comprobante se calcula como la diferencia contra el
    total de la venta: así el total de la factura coincide **exacto** con lo
    que ya entró a la caja, sin arrastrar el redondeo de cada línea.
    """
    total_venta = round(float(venta["total"]), 2)
    divisor = 1 + iva_rate

    items = []
    for it in venta["items"]:
        neto_linea = round(float(it["subtotal"]) / divisor, 2)
        items.append({
            "description": it["nombre"],
            "qty": float(it["qty"]),
            "unit_price": round(float(it["precio"]) / divisor, 2),
            "subtotal": neto_linea,
        })

    descuento = round(float(venta.get("descuento") or 0), 2)
    if descuento:
        neto_desc = round(descuento / divisor, 2)
        items.append({
            "description": "Descuento", "qty": 1,
            "unit_price": -neto_desc, "subtotal": -neto_desc,
        })

    subtotal = round(sum(i["subtotal"] for i in items), 2)

    if iva_rate:
        iva_amount = round(total_venta - subtotal, 2)
        total = total_venta
    else:
        # Sin IVA discriminado las líneas ya son el total. Si no coincide con
        # el de la venta, manda la venta: es la plata que está en la caja.
        iva_amount = 0.0
        total = subtotal
        if abs(total - total_venta) > 0.01:
            logger.warning(
                "Venta %s: las líneas suman %.2f y la venta dice %.2f — se factura por las líneas",
                venta["id"], total, total_venta,
            )

    return items, subtotal, iva_amount, total


def _condicion_venta(venta: dict) -> str:
    pagos = venta.get("pagos") or []
    if len(pagos) == 1:
        return _MEDIO_A_CONDICION.get(pagos[0].get("medio", ""), "Otra")
    if len(pagos) > 1:
        return "Otra"
    return "Contado"


def _punto_venta(venta: dict) -> int:
    """El del mostrador donde se cobró, o el de la empresa.

    Se resuelve por el `usuario_id` **de la venta** y no por el de la sesión: la
    auto-factura la dispara el webhook, donde no hay nadie logueado. Sin esto,
    un cobro por QR numeraría por el punto de venta de la empresa y el mismo
    cobro por efectivo por el del mostrador — dos series para la misma caja.
    """
    propio = resolver_punto_venta(venta.get("usuario_id"))
    if propio:
        return propio
    cfg = db_arca.config_del_emisor()
    return cfg.get("punto_venta", 1) if cfg else 1


def _nombre_del_candado(ventas: PuertoDeVentas, venta_id: int) -> str:
    return f"facturar-venta:{ventas.producto}:{venta_id}"


async def facturar_venta(ventas: PuertoDeVentas, venta_id: int, *,
                         usuario_id: int | None = None) -> dict:
    """Emite la factura de una venta, pide el CAE, genera el PDF y las vincula.

    Idempotente: si la venta ya tiene factura devuelve esa, sin emitir otra. Es
    lo que sostiene el reintento del webhook de MercadoPago y el poll de
    `mp-status`, que le pega cada pocos segundos mientras el cliente escanea.
    Dos llamadas **a la vez** sobre la misma venta no emiten dos: la segunda
    levanta `FacturacionEnCurso` (candado por venta, entre procesos).

    En un nodo sin conexión (o si ARCA no da el número) no emite: deja la venta
    en la bandeja y levanta `nodo_offline.FacturaPendiente`. Sin `arca_config`
    espejada o sin forma de autenticarse, `nodo_offline.NodoSinArca`.

    No toca la caja — ver el docstring del módulo.
    """
    with get_connection() as conn, candados.candado_de_base(conn, _nombre_del_candado(ventas, venta_id)) as tomado:
        if not tomado:
            raise FacturacionEnCurso(f"La venta {venta_id} ya se está facturando: esperá unos segundos.")
        return await _facturar_venta(ventas, venta_id, usuario_id=usuario_id)


def _items_de_la_bandeja(items: list, iva_rate: float) -> list:
    """Las líneas de la factura con la forma de la bandeja (`iva_rate` por línea: `calcular_total` da el total)."""
    return [{"description": i["description"], "qty": i["qty"], "unit_price": i["unit_price"],
             "iva_rate": iva_rate} for i in items]


def _a_la_bandeja(ventas: PuertoDeVentas, venta: dict, cliente: dict, items: list, iva_rate: float,
                  motivo: str) -> FacturaPendiente:
    """Deja la venta en la bandeja «a facturar» del nodo y devuelve la excepción para levantar.

    Idempotente por origen (`upsert_comprobante`): reintentar sin conexión no duplica la fila."""
    node_id = ventas.conexion.node_id() or ""
    try:
        pendiente_id, _ = db_pendientes.upsert_comprobante(
            origen_producto=ventas.producto, origen_tipo=db_pendientes.ORIGEN_VENTA,
            origen_id=str(venta["id"]), origen_instancia=node_id,
            cliente_id=venta.get("cliente_id") if cliente is not CONSUMIDOR_FINAL else None,
            cliente_razon=cliente.get("name") or CONSUMIDOR_FINAL["name"],
            cliente_cuit=cliente.get("cuit_dni", "") or "", cliente_domicilio=cliente.get("address", "") or "",
            fecha_sugerida=str(venta.get("fecha") or "")[:10], concepto="",
            condicion_venta=_condicion_venta(venta), items=_items_de_la_bandeja(items, iva_rate),
            observaciones=f"Venta {venta['numero']}: {motivo}",
        )
    except db_pendientes.ComprobanteYaResuelto:
        # Alguien la descartó de la bandeja: no se vuelve a abrir sola.
        fila = db_pendientes.get_por_origen(ventas.producto, node_id, db_pendientes.ORIGEN_VENTA, venta["id"])
        pendiente_id = fila["id"] if fila else None
    logger.warning("Nodo %s: la venta %s queda para facturar al volver (%s), pendiente %s",
                   node_id, venta["numero"], motivo, pendiente_id)
    return FacturaPendiente(comprobante_pendiente_id=pendiente_id, motivo=motivo)


def _cerrar_pendiente(ventas: PuertoDeVentas, venta_id: int, factura_id: int, usuario_id) -> None:
    """Si la venta estaba en la bandeja del nodo, la marca facturada con esta factura."""
    if not ventas.conexion.es_nodo():
        return
    fila = db_pendientes.get_por_origen(ventas.producto, ventas.conexion.node_id() or "",
                                        db_pendientes.ORIGEN_VENTA, venta_id)
    if fila and fila["estado"] in db_pendientes.ESTADOS_ABIERTOS:
        db_pendientes.marcar_facturado(fila["id"], factura_id, usuario=str(usuario_id or ""))


def pendiente_de_facturar(ventas: PuertoDeVentas, venta_id: int) -> dict | None:
    """La fila abierta de la bandeja de esta venta (nodo), o `None`. Para el ticket: «no válido como factura»."""
    if not ventas.conexion.es_nodo():
        return None
    fila = db_pendientes.get_por_origen(ventas.producto, ventas.conexion.node_id() or "",
                                        db_pendientes.ORIGEN_VENTA, venta_id)
    return fila if fila and fila["estado"] in db_pendientes.ESTADOS_ABIERTOS else None


async def _facturar_venta(ventas: PuertoDeVentas, venta_id: int, *, usuario_id: int | None) -> dict:
    venta = ventas.obtener(venta_id)
    if not venta:
        raise VentaNoFacturable(f"La venta {venta_id} no existe.")

    if venta.get("factura_id"):
        logger.info("Venta %s ya facturada (factura %s), no se reemite",
                    venta_id, venta["factura_id"])
        _cerrar_pendiente(ventas, venta_id, venta["factura_id"], usuario_id)
        return db_facturas.get_factura(venta["factura_id"])

    # `status` es el crudo; `estado` puede venir pisado por `status_detail`.
    if venta.get("status") == "cancelled" or venta.get("estado") == "anulada":
        raise VentaNoFacturable(f"La venta {venta_id} está anulada.")

    cfg = config_manager.load()
    emisor_cond = cfg.get("empresa_iva_condition", "Monotributista")

    cliente = CONSUMIDOR_FINAL
    if venta.get("cliente_id"):
        registrado = db_clients.get_client(venta["cliente_id"])
        if registrado:
            cliente = registrado

    tipo = _tipo_comprobante(emisor_cond, cliente.get("iva_condition", "Consumidor Final"))
    # El tipo C no discrimina IVA, venga la venta de donde venga.
    if tipo == 11:
        iva_rate = 0.0
    else:
        propia = ventas.alicuota_de(venta_id)
        iva_rate = IVA_RATE_DEFAULT if propia is None else float(propia)

    items, subtotal, iva_amount, total = _armar_items(venta, iva_rate)
    if not items:
        raise VentaNoFacturable(f"La venta {venta_id} no tiene ítems.")

    conexion = ventas.conexion
    en_nodo = conexion.es_nodo()
    if en_nodo and conexion.sin_conexion():
        raise _a_la_bandeja(ventas, venta, cliente, items, iva_rate, "sin conexión")

    punto_venta = _punto_venta(venta)
    numerar = ventas.numerar_comprobante or get_next_numero_with_arca
    try:
        numero, ta, arca = await numerar(punto_venta, tipo)
    except SinConexion as e:
        if not en_nodo:
            raise
        raise _a_la_bandeja(ventas, venta, cliente, items, iva_rate, e.mensaje) from None

    if en_nodo and not ta:
        # 🔴 En un nodo no hay numeración local: `get_next_numero_with_arca` cae a ella cuando ARCA no contesta
        # (o no hay ARCA), y ese número choca con el de ARCA y con el del central.
        if not arca:
            raise NodoSinArca(
                "Este equipo no tiene la configuración de ARCA del central: hay que espejar arca_config.")
        if ventas.numerar_comprobante is None and not all(arca_credenciales.paths_en_disco(arca)):
            raise NodoSinArca(
                "Este equipo no tiene con qué autenticarse en ARCA: configurá el ticket prestado del central "
                "(nodo_offline.numerador_con_ta).")
        raise _a_la_bandeja(ventas, venta, cliente, items, iva_rate, "ARCA no respondió")

    fecha = fechas.hoy()
    if en_nodo and pendiente_de_facturar(ventas, venta_id) is not None:
        # Al volver: la fecha de la venta, si ARCA todavía la admite (ver `fecha_de_la_factura`).
        fecha = fecha_de_la_factura(venta.get("fecha"), concepto=1,
                                    piso=_ultima_fecha_de_la_serie(punto_venta, tipo, ambiente_de(arca)))

    factura_id = db_facturas.create_factura(
        tipo=tipo, punto_venta=punto_venta, numero=numero,
        fecha=fecha.isoformat(),
        cliente_cuit=cliente.get("cuit_dni", ""),
        cliente_razon=cliente["name"],
        cliente_iva_cond=_IVA_CODES.get(cliente.get("iva_condition", "Consumidor Final"), 5),
        items=items,
        subtotal=subtotal, iva_amount=iva_amount, total=total,
        concepto=1,  # Productos
        observaciones=f"Venta {venta['numero']}",
        condicion_venta=_condicion_venta(venta),
        usuario_id=usuario_id if usuario_id is not None else venta.get("usuario_id"),
        # Del MISMO `arca` con el que se acaba de pedir el número: leerlo aparte
        # dejaría la factura marcada con un ambiente distinto del que la numeró.
        ambiente=ambiente_de(arca),
    )

    factura = db_facturas.get_factura(factura_id)
    factura = await solicitar_cae(factura_id, factura, ta, arca)

    try:
        pdf_path = pdf_gen.generate_pdf_factura(factura)
        db_facturas.update_factura_pdf_path(factura_id, pdf_path)
        factura = db_facturas.get_factura(factura_id)
    except Exception:
        # El PDF se regenera solo al descargarlo; perderlo no invalida el CAE,
        # y fallar acá dejaría la factura emitida y sin vincular a la venta.
        logger.exception("Error generando el PDF de la factura %s", factura_id)

    ventas.vincular_factura(venta_id, factura_id)
    vinculados = ventas.vincular_cobros(venta["numero"], factura_id)
    _cerrar_pendiente(ventas, venta_id, factura_id, usuario_id)
    try:
        ventas.al_facturar(ventas.obtener(venta_id) or venta, factura)
    except Exception:
        logger.exception("al_facturar falló para la venta %s (factura %s)", venta_id, factura_id)

    logger.info(
        "Venta %s facturada: %s %04d-%08d (CAE %s), %s movimiento/s de caja vinculado/s",
        venta["numero"], _TIPO_LABEL.get(tipo, "Factura"),
        punto_venta, factura["numero"], factura.get("cae") or "sin CAE", vinculados,
    )
    return factura


async def facturar_si_esta_prendida(ventas: PuertoDeVentas, venta_id: int,
                                    cfg: dict | None = None) -> int | None:
    """Emite la factura **si la instancia tiene la automática prendida**
    (`mp_auto_facturar_ventas`), y devuelve su id. `None` si está apagada o si
    falló.

    🔑 Existe porque hay DOS caminos por los que un producto se entera de que el
    QR se pagó: el webhook y el poll de `mp-status`. En la instancia real de
    Contalibra el webhook **no llegó nunca**; si sólo facturara el webhook, la
    venta quedaría cobrada y "Sin facturar" sin que nada lo dijera.

    **No propaga el error**: el cobro ya está registrado, y perderlo sería peor
    que quedarse sin la factura, que se puede emitir después desde el detalle.
    """
    cfg = config_manager.load() if cfg is None else cfg
    if not cfg.get("mp_auto_facturar_ventas"):
        return None
    try:
        factura = await facturar_venta(ventas, venta_id)
    except Exception as e:
        logger.error("Error auto-facturando la venta %s: %s", venta_id, e)
        return None
    logger.info("Auto-factura de la venta %s: id=%s CAE=%s",
                venta_id, factura["id"], factura.get("cae") or "sin CAE")
    return factura["id"]


def manejador_de_cobro_por_qr(
    ventas: PuertoDeVentas,
) -> Callable[[int, str, dict, dict], Awaitable[int | None]]:
    """El manejador de `external_reference` `venta-<id>` para
    `mp_webhook.build_mp_webhook_router(manejadores_de_referencia=...)`.

    Aplica a la venta el cobro que entró por su QR: sella el `payment_id`,
    **acredita** (acá también entra la plata a la caja: si la venta se creó con
    `cobrar_con_qr` su pago quedó pendiente y sin movimiento), sella la
    referencia de los pagos electrónicos y factura si la automática está
    prendida. El motor sólo llama acá con el pago **aprobado**.

    ⚠️ El `payment_id` llega por parámetro y no se saca de `pago["id"]`: el que
    vale es el de la notificación, que es el que sella la idempotencia.
    """

    async def _cobro_de_venta_por_qr(venta_id: int, payment_id: str, pago: dict,
                                     cfg: dict) -> int | None:
        ventas.set_pago_mp(venta_id, payment_id)
        acreditado = ventas.acreditar(venta_id, payment_id, None)
        ventas.sellar_referencia_mp(venta_id, payment_id)
        logger.info("Venta %s pagada via QR de MercadoPago, payment_id=%s (acreditada=%s)",
                    venta_id, payment_id, acreditado)
        return await facturar_si_esta_prendida(ventas, venta_id, cfg)

    return _cobro_de_venta_por_qr


# ── Facturar al volver la conexión (nodo, ADR-074) ───────────────────────────


async def facturar_pendientes_del_nodo(ventas: PuertoDeVentas, *, limite: int = 20,
                                       usuario_id: int | None = None) -> dict:
    """Factura las ventas que el nodo dejó en la bandeja sin conexión, **las más viejas primero**.

    Cada una se factura **desde la venta** (`facturar_venta`): la vincula, cierra su fila de la bandeja y, con el
    candado por venta, no emite dos veces aunque la tanda automática y el botón corran juntos. Idempotente: una venta
    que ya tiene factura sólo cierra su fila.

    - Si la conexión se vuelve a cortar a mitad de tanda, se corta ahí (lo que queda, queda para la próxima).
    - Una venta anulada o que ya no existe se **descarta** de la bandeja con el motivo: no hay nada que facturar.
    - `NodoSinArca` corta la tanda y se informa: no se arregla solo.

    Devuelve `{"es_nodo", "en_linea", "facturadas": [{venta_id, factura_id, cae}], "descartadas", "errores",
    "quedan"}`. Fuera de un nodo no hace nada."""
    conexion = ventas.conexion
    resultado = {"es_nodo": conexion.es_nodo(), "en_linea": not conexion.sin_conexion(),
                 "facturadas": [], "descartadas": [], "errores": [], "quedan": 0}
    if not resultado["es_nodo"] or not resultado["en_linea"]:
        resultado["quedan"] = ventas_a_facturar(ventas)
        return resultado

    for fila in db_pendientes.list_pendientes_de(
            db_pendientes.ORIGEN_VENTA, conexion.node_id() or "", limite, origen_producto=ventas.producto):
        venta_id = int(fila["origen_id"])
        try:
            factura = await facturar_venta(ventas, venta_id, usuario_id=usuario_id)
        except SinConexion:
            resultado["en_linea"] = False
            break
        except FacturacionEnCurso:
            continue
        except VentaNoFacturable as e:
            db_pendientes.descartar(fila["id"], motivo=str(e), usuario="nodo")
            resultado["descartadas"].append({"venta_id": venta_id, "motivo": str(e)})
            continue
        except NodoSinArca as e:
            resultado["errores"].append({"venta_id": venta_id, "error": str(e)})
            break
        except Exception as e:
            logger.exception("Nodo: no se pudo facturar la venta %s de la bandeja", venta_id)
            resultado["errores"].append({"venta_id": venta_id, "error": type(e).__name__})
            continue
        resultado["facturadas"].append({"venta_id": venta_id, "factura_id": factura["id"],
                                        "cae": factura.get("cae") or ""})
    resultado["quedan"] = ventas_a_facturar(ventas)
    return resultado


def ventas_a_facturar(ventas: PuertoDeVentas) -> int:
    """Cuántas ventas de este nodo esperan la factura en la bandeja (0 fuera de un nodo)."""
    if not ventas.conexion.es_nodo():
        return 0
    return len(db_pendientes.list_pendientes_de(
        db_pendientes.ORIGEN_VENTA, ventas.conexion.node_id() or "", 10_000, origen_producto=ventas.producto))


#: Cada cuánto mira la bandeja la facturación automática del nodo.
INTERVALO_AL_VOLVER = 60.0


def arrancar_facturacion_al_volver(ventas: PuertoDeVentas, *, intervalo: float = INTERVALO_AL_VOLVER,
                                   habilitada: Callable[[], bool] = lambda: True,
                                   en_cada_vuelta: Callable[[], object] | None = None) -> threading.Event | None:
    """En un nodo, arranca un hilo que cada `intervalo` segundos factura la bandeja si hay conexión.

    Devuelve el `Event` que lo para (`.set()`), o `None` si la instancia no es un nodo (no arranca nada). Va en un
    hilo y no en el loop de la app por lo mismo que las rutas del cobro son `def`: la firma del ticket y la base son
    sincrónicas y frenarían el loop entero. Con varios workers, cada uno arranca el suyo; el candado por venta hace
    que no se emita dos veces. `habilitada` es el mismo `facturacion_habilitada` del router (VentaLibra).

    `en_cada_vuelta` (opcional) se llama **una vez al arrancar el hilo** y en cada vuelta **después** de facturar,
    haya conexión o no: es donde el producto reconcilia lo que `al_facturar` no llegó a encolar (en LibraCommerce,
    `reconciliador(OUTBOX, get_connection)`, que encola la `venta.facturada` de lo recién facturado). Un error suyo se
    registra (sólo el tipo, sin datos) y no corta el ciclo; tampoco lo corta un error al facturar."""
    if not ventas.conexion.es_nodo():
        return None
    parar = threading.Event()

    def _reconciliar() -> None:
        if en_cada_vuelta is None:
            return
        try:
            en_cada_vuelta()
        except Exception as e:
            logger.error("Nodo: falló en_cada_vuelta de la facturación al volver: %s", type(e).__name__)

    def _bucle() -> None:
        _reconciliar()
        while not parar.wait(intervalo):
            try:
                if habilitada() and not ventas.conexion.sin_conexion():
                    asyncio.run(facturar_pendientes_del_nodo(ventas))
            except Exception as e:
                logger.error("Nodo: falló la facturación automática de la bandeja: %s", type(e).__name__)
            _reconciliar()

    threading.Thread(target=_bucle, name="libracore-facturar-al-volver", daemon=True).start()
    return parar
