"""La nota de crédito de la familia: UNA implementación, normalizada para todos los productos.

Decidido por el humano el 2026-10-04: **las notas de crédito salen del motor y son iguales para todos los
productos.** No hay una nota «de LibraCargo» ni una «de Contalibra»: lo que se decide una vez vale para los siete
que emiten por ARCA (`reglas/producto.md` del wiki: el arreglo de fondo vive siempre en el motor).

Qué vive acá, porque **no depende del modelo de cada producto**:

- qué nota corresponde a qué comprobante (`TIPO_NC`: la letra y si es FCE se heredan);
- las **guardas**: la factura no se acredita dos veces, una nota que quedó sin CAE no se duplica, el receptor
  sirve para ARCA, y dos pedidos simultáneos emiten una sola nota;
- el **armado** de la nota: fecha de hoy (no se elige), el comprobante asociado (`cbte_asoc_*`) que la ata a su
  factura ante ARCA, la marca de la FCE;
- el **orden de las operaciones**: numerar → registrar → pedir el CAE.

Qué NO vive acá, porque sí depende del modelo, y cada producto lo aporta como **costura** (un callable):
cómo se carga el original, dónde se guarda la nota y qué pasa con lo suyo cuando se acredita (las órdenes de
LibraCargo, la venta de VentaLibra, el asiento de cuenta corriente). Un producto que no tiene la costura que
necesita **la agrega acá**; no escribe su propia versión de la lógica.

Fase 1: nota de crédito **total**. Fase 2 (esta): nota **parcial** por importe y **tope acumulado** (ADR-018): las notas de una
factura suman, y la suma nunca supera su total. ARCA no lo valida (acepta de más: medido el 2026-10-03), así que es nuestro.

Medido contra ARCA de homologación (2026-10-03 y 2026-10-04): ARCA **no** lleva el saldo de una factura común
(acepta una segunda nota total y una nota por más del importe), así que estas guardas son la única defensa; la
fecha de la nota tiene que ser la de hoy (ARCA exige fechas no decrecientes por tipo y punto de venta); y una
nota de crédito A a un CUIT inexistente, asociada a la factura que se le emitió, sí se autoriza.
"""

from __future__ import annotations

import contextlib
import datetime
import inspect
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from libracore import arca_wsfe, fechas
from libracore import tipos_comprobante as tipos
from libracore.candados import candado_de_base

#: De qué factura sale qué nota de crédito. La letra se conserva y también si es FCE.
TIPO_NC = tipos.TIPO_NC
_TIPOS_C = tipos.C   # C y FCE C: no discriminan IVA, todo el importe va como neto


class NotaNoPermitida(Exception):
    """El motivo por el que la nota no se puede emitir, dicho para una persona.

    Lleva un `codigo` para que cada producto lo traduzca a su respuesta (un router lo mapea a 400, 409 o 422;
    un servicio sin HTTP lo muestra como quiera). El motor no sabe de HTTP acá a propósito.
    """

    TIPO = "tipo"                    # el comprobante no admite nota de crédito (ya es una nota, o no es de ARCA)
    YA_TIENE_NOTA = "ya_tiene_nota"  # la factura ya fue acreditada
    NOTA_SIN_CAE = "nota_sin_cae"    # ya hay una nota, pero quedó sin CAE: se autoriza o se borra
    EN_CURSO = "en_curso"            # otro pedido está emitiendo la nota de esta misma factura
    RECEPTOR = "receptor"            # el CUIT del receptor no sirve para ARCA
    IMPORTE = "importe"              # el importe pedido no es un monto válido (cero, negativo, más de 2 decimales)
    SUPERA_SALDO = "supera_saldo"    # lo acreditado más este importe superaría el total de la factura
    ORIGINAL_SIN_CAE = "original_sin_cae"  # el comprobante no tiene CAE: ARCA no lo conoce (la nota de débito lo exige)
    DESTINO = "destino"              # el destino de la plata (devolver / a favor) no sirve para esta factura (ADR-065)
    SIN_TURNO = "sin_turno"          # hay que devolver plata de la caja y quien emite no tiene un turno abierto (ADR-065)

    def __init__(self, mensaje: str, codigo: str):
        super().__init__(mensaje)
        self.codigo = codigo


def nombre_de_tipo(tipo: int) -> str:
    """`Factura C`, `Nota de Crédito FCE A`… como se lee en un papel."""
    return tipos.NOMBRE.get(int(tipo), "comprobante")


def etiqueta(comprobante: dict) -> str:
    """`Factura C 0001-00000123`: lo que el humano busca en su listado."""
    return (f"{nombre_de_tipo(comprobante['tipo'])} "
            f"{str(comprobante['punto_venta']).zfill(4)}-{str(comprobante['numero']).zfill(8)}")


def tipo_de_nota_de_credito(tipo_original: int) -> int:
    """El tipo de la nota que acredita un comprobante de este tipo."""
    nota = TIPO_NC.get(int(tipo_original))
    if nota is None:
        raise NotaNoPermitida("Tipo de comprobante no admite nota de crédito", NotaNoPermitida.TIPO)
    return nota


# ── El candado ─────────────────────────────────────────────────────────────

#: Los comprobantes que tienen una nota **en camino** en este proceso. Ver `una_nota_a_la_vez`.
_EN_CURSO: set[Any] = set()
_EN_CURSO_LOCK = threading.Lock()


def en_curso(clave: Any) -> bool:
    """¿Hay una nota en camino para esta clave **en este proceso**? (Lo usan los tests.)"""
    with _EN_CURSO_LOCK:
        return clave in _EN_CURSO


def _nombre_del_candado(clave: Any) -> str:
    return f"nota-de-credito:{clave!r}"


@contextlib.contextmanager
def una_nota_a_la_vez(clave: Any, conn: Any = None, *, clase: str = "crédito"):
    """Un solo pedido de nota de crédito por comprobante a la vez.

    🔴 **Sin esto la consulta «¿ya tiene una nota?» no alcanza**: dos pedidos que llegan juntos —un doble
    clic, o dos admins— la contestan los dos antes de que ninguno haya creado la nota, y los dos emiten. La nota
    tarda: pide número a ARCA, crea la fila y pide el CAE.

    **Dos capas, porque el motor puede correr con varios procesos por instancia (`uvicorn --workers N`):**

    1. Un candado **en memoria**, siempre: frena a los pedidos del mismo proceso sin ir a la base.
    2. Con `conn`, un **candado de la base** (`libracore.candados.candado_de_base`): frena a los de los otros
       procesos. Sin esta capa, con N workers un doble clic cae en dos procesos distintos y **emite dos notas ante
       ARCA** —la guarda en memoria no los ve—. Sin `conn` (o con SQLite) queda sólo la primera capa, que es lo
       que había con un solo proceso.

    `conn` es la conexión **de la transacción donde el producto escribe la nota** (la de `libracore.db`, `psycopg` o
    una `Session` de SQLAlchemy). 🔴 En PostgreSQL el candado dura **hasta que esa transacción termina**: el
    producto no hace `commit` ni `rollback` de `conn` hasta que la nota esté emitida, y como el candado se suelta
    con el `commit`, el pedido siguiente ya encuentra la nota que dejó este. Si el producto escribe con otras
    conexiones (los `db_*` del motor abren y cierran la suya en cada llamada), le pasa **una conexión aparte, abierta
    durante toda la emisión** y sin escribir nada: el candado es su única función.

    Una restricción única en la base sería más fuerte, pero una base con duplicados históricos no podría crearla, y
    esta guarda no puede fallar el arranque de un producto en producción.

    `clave` es lo que identifica al comprobante **en ese producto** (su id, o una tupla si hace falta distinguir
    tablas); el motor no le da otro significado. `clase` sólo cambia el texto del rechazo (`crédito` o `débito`): la
    nota de débito usa su propia `clave`, así que una nota de crédito en camino no frena a una de débito.
    """
    with _EN_CURSO_LOCK:
        if clave in _EN_CURSO:
            raise _en_curso_error(clase)
        _EN_CURSO.add(clave)
    try:
        if conn is None:
            yield
            return
        with candado_de_base(conn, _nombre_del_candado(clave)) as obtenido:
            if not obtenido:
                raise _en_curso_error(clase)
            yield
    finally:
        with _EN_CURSO_LOCK:
            _EN_CURSO.discard(clave)


def _en_curso_error(clase: str = "crédito") -> NotaNoPermitida:
    return NotaNoPermitida(
        f"Ya hay una nota de {clase} en curso para esta factura: esperá a que termine antes de pedir otra.",
        NotaNoPermitida.EN_CURSO,
    )


# ── El dinero de una nota ──────────────────────────────────────────────────

_CENTAVO = Decimal("0.01")


def _dec(valor: Any) -> Decimal:
    """Un importe como `Decimal` de dos decimales. Pasa por `str` para no arrastrar el error binario de un `float`."""
    return Decimal(str(valor)).quantize(_CENTAVO, rounding=ROUND_HALF_UP)


def _es_cae(cae: Any) -> bool:
    return bool(cae) and cae != "PENDIENTE"


def acreditado(original: dict, previas: Sequence[dict]) -> Decimal:
    """Lo que ya acreditan las notas **con CAE** de esta factura.

    Una previa sin `total` (un producto que todavía no lo informa) cuenta como **la factura entera**: es el
    comportamiento de la fase 1, donde cualquier nota previa bloqueaba, y es el lado seguro.
    """
    suma = Decimal("0.00")
    for nota in previas:
        if not _es_cae(nota.get("cae")):
            continue
        suma += _dec(nota["total"]) if nota.get("total") is not None else _dec(original["total"])
    return suma


def saldo_acreditable(original: dict, previas: Sequence[dict]) -> Decimal:
    """Cuánto se puede todavía acreditar de la factura: su total menos lo que ya acreditan sus notas con CAE."""
    return max(_dec(original["total"]) - acreditado(original, previas), Decimal("0.00"))


def repartir_importe(original: dict, importe: Any) -> tuple[Decimal, Decimal]:
    """`(neto, iva)` de una nota parcial por `importe` (con IVA incluido), con la alícuota **de la factura**.

    El motor arma un solo bloque de IVA por comprobante (`arca_wsfe.solicitar_cae`), así que la alícuota es la que sale
    de los montos de la factura. El neto sale del importe y el IVA es la **resta**: `neto + iva == importe` siempre,
    sin un centavo perdido. Medido en homologación (2026-10-04): ARCA autoriza la nota con el IVA a hasta 15 centavos
    del exacto, así que el redondeo no es un riesgo.
    Un comprobante C no discrimina IVA: todo el importe va como neto.
    """
    importe = _dec(importe)
    sub, iva = _dec(original["subtotal"]), _dec(original["iva_amount"])
    if original["tipo"] in _TIPOS_C or sub <= 0 or iva <= 0:
        return importe, Decimal("0.00")
    neto = (importe * sub / (sub + iva)).quantize(_CENTAVO, rounding=ROUND_HALF_UP)
    return neto, importe - neto


def importe_valido(importe: Any) -> Decimal:
    """El importe de una nota como `Decimal`, o `NotaNoPermitida` (`IMPORTE`) si no es un monto: un número finito,
    mayor que cero y con hasta dos decimales. Lo comparten la nota de crédito y la de débito."""
    try:
        valor = Decimal(str(importe))
    except InvalidOperation:
        raise NotaNoPermitida(f"El importe {importe!r} no es un número.", NotaNoPermitida.IMPORTE) from None
    if not valor.is_finite() or valor <= 0 or valor != valor.quantize(_CENTAVO):
        raise NotaNoPermitida(
            "El importe de la nota tiene que ser mayor que cero y con hasta dos decimales.",
            NotaNoPermitida.IMPORTE,
        )
    return valor


# ── Las guardas ────────────────────────────────────────────────────────────

def validar_nota_de_credito(original: dict, previas: Sequence[dict], importe: Any = None) -> int:
    """Dice, antes de pedirle nada a ARCA, si esta factura se puede acreditar. Devuelve el tipo de la nota.

    `original` es el comprobante a acreditar, con las claves que usa toda la familia (`tipo`, `punto_venta`,
    `numero`, `total`, `cliente_cuit`, `cliente_razon`…). `previas` son las notas de crédito que **ya cuelgan** de él,
    cualquiera sea su estado (cada una con `cae` y, para que sumen, `total`).

    - **Sin `importe`: nota total.** Copia la factura entera, así que sólo se admite si la factura no tiene ninguna
      nota. ARCA no lo frena (acepta una segunda nota total y una nota por más del importe: medido el 2026-10-03), y
      cada nota registra un abono: una factura a cuenta corriente quedaba con el saldo en −total.
    - **Con `importe`: nota parcial.** Es el monto a acreditar, con IVA. Se admite mientras **lo ya acreditado más este
      importe no supere el total de la factura** (el tope acumulado). Una factura se acredita en una o en varias notas.
    - **Una FCE** sólo admite notas **con `importe`, y por menos que su saldo**: ARCA no deja anularla por completo si el
      comprador no la rechazó (`10154` y `10184`, medidos el 2026-10-03).

    En los dos casos, cualquier nota previa **sin CAE** frena: ya tiene número, y lo que corresponde es autorizarla o
    borrarla, no pedir otra encima.
    """
    tipo_nota = tipo_de_nota_de_credito(original["tipo"])
    total = _dec(original["total"])
    if importe is not None:
        importe = importe_valido(importe)
    for nota in previas:
        if not _es_cae(nota.get("cae")):
            raise NotaNoPermitida(
                f"Esta factura ya tiene la {etiqueta(nota)}, que todavía no tiene CAE: autorizala o eliminala; "
                "no se pide otra nota encima.",
                NotaNoPermitida.NOTA_SIN_CAE,
            )
    saldo = saldo_acreditable(original, previas)
    if importe is None:
        if int(original["tipo"]) in tipos.FCE_FACTURA:
            # La nota total copia la factura entera, y una FCE sólo admite notas por MENOS que su saldo (10184);
            # anularla por completo exige que el comprador la rechace (10154). Sin esto la frenaba ARCA, después de
            # tomar el número.
            raise NotaNoPermitida(
                f"La nota de una factura de crédito electrónica va por un importe menor que el saldo ({saldo}): "
                "ARCA sólo deja anularla por completo si el comprador la rechazó.",
                NotaNoPermitida.IMPORTE,
            )
        if previas:
            nota = previas[0]
            if saldo > 0:
                raise NotaNoPermitida(
                    f"Esta factura ya tiene notas por {_dec(total - saldo)} y la nota total copia la factura entera: "
                    f"pedí una nota por el saldo acreditable ({saldo}).",
                    NotaNoPermitida.YA_TIENE_NOTA,
                )
            raise NotaNoPermitida(
                f"Esta factura ya tiene la {etiqueta(nota)}: una factura se acredita una sola vez.",
                NotaNoPermitida.YA_TIENE_NOTA,
            )
    else:
        if importe > saldo:
            raise NotaNoPermitida(
                (f"El importe ({importe}) supera lo que queda por acreditar de la factura ({saldo}): "
                 f"su total es {total} y ya hay notas por {_dec(total - saldo)}.")
                if saldo > 0 else
                f"Esta factura ya está acreditada por completo ({total}): no queda nada para una nota nueva.",
                NotaNoPermitida.SUPERA_SALDO,
            )
        if int(original["tipo"]) in tipos.FCE_FACTURA and importe >= saldo:
            # Medido (2026-10-03): una FCE lleva saldo en ARCA y una nota sin anulación sólo puede acreditar MENOS
            # que el saldo (10184); revertirla entera exige que el comprador la rechace.
            raise NotaNoPermitida(
                f"La nota de una factura de crédito electrónica tiene que ser por menos que el saldo ({saldo}): "
                "ARCA sólo deja anularla por completo si el comprador la rechazó.",
                NotaNoPermitida.IMPORTE,
            )
    problema = arca_wsfe.problema_del_receptor({
        "tipo": tipo_nota,
        "cliente_cuit": original.get("cliente_cuit") or "",
        "cliente_razon": original.get("cliente_razon") or "",
    })
    if problema:
        raise NotaNoPermitida(problema, NotaNoPermitida.RECEPTOR)
    return tipo_nota


# ── El armado ──────────────────────────────────────────────────────────────

def armar_nota(
    original: dict, tipo_nota: int, *, hoy: datetime.date | None = None,
    prefijo: str = "Anula", motivo: str = "", importe: Any = None,
    leyenda: str | None = None, por_importe: bool = False,
) -> dict:
    """El diccionario de la nota (sin número todavía), con las claves que usa toda la familia.

    🔑 **La nota total copia los importes —y los ítems, si el producto los tiene— del original tal cual.** Una nota que
    anula tiene que decir exactamente lo mismo que anula: si los recalculara —con la tasa de hoy, o con un precio que
    cambió— anularía un importe distinto del que se facturó, y ante ARCA quedarían dos comprobantes que no cierran.

    🔑 **La nota parcial (`importe` menor que el total) acredita un monto, no unas líneas:** el neto y el IVA salen de
    `repartir_importe` con la alícuota de la factura, y lleva **un solo ítem** que dice qué acredita (los ítems del
    original no se copian: no son lo que se está acreditando).

    🔑 **La fecha es la de hoy y no se elige.** ARCA exige fechas no decrecientes por tipo y punto de venta: una
    nota con fecha futura bloquea a todas las siguientes de ese tipo hasta esa fecha, y no hay cómo deshacerlo.

    `cbte_asoc_*` es lo que ata la nota a su factura ante ARCA; sin eso es un comprobante suelto. Una nota de FCE
    exige además la fecha del asociado (`10158`) y decir si anula la factura (opcional 22): `N` siempre, porque `S`
    sólo la acepta ARCA si el comprador rechazó la factura (`10154`), y eso no lo decide quien emite.

    `leyenda` pisa el texto que la nota lleva como observación y como descripción de su ítem, y `por_importe` hace que
    la nota vaya siempre por `importe` y no copie la factura aunque coincidan: los dos los usa la nota de débito
    (`libracore.notas_de_debito`). Sin ellos, la nota de crédito se arma exactamente como siempre.
    """
    fecha = (hoy or fechas.hoy()).isoformat()
    # `por_importe` (la nota de débito, ADR-049): la nota va SIEMPRE por el importe pedido, aunque coincida con el total
    # de la factura. Una de débito no copia el comprobante: cobra un monto nuevo (intereses, un gasto, una diferencia).
    parcial = importe is not None and (por_importe or _dec(importe) != _dec(original["total"]))
    if parcial:
        neto, iva = repartir_importe(original, importe)
        subtotal, iva_amount, total = float(neto), float(iva), float(_dec(importe))
        leyenda = leyenda or f"Acredita {_dec(importe)} de {etiqueta(original)}"
    else:
        subtotal, iva_amount, total = original["subtotal"], original["iva_amount"], original["total"]
        leyenda = leyenda or f"{prefijo} {etiqueta(original)}"
    nota = {
        "tipo": tipo_nota,
        "punto_venta": original["punto_venta"],
        "fecha": fecha,
        "cliente_cuit": original.get("cliente_cuit") or "",
        "cliente_razon": original.get("cliente_razon") or "",
        "cliente_iva_cond": original.get("cliente_iva_cond") or 0,
        "subtotal": subtotal,
        "iva_amount": iva_amount,
        "total": total,
        "concepto": original.get("concepto", 1),
        "observaciones": leyenda + (f" — {motivo}" if motivo else ""),
        "cliente_domicilio": original.get("cliente_domicilio", ""),
        "fch_serv_desde": original.get("fch_serv_desde", ""),
        "fch_serv_hasta": original.get("fch_serv_hasta", ""),
        "fch_vto_pago": fecha,
        "cbte_asoc_tipo": original["tipo"],
        "cbte_asoc_pv": original["punto_venta"],
        "cbte_asoc_nro": original["numero"],
        "cbte_asoc_fecha": original["fecha"],
        "fce_anulacion": "N" if tipo_nota in tipos.FCE_NOTA else "",
    }
    if parcial:
        nota["items"] = [{"description": leyenda, "qty": 1, "unit_price": subtotal, "subtotal": subtotal}]
    elif "items" in original:
        nota["items"] = original["items"]
    return nota


# ── El orden de las operaciones ────────────────────────────────────────────

@dataclass(frozen=True)
class NotaEmitida:
    """Lo que devolvió cada paso, para que el producto cierre lo suyo (órdenes, venta, cuenta corriente)."""

    nota: dict          # la nota armada, con su número
    registro: Any       # lo que devolvió `registrar` (el id de la fila, el objeto del ORM…)
    resultado: Any      # lo que devolvió `pedir_cae`


async def _esperar(valor: Any) -> Any:
    """Acepta tanto una función común como una corrutina: el producto elige."""
    return await valor if inspect.isawaitable(valor) else valor


async def emitir_nota_de_credito(
    original: dict,
    *,
    clave: Any,
    cargar_previas: Callable[[], Sequence[dict]],
    numerar: Callable[[int, int], Any],
    registrar: Callable[[dict, Any], Any],
    pedir_cae: Callable[[Any, dict, Any], Any],
    hoy: datetime.date | None = None,
    prefijo: str = "Anula",
    motivo: str = "",
    importe: Any = None,
    conn: Any = None,
) -> NotaEmitida:
    """Emite la nota de crédito de un comprobante: **total** (sin `importe`) o **parcial** (`importe`, con IVA). El orden
    y las guardas son los mismos en todos los productos.

    Las **costuras** que aporta cada producto:

    - `cargar_previas()`: las notas de crédito que ya cuelgan del original (de **su** modelo). Se llama
      **adentro del candado**: si el producto las leyera antes, dos pedidos simultáneos la contestarían vacía los dos.
    - `numerar(tipo_nota, punto_venta)` → `(numero, contexto)`: el número que sigue (de ARCA si emite por ARCA).
      El `contexto` es opaco para el motor (el ticket de WSAA, la configuración…) y se lo devuelve a los otros pasos.
    - `registrar(nota, contexto)` → registro: guarda la nota con su número, todavía sin CAE (de **su** modelo).
    - `pedir_cae(registro, nota, contexto)` → resultado: pide el CAE y completa el registro. Si ARCA rechaza, que
      levante: el motor no atrapa nada, así el producto decide qué queda (en una transacción, nada).

    Lo que pasa con lo suyo (las órdenes, la venta, la cuenta corriente) lo hace el producto **con el resultado**,
    después de que esto vuelva.

    `conn` (opcional) es la conexión con la que se toma el candado de la base; sin ella, con varios procesos por
    instancia, dos pedidos simultáneos pueden emitir dos notas. Ver `una_nota_a_la_vez`.

    Levanta `NotaNoPermitida` si la nota no corresponde, sin haberle pedido nada a ARCA.
    """
    with una_nota_a_la_vez(clave, conn):
        tipo_nota = validar_nota_de_credito(original, cargar_previas(), importe)
        nota = armar_nota(original, tipo_nota, hoy=hoy, prefijo=prefijo, motivo=motivo, importe=importe)
        numero, contexto = await _esperar(numerar(tipo_nota, original["punto_venta"]))
        nota["numero"] = numero
        registro = await _esperar(registrar(nota, contexto))
        resultado = await _esperar(pedir_cae(registro, nota, contexto))
        return NotaEmitida(nota=nota, registro=registro, resultado=resultado)


# ── La cuenta corriente: quién la acredita ─────────────────────────────────

def referencia_cc_de_nota(factura_id: int, nota_id: int | None = None) -> str:
    """La marca con la que la nota de crédito de una factura deja su abono en la cuenta corriente.

    🔴 Existe para que **anular una venta no acredite la misma deuda por segunda vez**. La nota (de una factura a
    cuenta corriente) y `anular_venta` (libracommerce) acreditan la misma deuda y ninguna sabe que la otra ya lo hizo:
    el saldo quedaba en −total. La marca va en `cc_pagos.referencia`, que es texto libre y no se usaba.

    Una factura puede tener **varias notas** (parciales): cada abono lleva `nc:factura:<id>:<nota_id>`, y la forma
    sin `nota_id` (`nc:factura:<id>`, de la fase 1) sigue valiendo para los abonos que ya están en la base.
    """
    base = f"nc:factura:{int(factura_id)}"
    return base if nota_id is None else f"{base}:{int(nota_id)}"


def cc_acreditado_por_notas(conn: Any, factura_id: int) -> Decimal:
    """Cuánto abonaron en la cuenta corriente las notas de esta factura (`conn`: la conexión del producto)."""
    base = referencia_cc_de_nota(factura_id)
    fila = conn.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM cc_pagos WHERE referencia = ? OR referencia LIKE ?",
        (base, base + ":%"),
    ).fetchone()
    return _dec(fila[0])


def cc_acreditada_por_nota(conn: Any, factura_id: int) -> bool:
    """¿Alguna nota de esta factura ya abonó la cuenta corriente del cliente?"""
    return cc_acreditado_por_notas(conn, factura_id) > 0


def devuelto_por_notas(conn: Any, factura_id: int) -> Decimal:
    """Cuánta plata **salió de la caja** por las notas de esta factura (ADR-065): los egresos con la marca de la nota.

    Es la contracara de `cc_acreditado_por_notas`: una nota de una factura **al contado** que se emitió eligiendo
    «Devolver» deja un egreso con `referencia = nc:factura:<id>:<nota_id>` y el `factura_id` de la factura; una
    emitida eligiendo «Dejar a favor del cliente» deja un abono con la misma marca, y ése lo suma
    `cc_acreditado_por_notas`. 🔴 **Quien anula la venta de esa factura tiene que restar las dos cosas** de lo que
    revierte de la caja: la plata que la nota ya devolvió no se devuelve otra vez. Un movimiento anulado no cuenta."""
    from libracore.db.caja import sql_no_anulado

    base = referencia_cc_de_nota(factura_id)
    fila = conn.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos"
        f" WHERE tipo = 'egreso' AND factura_id = ? AND {sql_no_anulado()}"
        " AND (referencia = ? OR referencia LIKE ?)",
        (int(factura_id), base, base + ":%"),
    ).fetchone()
    return _dec(fila[0])
