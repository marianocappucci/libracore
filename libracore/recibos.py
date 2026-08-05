"""
Emisión de recibos: de qué cobros se arma el papel y cuándo hay que emitir uno
nuevo en vez de reimprimir el que ya salió.

## Por qué esto no estaba

Hasta acá el recibo era `pdf_generator.generate_pdf_recibo(factura, cobros)`:
un PDF armado en el momento desde los cobros que la factura tuviera **en ese
momento**. Funcionaba para mirarlo en pantalla y fallaba como comprobante, por
tres motivos que se arreglan juntos con la tabla `recibos`:

1. **No tenía número.** Nada identificaba al papel que el cliente se llevó.
2. **No era estable.** Cobrar un saldo pendiente después cambiaba el recibo de
   la primera cuota: el mismo pedido devolvía otro documento.
3. **Sólo existía atado a un comprobante.** El caso más común del mostrador —el
   cliente paga a cuenta contra su cuenta corriente— no tenía recibo posible,
   porque no hay factura de la cual colgarlo. Ese pago se registraba en
   `cc_pagos` y no emitía nada.

## Las tres entradas

Una por origen, y cada una decide sola entre emitir y reimprimir, para que la
pantalla tenga **un solo botón** y no le pregunte al usuario algo que el
sistema ya sabe:

- `emitir_recibo_cobranza(cc_pago_id)` — 1:1 con el pago. Es el caso que no
  existía.
- `emitir_recibo_venta(venta_id)` — 1:1 con la venta: en el POS se cobra el
  total de una, así que no hay cobros parciales que acumular.
- `emitir_recibo_factura(factura_id)` — el único con varios recibos posibles.
  Una factura se puede cobrar en cuotas, y cada cobro merece su papel. Cubre
  **los cobros que ningún recibo vigente cubra ya**, mirando el
  `caja_movimiento_id` que quedó guardado en el snapshot de cada recibo. Sin
  eso, apretar el botón dos veces emitiría dos recibos por la misma plata.

Un recibo **anulado no cubre nada**: su plata vuelve a estar disponible para
uno nuevo. Eso lo resuelve `get_recibos_de_origen`, que por defecto los
excluye.

Las lecturas se inyectan —mismo patrón que `libracore.cobros`— para poder
probar la lógica sin base y para que cada producto pase las suyas: las ventas
de VentaLibra, por ejemplo, no viven en la base de LibraCore.
"""
import datetime

from libracore.db import recibos as db_recibos
from libracore.db.recibos import ORIGEN_CC_PAGO, ORIGEN_FACTURA, ORIGEN_VENTA


class SinCobros(ValueError):
    """No hay plata registrada de la cual emitir un recibo.

    Es un error del pedido, no del sistema: se pidió el recibo de una factura
    que todavía nadie pagó. Cada producto lo traduce a la respuesta que
    corresponda (un 404 o un 409 con `str(exc)`)."""


MENSAJE_SIN_COBROS = (
    "No hay cobros registrados: el recibo se emite contra plata que entro, "
    "asi que primero hay que registrar el cobro."
)


def _hoy() -> str:
    return datetime.date.today().isoformat()


def _movimientos_ya_cubiertos(recibos_previos) -> set:
    """Los `caja_movimiento_id` que ya están dentro de algún recibo vigente."""
    cubiertos = set()
    for recibo in recibos_previos:
        for pago in recibo.get("pagos") or []:
            mid = pago.get("caja_movimiento_id")
            if mid is not None:
                cubiertos.add(mid)
    return cubiertos


def _pago_desde_movimiento(mov: dict) -> dict:
    """Traduce un `caja_movimientos` al formato que se guarda en el snapshot."""
    return {
        "fecha":              (mov.get("fecha") or "")[:10],
        "medio_pago":         mov.get("medio_pago") or "",
        "referencia":         mov.get("referencia") or "",
        "monto":              float(mov.get("monto") or 0),
        "caja_movimiento_id": mov.get("id"),
    }


def _emitir(*, fecha, cliente_razon, origen_tipo, origen_id, total, pagos,
            concepto, punto_venta, cliente_id, cliente_cuit, cliente_domicilio,
            observaciones, usuario_id) -> dict:
    """Inserta y **relee**. Releer no es paranoia: `create_recibo` reintenta
    ante colisión de numeración, así que el número que terminó en la base
    puede no ser el que se calculó."""
    recibo_id = db_recibos.create_recibo(
        fecha=fecha,
        cliente_razon=cliente_razon or "Consumidor Final",
        origen_tipo=origen_tipo,
        origen_id=origen_id,
        total=total,
        pagos=pagos,
        concepto=concepto,
        punto_venta=punto_venta,
        cliente_id=cliente_id,
        cliente_cuit=cliente_cuit or "",
        cliente_domicilio=cliente_domicilio or "",
        observaciones=observaciones,
        usuario_id=usuario_id,
    )
    return db_recibos.get_recibo(recibo_id)


# ── Cobranza de cuenta corriente ─────────────────────────────────────────────

def emitir_recibo_cobranza(cc_pago_id: int, punto_venta: int = 1,
                           usuario_id=None, observaciones: str = "",
                           get_cc_pago=None) -> dict:
    """El recibo de un pago a cuenta. Idempotente: si ya existe uno vigente
    para este pago, lo devuelve en vez de emitir otro.

    Idempotente y no acumulativo, a diferencia de la factura: un `cc_pagos` es
    **un** movimiento de plata, no una deuda que se va cobrando de a partes.
    Dos pagos del mismo cliente el mismo día son dos filas y dos recibos.
    """
    if get_cc_pago is None:
        from libracore.db.cuenta_corriente import get_cc_pago

    pago = get_cc_pago(cc_pago_id)
    if not pago:
        raise SinCobros(MENSAJE_SIN_COBROS)

    previos = db_recibos.get_recibos_de_origen(ORIGEN_CC_PAGO, cc_pago_id)
    if previos:
        return previos[-1]

    monto = float(pago.get("monto") or 0)
    fecha = (pago.get("fecha") or _hoy())[:10]
    concepto_pago = (pago.get("concepto") or "").strip() or "Pago a cuenta"

    return _emitir(
        fecha=fecha,
        cliente_razon=pago.get("cliente_nombre"),
        origen_tipo=ORIGEN_CC_PAGO,
        origen_id=cc_pago_id,
        total=monto,
        # Un solo pago, y sin `caja_movimiento_id`: el movimiento de caja lo
        # escribe el producto por separado y no siempre existe (un pago cargado
        # sin caja seleccionada no genera ninguno). Como el origen es 1:1, no
        # hace falta para saber qué está cubierto.
        pagos=[{
            "fecha":      fecha,
            "medio_pago": pago.get("medio_pago") or "",
            "referencia": pago.get("referencia") or "",
            "monto":      monto,
        }],
        concepto=f"{concepto_pago} en cuenta corriente",
        punto_venta=punto_venta,
        cliente_id=pago.get("cliente_id"),
        cliente_cuit=pago.get("cliente_cuit"),
        cliente_domicilio=pago.get("cliente_domicilio"),
        observaciones=observaciones,
        usuario_id=usuario_id if usuario_id is not None else pago.get("usuario_id"),
    )


# ── Venta de mostrador ───────────────────────────────────────────────────────

def emitir_recibo_venta(venta_id: int, punto_venta: int = 1, usuario_id=None,
                        observaciones: str = "", get_venta=None) -> dict:
    """El recibo de una venta del POS. Idempotente por el mismo motivo que la
    cobranza: la venta se cobra entera en el acto."""
    if get_venta is None:
        from libracore.db.ventas import get_venta

    venta = get_venta(venta_id)
    if not venta:
        raise SinCobros(MENSAJE_SIN_COBROS)

    previos = db_recibos.get_recibos_de_origen(ORIGEN_VENTA, venta_id)
    if previos:
        return previos[-1]

    fecha = (venta.get("fecha") or _hoy())[:10]
    pagos = [
        {
            "fecha":      fecha,
            "medio_pago": p.get("medio") or "",
            "referencia": p.get("referencia") or "",
            "monto":      float(p.get("monto") or 0),
        }
        for p in (venta.get("pagos") or [])
    ]
    total = sum(p["monto"] for p in pagos)
    if total <= 0:
        raise SinCobros(MENSAJE_SIN_COBROS)

    numero_visible = venta.get("numero") or venta_id
    return _emitir(
        fecha=fecha,
        cliente_razon=venta.get("cliente_nombre"),
        origen_tipo=ORIGEN_VENTA,
        origen_id=venta_id,
        total=total,
        pagos=pagos,
        concepto=f"Venta N\xb0 {numero_visible}",
        punto_venta=punto_venta,
        cliente_id=venta.get("cliente_id"),
        cliente_cuit=venta.get("cliente_cuit"),
        cliente_domicilio=venta.get("cliente_domicilio"),
        observaciones=observaciones,
        usuario_id=usuario_id,
    )


# ── Factura ──────────────────────────────────────────────────────────────────

def emitir_recibo_factura(factura_id: int, punto_venta: int = 1, usuario_id=None,
                          observaciones: str = "", get_factura=None,
                          get_cobros_factura=None, tipo_label=None) -> dict:
    """El recibo de los cobros de una factura.

    A diferencia de los otros dos orígenes, **acumula**: una factura se puede
    cobrar en varias veces, y cada tanda de cobros nuevos merece su propio
    papel. Emite cubriendo los cobros que ningún recibo vigente cubra ya; si no
    hay ninguno nuevo, devuelve el último emitido (que es lo que quiere quien
    apretó el botón para reimprimir).

    Levanta `SinCobros` si la factura no tiene cobros registrados — el estado
    en el que el botón no debería estar habilitado, pero la API no puede
    confiar en eso.
    """
    if get_factura is None:
        from libracore.db.facturas import get_factura
    if get_cobros_factura is None:
        from libracore.db.caja import get_cobros_factura

    factura = get_factura(factura_id)
    if not factura:
        raise SinCobros(MENSAJE_SIN_COBROS)

    cobros = get_cobros_factura(factura_id) or []
    previos = db_recibos.get_recibos_de_origen(ORIGEN_FACTURA, factura_id)
    cubiertos = _movimientos_ya_cubiertos(previos)
    nuevos = [c for c in cobros if c.get("id") not in cubiertos]

    if not nuevos:
        if previos:
            return previos[-1]
        raise SinCobros(MENSAJE_SIN_COBROS)

    pagos = [_pago_desde_movimiento(c) for c in nuevos]
    total = sum(p["monto"] for p in pagos)

    # "Cancelación" sólo si con esto la factura queda saldada. Se mira el total
    # cobrado acumulado, no el de este recibo: dos cobros de la mitad cada uno
    # cancelan la factura, y decir "pago parcial" en el segundo sería falso.
    cobrado_total = sum(float(c.get("monto") or 0) for c in cobros)
    parcial = cobrado_total < float(factura.get("total") or 0) - 0.005

    if tipo_label is None:
        from libracore.pdf_generator import _TIPO_NOMBRE_DOC

        tipo_label = _TIPO_NOMBRE_DOC.get(int(factura.get("tipo") or 11), "Comprobante")
    pv_fac = str(factura.get("punto_venta") or 0).zfill(4)
    num_fac = str(factura.get("numero") or 0).zfill(8)
    referencia = f"{tipo_label} {pv_fac}-{num_fac}"
    concepto = f"{'Pago parcial' if parcial else 'Cancelacion'} de {referencia}"

    return _emitir(
        fecha=_hoy(),
        cliente_razon=factura.get("cliente_razon"),
        origen_tipo=ORIGEN_FACTURA,
        origen_id=factura_id,
        total=total,
        pagos=pagos,
        concepto=concepto,
        punto_venta=punto_venta,
        cliente_id=None,
        cliente_cuit=factura.get("cliente_cuit"),
        cliente_domicilio=factura.get("cliente_domicilio"),
        observaciones=observaciones,
        usuario_id=usuario_id,
    )
