"""Resuelve la URL de la base de una instancia desde el entorno, con UN nombre
normalizado para los productos de la familia.

Antes del 2026-08-11 cada producto nombraba lo mismo distinto — cuatro
convenciones entre seis productos, y dos de ellas mentían:

| Producto | Dominio | LibraCore |
|---|---|---|
| contalibra, restolibra | `<PREFIJO>_DATABASE_URL` | (la misma) |
| libradesk | `DATABASE_URL` | — (una sola base) |
| gestiolibra, medlibra | `DATABASE_URL` | — (una sola base desde ADR-083) / `<PREFIJO>_LIBRACORE_DB_PATH` histórico |
| ventalibra | `VENTALIBRA_DB_PATH` | `VENTALIBRA_LIBRACORE_DB_PATH` |
| libracargo, libraclub | `DATABASE_URL` | `<PREFIJO>_LIBRACORE_DATABASE_URL` |

🔴 **La última fila faltó hasta el 2026-09-16.** La tabla se escribió para seis
productos y LibraCargo y LibraClub llegaron después con `DATABASE_URL` a secas
para el dominio. No se notó porque cada app lo parcheó en su `app/config.py`
(`url_de_instancia(...) or os.environ.get("DATABASE_URL")`). Lo destapó
`libraauth-migrar --prefijo libracargo --base dominio`, que no hace ese
fallback y dejó el `-dev` sin arrancar.

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
    # 2026-09-16. Solo el DOMINIO: `DATABASE_URL` nunca resuelve el core por nombre. Desde el 2026-10-22 los dos son
    # de una sola base y el core cae al dominio por `_UNA_SOLA_BASE`, no por este histórico.
    ("libracargo", False): ("DATABASE_URL",),
    ("libraclub", False): ("DATABASE_URL",),
    ("ventalibra", False): ("VENTALIBRA_DB_PATH",),
    # 🔴 GestioLibra y MedLibra siguen acá a propósito (ADR-083, vuelto a medir en ADR-084 el 2026-10-25): son de una sola
    # base y sus instancias de dev y demo ya están unidas y sin la variable (no tienen clientes reales), pero sobre
    # `origin/main` de los dos productos `<PREFIJO>_LIBRACORE_DB_PATH` todavía se nombra en `scripts/reset_demo.sh` (lo lee
    # con `printenv`) y en `tests/test_una_sola_base.py` (que afirma que el histórico se respeta), y su `app/config.py` lo
    # resuelve por `url_de_instancia(..., core=True)`. Sacarlo de acá antes de que el producto saque esas líneas pondría
    # rojos sus tests al subir el pin, y una instancia que todavía lo llevara caería al dominio en silencio. Se borran
    # juntos: primero el producto (sin tocar su `config.py`, que sigue andando), después esta línea en un patch.
    ("gestiolibra", True): ("GESTIOLIBRA_LIBRACORE_DB_PATH",),
    ("medlibra", True): ("MEDLIBRA_LIBRACORE_DB_PATH",),
    ("ventalibra", True): ("VENTALIBRA_LIBRACORE_DB_PATH",),
}


#: 🔴 Los productos cuyo schema de LibraCore vive en la MISMA base que el
#: dominio. Es lo que autoriza a `libracore.migrar.url_de_core` a caer a la base
#: del dominio cuando no hay variable del core.
#:
#: **Se nombra, no se deduce** (2026-09-16, decisión del humano). Hasta esa fecha
#: la regla era "si la variable del core no existe, el producto no separa las
#: bases". Eso era cierto para unos y falso para otros: los que llevaban el core
#: APARTE (Gestiolibra, MedLibra, LibraCargo y LibraClub) y ahí la ausencia de la
#: variable no dice "una sola base" sino "falta configuración".
#: Caer al dominio migraría la base equivocada **sin fallar**. Se volvió urgente
#: al sumar `DATABASE_URL` como histórico de LibraCargo y LibraClub: antes su
#: dominio no resolvía y la caída fallaba por casualidad.
#:
#: Un producto que no esté acá **falla**: agregarlo es una decisión explícita, no
#: un default. Medido el 2026-09-16 contra las bases reales de cada instancia.
#:
#: **2026-10-22: se suman LibraCargo y LibraClub** (ADR-078, decisión del humano «una sola base por producto»). LibraCargo
#: es una sola base desde el 2026-10-05 (ADR-029 de LibraCargo) y LibraClub desde que `libracore-unir-bases` unió
#: `libraclub-dev` y `libraclub-demo`.
#:
#: **2026-10-25: se suman GestioLibra y MedLibra** (ADR-083, PR 5 del plan de escritorio). Con `agenda_clients` y
#: `alembic_version_libragenda` (libragenda v0.14.0), su `modulos` renombrada y `libraauth` copiado por
#: `libracore-unir-bases` (ADR-082), las instancias de prueba (dev y demo de cada uno) se unen con esa herramienta,
#: con respaldo y OK por instancia, **antes** de que el producto tome este pin.
#: Desde acá una instancia nueva de estos dos nace con UNA base (`ProductConfig.db_urls`) y, sin la variable del core,
#: `libracore-migrar` y `libraauth-migrar --base core` caen a la del dominio. **Ya no queda ningún producto con el core
#: aparte**; la lista sigue nombrada para que un producto nuevo falle en vez de adivinar.
_UNA_SOLA_BASE = frozenset({
    "contalibra", "restolibra", "ventalibra", "libradesk", "libracargo", "libraclub", "gestiolibra", "medlibra",
})


def comparte_base_con_el_dominio(prefijo: str) -> bool:
    """Si el schema de LibraCore de este producto vive en la base del dominio."""
    return (prefijo or "").strip().lower() in _UNA_SOLA_BASE


#: 🔴 Los productos de `_UNA_SOLA_BASE` cuya app todavía **necesita** la variable del core en su entorno (ADR-084).
#:
#: El alta de una instancia nueva deja de escribir `<PREFIJO>_LIBRACORE_DATABASE_URL` (y el histórico
#: `<PREFIJO>_LIBRACORE_DB_PATH`) para los productos de `_UNA_SOLA_BASE`: apuntaban a la misma base que la del dominio y
#: no decían nada. Se midió sobre `origin/main` de cada producto (2026-10-25) que arrancan sin ellas, **salvo
#: VentaLibra**: `app/main.py` resuelve el core con `url_de_instancia("ventalibra", core=True, default="./data/ventalibra_libracore.db")`
#: y, sin la variable, `billing.configure` recibe una ruta SQLite que rechaza. Mientras VentaLibra no deje de pedirla
#: (el día que lo haga se saca de acá y listo), su alta sigue escribiéndola.
#:
#: Un producto que figure acá **y** en `_UNA_SOLA_BASE` conserva el comportamiento de siempre; uno que no figura en
#: `_UNA_SOLA_BASE` tiene el core aparte y la variable es la que dice cuál es.
_EXIGEN_LA_VARIABLE_DEL_CORE = frozenset({"ventalibra"})


def escribe_la_variable_del_core(prefijo: str) -> bool:
    """Si el alta de este producto tiene que escribir `<PREFIJO>_LIBRACORE_DATABASE_URL` (y su histórico) en el compose.

    - Producto con el core **aparte** (no figura en `_UNA_SOLA_BASE`): sí, la variable dice cuál es esa base.
    - Producto de **una sola base**: no, la ausencia significa «la del dominio» (`libracore.migrar.url_de_core`, las
      apps). Salvo los que todavía la exigen (`_EXIGEN_LA_VARIABLE_DEL_CORE`)."""
    p = (prefijo or "").strip().lower()
    return p not in _UNA_SOLA_BASE or p in _EXIGEN_LA_VARIABLE_DEL_CORE


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
                     requerida: bool = False, entorno=None) -> str:
    """La URL (o ruta) de la base de esta instancia, o `default` si no hay
    ninguna variable puesta.

    Una variable **vacía cuenta como no puesta**: un `FOO=` en un compose es
    casi siempre un valor que no se llegó a interpolar, y tomarlo como bueno
    manda a la app a conectarse a la cadena vacía —que falla lejos del origen,
    con un error que no nombra la variable—.

    `requerida=True` reemplaza a los `os.environ["..."]` que había en las apps:
    sin él, cambiar un acceso por índice —que revienta con `KeyError` y el
    nombre a la vista— por esta función convertiría un arranque en falta de
    configuración en una conexión a la cadena vacía. El mensaje **nombra todas
    las variables aceptadas**, porque durante la transición hay dos y no saber
    cuál se esperaba es la mitad del problema.
    """
    env = os.environ if entorno is None else entorno
    nombres = nombres_aceptados(prefijo, core=core)
    for nombre in nombres:
        valor = (env.get(nombre) or "").strip()
        if valor:
            return valor
    if requerida:
        raise RuntimeError(
            f"Falta la URL de la base de {prefijo}: definí "
            + " o ".join(nombres)
            + ("" if len(nombres) == 1 else " (el primero es el nombre vigente)")
        )
    return default
