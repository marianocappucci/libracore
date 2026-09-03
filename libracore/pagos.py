"""El estado de acreditación de un pago: existir no es haber entrado.

Paso 1 de [[pago-pendiente-de-acreditacion-familia-libra]]. **Sin consumidores
todavía**: acá está el vocabulario y la aritmética, y los productos lo adoptan
en pasos posteriores, cada uno con su migración.

## El defecto que este módulo existe para hacer imposible

En Contalibra y Restolibra una línea de pago no tiene estado: existe, y por lo
tanto cuenta. El POS crea la venta con la línea de MercadoPago cargada por el
total y el estado sale `cobrada` en el acto — antes de que nadie escanee el QR.

🔴 **Y el cartel es lo de menos.** `crear_venta_directa` escribe un movimiento
de caja por cada medio de pago en el momento de crear la venta, así que una
venta por QR que el cliente nunca paga **mete plata en la caja que no entró** y
el arqueo cierra mal, con el error apareciendo horas después.

La regla que ordena todo el plan, y que este módulo hace expresable:

> **El movimiento de caja se escribe al acreditar, no al declarar.**

## Por qué el vocabulario es el de LibraClub

Porque ya existe y ya está probado en producción: `app/models/reservas.py`
define `EstadoPago` con los mismos cuatro valores, incluido `VENCIDO` —*"el
jugador nunca volvió de MercadoPago"*—, que es exactamente el caso que a los
otros tres productos les falta. Inventar un enum nuevo cuando hay uno andando
sería la quinta variante del mismo concepto.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from decimal import Decimal
from typing import Any


class EstadoAcreditacion(str, enum.Enum):
    """En qué anda la plata de un pago.

    Hereda de `str` para que se serialice solo y se compare contra el texto que
    ya guardan las bases de los productos, sin conversiones en cada borde.
    """

    #: Declarado y todavía no entró. **No cuenta para nada**: ni para el estado
    #: del comprobante, ni para la caja, ni para el arqueo.
    PENDIENTE = "pendiente"
    #: La plata entró. Es lo único que suma.
    APROBADO = "aprobado"
    #: MercadoPago lo rechazó, o se canceló.
    RECHAZADO = "rechazado"
    #: Nadie pagó y se agotó la espera. Es distinto de `RECHAZADO` a propósito:
    #: uno es una respuesta y el otro es la falta de respuesta, y el listado de
    #: pendientes se limpia con éste.
    VENCIDO = "vencido"


#: Los estados que cuentan como plata adentro. Es **uno solo**, y está como
#: conjunto para que el día que aparezca otro —un cobro en cuotas, una
#: acreditación parcial— se agregue acá y no en cada `if` de cada producto.
ACREDITAN = frozenset({EstadoAcreditacion.APROBADO})


class PagoSinEstado(ValueError):
    """Un pago llegó sin estado de acreditación.

    🔴 **Es un error y no un default, a propósito.** Los dos defaults posibles
    mueven plata en silencio y en direcciones opuestas: suponer `APROBADO` hace
    contar un pago que no entró —el defecto que este módulo viene a cerrar— y
    suponer `PENDIENTE` hace desaparecer plata que sí entró. Que reviente es lo
    único que no miente.

    En los productos esto no puede pasar después de la migración: el backfill
    deja todas las filas existentes en `APROBADO`, porque todo lo que está
    guardado hoy ya cobró.
    """


def _leer(pago: Any, campo: str) -> Any:
    """El campo de un pago, venga como dict o como objeto del ORM.

    Los productos guardan esto distinto —Contalibra y Restolibra pasan dicts,
    LibraClub filas de SQLAlchemy— y no vale la pena hacerlos converger para
    sumar dos números.
    """
    if hasattr(pago, "get"):
        return pago.get(campo)
    return getattr(pago, campo, None)


def estado_de(pago: Any) -> EstadoAcreditacion:
    """El estado de acreditación de un pago. Levanta si no lo tiene."""
    crudo = _leer(pago, "estado")
    if crudo is None or crudo == "":
        raise PagoSinEstado(
            "Un pago llegó sin `estado` de acreditación. No hay default seguro: "
            "suponer 'aprobado' cuenta plata que no entró y suponer 'pendiente' "
            "borra plata que sí. Ver `PagoSinEstado`."
        )
    if isinstance(crudo, EstadoAcreditacion):
        return crudo
    try:
        return EstadoAcreditacion(str(crudo))
    except ValueError:
        raise PagoSinEstado(
            f"Estado de acreditación desconocido: {crudo!r}. Los válidos son "
            f"{[e.value for e in EstadoAcreditacion]}."
        ) from None


def _monto(pago: Any) -> Decimal:
    """El monto como `Decimal`, venga como float, str o Decimal.

    🔑 **Decimal y no float**, aunque Contalibra guarde `REAL`: esto se suma
    para decidir si un comprobante está cobrado, y el redondeo binario de los
    float hace que 0.1 + 0.2 no sea 0.3. Se pasa por `str` a propósito —
    `Decimal(0.1)` arrastra el error del float, `Decimal(str(0.1))` no.
    """
    crudo = _leer(pago, "monto")
    if crudo is None:
        return Decimal("0")
    if isinstance(crudo, Decimal):
        return crudo
    return Decimal(str(crudo))


def acreditado(pagos: Iterable[Any]) -> Decimal:
    """Cuánta plata entró de verdad.

    Es **el único lugar** que decide eso. Hoy esa suma está escrita a mano en
    cada producto —`sum(p["monto"] for p in pagos)`, sin mirar ningún estado— y
    por eso el defecto apareció igual en dos.
    """
    return sum((_monto(p) for p in pagos if estado_de(p) in ACREDITAN), Decimal("0"))


def pendiente_de_acreditar(pagos: Iterable[Any]) -> Decimal:
    """Cuánta plata se declaró y todavía no entró.

    La contracara de `acreditado`, y no un detalle de la pantalla: es lo que
    distingue "falta cobrar" de "está esperando que el cliente escanee", que
    para el que está atendiendo son dos cosas muy distintas.
    """
    return sum(
        (_monto(p) for p in pagos if estado_de(p) is EstadoAcreditacion.PENDIENTE),
        Decimal("0"),
    )


#: Cómo se lee el `status` crudo de MercadoPago.
#:
#: Sale de `libraclub/app/routers/portal.py`, que es donde este criterio ya
#: estaba resuelto contra pagos reales. Se copia el mapeo y no se reinventa.
#:
#: ⚠️ `authorized` es **pendiente**: MercadoPago retuvo el dinero y todavía no
#: lo capturó. Tratarlo como aprobado acreditaría plata que aún puede no entrar.
_DESDE_MERCADOPAGO = {
    "approved": EstadoAcreditacion.APROBADO,
    "rejected": EstadoAcreditacion.RECHAZADO,
    "cancelled": EstadoAcreditacion.RECHAZADO,
    "pending": EstadoAcreditacion.PENDIENTE,
    "in_process": EstadoAcreditacion.PENDIENTE,
    "in_mediation": EstadoAcreditacion.PENDIENTE,
    "authorized": EstadoAcreditacion.PENDIENTE,
}


def estado_desde_mercadopago(estado_mp: str | None) -> EstadoAcreditacion:
    """Traduce el `status` de MercadoPago al vocabulario de la familia.

    🔑 **Un estado que no conocemos es `PENDIENTE`, no aprobado.** MercadoPago
    puede agregar estados; el único default que no acredita plata de más es
    dejarlo esperando. El estado crudo lo guarda el producto igual, que es lo
    único que después dice cuál era.
    """
    return _DESDE_MERCADOPAGO.get((estado_mp or "").strip().lower(),
                                  EstadoAcreditacion.PENDIENTE)
