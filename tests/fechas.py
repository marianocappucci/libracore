"""«Hoy» para los tests: se calcula al usarlo, nunca al importar el módulo.

Una constante `HOY = datetime.date.today().isoformat()` a nivel de módulo queda fija en la fecha en que se
importó el archivo, o sea al juntar los tests. Si la suite cruza la medianoche, el código bajo prueba escribe
la fecha nueva y el test compara contra la vieja: pasó el 2026-10-18 en libracore#408 con siete rojos
(«assert '2026-10-18' == '2026-10-17'»). Por eso los tests piden la fecha acá, adentro de cada test o de la
función que arma sus datos, y el reloj es el mismo que usa el código: `libracore.fechas.hoy()`, el «hoy» de
Argentina (ADR-070). Con `date.today()` (el reloj del proceso) estos helpers divergirían del código entre las 21:00
y la medianoche en un contenedor en UTC, que es donde corre el CI.
"""

from __future__ import annotations

from libracore import fechas as reloj


def hoy() -> str:
    return reloj.hoy().isoformat()


def desde_del_mes() -> str:
    """El primer día del mes en curso: los datos de los tests están fechados hoy, así que un rango FIJO (p. ej.
    septiembre) los deja afuera apenas cambia el mes y el test se rompe solo."""
    return reloj.hoy().replace(day=1).isoformat()


def periodo() -> tuple[str, str]:
    """El mes en curso hasta hoy, como lo piden `get_resumen_core` y `get_dashboard_data`."""
    return desde_del_mes(), hoy()


def del_anio() -> tuple[str, str]:
    """El año en curso entero, calculado al usarse (un período fijo como `2026-01-01`/`2026-12-31` deja afuera lo
    que el código fecha hoy en cuanto cambia el año)."""
    anio = reloj.hoy().year
    return f"{anio}-01-01", f"{anio}-12-31"
