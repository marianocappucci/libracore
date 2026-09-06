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
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from libracore import config_manager
from libracore import pdf_generator as pdf_gen
from libracore.arca_facturacion import ambiente_de, get_next_numero_with_arca, solicitar_cae
from libracore.db import arca_config as db_arca
from libracore.db import clients as db_clients
from libracore.db import facturas as db_facturas
from libracore.db.caja import resolver_punto_venta

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


def _sin_alicuota_propia(venta_id: int) -> float | None:
    return None


def _nada(*args) -> None:
    return None


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


def _tipo_comprobante(emisor_cond: str, cliente_cond: str) -> int:
    """A/B/C según el emisor, y A sólo si el cliente también es RI."""
    if emisor_cond == "Monotributista":
        return 11
    if cliente_cond in ("Responsable Inscripto", "IVA Responsable Inscripto"):
        return 1
    return 6


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
    configs = db_arca.obtener_todas_arca_configs()
    return configs[0].get("punto_venta", 1) if configs else 1


async def facturar_venta(ventas: PuertoDeVentas, venta_id: int, *,
                         usuario_id: int | None = None) -> dict:
    """Emite la factura de una venta, pide el CAE, genera el PDF y las vincula.

    Idempotente: si la venta ya tiene factura devuelve esa, sin emitir otra. Es
    lo que sostiene el reintento del webhook de MercadoPago y el poll de
    `mp-status`, que le pega cada pocos segundos mientras el cliente escanea.

    No toca la caja — ver el docstring del módulo.
    """
    venta = ventas.obtener(venta_id)
    if not venta:
        raise VentaNoFacturable(f"La venta {venta_id} no existe.")

    if venta.get("factura_id"):
        logger.info("Venta %s ya facturada (factura %s), no se reemite",
                    venta_id, venta["factura_id"])
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

    punto_venta = _punto_venta(venta)
    numero, ta, arca = await get_next_numero_with_arca(punto_venta, tipo)

    factura_id = db_facturas.create_factura(
        tipo=tipo, punto_venta=punto_venta, numero=numero,
        fecha=datetime.date.today().isoformat(),
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
