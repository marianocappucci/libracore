"""Filtros de rango de fechas por **día**, sobre columnas de fecha que son TEXT libre (ADR-037).

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
"""
from __future__ import annotations

import re
from datetime import date, timedelta

_DIA = re.compile(r"\d{4}-\d{2}-\d{2}")


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
