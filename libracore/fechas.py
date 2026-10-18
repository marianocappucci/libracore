"""Fechas del motor: el reloj único de Argentina (ADR-070) y los filtros por **día** de columnas de fecha TEXT libre (ADR-037).

Las columnas `fecha` del motor (`facturas`, `egresos`, `ventas`, `caja_movimientos`,
`movimientos_stock`, `movimientos_tesoreria`, `cc_asientos`, `recibos`, `cierres_diarios`
y `date` en `remitos` y `presupuestos`) son `TEXT`. Los escritores internos guardan
`AAAA-MM-DD`, pero los routers aceptan `fecha: str` sin validar, así que una fila puede
traer hora (`2026-10-09 13:00:00` o `2026-10-09T13:00`).

🔴 `fecha <= '2026-10-09'` **deja afuera** esa fila: como texto, `'2026-10-09 13:00:00'` es
mayor que `'2026-10-09'`. El último día del rango desaparece del listado, del reporte o
del libro IVA sin ningún error. Hasta el 2026-10-09 sólo `tesoreria` lo había parcheado
(`hasta + " 23:59:59"`), y el parche falla con la `T` (`'T'` > `' '`).

El criterio es el de **día completo**, que sólo usa comparaciones de texto (mismo SQL en
SQLite y en PostgreSQL) y no depende del formato de la hora:

- `desde` → `columna >= 'AAAA-MM-DD'`: todo lo del día `desde`, con o sin hora.
- `hasta` → `columna < '<día siguiente>'`: todo lo del día `hasta`, con o sin hora.

Para una fecha sin hora el resultado es el de siempre: el rango `[d, h]` incluye los dos días.

## El reloj único: «hoy» y «ahora» de Argentina (ADR-070)

`hoy()` y `ahora()` son **la** fuente de la fecha de negocio del motor: la que fecha un cobro, un
turno, un cierre, una factura, una nota, un recibo, un egreso o un movimiento de stock. Antes había
dos relojes: `datetime.date.today()` (el del **proceso**, que depende de la `TZ` del contenedor) y
`_ar_now()` (UTC-3). Con el contenedor en UTC, entre las 21:00 y la medianoche de Argentina daban
días distintos: un cobro fechado mañana dentro de un turno de hoy.

Es un **desfase fijo de -3 h**, no `zoneinfo`, a propósito: Argentina no tiene horario de verano
desde 2009, y `zoneinfo` depende de que la imagen traiga la base de zonas del sistema (o el paquete
`tzdata`, que el motor no declara): sin ella `ZoneInfo("America/Argentina/Buenos_Aires")` levanta
`ZoneInfoNotFoundError`. Es la razón que ya documenta libracommerce, y el mismo criterio de `_ar_now()` (que ahora
delega acá), de `errores`, de `resguardo_enlace`, del `datetime('now','-3 hours')` de las
columnas `created_at` y de libracommerce (`_ZONA_LOCAL`).

Un test (o un llamador) que necesite fijar el instante pisa `_ahora_utc`, la única función que lee
el reloj del sistema; `hoy()`, `ahora()` y `_ar_now()` salen todos de ella.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone

#: Argentina: UTC-3 fijo, sin horario de verano (ver el docstring del módulo).
ZONA_AR = timezone(timedelta(hours=-3))

_DIA = re.compile(r"\d{4}-\d{2}-\d{2}")


def _ahora_utc() -> datetime:
    """El instante actual, con zona UTC. Lo único de este módulo que mira el reloj del sistema."""
    return datetime.now(UTC)


def ahora() -> datetime:
    """El instante actual en Argentina, con zona (`tzinfo` UTC-3)."""
    return _ahora_utc().astimezone(ZONA_AR)


def hoy() -> date:
    """El día de hoy en Argentina: la fecha de negocio, la misma para todo el motor."""
    return ahora().date()


def dia_iso(valor: str | None) -> date | None:
    """El día de `valor` si empieza con una fecha ISO válida (`AAAA-MM-DD`), o `None`.

    Después del día sólo se admite nada, un espacio o una `T` (`2026-10-09 13:00:00`,
    `2026-10-09T13:00`). `20261009`, `2026-10-09x` o `2026-13-40` no son una fecha.
    """
    if not isinstance(valor, str):
        return None
    texto = valor.strip()
    if not _DIA.fullmatch(texto[:10]) or texto[10:11] not in ("", " ", "T"):
        return None
    try:
        return date.fromisoformat(texto[:10])
    except ValueError:
        return None


def rango_por_dia(columna: str, desde: str | None, hasta: str | None) -> tuple[list[str], list]:
    """Las condiciones SQL (con `?`) y sus parámetros para filtrar `columna` entre dos días.

    Devuelve `(condiciones, params)`, para sumar a las del llamador (`conds.extend(c)`,
    `params.extend(p)`). Función pura: no toca la base. `columna` es un nombre fijo del
    código llamador (`"m.fecha"`), nunca un dato del usuario: se interpola en el SQL.

    - `desde` válido (con o sin hora) → `columna >= 'AAAA-MM-DD'`.
    - `hasta` válido (con o sin hora) → `columna < '<día siguiente>'`.
    - Un extremo que **no** es una fecha ISO se usa tal cual, con `>=` / `<=`, como antes de
      esta función: un llamador raro no se rompe, sólo conserva el comportamiento viejo.
    - `None` o vacío → sin condición.
    """
    conds: list[str] = []
    params: list = []
    if desde:
        dia = dia_iso(desde)
        conds.append(f"{columna} >= ?")
        params.append(dia.isoformat() if dia else desde)
    if hasta:
        dia = dia_iso(hasta)
        if dia is not None and dia < date.max:
            conds.append(f"{columna} < ?")
            params.append((dia + timedelta(days=1)).isoformat())
        else:
            # No es una fecha (o es el último día representable, sin «siguiente»).
            conds.append(f"{columna} <= ?")
            params.append(dia.isoformat() if dia else hasta)
    return conds, params
