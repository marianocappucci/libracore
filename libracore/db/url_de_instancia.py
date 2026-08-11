"""Resuelve la URL de la base de una instancia desde el entorno, con UN nombre
normalizado para los seis productos de la familia.

Antes del 2026-08-11 cada producto nombraba lo mismo distinto — cuatro
convenciones entre seis productos, y dos de ellas mentían:

| Producto | Dominio | LibraCore |
|---|---|---|
| contalibra, restolibra | `<PREFIJO>_DATABASE_URL` | (la misma) |
| libradesk, gestiolibra, medlibra | `DATABASE_URL` | — / `<PREFIJO>_LIBRACORE_DB_PATH` |
| ventalibra | `VENTALIBRA_DB_PATH` | `VENTALIBRA_LIBRACORE_DB_PATH` |

`..._DB_PATH` viene de cuando el valor era una ruta a un archivo SQLite. Desde
la migración guarda una URL de PostgreSQL, así que el nombre dice una cosa y el
contenido es otra — que es peor que un nombre feo: manda a buscar un archivo.

**El nombre normalizado es el que ya usaban Contalibra y Restolibra**, o sea que
la convención no se inventó acá: se eligió la que dos de los seis ya cumplían.

    <PREFIJO>_DATABASE_URL            la base del dominio
    <PREFIJO>_LIBRACORE_DATABASE_URL  la de LibraCore, cuando va separada

Los nombres históricos **se siguen aceptando** para que ninguna instancia viva
se rompa mientras se actualizan los composes. Cuando las 15 estén al día, se
borra `_HISTORICOS` y listo: **está en un solo lugar justamente para que sacarlo
sea una línea y no una cacería por seis repos.**
"""
import os

# Por producto, los nombres viejos que todavía pueden estar en el entorno de una
# instancia. El orden importa: se prueba el normalizado primero y estos después.
#
# ⚠️ `DATABASE_URL` a secas sólo figura para los productos que REALMENTE lo
# usaban. Ponerlo para todos haría que Contalibra —que nunca lo leyó— empiece a
# tomar una variable genérica que en un CI o en un contenedor cualquiera puede
# estar puesta apuntando a otra base.
_HISTORICOS = {
    ("libradesk", False): ("DATABASE_URL",),
    ("gestiolibra", False): ("DATABASE_URL",),
    ("medlibra", False): ("DATABASE_URL",),
    ("ventalibra", False): ("VENTALIBRA_DB_PATH",),
    ("gestiolibra", True): ("GESTIOLIBRA_LIBRACORE_DB_PATH",),
    ("medlibra", True): ("MEDLIBRA_LIBRACORE_DB_PATH",),
    ("ventalibra", True): ("VENTALIBRA_LIBRACORE_DB_PATH",),
}


def nombre_normalizado(prefijo: str, *, core: bool = False) -> str:
    """`GESTIOLIBRA_DATABASE_URL` / `GESTIOLIBRA_LIBRACORE_DATABASE_URL`."""
    p = prefijo.upper()
    return f"{p}_LIBRACORE_DATABASE_URL" if core else f"{p}_DATABASE_URL"


def nombres_aceptados(prefijo: str, *, core: bool = False) -> tuple:
    """El normalizado primero, después los históricos de ese producto."""
    return (nombre_normalizado(prefijo, core=core),) + _HISTORICOS.get(
        (prefijo.lower(), core), ()
    )


def url_de_instancia(prefijo: str, *, core: bool = False, default: str = "",
                     entorno=None) -> str:
    """La URL (o ruta) de la base de esta instancia, o `default` si no hay
    ninguna variable puesta.

    Una variable **vacía cuenta como no puesta**: un `FOO=` en un compose es
    casi siempre un valor que no se llegó a interpolar, y tomarlo como bueno
    manda a la app a conectarse a la cadena vacía —que falla lejos del origen,
    con un error que no nombra la variable—.
    """
    env = os.environ if entorno is None else entorno
    for nombre in nombres_aceptados(prefijo, core=core):
        valor = (env.get(nombre) or "").strip()
        if valor:
            return valor
    return default
