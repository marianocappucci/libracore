"""Los índices de ajuste de la familia y la cuenta del ajuste (ADR-085).

## Qué problema resuelve

Un alquiler, un abono o un contrato de leasing se actualiza por un índice: el ICL, el IPC, la UVA, el dólar. Hasta
hoy nadie en la familia sabía cuánto vale un índice: LibraDesk declara `metodo_actualizacion='indice'` y lo trata como
`manual` «hasta que se defina de dónde sale el índice» (`app/services/contratos.py`). LibraInmo, que administra
alquileres, no puede existir sin esto. El valor se guarda en `indices_ajuste` (`libracore.db.indices_ajuste`) y se
baja de la fuente oficial con `libracore.indices_descarga`.

## Las decisiones

1. **El catálogo es cerrado** (`INDICES`). Un índice nuevo es una línea acá, con su fuente medida, no un texto libre
   que cada producto escribe a su manera.
2. **La cuenta es una división de niveles**: el importe nuevo es `importe × nivel(ajuste) / nivel(base)`. Para el
   IPC hay dos series que dicen lo mismo de dos formas, y las dos dan esa misma cuenta: `ipc` (el nivel del INDEC)
   divide niveles, e `ipc_variacion` (la variación mensual que republica el BCRA) compone los meses **posteriores**
   al de base hasta el de ajuste inclusive, que es la misma razón de niveles. Existen las dos porque el nivel llega
   con un mes más de demora (medido el 2026-10-26: datos.gob.ar tenía hasta agosto y el BCRA ya tenía septiembre),
   y la variación viene con un solo decimal. Cuál usa un contrato es una cláusula del contrato, no del motor.
3. **Qué fecha o qué mes se toma es del producto.** El motor ajusta entre las dos fechas que le pasan. Si un
   contrato dice «el IPC del segundo mes anterior», la cuenta de qué mes es ése la hace quien conoce el contrato.
4. **Si falta un valor, no hay ajuste** (`IndiceFaltante`). No se usa el anterior ni se interpola: un aumento
   calculado con el índice equivocado es una diferencia que se le cobra a alguien. **Una excepción, decidida por el
   humano el 2026-10-26:** un índice que se publica sólo en días hábiles (el dólar A 3500) toma, en un fin de semana o
   feriado, el valor del último día hábil anterior. No es tapar un dato que falta: ese día no hay dato que publicar.
   El valor tomado dice su fecha y la `fecha_pedida`.
5. **Exacto de punta a punta**: `Decimal`, y el importe se redondea a centavos con `ROUND_HALF_UP` una sola vez, al
   final, como `libradesk/app/services/cuotas.py`. El coeficiente se devuelve sin redondear: quien lo imprime elige
   cuántos decimales muestra, y la cuenta no cambia por eso.

Lo que **no** hace: no decide cuándo toca ajustar (la periodicidad del ajuste es del contrato), no congela el
importe ajustado (lo guarda quien lo usa, igual que la cuota de LibraDesk congela su importe) y no baja nada.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from libracore import feriados
from libracore.db import indices_ajuste as db

DIARIA = "diaria"
MENSUAL = "mensual"

FUENTE_BCRA = "bcra"
FUENTE_DATOS_GOB_AR = "datos_gob_ar"
FUENTE_MANUAL = db.FUENTE_MANUAL

#: Cómo se lee la serie: `nivel` es un índice (se dividen dos valores), `variacion` un porcentaje mensual (se
#: componen los meses).
NIVEL = "nivel"
VARIACION = "variacion"


@dataclass(frozen=True)
class Indice:
    clave: str
    nombre: str
    periodicidad: str
    forma: str
    fuente: str
    #: El id de la serie en la fuente (`idVariable` del BCRA, `ids` de datos.gob.ar). `None` si se carga a mano.
    serie: int | str | None
    #: Se publica sólo en días hábiles (el dólar A 3500): una fecha sin valor toma la del último día hábil anterior,
    #: si todos los días del medio son fin de semana o feriado (decisión del humano, 2026-10-26).
    solo_dias_habiles: bool = False


#: Las series se midieron el 2026-10-26 contra la v4 de la API del BCRA y la de series de datos.gob.ar
#: (`docs/indices-de-ajuste.md`).
INDICES: dict[str, Indice] = {i.clave: i for i in (
    Indice("icl", "Índice para Contratos de Locación (ICL, base 30-06-2020 = 1)", DIARIA, NIVEL, FUENTE_BCRA, 40),
    Indice("ipc", "IPC nivel general nacional (INDEC, base diciembre 2016)", MENSUAL, NIVEL, FUENTE_DATOS_GOB_AR,
           "148.3_INIVELNAL_DICI_M_26"),
    Indice("ipc_variacion", "Inflación mensual (variación % del IPC)", MENSUAL, VARIACION, FUENTE_BCRA, 27),
    Indice("cer", "Coeficiente de Estabilización de Referencia (CER, base 02-02-2002 = 1)", DIARIA, NIVEL,
           FUENTE_BCRA, 30),
    Indice("uva", "Unidad de Valor Adquisitivo (UVA)", DIARIA, NIVEL, FUENTE_BCRA, 31),
    Indice("dolar_a3500", "Tipo de cambio mayorista de referencia (Comunicación A 3500)", DIARIA, NIVEL,
           FUENTE_BCRA, 5, solo_dias_habiles=True),
    Indice("casa_propia", "Coeficiente Casa Propia", MENSUAL, NIVEL, FUENTE_MANUAL, None),
)}

_CENTAVO = Decimal("0.01")
_CIEN = Decimal(100)


class IndiceDesconocido(ValueError):
    """La clave no está en `INDICES`."""


class IndiceFaltante(LookupError):
    """Falta el valor de un índice en una fecha que la cuenta necesita. El mensaje dice cuál."""

    def __init__(self, indice: str, fechas: list[str]):
        self.indice, self.fechas = indice, fechas
        super().__init__(f"Falta el valor de {INDICES[indice].nombre} para {', '.join(fechas)}.")


def indice(clave: str) -> Indice:
    try:
        return INDICES[clave]
    except KeyError:
        raise IndiceDesconocido(f"«{clave}» no es un índice de ajuste conocido.") from None


def _como_fecha(f: date | str) -> date:
    return f if isinstance(f, date) else date.fromisoformat(str(f)[:10])


def fecha_del_indice(clave: str, f: date | str) -> str:
    """La fecha con que se guarda el valor: la misma si es diario, el primer día del mes si es mensual."""
    d = _como_fecha(f)
    if indice(clave).periodicidad == MENSUAL:
        d = d.replace(day=1)
    return d.isoformat()


def _meses_entre(desde: date, hasta: date) -> list[str]:
    """Los meses posteriores al de `desde`, hasta el de `hasta` inclusive, como `AAAA-MM-01`."""
    anio, mes, meses = desde.year, desde.month, []
    while (anio, mes) < (hasta.year, hasta.month):
        mes += 1
        if mes == 13:
            anio, mes = anio + 1, 1
        meses.append(date(anio, mes, 1).isoformat())
    return meses


def redondear(valor: Decimal) -> Decimal:
    """A centavos, medio hacia arriba (no el redondeo del banquero, que da 0,12 sobre 0,125)."""
    return valor.quantize(_CENTAVO, rounding=ROUND_HALF_UP)


def coeficiente_por_niveles(valor_base, valor_ajuste) -> Decimal:
    base, ajuste = Decimal(valor_base), Decimal(valor_ajuste)
    if base <= 0 or ajuste <= 0:
        raise ValueError("Los valores de un índice son mayores que cero.")
    return ajuste / base


def coeficiente_por_variaciones(variaciones) -> Decimal:
    """El producto de `(1 + v/100)` de cada variación mensual, en porcentaje (`Decimal('1.9')` es 1,9 %)."""
    coef = Decimal(1)
    for v in variaciones:
        coef *= 1 + Decimal(v) / _CIEN
    return coef


def aplicar(importe, coeficiente: Decimal) -> dict:
    """`{importe_anterior, coeficiente, importe_nuevo, variacion_pct}`. Puro: no lee nada."""
    anterior = Decimal(importe)
    nuevo = redondear(anterior * coeficiente)
    return {
        "importe_anterior": anterior,
        "coeficiente": coeficiente,
        "importe_nuevo": nuevo,
        "variacion_pct": redondear((coeficiente - 1) * _CIEN),
    }


def aplicar_porcentaje(importe, porcentaje) -> dict:
    """El ajuste por un porcentaje fijo pactado (`metodo_actualizacion='porcentaje'`)."""
    return {**aplicar(importe, 1 + Decimal(porcentaje) / _CIEN), "metodo": "porcentaje",
            "porcentaje": Decimal(porcentaje)}


#: Cuántos días hacia atrás se busca el último día hábil. Diez cubren el fin de semana largo más largo del calendario
#: (Carnaval o Semana Santa pegados a un puente) sin llegar a tomar un valor de otra semana por un dato que falta.
_DIAS_HACIA_ATRAS = 10


def _no_habil(dia: date) -> bool:
    if dia.weekday() >= 5:
        return True
    try:
        return feriados.es_feriado(dia) is not None
    except feriados.FueraDeCobertura:
        # Sin el calendario de ese año, sólo el fin de semana es seguro: un hábil que no se puede probar no se saltea.
        return False


def _valor_en(ind: Indice, f: str, conn) -> dict | None:
    """El valor de `ind` en `f`; para un índice de días hábiles, el del último día hábil anterior si `f` no lo es.

    🔑 **Sólo si todos los días del medio son no hábiles.** Un hábil sin valor es un dato que falta, no un fin de
    semana, y no se tapa con el anterior: eso sí sería ajustar con el índice equivocado (decisión 4 del módulo)."""
    fila = db.obtener(ind.clave, f, conn=conn)
    if fila is not None or not ind.solo_dias_habiles:
        return fila
    pedido = date.fromisoformat(f)
    previo = db.ultimo_hasta(ind.clave, f, conn=conn)
    if previo is None:
        return None
    desde = date.fromisoformat(previo["fecha"])
    if (pedido - desde).days > _DIAS_HACIA_ATRAS:
        return None
    dia = desde + timedelta(days=1)
    while dia <= pedido:
        if not _no_habil(dia):
            return None
        dia += timedelta(days=1)
    return previo


def ajustar(clave: str, importe, fecha_base: date | str, fecha_ajuste: date | str, *, conn=None) -> dict:
    """El importe ajustado por `clave` entre `fecha_base` y `fecha_ajuste`, con los valores que tomó.

    Lee `indices_ajuste`. Si falta cualquiera de los valores que necesita, levanta `IndiceFaltante` con todas las
    fechas que faltan (no sólo la primera), así quien carga a mano sabe qué cargar."""
    ind = indice(clave)
    base, ajuste = _como_fecha(fecha_base), _como_fecha(fecha_ajuste)
    if ajuste < base:
        raise ValueError("La fecha de ajuste no puede ser anterior a la de base.")

    if ind.forma == NIVEL:
        fechas = [fecha_del_indice(clave, base), fecha_del_indice(clave, ajuste)]
        valores = {f: _valor_en(ind, f, conn) for f in fechas}
        faltan = [f for f, v in valores.items() if v is None]
        if faltan:
            raise IndiceFaltante(clave, faltan)
        coef = coeficiente_por_niveles(valores[fechas[0]]["valor"], valores[fechas[1]]["valor"])
        tomados = [{"fecha": valores[f]["fecha"], "valor": valores[f]["valor"], "fuente": valores[f]["fuente"],
                    **({"fecha_pedida": f} if valores[f]["fecha"] != f else {})} for f in fechas]
    else:
        meses = _meses_entre(base, ajuste)
        valores = {m: db.obtener(clave, m, conn=conn) for m in meses}
        faltan = [m for m, v in valores.items() if v is None]
        if faltan:
            raise IndiceFaltante(clave, faltan)
        coef = coeficiente_por_variaciones(valores[m]["valor"] for m in meses)
        tomados = [{"fecha": m, "valor": valores[m]["valor"], "fuente": valores[m]["fuente"]} for m in meses]

    return {**aplicar(importe, coef), "metodo": "indice", "indice": clave, "indice_nombre": ind.nombre,
            "fecha_base": base.isoformat(), "fecha_ajuste": ajuste.isoformat(), "valores": tomados}
