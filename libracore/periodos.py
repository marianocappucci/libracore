"""La aritmética de períodos de un contrato: de qué fecha a qué fecha se cobra, cuánto, y hasta cuándo se puede pagar.

Es **pura**: no lee la config, ni la base, ni importa SQLAlchemy. Quien la llama le pasa datos simples (fecha de inicio y de fin del
contrato, periodicidad, día de vencimiento, importe) y no un `Contrato`: el motor no sabe qué es el contrato de un producto. Es la misma
forma de `libracore/abono.py` y por la misma razón: la cuenta es una sola para la pantalla de previsualización, para el generador de
cuotas y para el PDF, y se prueba con una tabla.

## Qué problema resuelve

Todo producto que cobra por período —el alquiler de un equipo, el abono de mantenimiento, el alquiler de una propiedad— necesita lo mismo
y lo mismo sale mal: qué ventana cubre la cuota, qué pasa con el primer y el último mes, cuándo vence y cómo evitar emitir el mismo mes
dos veces. Son seis o siete reglas chicas y sutiles, y la segunda copia es siempre la que se olvida de febrero.

Nace de `app/services/cuotas.py` de LibraDesk (devengado de contratos, 2026-08-15, `wiki/analyses/libradesk-alquiler-de-equipos-diseno.md`), donde
estuvo andando desde entonces, y se sube al motor con la fase 0 de LibraInmo (`wiki/analyses/inmobiliaria-administracion-alquileres-diseno.md`) para
que el producto nuevo no cree un segundo devengado. **LibraDesk todavía no la importa**: pasa a hacerlo en un paso propio, sin cambiar
su comportamiento (los tests de este módulo son los casos de aritmética de los suyos).

## Las decisiones (las cuatro del humano del 2026-08-15, más las que salen de ellas)

1. **El mes se cobra ADELANTADO.** La cuota de agosto se emite el primer día que el contrato cubre de agosto y no al terminarlo:
   `fecha_emision == desde`.
2. **Prorrateo proporcional por días, en las DOS puntas.** Un contrato que arranca el 20 cobra 12/31 de agosto; uno que termina el 10
   cobra 10/31. El divisor son los días del período **entero** —28, 30, 31 o los 90 de un trimestre— y no 30 fijos: un mes de 28 días
   vale lo mismo que uno de 31, así que el día de febrero vale más, y dividir siempre por 30 le cobraría de menos a febrero.
3. 🔴 **Los períodos son de calendario, no se cuentan desde el inicio del contrato.** El primer intento de LibraDesk los anclaba al
   arranque (un mensual que empieza el 20 devengaba del 20 al 19) y eso **hace imposible el prorrateo**: el primer período arranca justo
   el día que arranca el contrato, así que siempre está completo y no hay nada que prorratear. El test que lo cubría compartía la
   premisa y pasaba en verde. Con períodos de calendario, un mensual que arranca el 20 de agosto tiene el período 01-08 al 31-08 y cubre
   12 de esos 31 días. Los multi-mes se alinean al **año**: un trimestral devenga ene-mar, abr-jun, jul-sep, oct-dic sin importar cuándo
   arrancó el contrato, y el primero le sale prorrateado igual que al mensual.
4. **Las puntas son inclusive** (`Periodo.dias` cuenta el `desde` y el `hasta`) y dos períodos contiguos no se pisan: uno termina el día
   antes de que arranque el siguiente.
5. **Un solo cargo recurrente por `(contrato, desde)`.** Un período tiene UN cargo que lo representa —completo o prorrateado, nunca los
   dos— y la clave de idempotencia es `clave_de_periodo()`. Ver más abajo qué es del motor y qué del producto.
6. **Todo en `Decimal`, redondeado a centavos con `ROUND_HALF_UP`** y no con el `ROUND_HALF_EVEN` que trae Python: el redondeo del
   banquero lleva 0,125 a 0,12 y una factura argentina lo lleva a 0,13. Sobre un prorrateo, que es una división, esto aparece seguido.

## La idempotencia: la clave es del motor, el índice único es del producto

Lo que hace que emitir dos veces el mismo mes sea imposible es un índice único **parcial** en la base del producto, sobre
`(contrato_id, periodo_desde)` filtrando los tipos de cargo que representan el período y descartando las cuotas anuladas (anular tiene
que dejar volver a generar). Eso es SQL y vive en cada producto. Acá queda la mitad pura: `clave_de_periodo()` arma la clave y
`ya_generada()` dice si la clave ya está entre las existentes, que es lo que permite mostrar «ya emitida» antes de pegarle a la base.

## Lo que NO hace

- **No sabe de contratos, precios ni monedas.** El importe llega resuelto: de qué precio vigente sale es cosa del producto.
- **No persiste ni escribe nada.** `proponer_cuota()` devuelve una propuesta; confirmarla y guardarla es del producto (en LibraDesk, la
  emisión es con confirmación humana y el importe se congela al generar).
- **No pone texto.** El concepto que ve el cliente («Abono agosto 2026 — C-0001») depende del tipo de contrato y del número, que son
  del producto.
- **No decide el nombre del cargo** (`alquiler`, `proporcional`, `mantenimiento`...): devuelve `prorrateada` y cada producto lo nombra.
- **No corre el vencimiento por feriados ni calcula mora.** Eso es `libracore.mora`.
- **No inventa un vencimiento.** Sin día de vencimiento pactado devuelve `None`, que significa «no pactado».
- Si el contrato no toca el período (empieza después o terminó antes), `proponer_cuota()` devuelve `None` y **no una cuota en cero**: «este
  mes se devengó nada» no es lo mismo que «este mes no se devengó».

## Cómo se usa

    from libracore.periodos import proponer_cuota

    cuota = proponer_cuota(
        fecha_inicio=date(2026, 8, 20), fecha_fin=None, periodicidad="mensual",
        importe=Decimal("31000"), ancla=date(2026, 8, 25), dia_vencimiento=10,
    )
    # cuota.periodo == Periodo(2026-08-20, 2026-08-31), cuota.importe == 12000.00,
    # cuota.dias_cubiertos == 12, cuota.dias_del_periodo == 31, cuota.prorrateada is True
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

#: Cuántos meses cubre un período, por periodicidad. Es el vocabulario de LibraDesk (`PERIODICIDADES` de `contratos.py`), que usa los
#: mismos cinco valores para la cadencia de cobro y para la de visita.
MESES_POR_PERIODICIDAD = {
    "mensual": 1,
    "bimestral": 2,
    "trimestral": 3,
    "semestral": 6,
    "anual": 12,
}

#: Las periodicidades válidas, de la más corta a la más larga.
PERIODICIDADES = tuple(MESES_POR_PERIODICIDAD)

_CENTAVO = Decimal("0.01")

#: El paso entre dos períodos contiguos: uno termina el día antes de que arranque el siguiente.
_UN_DIA = timedelta(days=1)


def redondear(valor: Decimal) -> Decimal:
    """Dos decimales, medio hacia arriba.

    `ROUND_HALF_UP` y no el `ROUND_HALF_EVEN` que trae Python por defecto: el banquero redondea 0.125 a 0.12 y una factura argentina lo
    redondea a 0.13. Es público porque `libracore.mora` redondea igual y una segunda copia es la que termina divergiendo.
    """
    return valor.quantize(_CENTAVO, rounding=ROUND_HALF_UP)


def a_decimal(valor: Any) -> Decimal:
    """El valor como `Decimal`, venga como `int`, `str`, `float` o `Decimal`.

    Se pasa por `str` a propósito —`Decimal(0.1)` arrastra el error binario del float, `Decimal(str(0.1))` no—, igual que
    `libracore.pagos`.
    """
    return valor if isinstance(valor, Decimal) else Decimal(str(valor))


def _ultimo_dia(anio: int, mes: int) -> int:
    return calendar.monthrange(anio, mes)[1]


def sumar_meses(d: date, meses: int) -> date:
    """`d` más N meses, recortando el día al último del mes destino.

    El 31 de enero más un mes es el 28 (o 29) de febrero. Sin este recorte la aritmética de períodos revienta con
    `ValueError: day is out of range` en los meses cortos, que es un defecto que sale una vez al año y en producción.
    """
    total = d.month - 1 + meses
    anio = d.year + total // 12
    mes = total % 12 + 1
    return date(anio, mes, min(d.day, _ultimo_dia(anio, mes)))


@dataclass(frozen=True)
class Periodo:
    """La ventana que cubre una cuota, con sus dos puntas inclusive."""

    desde: date
    hasta: date

    @property
    def dias(self) -> int:
        return (self.hasta - self.desde).days + 1

    def recortado(self, inicio: date, fin: date | None = None) -> Periodo | None:
        """La intersección del período con la vida de un contrato (`inicio` y `fin` inclusive; `fin=None` es sin fecha de fin).

        Es lo que produce el prorrateo de las dos puntas. Devuelve `None` si no se tocan: no hay período de cero días.
        """
        desde = max(self.desde, inicio)
        hasta = self.hasta if fin is None else min(self.hasta, fin)
        if desde > hasta:
            return None
        return Periodo(desde=desde, hasta=hasta)


def periodo_por_cadencia(cadencia: str, ancla: date) -> Periodo:
    """El período de CALENDARIO que **contiene** a `ancla`, para una cadencia de `PERIODICIDADES`.

    Es la aritmética a secas, con el nombre de la cadencia y no del cobro, porque tiene dos usos que no son lo mismo: cobrar y visitar
    (en LibraDesk se puede cobrar mensual y visitar trimestral). Una sola función con dos llamadores y no dos copias que puedan
    divergir: el recorte de día de `sumar_meses()` y la alineación al año de los multi-mes son sutiles.

    Para el mensual el bloque es su mes; para los multi-mes se alinea al año (ver el docstring del módulo, decisión 3).
    """
    try:
        paso = MESES_POR_PERIODICIDAD[cadencia]
    except KeyError:
        raise ValueError(f"Periodicidad desconocida: {cadencia!r}. Las válidas son {list(PERIODICIDADES)}.") from None
    # En qué bloque del año cae el ancla. Para el mensual (paso 1) esto es simplemente su mes.
    bloque = (ancla.month - 1) // paso
    desde = date(ancla.year, bloque * paso + 1, 1)
    hasta = sumar_meses(desde, paso) - _UN_DIA
    return Periodo(desde=desde, hasta=hasta)


def periodo_anclado(cadencia: str, *, ancla: date, arranque: date) -> Periodo:
    """El período que contiene a `ancla`, **contado desde `arranque`** y no alineado al calendario.

    Es la diferencia con `periodo_por_cadencia()`. Un trimestral que arranca el 15-02 va de feb a abr, de may a jul, de ago a oct y de nov
    a ene, que es como se pacta con un cliente. Lo usan las visitas de mantenimiento de LibraDesk (de donde viene, `app/services/visitas.py`)
    y el ajuste por índice de LibraInmo («se ajusta cada tres meses desde el inicio del contrato»). **Las cuotas siguen usando la de
    calendario**: el prorrateo del primer mes lo exige (decisión 3 del módulo), y una visita o un ajuste no se prorratean.

    Los períodos se encadenan desde `arranque` (`sumar_meses(arranque, n × paso)`), no uno desde el anterior: un mensual que arranca el 31-01
    va del 31-01 al 27-02, del 28-02 al 30-03 y del 31-03 al 30-04, sin que el recorte de febrero se arrastre a los meses siguientes.

    🔴 **El período se corrige hasta que contenga a `ancla`.** La cuenta de meses de LibraDesk restaba uno cuando el día del ancla era menor
    que el del arranque, y con un arranque a fin de mes eso falla: arranque 31-01 y ancla 28-02 daba el período del 31-01 al 27-02, que no
    contiene al 28. Acá la estimación se corrige mirando el período armado, que es lo que no se puede equivocar.

    Con `ancla` anterior a `arranque` devuelve el período anterior al primero: quien llama controla la vigencia, como en LibraDesk.
    """
    try:
        paso = MESES_POR_PERIODICIDAD[cadencia]
    except KeyError:
        raise ValueError(f"Periodicidad desconocida: {cadencia!r}. Las válidas son {list(PERIODICIDADES)}.") from None
    meses = (ancla.year - arranque.year) * 12 + (ancla.month - arranque.month)
    n = meses // paso

    def armar(k: int) -> Periodo:
        desde = sumar_meses(arranque, k * paso)
        return Periodo(desde=desde, hasta=sumar_meses(arranque, (k + 1) * paso) - _UN_DIA)

    periodo = armar(n)
    while ancla < periodo.desde:
        n -= 1
        periodo = armar(n)
    while ancla > periodo.hasta:
        n += 1
        periodo = armar(n)
    return periodo


def periodo_de(periodicidad: str, ancla: date) -> Periodo:
    """El período de FACTURACIÓN de calendario que contiene a `ancla`.

    Envoltorio de `periodo_por_cadencia()` sobre la periodicidad de **cobro** del contrato; se conserva con el nombre que tiene en
    LibraDesk para que su migración sea cambiar el import.
    """
    return periodo_por_cadencia(periodicidad, ancla)


def vencimiento_de(dia_vencimiento: int | None, periodo: Periodo) -> date | None:
    """El `dia_vencimiento` pactado, dentro del mes en que arranca `periodo`.

    Se recorta al último día del mes: un contrato con vencimiento el 31 no puede vencer el 31 de febrero. Sin día pactado (`None`) no se
    inventa ninguno: devuelve `None`, que significa «no pactado».

    🔴 Cuenta el mes de `periodo.desde`, y en una cuota prorrateada ése es el del **primer día cubierto**, no el del mes calendario
    entero. Si el contrato arranca el 20 de agosto con vencimiento el 10, el vencimiento es el 10 de agosto: **anterior a la emisión**.
    Es el comportamiento que LibraDesk tiene hoy y se porta tal cual; quien lo muestre o calcule mora sobre él tiene que saberlo.
    """
    if dia_vencimiento is None:
        return None
    if not 1 <= dia_vencimiento <= 31:
        raise ValueError(f"El día de vencimiento tiene que estar entre 1 y 31, no {dia_vencimiento}.")
    base = periodo.desde
    return date(base.year, base.month, min(dia_vencimiento, _ultimo_dia(base.year, base.month)))


@dataclass(frozen=True)
class CuotaPropuesta:
    """Lo que se cobraría por un contrato en un período. **No se persiste.**

    Existe para que la previsualización y la generación compartan el cálculo entero en vez de tener cada una el suyo: si fueran dos
    caminos, la pantalla podría mostrar un número y la base guardar otro, y nadie se enteraría hasta que un cliente reclamara.
    """

    #: Lo que efectivamente se cobra: el período calendario recortado por la vigencia del contrato.
    periodo: Periodo
    #: Los días cubiertos y los del período entero. Iguales salvo en el prorrateo; es lo que la pantalla muestra para explicar por qué
    #: un mes sale menos que el anterior.
    dias_cubiertos: int
    dias_del_periodo: int
    #: El importe del período: prorrateado (`importe * dias_cubiertos / dias_del_periodo`) o completo, a centavos con `ROUND_HALF_UP`.
    importe: Decimal
    #: Mes adelantado: el primer día que el contrato cubre del período.
    fecha_emision: date
    #: `None` si el contrato no tiene día de vencimiento pactado.
    fecha_vencimiento: date | None

    @property
    def prorrateada(self) -> bool:
        return self.dias_cubiertos < self.dias_del_periodo


def proponer_cuota(
    *,
    fecha_inicio: date,
    fecha_fin: date | None,
    periodicidad: str,
    importe: Any,
    ancla: date,
    dia_vencimiento: int | None = None,
) -> CuotaPropuesta | None:
    """La cuota del período que contiene a `ancla`, o `None` si el contrato no llega a tocarlo.

    `fecha_inicio` y `fecha_fin` son inclusive y `fecha_fin=None` es un contrato sin fecha de fin. `importe` es el del período entero
    (el alquiler mensual, o el trimestral si la periodicidad es trimestral) y puede venir como `Decimal`, `int`, `str` o `float`.
    `ancla` es cualquier fecha del período: el 5 y el 25 de agosto dan la misma cuota.

    Devuelve `None` —y no una cuota en cero— si la vigencia no toca el período, y también si `fecha_fin < fecha_inicio` (un contrato
    sin días no devenga nada; validar que las fechas tengan sentido es del producto, al cargar el contrato).
    """
    periodo = periodo_de(periodicidad, ancla)
    cubierto = periodo.recortado(fecha_inicio, fecha_fin)
    if cubierto is None:
        return None

    importe = a_decimal(importe)
    if cubierto.dias < periodo.dias:
        # Sobre el período COMPLETO, no sobre 30 fijos (ver el docstring del módulo, decisión 2).
        total = redondear(importe * Decimal(cubierto.dias) / Decimal(periodo.dias))
    else:
        total = redondear(importe)

    return CuotaPropuesta(
        periodo=cubierto,
        dias_cubiertos=cubierto.dias,
        dias_del_periodo=periodo.dias,
        importe=total,
        # ADELANTADO (decisión del 2026-08-15): se emite el primer día que el contrato cubre del período, no al terminarlo.
        fecha_emision=cubierto.desde,
        fecha_vencimiento=vencimiento_de(dia_vencimiento, cubierto),
    )


def clave_de_periodo(contrato: Any, periodo: Periodo) -> tuple[Any, date]:
    """La clave de idempotencia de un cargo recurrente: `(contrato, periodo.desde)`.

    Un contrato tiene **un** cargo que representa cada período. La clave usa sólo `desde` y no `hasta`: un contrato que se acorta a mitad
    de mes no puede terminar con dos cargos del mismo mes, uno por cada `hasta`. `contrato` es lo que el producto use para identificarlo
    (su `id`, su número); el motor no lo interpreta.
    """
    return (contrato, periodo.desde)


def ya_generada(clave: tuple[Any, date], existentes: Iterable[tuple[Any, date]]) -> bool:
    """Si la clave ya está entre las de los cargos recurrentes existentes.

    El producto pasa las claves de sus cuotas **no anuladas** (anular una cuota tiene que permitir volver a generar el período). Es la
    mitad «el error»; la mitad «el duplicado imposible» es el índice único parcial de su base. Ver el docstring del módulo.
    """
    return clave in set(existentes)
