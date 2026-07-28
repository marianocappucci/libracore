"""
Borrador de un comprobante duplicado.

Duplicar una factura es una conveniencia de UI presente hoy en Contalibra y
Restolibra: se toma un comprobante ya emitido y se arma con sus datos el
formulario de uno nuevo, que el usuario revisa y emite. Vivía entero en el
frontend de cada producto (`FacturaDetalle.tsx::duplicar`), duplicado entre
ambos, y por eso arrastró el mismo bug en los dos: las fechas del período de
servicio y el vencimiento de pago se copiaban tal cual del original mientras
la fecha de emisión se reseteaba a hoy, así que el comprobante nuevo nacía
con período y vencimiento anteriores a su propia emisión.

La regla vive acá para que cualquier producto de la familia que sume el
flujo la herede: el período de servicio arranca de nuevo (desde = hasta =
hoy) y el vencimiento de pago conserva los días de plazo que el original
tenía respecto del fin de su período, sin poder caer nunca en el pasado.

`armar_borrador()` es la única entrada. No escribe nada: devuelve el
borrador y el caller decide qué hacer con él (en los dos productos, un
`POST /api/facturas/{id}/duplicar` que la SPA usa para prefillear el
formulario de alta).
"""
import datetime

_TASA_IVA_DEFAULT = 0.21


def _iso_a_dias(iso):
    """Convierte una fecha ISO (YYYY-MM-DD) a días desde la época, o None si
    no lo es. Las fechas de servicio son opcionales en el modelo (concepto
    Productos no las usa) y llegan como cadena vacía."""
    try:
        return datetime.date.fromisoformat(iso).toordinal()
    except (TypeError, ValueError):
        return None


def vencimiento_duplicado(fch_serv_hasta, fch_vto_pago, hoy):
    """Vencimiento de pago para la copia de un comprobante emitido hoy.

    Conserva los días de plazo que el original tenía entre el fin de su
    período de servicio y su vencimiento. Si el original vencía antes de
    cerrar el período (servicio prepago) o le faltan fechas, el vencimiento
    es hoy: nunca una fecha pasada."""
    hasta = _iso_a_dias(fch_serv_hasta)
    vto = _iso_a_dias(fch_vto_pago)
    hoy_dias = _iso_a_dias(hoy)
    if hasta is None or vto is None or hoy_dias is None:
        return hoy
    return datetime.date.fromordinal(hoy_dias + max(0, vto - hasta)).isoformat()


def tasa_iva(factura):
    """Tasa de IVA efectiva del comprobante, deducida de sus montos. El
    modelo guarda el importe de IVA, no la alícuota."""
    subtotal = factura.get("subtotal") or 0
    if subtotal <= 0:
        return _TASA_IVA_DEFAULT
    return round((factura.get("iva_amount") or 0) / subtotal, 4)


def armar_borrador(factura, hoy=None, resolver_cliente=None):
    """Arma el borrador de una copia de `factura` para emitir hoy.

    `resolver_cliente` es un callable CUIT -> dict|None que permite
    reencontrar al cliente en el padrón propio del producto; por defecto usa
    `libracore.db.clients.get_client_by_cuit`. Si el cliente ya no existe (o
    la factura se emitió a un nombre libre), el borrador viaja con la razón
    social del original en `client_name` y sin `client_id`, igual que el alta
    manual de un comprobante a un no-cliente.

    Todo lo que se copia es reeditable por el usuario antes de emitir: esto
    arma el formulario, no un comprobante.
    """
    if hoy is None:
        hoy = datetime.date.today().isoformat()
    if resolver_cliente is None:
        from libracore.db.clients import get_client_by_cuit

        resolver_cliente = get_client_by_cuit

    cuit = factura.get("cliente_cuit") or ""
    cliente = resolver_cliente(cuit) if cuit else None

    return {
        "tipo": factura["tipo"],
        "punto_venta": factura["punto_venta"],
        "concepto": factura.get("concepto") or 1,
        "condicion_venta": factura.get("condicion_venta") or "Contado",
        "tax_rate": tasa_iva(factura),
        "client_id": cliente["id"] if cliente else None,
        "client_name": "" if cliente else (factura.get("cliente_razon") or ""),
        "observations": factura.get("observaciones") or "",
        "items": [
            {
                "description": it.get("description", ""),
                "qty": it.get("qty", 0),
                "unit_price": it.get("unit_price", 0),
            }
            for it in (factura.get("items") or [])
        ],
        # El comprobante nuevo se emite hoy: el período de servicio arranca
        # de nuevo en vez de heredar el del original, que ya pasó.
        "fch_serv_desde": hoy,
        "fch_serv_hasta": hoy,
        "fch_vto_pago": vencimiento_duplicado(
            factura.get("fch_serv_hasta") or "",
            factura.get("fch_vto_pago") or "",
            hoy,
        ),
    }
