"""
De uno o varios comprobantes pendientes al formulario de la factura.

Las filas las escribe y lee `libracore.db.comprobantes_pendientes`. Acá vive la
única parte con reglas: **agrupar**. La decisión de producto (2026-08-07) es que
el productor manda granular —una cuota, un ticket, un remito por fila, que es lo
que hace idempotente el reenvío— y que **la agrupación la elige una persona en
el momento de facturar**, con los ítems a la vista. Un cliente con 3 contratos y
8 tickets del mes puede terminar en una factura o en once, y eso no lo decide
ningún automatismo.

`armar_prefill()` no escribe nada: devuelve el borrador con la misma forma que
`libracore.facturas_borrador.armar_borrador`, para que la SPA lo cargue en el
mismo formulario de alta. Marcar los pendientes como facturados es un paso
aparte y posterior a que ARCA haya devuelto el CAE — ver
`db.comprobantes_pendientes.marcar_facturado`.
"""
import datetime

_TASA_IVA_DEFAULT = 0.21


class ClientesMezclados(Exception):
    """Se pidió facturar juntos pendientes de clientes distintos.

    Es el único error que no se puede dejar pasar con un aviso: una factura
    tiene un solo receptor, y elegir cuál por nuestra cuenta sería emitirle a
    alguien un comprobante que no le corresponde.
    """


def _clave_cliente(c: dict) -> str:
    """Con qué se decide si dos pendientes son del mismo cliente.

    El CUIT manda cuando está —es el dato fiscal y es la clave de cruce entre
    los dos sistemas—; si falta, se cae a la razón social normalizada, que es
    lo único que queda cuando el productor mandó un cliente sin CUIT cargado.
    """
    cuit = (c.get("cliente_cuit") or "").replace("-", "").strip()
    if cuit:
        return f"cuit:{cuit}"
    return "razon:" + (c.get("cliente_razon") or "").strip().lower()


def _tasa_iva(items: list) -> tuple[float, bool]:
    """La alícuota del comprobante y si hubo que aplastar más de una.

    La factura de Contalibra lleva **una** tasa para todo el comprobante
    (`FacturaPayload.tax_rate`), mientras que un pendiente puede traer una
    alícuota por ítem. Cuando no coinciden no se promedia en silencio: se
    devuelve la más alta y se avisa, porque el que tiene que resolverlo es
    quien está mirando el formulario, no esta función.
    """
    tasas = {round(float(i.get("iva_rate") or 0), 4) for i in items}
    if not tasas:
        return _TASA_IVA_DEFAULT, False
    if len(tasas) == 1:
        return tasas.pop(), False
    return max(tasas), True


def _rango(valores: list) -> tuple[str, str]:
    fechas = sorted(v for v in valores if v)
    return (fechas[0], fechas[-1]) if fechas else ("", "")


def armar_prefill(comprobantes: list, hoy=None) -> dict:
    """Arma el formulario de una factura a partir de los pendientes dados.

    `comprobantes` son dicts como los devuelve
    `db.comprobantes_pendientes.get_comprobantes()`. Todos tienen que ser del
    mismo cliente; si no, levanta `ClientesMezclados`.

    El resultado trae además `comprobantes_ids` —los que quedaron cubiertos— y
    `avisos`, una lista de textos para mostrar arriba del formulario. Todo lo
    demás es reeditable antes de emitir: esto arma el formulario, no un
    comprobante.

    **No devuelve `tipo` ni `punto_venta`**: los decide el producto según la
    condición de IVA del emisor y del receptor, y meterse ahí desde el motor
    sería elegir por él qué letra emite.
    """
    if not comprobantes:
        raise ValueError("no hay comprobantes que facturar")
    if hoy is None:
        hoy = datetime.date.today().isoformat()

    claves = {_clave_cliente(c) for c in comprobantes}
    if len(claves) > 1:
        raise ClientesMezclados(
            "Los comprobantes seleccionados son de clientes distintos: "
            + ", ".join(sorted(c.get("cliente_razon") or "(sin nombre)"
                               for c in comprobantes))
        )

    primero = comprobantes[0]
    items: list = []
    for c in comprobantes:
        items.extend(c.get("items") or [])

    tax_rate, mezcla_iva = _tasa_iva(items)
    desde, hasta = _rango([c.get("periodo_desde") for c in comprobantes]
                          + [c.get("periodo_hasta") for c in comprobantes])

    avisos = []
    if mezcla_iva:
        avisos.append(
            "Los comprobantes traen más de una alícuota de IVA y la factura "
            f"lleva una sola: se dejó la más alta ({tax_rate:.0%}). Revisá los "
            "ítems antes de emitir."
        )
    conceptos = {(c.get("concepto") or "").strip() for c in comprobantes}
    conceptos.discard("")

    # Condición de venta: la del primero que la traiga. Si el productor mandó
    # dos distintas para el mismo cliente no hay forma de saber cuál gana, así
    # que se avisa en vez de elegir en silencio.
    condiciones = {(c.get("condicion_venta") or "").strip() for c in comprobantes}
    condiciones.discard("")
    if len(condiciones) > 1:
        avisos.append(
            "Los comprobantes traen condiciones de venta distintas ("
            + ", ".join(sorted(condiciones)) + "). Elegí una antes de emitir."
        )

    observaciones = [c.get("observaciones") or "" for c in comprobantes]

    return {
        "comprobantes_ids": [c["id"] for c in comprobantes],
        "avisos": avisos,
        "concepto": 2 if conceptos else 1,
        "condicion_venta": sorted(condiciones)[0] if condiciones else "Contado",
        "tax_rate": tax_rate,
        "client_id": primero.get("cliente_id"),
        "client_name": primero.get("cliente_razon") or "",
        "client_cuit": primero.get("cliente_cuit") or "",
        "client_address": primero.get("cliente_domicilio") or "",
        "fecha": hoy,
        "observations": "\n".join(o for o in observaciones if o),
        "items": [
            {
                "description": it.get("description", ""),
                "qty": it.get("qty", 0),
                "unit_price": it.get("unit_price", 0),
            }
            for it in items
        ],
        # El período de servicio es el que abarcan los pendientes, no hoy: la
        # factura de agosto se emite en septiembre y tiene que decir agosto.
        # Es la diferencia de fondo con `facturas_borrador.armar_borrador`,
        # donde el duplicado empieza un período nuevo.
        "fch_serv_desde": desde,
        "fch_serv_hasta": hasta,
        "fch_vto_pago": max(
            [c.get("fecha_sugerida") or "" for c in comprobantes] + [hoy]
        ),
    }
