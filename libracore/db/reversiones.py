"""Deshacer el dinero de una venta: anulación y devolución.

La contracara de cobrar. Cuando una venta se anula o se devuelve una parte,
hay que sacar de la caja lo que había entrado y, si se había fiado, bajarle
la deuda al cliente.

Extraído de Contalibra y Restolibra el 2026-07-28, donde el cuerpo de
`anular_venta()` era **idéntico** salvo el docstring — cuarta duplicación de
esta clase encontrada el mismo día. La parte de stock no está acá: vive en
`libracommerce.usecases.sales` (`cancel_sale`), porque el inventario es de
ese contexto y el dinero de este.
"""
import sqlite3
import contextlib

from libracore.db.caja import MEDIOS_PAGO_LABELS, create_caja_movimiento
from libracore.db.core import get_connection
from libracore.db.cuenta_corriente import create_cc_pago

#: Medio que representa el fiado. Su reversión no toca la caja (no había
#: entrado plata): le acredita la deuda al cliente.
MEDIO_CUENTA_CORRIENTE = "cuenta_corriente"


def revertir_cobro_venta(
    venta_id: int,
    numero: str,
    fecha: str,
    pagos: list[dict],
    cliente_id: int | None = None,
    usuario_id: int | None = None,
    motivo: str = "Anulación",
    conn: sqlite3.Connection | None = None,
) -> None:
    """Deshace el cobro de una venta: un egreso de caja por cada pago.

    `pagos` es una lista de dicts con `id`, `medio` y `monto` — la forma en
    que cada producto guarda sus pagos difiere, así que se los pasa ya
    leídos en vez de consultarlos acá.

    El pago a cuenta corriente lleva **las dos cosas**: el movimiento de caja
    (para que el historial muestre la reversión completa) y además el crédito
    que le baja la deuda al cliente. Que el egreso a cuenta corriente no
    descuadre el arqueo no depende de omitirlo acá sino de
    `get_caja_resumen()`, que filtra ese medio de los totales justamente
    porque nunca fue plata del cajón — su docstring nombra a esta reversión.

    Se probó omitir la fila por considerarla redundante y **se descartó**: la
    comparación contra el comportamiento anterior mostró que desaparecía del
    listado de movimientos de caja, o sea del historial que ve el usuario.
    Los totales daban igual, la pantalla no.

    Idempotente por la referencia de cada movimiento
    (`anulacion:venta:<id>:pago:<id>`): reintentar no saca dos veces lo
    mismo de la caja.
    """
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        for pago in pagos:
            medio = pago["medio"]
            monto = pago["monto"]
            label = MEDIOS_PAGO_LABELS.get(medio, medio)

            create_caja_movimiento(
                fecha=fecha, tipo="egreso",
                concepto=f"{motivo} venta {numero} — {label}",
                monto=monto,
                referencia=f"anulacion:venta:{venta_id}:pago:{pago['id']}",
                medio_pago=medio, usuario_id=usuario_id, conn=c,
            )
            if medio == MEDIO_CUENTA_CORRIENTE and cliente_id:
                create_cc_pago(
                    cliente_id=cliente_id, monto=monto, fecha=fecha,
                    concepto=f"{motivo} venta {numero}",
                    referencia="", medio_pago=MEDIO_CUENTA_CORRIENTE,
                    caja_id=None, usuario_id=usuario_id, conn=c,
                )


def reintegrar_devolucion(
    venta_id: int,
    numero: str,
    fecha: str,
    monto: float,
    medio_pago: str = "efectivo",
    referencia: str = "",
    cliente_id: int | None = None,
    usuario_id: int | None = None,
    turno_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Devuelve plata por una devolución parcial.

    A diferencia de la anulación, acá no se reversa pago por pago: se
    reintegra un importe, que puede no coincidir con ningún pago original —
    el cliente pagó tres cosas juntas y devuelve una.

    Si el reintegro va a cuenta corriente, baja la deuda en vez de salir de
    la caja: es lo que corresponde cuando la compra estaba fiada y todavía
    no se pagó.
    """
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        if medio_pago == MEDIO_CUENTA_CORRIENTE:
            if cliente_id:
                create_cc_pago(
                    cliente_id=cliente_id, monto=monto, fecha=fecha,
                    concepto=f"Devolución venta {numero}",
                    referencia=referencia, medio_pago=MEDIO_CUENTA_CORRIENTE,
                    caja_id=None, usuario_id=usuario_id, conn=c,
                )
            return

        create_caja_movimiento(
            fecha=fecha, tipo="egreso",
            concepto=f"Devolución venta {numero}",
            monto=monto,
            referencia=referencia or f"devolucion:venta:{venta_id}",
            medio_pago=medio_pago, usuario_id=usuario_id, turno_id=turno_id, conn=c,
        )
