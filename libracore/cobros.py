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

## Por qué un cobro no puede superar lo que falta (ADR-058)

Cobrar una **factura puntual** más de lo que falta no es «saldo a favor»: es
plata que nadie debía, asentada en la caja y en el arqueo, con la factura
figurando «Cobrado completo — $ 10.999,00» sobre un comprobante de 4.500.
Medido el 2026-10-15 en el e2e de Contalibra: 9.999 sobre 3.500 pendientes
daba 200. (Una **cuenta corriente** sí admite pagar de más —queda a favor—,
pero eso es `cuenta_corriente_router`, donde el resto se asienta como pago a
cuenta; acá no hay a qué asentarlo.)

Lo mismo vale para una **nota de débito** (ADR-064): su `total` es el monto nuevo que cobra
(intereses, un gasto, una diferencia), y cobrarle más es plata que nadie debía. Las notas de
crédito no se cobran y no tienen tope.

Lo que falta es `total − lo cobrado − lo acreditado por notas de crédito con CAE`
(`saldo_de_cobro`, ADR-059), todo en centavos: la misma cuenta de «pendiente» que hace el
resto del motor.
La regla vive acá, en el dominio, y no en el router, para que no la saltee otra
entrada: el router
`POST /api/facturas/{id}/cobrar`, el movimiento manual de `POST /api/caja` con
`factura_id` y cualquier producto que cobre una factura con su propia caja
(`exigir_cobro_posible`). **Se mira y se escribe en una sola transacción, con la
fila de la factura bloqueada**: dos cobros simultáneos que juntos superan lo
pendiente se hacen cola y el segundo ve lo que dejó el primero.

`registrar_cobro_factura()` es la entrada del cobro de una factura. Las
escrituras se inyectan para poder probarla sin base y para que cada producto
pase las suyas.
"""
import contextlib
import datetime
import sqlite3
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

# Las dos grafías que conviven en la base viven en `db.caja`, que es de donde
# salen también los fragmentos SQL que las consultan. Se importan en vez de
# copiarse: tener dos listas del mismo criterio es cómo se llega a que una
# consulta cuente un movimiento como deuda y otra no.
from libracore import tipos_comprobante as tipos  # noqa: E402
from libracore.db.caja import MEDIOS_CUENTA_CORRIENTE  # noqa: E402
from libracore.validacion import rechazar_booleanos  # noqa: E402

CONDICION_CUENTA_CORRIENTE = "Cuenta Corriente"

MENSAJE_SIN_TURNO = "No hay una caja abierta. Abrí el turno antes de cobrar."

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


class CobroNoPermitido(ValueError):
    """El cobro no se puede registrar contra esa factura, dicho para una persona (ADR-058).

    Lleva un `codigo` para que cada producto lo traduzca a su respuesta: el router del motor
    contesta 409 (es el estado de la factura el que lo impide, no el formato del pedido), igual
    que `NotaNoPermitida.SUPERA_SALDO`. `pendiente` es lo que falta cobrar, en pesos; el texto ya
    lo dice. El motor no sabe de HTTP acá a propósito."""

    NO_EXISTE = "no_existe"      # el comprobante no está
    ANULADA = "anulada"          # está anulado (ADR-022): no se cobra
    SIN_SALDO = "sin_saldo"      # no queda nada por cobrar (ya cobrada, o acreditada por notas de crédito)
    EXCEDE = "excede"            # el cobro supera lo que falta
    SIN_TURNO = "sin_turno"      # el producto lleva la caja por turno y quien cobra no tiene uno abierto

    def __init__(self, mensaje: str, codigo: str, *, pendiente: float | None = None):
        super().__init__(mensaje)
        self.codigo = codigo
        self.pendiente = pendiente


_CENTAVO = Decimal("0.01")


def centavos(valor) -> int:
    """`valor` en centavos enteros, redondeado al centavo (la mitad hacia arriba).

    **Es la tolerancia del tope de cobro, y es mínima a propósito**: medio centavo. Lo que el
    motor guarda son `float` (`4500.000000001` y `4499.999999999` son la misma plata) y
    `recibos` y el PDF ya tratan como «cobrada» a la factura que está a menos de `0.005` de su
    total; pasar de ahí es cobrar de más, y no hay un peso de gracia. Pasa por `str` para no
    arrastrar el error binario del `float` (mismo criterio que `notas_de_credito._dec`)."""
    return int((Decimal(str(valor or 0)).quantize(_CENTAVO, rounding=ROUND_HALF_UP) * 100)
               .to_integral_value())


def _pesos(en_centavos: int) -> str:
    """`$ 3.500,00`: como lo escribe la pantalla (es-AR)."""
    entero, resto = divmod(abs(en_centavos), 100)
    return f"{'-' if en_centavos < 0 else ''}$ {entero:,}".replace(",", ".") + f",{resto:02d}"


def saldo_de_cobro(factura, cobros: Sequence[dict], acreditado: float = 0.0) -> dict:
    """Lo que falta cobrar de una factura: `total − cobrado − acreditado`, en centavos (ADR-059).

    Puro: no toca la base. `cobros` son los movimientos de ingreso que cuentan (los de
    `db.caja.get_cobros_factura`: sin anulados y sin la marca de cuenta corriente) y `acreditado` lo
    que restan sus notas de crédito **con CAE y vigentes** (`db.facturas.sql_acreditado`).

    Devuelve `total`, `cobrado`, `acreditado` y `pendiente` en pesos, y `pendiente_centavos` para
    comparar sin error de coma flotante. **Nunca es negativo**: una factura cobrada y después
    acreditada, o con un cobro de más de antes del ADR-058, queda en 0 —no hay «saldo a favor» de una
    factura puntual, sólo de una cuenta corriente—.

    🔑 **Es la misma cuenta que las consultas SQL** (`sql_pendiente`: listado `sin_cobrar`, resumen,
    tablero, cuenta corriente) y para los mismos comprobantes (`tipos.COBRABLES` / `sql_es_cobrable`, ADR-066):
    un test las cruza, para una factura y para una nota de débito. Si la pantalla ofreciera una cifra y el tope aceptara
    otra, el cobro sugerido por la propia pantalla saldría rechazado."""
    total = centavos(factura["total"])
    cobrado = sum(centavos(c["monto"]) for c in cobros)
    acreditado_c = centavos(acreditado)
    pendiente = max(0, total - cobrado - acreditado_c)
    return {
        "total": total / 100, "cobrado": cobrado / 100, "acreditado": acreditado_c / 100,
        "pendiente": pendiente / 100, "pendiente_centavos": pendiente,
    }


def estado_de_cobro(factura_id, *, conn=None) -> dict | None:
    """El saldo de cobro **de ahora** de una factura, leído de la base (`None` si no existe).

    Es `saldo_de_cobro` más `anulada` (¿está dado de baja?) y `con_tope` (¿es un comprobante de los
    que tienen un total que cobrar?). Lo tienen las facturas **y las notas de débito** (ADR-064): una
    nota de débito cobra un monto nuevo —intereses, un gasto, una diferencia—, y ese monto es su
    `total`; lo que falta es `total − cobrado − acreditado` como en una factura (por si una nota de
    crédito la acredita: `sql_acreditado` mira el comprobante asociado, sea cual sea su tipo). Una nota
    de crédito no se cobra —resta— y no tiene tope. Con `conn`, lo ve como lo ve esa transacción.

    Lo cobrado y lo acreditado salen de los fragmentos SQL de `db.facturas` (`sql_cobrado`,
    `sql_acreditado`): una sola definición para esto y para todas las listas."""
    from libracore.db import facturas as db_facturas

    factura = db_facturas.get_factura(factura_id, conn=conn)
    if factura is None:
        return None
    cobrado, acreditado = db_facturas.get_cobrado_y_acreditado(factura_id, conn=conn)
    return {
        **saldo_de_cobro(factura, [{"monto": cobrado}], acreditado),
        "anulada": bool(factura.get("anulada_en")),
        "con_tope": factura["tipo"] in tipos.COBRABLES,
        # Cómo se nombra en los mensajes del tope («Esta factura ya está cobrada…»).
        "rotulo": "nota de débito" if factura["tipo"] in tipos.ND else "factura",
    }


def _bloquear_factura(conn, factura_id) -> None:
    """Toma la fila de la factura hasta el fin de la transacción de `conn`: quien llegue después
    espera y ve lo que dejó este (ADR-058).

    PostgreSQL: `FOR UPDATE` (bajo `READ COMMITTED`, la lectura que sigue ya ve lo que confirmó quien
    la tenía). SQLite: un `UPDATE` sin efecto toma el candado de escritura de la base, que
    ya serializa a los escritores (el mismo recurso que usa `libracommerce`). Un comprobante
    que no existe no bloquea nada: lo dice quien llama."""
    if isinstance(conn, sqlite3.Connection):
        conn.execute("UPDATE facturas SET total = total WHERE id = ?", (factura_id,))
    else:
        conn.execute("SELECT id FROM facturas WHERE id = ? FOR UPDATE", (factura_id,)).fetchall()


def bloquear_facturas(conn, factura_ids) -> None:
    """Bloquea **varias** facturas hasta el fin de la transacción de `conn`, **en orden de id ascendente**
    (ADR-063).

    Es `_bloquear_factura` para quien aplica una operación a más de una factura —el pago a cuenta
    corriente que cubre varias—. El orden es lo que evita el interbloqueo: dos pagos que tocan las
    mismas facturas en distinto orden (`[7, 3]` y `[3, 7]`) tomarían los candados cruzados y se
    esperarían para siempre; con un orden único, el segundo espera en el primero y nadie se cruza.
    Los repetidos se bloquean una vez y un id que no existe no bloquea nada (lo dice quien llama)."""
    for factura_id in sorted(set(factura_ids)):
        _bloquear_factura(conn, factura_id)


def exigir_cobro_posible(factura_id, monto, *, conn=None, leer_estado=None) -> None:
    """Levanta `CobroNoPermitido` si cobrar `monto` pesos (la **suma** de los medios) a esa factura
    la deja cobrada de más. No escribe nada.

    Con `conn`, **bloquea la factura** hasta el fin de esa transacción (ver `_bloquear_factura`):
    es lo que hace que dos cobros simultáneos se hagan cola, así que el que cobra tiene que
    escribir **en esa misma transacción** —un producto con caja propia llama esto con la conexión
    donde escribe su movimiento—. Sin `conn` sólo mira, sin candado: sirve de chequeo previo, pero
    no frena una carrera.

    `leer_estado(factura_id, conn)` reemplaza a `estado_de_cobro` (para probar sin base)."""
    leer_estado = leer_estado or (lambda fid, c: estado_de_cobro(fid, conn=c))
    if conn is not None:
        _bloquear_factura(conn, factura_id)
    estado = leer_estado(factura_id, conn)
    if estado is None:
        raise CobroNoPermitido("El comprobante no existe.", CobroNoPermitido.NO_EXISTE)
    if estado.get("anulada"):
        raise CobroNoPermitido("El comprobante está anulado: no se cobra.", CobroNoPermitido.ANULADA)
    if not estado.get("con_tope", True):
        return
    pedido = centavos(monto)
    pendiente = estado["pendiente_centavos"]
    rotulo = estado.get("rotulo", "factura")
    if pedido <= 0 or pedido <= pendiente:
        return
    detalle = f"su total es {_pesos(centavos(estado['total']))}"
    if estado["cobrado"]:
        detalle += f", ya se cobró {_pesos(centavos(estado['cobrado']))}"
    if estado.get("acreditado"):
        detalle += f" y las notas de crédito acreditan {_pesos(centavos(estado['acreditado']))}"
    if pendiente == 0:
        if estado.get("acreditado") and centavos(estado["acreditado"]) >= centavos(estado["total"]):
            quien = "ya está acreditada por completo por notas de crédito"
        elif estado.get("acreditado"):
            quien = "no tiene nada por cobrar"
        else:
            quien = "ya está cobrada por completo"
        raise CobroNoPermitido(
            f"Esta {rotulo} {quien} ({detalle}): no se puede registrar un cobro de {_pesos(pedido)}.",
            CobroNoPermitido.SIN_SALDO, pendiente=0.0)
    raise CobroNoPermitido(
        f"El cobro ({_pesos(pedido)}) supera lo que falta cobrar de la {rotulo} "
        f"({_pesos(pendiente)}): {detalle}.", CobroNoPermitido.EXCEDE, pendiente=pendiente / 100)


@contextlib.contextmanager
def transaccion_de_cobro(factura_id, monto, *, conn=None, leer_estado=None):
    """La transacción donde un producto escribe **su propio** cobro de una factura, con el tope y el candado.

    Abre una conexión (o usa la de `conn`), **bloquea la factura**, levanta `CobroNoPermitido` si `monto`
    supera lo que falta, y entrega la conexión: el producto escribe su movimiento ahí
    (`create_caja_movimiento(..., conn=c)`) y al salir del `with` se confirma todo junto. Es
    `exigir_cobro_posible` con la transacción incluida, para el cobro que no pasa por
    `registrar_cobro_factura` (el de la caja de un producto, la ruta manual de caja)::

        with transaccion_de_cobro(factura_id, monto) as c:
            create_caja_movimiento(..., factura_id=factura_id, conn=c)

    🔴 Sólo para cobros que decide una persona. El cobro con QR o MercadoPago que **ya entró** se registra
    siempre: rechazarlo sería perder plata real del cajón.

    Con `conn`, el commit es de quien llama (como todo `libracore.db`)."""
    cm = contextlib.nullcontext(conn) if conn is not None else _conexion()
    with cm as c:
        exigir_cobro_posible(factura_id, monto, conn=c, leer_estado=leer_estado)
        yield c


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
    leer_estado=None,
    conn=None,
    turno_de=None,
    exigir_turno=False,
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
    como medio (ver el docstring del módulo), y `CobroNoPermitido` si **la suma de
    los pagos** supera lo que falta cobrar de la factura, o la factura está anulada o ya
    cobrada (ADR-058). En los dos casos no escribe nada.

    **Transacción.** Con las escrituras por defecto (o con `leer_estado` por defecto), todo
    corre en una sola transacción —la de `conn`, o una propia—, con la fila de la factura
    bloqueada: ver `exigir_cobro_posible`. Eso es lo que impide que dos cobros simultáneos
    superen juntos lo pendiente. Si se inyecta alguna escritura, **tiene que aceptar `conn=`**
    y escribir con esa conexión (la propia se quedaría esperando el candado). Si se inyectan
    las tres escrituras y la lectura (los tests sin base), no hay base ni transacción.

    **Caja por turno** (ADR-060). `turno_de(conn)` devuelve el turno abierto de quien cobra (o `None`):
    `libracore.db.turnos.get_turno_activo`, con la conexión de esta transacción. Con turno, el movimiento
    y el abono de cuenta corriente entran **en ese turno** —`turno_id`, la caja del turno y su dueño como
    usuario— y `fecha` y `caja_id` del pedido se ignoran: la caja y el día los define el turno abierto, no
    lo que mande la pantalla (aceptarlos sería una segunda puerta para cargar plata en una caja o un día
    que no son los del arqueo). Sin turno y con `exigir_turno`, levanta `CobroNoPermitido`
    (`SIN_TURNO`) antes de escribir nada; sin `exigir_turno`, el cobro entra suelto, como siempre.

    Devuelve `{"total", "movimientos", "cc_pago_id"}`. No relee la factura: el
    caller decide si la vuelve a buscar para responder.
    """
    if exigir_turno and turno_de is None:
        raise ValueError("exigir_turno necesita turno_de: no hay de dónde sacar el turno")
    usa_base = any(f is None for f in (crear_movimiento, crear_cc_pago, leer_estado))
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

    # `float(True)` es `1.0`: un `{"monto": true}` registraría un cobro de 1 peso. El router ya lo rechaza (422), pero otro llamador puede pasar el dict directo.
    rechazar_booleanos(pagos or [], ("monto",), "pagos[].")
    rechazar_booleanos(pagos or [], ("medio_id",), "pagos[].", esperado="un texto")
    a_registrar = [p for p in (pagos or []) if float(p.get("monto") or 0) > 0]
    for pago in a_registrar:
        if es_medio_cuenta_corriente(pago.get("medio_id", "")):
            raise MedioNoEsDeCobro(MENSAJE_MEDIO_INVALIDO)

    concepto = concepto_de_cobro(factura, tipo_label)
    # El cliente de la cuenta corriente se busca antes de abrir la transacción: es una lectura, y
    # con SQLite la transacción de escritura ya abierta no debe esperar a otra conexión.
    cliente = None
    if a_registrar and factura.get("condicion_venta") == CONDICION_CUENTA_CORRIENTE:
        # Si el comprobante se emitió a un nombre libre (sin cliente en el padrón), no hay cuenta
        # corriente que mover y el cobro igual queda registrado en la caja.
        cliente = resolver_cliente(factura.get("cliente_cuit"))

    cm = (contextlib.nullcontext(conn) if conn is not None else _conexion()) if usa_base \
        else contextlib.nullcontext(None)
    with cm as c:
        # `conn=` sólo viaja cuando hay transacción: los dobles de los tests no la conocen.
        en_tx = {"conn": c} if c is not None else {}
        del_turno = {}
        turno = turno_de(c) if turno_de is not None and a_registrar else None
        if turno is None and exigir_turno and a_registrar:
            raise CobroNoPermitido(MENSAJE_SIN_TURNO, CobroNoPermitido.SIN_TURNO)
        if turno is not None:
            # La caja, el día y el dueño del movimiento son los del turno (ver el docstring).
            fecha = datetime.date.today().isoformat()
            caja_id = turno.get("caja_id")
            usuario_id = turno.get("usuario_id", usuario_id)
            del_turno = {"turno_id": turno["id"]}   # el abono de cuenta corriente no tiene turno: sólo el movimiento
        # Se mira y se bloquea ANTES de escribir, y con lo que hay en la base ahora (no con el
        # `factura` que trajo el llamador, que pudo cobrarse o anularse mientras tanto).
        total_pedido = sum(centavos(p["monto"]) for p in a_registrar) / 100
        if a_registrar:
            exigir_cobro_posible(factura["id"], total_pedido, conn=c, leer_estado=leer_estado)

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
                    **en_tx,
                    **del_turno,
                )
            )
            total += monto

        cc_pago_id = None
        if total > 0 and cliente:
            # El cargo lo creó la emisión; este abono es el que lo cancela.
            cc_pago_id = crear_cc_pago(
                cliente_id=cliente["id"],
                monto=total,
                fecha=fecha,
                concepto=concepto_de_cobro(factura, tipo_label, con_cliente=False),
                referencia="",
                medio_pago="",
                caja_id=caja_id,
                usuario_id=usuario_id,
                **en_tx,
            )

    return {"total": total, "movimientos": movimientos, "cc_pago_id": cc_pago_id}


def _conexion():
    from libracore.db.core import get_connection

    return get_connection()
