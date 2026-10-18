"""«Hoy» para los tests: se calcula al usarlo, nunca al importar el módulo.

Una constante `HOY = datetime.date.today().isoformat()` a nivel de módulo queda fija en la fecha en que se
importó el archivo, o sea al juntar los tests. Si la suite cruza la medianoche, el código bajo prueba escribe
la fecha nueva y el test compara contra la vieja: pasó el 2026-10-18 en libracore#408 con siete rojos
(«assert '2026-10-18' == '2026-10-17'»). Por eso los tests piden la fecha acá, adentro de cada test o de la
función que arma sus datos, y el reloj es el mismo `date.today()` que usa el código.
"""

from __future__ import annotations

import datetime


def hoy() -> str:
    return datetime.date.today().isoformat()


def desde_del_mes() -> str:
    """El primer día del mes en curso: los datos de los tests están fechados hoy, así que un rango FIJO (p. ej.
    septiembre) los deja afuera apenas cambia el mes y el test se rompe solo."""
    return datetime.date.today().replace(day=1).isoformat()


def periodo() -> tuple[str, str]:
    """El mes en curso hasta hoy, como lo piden `get_resumen_core` y `get_dashboard_data`."""
    return desde_del_mes(), hoy()


def del_anio() -> tuple[str, str]:
    """El año en curso entero, calculado al usarse (un período fijo como `2026-01-01`/`2026-12-31` deja afuera lo
    que el código fecha hoy en cuanto cambia el año)."""
    anio = datetime.date.today().year
    return f"{anio}-01-01", f"{anio}-12-31"
