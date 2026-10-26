"""El punitorio de un pago fuera de término: interés simple por los días de atraso, con gracia y corrimiento por día no hábil.

Es **puro**: no lee la config ni la base. Quien lo llama le pasa los datos del contrato (importe adeudado, vencimiento, tasa, días de
gracia) y la fecha del pago, y recibe un dict con la cuenta completa para imprimirla en el recibo. Misma forma que `libracore/abono.py` y
`libracore/periodos.py`: una tabla alcanza para probarlo, y es la misma cuenta para la pantalla del cobro, para el aviso de deuda y para
el PDF.

## Qué problema resuelve

Un inquilino paga el 14 un alquiler que vencía el 10 y el contrato pacta un punitorio. Hoy esa cuenta —¿desde cuándo corre?, ¿cuenta el
domingo?, ¿y los días de gracia?— se resuelve a ojo, distinto en cada pantalla, y el número que se cobra no se puede explicar después.
Acá se decide **una vez** y el resultado trae el detalle que lo justifica.

Lo necesitan dos productos: LibraDesk, que ya tiene `vencida` como estado de cuota (`wiki/analyses/libradesk-alquiler-de-equipos-diseno.md`), y
LibraInmo (`wiki/analyses/inmobiliaria-administracion-alquileres-diseno.md`), que cobra alquileres con punitorio pactado. Se sube al motor con su fase 0,
junto con `libracore.periodos`, de donde salen el vencimiento y el redondeo.

## Las decisiones

1. **Interés simple**, sin capitalizar: `importe × tasa × días`. Un punitorio de alquiler se pacta así y es lo que se puede escribir en
   un recibo sin una planilla al lado.
2. **La tasa es un porcentaje** (`0.5` es 0,5 %, no 50 %), porque así se carga en un contrato y así se lee. Es **diaria** o **mensual**
   (`base`). La mensual se toma como `tasa / 30` por día, de **30 fijos** y no de los días reales del mes: es la convención de los
   contratos de locación, y deja el mismo interés por día en febrero y en enero. (Distinto del prorrateo de `libracore.periodos`, que sí
   divide por los días reales: allá se reparte un importe entre los días de un período; acá se aplica una tasa pactada.)
3. **El vencimiento que cae en día no hábil se corre al siguiente hábil** (`correr_dia_no_habil`, activo por defecto). Hábil es de lunes a
   viernes y que no sea feriado nacional (`libracore.feriados`). El producto puede sumar sus propios días no hábiles con
   `dias_no_habiles`: el catálogo nacional no trae los provinciales ni los cierres propios, y «el feed propone, no dispone».
4. **La gracia son días corridos** que se cuentan **desde el vencimiento ya corrido**. Pagar hasta el último día de gracia inclusive no
   genera punitorio. Pagar un día después sí.
5. **El interés corre desde el vencimiento** (regla por defecto): si se pasó de la gracia, se cobra por *todos* los días de atraso, los de
   gracia incluidos. Con `desde_fin_de_gracia=True` corre sólo desde que termina la gracia. Son dos pactos distintos y se ven en el
   contrato como «tolerancia de 3 días» contra «intereses a partir del 4°»; el motor no elige por el producto.
6. **Se redondea una sola vez, al final**, a centavos con `ROUND_HALF_UP` (el de `libracore.periodos.redondear`): redondear la tasa
   diaria antes de multiplicar acumularía el error en cada día.
7. **Pagar antes o el mismo día del vencimiento no es un caso especial**: cero días de atraso, punitorio cero.

## 🔴 Los feriados vienen de un archivo con ventana de años

Si el año de una fecha que hay que verificar no está en `libracore/datos/feriados.json`, `libracore.feriados.es_feriado()` levanta
`FueraDeCobertura` en vez de contestar «no es feriado» —ver su docstring—, y **acá se deja subir**. Se podría tragar y seguir sólo con
los fines de semana, pero eso daría un punitorio de más o de menos sin ningún aviso, que es peor que un error visible. Quien cobre
fechas fuera de la ventana decide: regenerar el archivo, o pasar `correr_dia_no_habil=False`.

## Lo que NO hace

- **No decide si hay mora ni la cobra**: calcula el número. Sumarlo a la cuenta corriente, a la cuota (`interes_mora`) o a una nota de
  débito es del producto.
- **No corre el fin de la gracia** si cae en día no hábil: la gracia es un plazo corrido y se cuenta tal cual desde el vencimiento.
- **No capitaliza, no escalona tasas ni topea el interés.** Un contrato con tasa que cambia por tramos se calcula por tramos, llamando
  una vez por cada uno.
- **No convierte monedas** ni sabe qué moneda es el importe.
- **No sabe de pagos parciales**: el importe adeudado es el que quedó impago a la fecha de vencimiento.

## Cómo se usa

    from libracore.mora import calcular_punitorio

    r = calcular_punitorio(
        importe=Decimal("100000"), vencimiento=date(2026, 8, 10), fecha_pago=date(2026, 8, 14),
        tasa=Decimal("0.5"), base="diaria", dias_gracia=3,
    )
    # El 10/08/2026 es lunes y hábil: r["vencimiento_efectivo"] == date(2026, 8, 10)
    # 4 días de atraso, pasa los 3 de gracia: r["dias_computados"] == 4 y r["punitorio"] == Decimal("2000.00")
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from libracore.feriados import es_feriado
from libracore.periodos import a_decimal, redondear

#: Las formas de pactar la tasa: por día, o por mes (que se toma como `tasa / DIAS_DEL_MES_PACTADO` por día).
BASES = ("diaria", "mensual")

#: El mes de la tasa mensual, en días. Fijo y no el real del mes (ver el docstring del módulo, decisión 2).
DIAS_DEL_MES_PACTADO = 30

#: Tope de días que se busca hacia adelante un día hábil. El fin de semana largo más largo del calendario argentino ronda los 5 días;
#: 15 es holgura, y evita un bucle sin fin si alguien pasa un `dias_no_habiles` absurdo.
_MAXIMO_CORRIMIENTO = 15


def es_dia_habil(dia: date, dias_no_habiles: Iterable[date] = ()) -> bool:
    """Lunes a viernes, que no sea feriado nacional ni uno de los `dias_no_habiles` que suma el producto.

    Levanta `libracore.feriados.FueraDeCobertura` si el año no está en el catálogo empaquetado (ver el docstring del módulo).
    """
    if dia.weekday() >= 5:
        return False
    if dia in set(dias_no_habiles):
        return False
    return es_feriado(dia) is None


def proximo_dia_habil(dia: date, dias_no_habiles: Iterable[date] = ()) -> date:
    """`dia` si ya es hábil; si no, el primer día hábil posterior."""
    extra = frozenset(dias_no_habiles)
    for _ in range(_MAXIMO_CORRIMIENTO + 1):
        if es_dia_habil(dia, extra):
            return dia
        dia += timedelta(days=1)
    raise ValueError(f"No hay un día hábil dentro de los {_MAXIMO_CORRIMIENTO} días siguientes: revisar `dias_no_habiles`.")


def calcular_punitorio(
    importe: Any,
    vencimiento: date,
    fecha_pago: date,
    tasa: Any,
    *,
    base: str = "diaria",
    dias_gracia: int = 0,
    correr_dia_no_habil: bool = True,
    desde_fin_de_gracia: bool = False,
    dias_no_habiles: Iterable[date] = (),
) -> dict:
    """El punitorio de pagar `importe` el `fecha_pago` cuando vencía el `vencimiento`.

    `tasa` es un **porcentaje** (`0.5` = 0,5 %) por día si `base="diaria"` o por mes si `base="mensual"`. `importe` puede venir como
    `Decimal`, `int`, `str` o `float`. Levanta `ValueError` con importe o tasa negativos, gracia negativa o una `base` desconocida.

    Devuelve, para imprimir en el recibo:

    - `importe`, `vencimiento`, `fecha_pago`, `dias_gracia`: lo que entró, normalizado.
    - `vencimiento_efectivo`: el vencimiento ya corrido al siguiente hábil (o el mismo, si ya era hábil o no se pidió el corrimiento), y
      `corrido`: si hubo corrimiento.
    - `fin_de_gracia`: `vencimiento_efectivo + dias_gracia`; el último día en que se paga sin punitorio.
    - `dias_de_atraso`: días corridos de `fecha_pago` sobre `vencimiento_efectivo` (0 si no se pasó). Es el atraso real, tenga o no
      punitorio.
    - `dias_computados`: los días sobre los que corre el interés. 0 dentro de la gracia; si no, `dias_de_atraso` (o los que pasan del fin
      de la gracia, con `desde_fin_de_gracia`).
    - `interes_desde`: el día desde el que corre el interés, o `None` si no hay punitorio.
    - `base`, `tasa` (el porcentaje que entró) y `tasa_diaria` (el porcentaje por día que se aplicó; para la mensual, `tasa / 30`).
    - `punitorio`: `importe × tasa_diaria / 100 × dias_computados`, a centavos con `ROUND_HALF_UP`.
    - `total`: `importe + punitorio`.
    """
    if base not in BASES:
        raise ValueError(f"Base de tasa desconocida: {base!r}. Las válidas son {list(BASES)}.")
    importe = a_decimal(importe)
    tasa = a_decimal(tasa)
    if importe < 0:
        raise ValueError(f"El importe adeudado no puede ser negativo: {importe}.")
    if tasa < 0:
        raise ValueError(f"La tasa no puede ser negativa: {tasa}.")
    if dias_gracia < 0:
        raise ValueError(f"Los días de gracia no pueden ser negativos: {dias_gracia}.")

    efectivo = proximo_dia_habil(vencimiento, dias_no_habiles) if correr_dia_no_habil else vencimiento
    fin_de_gracia = efectivo + timedelta(days=dias_gracia)
    atraso = max(0, (fecha_pago - efectivo).days)

    if fecha_pago <= fin_de_gracia:
        computados, interes_desde = 0, None
    else:
        interes_desde = fin_de_gracia if desde_fin_de_gracia else efectivo
        computados = (fecha_pago - interes_desde).days

    # Una sola división, al final, sobre el producto entero: `importe × tasa × días / (100 × 30)`. Dividir primero `tasa / 30` y
    # multiplicar después arrastraría ese redondeo de 28 dígitos a cada centavo.
    divisor = Decimal(100) * (DIAS_DEL_MES_PACTADO if base == "mensual" else 1)
    punitorio = redondear(importe * tasa * computados / divisor)

    return {
        "importe": importe,
        "vencimiento": vencimiento,
        "fecha_pago": fecha_pago,
        "dias_gracia": dias_gracia,
        "vencimiento_efectivo": efectivo,
        "corrido": efectivo != vencimiento,
        "fin_de_gracia": fin_de_gracia,
        "dias_de_atraso": atraso,
        "dias_computados": computados,
        "interes_desde": interes_desde,
        "base": base,
        "tasa": tasa,
        "tasa_diaria": tasa / DIAS_DEL_MES_PACTADO if base == "mensual" else tasa,
        "punitorio": punitorio,
        "total": importe + punitorio,
    }
