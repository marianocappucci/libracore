"""
Registro del cobro de un comprobante ya emitido.

Cobrar una factura son tres cosas y ninguna es HTTP: validar los medios,
escribir un movimiento de caja por cada pago, y —si el comprobante se emitió
en cuenta corriente— acreditar el saldo del cliente. Vivía duplicado byte a
byte en el router `POST /api/facturas/{id}/cobrar` de Contalibra y Restolibra,
que son hoy los dos productos de la familia con el flujo (los otros tres
facturan desde el turno o desde el POS, sin pantalla de cobro de comprobante).

Y por estar duplicado arrastró el mismo bug en los dos: el diálogo ofrecía
**"Cuenta corriente" como medio de cobro**, y elegirla registraba un cobro que
el propio motor después ignoraba.

## Por qué "cuenta corriente" no es un medio de cobro

Es la marca de que el comprobante se emitió **a crédito**. Al emitir una
factura en cuenta corriente se escribe un movimiento de caja con ese medio, y
**ese movimiento es la deuda**, no un ingreso de plata. Por eso el motor lo
excluye en todos lados donde calcula lo cobrado —`get_cobros_factura`,
`get_facturas_filtradas`, `get_caja_resumen`— y lo suma como débito en
`get_cc_saldo`.

Aceptarlo como medio de cobro dejaba un estado contradictorio: la factura
seguía "Sin cobrar" y la cuenta corriente mostraba **la misma factura dos veces
como cargo**, compensada por el abono, así que el saldo tampoco se movía.
Incidente real: FC 0005-00000005 de compulibra, 2026-08-03.

Sigue siendo un medio válido en el POS (`/api/ventas`), que es donde significa
"se lo lleva a cuenta" — de ahí que las cajas lo tengan habilitado y que el
rechazo sea de esta operación y no de la configuración.

`registrar_cobro_factura()` es la única entrada. Las escrituras se inyectan
para poder probarla sin base y para que cada producto pase las suyas.
"""
import datetime

# Las dos grafías que conviven en la base viven en `db.caja`, que es de donde
# salen también los fragmentos SQL que las consultan. Se importan en vez de
# copiarse: tener dos listas del mismo criterio es cómo se llega a que una
# consulta cuente un movimiento como deuda y otra no.
from libracore.db.caja import MEDIOS_CUENTA_CORRIENTE  # noqa: E402

CONDICION_CUENTA_CORRIENTE = "Cuenta Corriente"

MENSAJE_MEDIO_INVALIDO = (
    '"Cuenta corriente" no es un medio de cobro: es la marca de que el '
    "comprobante se emitio a credito. Elegi el medio con el que entro la "
    "plata (efectivo, transferencia, etc.); si la factura es de cuenta "
    "corriente, el cobro descuenta el saldo del cliente solo."
)


class MedioNoEsDeCobro(ValueError):
    """Se intentó cobrar con un medio que no representa entrada de plata.

    Es un error del pedido, no del sistema: cada producto lo traduce a la
    respuesta que corresponda (los dos que hoy tienen el flujo devuelven un
    400 con `str(exc)` como detalle, para que el mensaje sea el mismo en
    todos lados)."""


def es_medio_cuenta_corriente(medio) -> bool:
    """Si `medio` es la marca de venta a crédito, en cualquiera de sus dos
    grafías y sin importar mayúsculas ni espacios alrededor."""
    return str(medio or "").strip().lower() in MEDIOS_CUENTA_CORRIENTE


def concepto_de_cobro(factura, tipo_label, con_cliente=True) -> str:
    """Texto del movimiento de caja y del abono en cuenta corriente.

    Se conserva tal cual lo venían escribiendo los dos productos, porque hay
    movimientos históricos con este formato y la cuenta corriente los muestra
    al usuario: `Cobro FACTURA C 0005-00000005 — RAZON SOCIAL`. El abono en
    cuenta corriente lleva la misma línea sin la razón social."""
    pv = str(factura["punto_venta"]).zfill(4)
    num = str(factura["numero"]).zfill(8)
    base = f"Cobro {tipo_label} {pv}-{num}"
    if con_cliente:
        return f"{base} — {factura['cliente_razon']}"
    return base


def registrar_cobro_factura(
    factura,
    pagos,
    fecha=None,
    caja_id=None,
    usuario_id=None,
    crear_movimiento=None,
    crear_cc_pago=None,
    resolver_cliente=None,
    tipo_label=None,
):
    """Registra el cobro de `factura` y devuelve qué se escribió.

    `pagos` es la lista que manda la pantalla: dicts con `medio_id`, `monto` y
    opcionalmente `referencia`. Los de monto cero o negativo se ignoran — son
    las filas vacías del formulario.

    **Valida todos los pagos antes de escribir el primero.** Si el rechazo
    ocurriera dentro del loop, un cobro de dos medios con la cuenta corriente
    en segundo lugar ya habría dejado registrado el movimiento del primero, y
    el usuario vería un error junto con un cobro parcial que no pidió.

    Levanta `MedioNoEsDeCobro` si alguno de los pagos usa la cuenta corriente
    como medio (ver el docstring del módulo).

    Devuelve `{"total", "movimientos", "cc_pago_id"}`. No relee la factura: el
    caller decide si la vuelve a buscar para responder.
    """
    if fecha is None:
        fecha = datetime.date.today().isoformat()
    if crear_movimiento is None:
        from libracore.db.caja import create_caja_movimiento

        crear_movimiento = create_caja_movimiento
    if crear_cc_pago is None:
        from libracore.db.cuenta_corriente import create_cc_pago

        crear_cc_pago = create_cc_pago
    if resolver_cliente is None:
        from libracore.db.clients import get_client_by_cuit

        resolver_cliente = get_client_by_cuit
    if tipo_label is None:
        from libracore.pdf_generator import _TIPO_LABELS

        tipo_label = _TIPO_LABELS.get(factura["tipo"], "Factura")

    a_registrar = [p for p in (pagos or []) if float(p.get("monto") or 0) > 0]
    for pago in a_registrar:
        if es_medio_cuenta_corriente(pago.get("medio_id", "")):
            raise MedioNoEsDeCobro(MENSAJE_MEDIO_INVALIDO)

    concepto = concepto_de_cobro(factura, tipo_label)
    movimientos = []
    total = 0.0
    for pago in a_registrar:
        monto = float(pago["monto"])
        movimientos.append(
            crear_movimiento(
                fecha=fecha,
                tipo="ingreso",
                concepto=concepto,
                monto=monto,
                referencia=str(pago.get("referencia", "")).strip(),
                factura_id=factura["id"],
                caja_id=caja_id,
                medio_pago=pago.get("medio_id", ""),
                usuario_id=usuario_id,
            )
        )
        total += monto

    cc_pago_id = None
    if total > 0 and factura.get("condicion_venta") == CONDICION_CUENTA_CORRIENTE:
        # El cargo lo creó la emisión; este abono es el que lo cancela. Si el
        # comprobante se emitió a un nombre libre (sin cliente en el padrón),
        # no hay cuenta corriente que mover y el cobro igual queda registrado
        # en la caja.
        cliente = resolver_cliente(factura.get("cliente_cuit"))
        if cliente:
            cc_pago_id = crear_cc_pago(
                cliente_id=cliente["id"],
                monto=total,
                fecha=fecha,
                concepto=concepto_de_cobro(factura, tipo_label, con_cliente=False),
                referencia="",
                medio_pago="",
                caja_id=caja_id,
                usuario_id=usuario_id,
            )

    return {"total": total, "movimientos": movimientos, "cc_pago_id": cc_pago_id}
