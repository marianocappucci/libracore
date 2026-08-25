"""Aplicar las migraciones de LibraCore desde el paquete instalado.

Hasta acá las migraciones **no viajaban en el wheel**: vivían en `migrations/`
en la raíz del repo, fuera de `packages = ["libracore"]`. La única forma de
aplicarlas era `scripts/run_migrations.sh`, que **clona el repo** en el tag
pineado, arma un venv y corre alembic.

🔴 **Eso las dejaba fuera del alcance de un contenedor**, y el resultado está
medido: de las 14 bases con schema de LibraCore, **7 no tienen `alembic_version`
ninguna** y 2 quedaron en `0001_baseline`. Sólo las 5 de dev llegaron a `0002`.
Las cuatro columnas que esa revisión agrega a `clients` faltan en las 9 — y no
rompen sólo porque `libracore.db.clients` introspecta la tabla en cada alta y
escribe únicamente las columnas presentes.

Ahora `migrations/` vive **adentro del paquete** y esto es lo que las corre:

    libracore-migrar upgrade --prefijo gestiolibra
    python -m libracore.migrar upgrade --prefijo gestiolibra
    from libracore.migrar import upgrade; upgrade(destino)

Es el espejo de `libragenda.migrar`, con **una diferencia que importa**: acá el
destino no sale de `DATABASE_URL` a secas. Ver `url_de_core`.

🔑 **`script_location` se resuelve desde `__file__`, no desde el cwd.** Es la
diferencia entre andar en el repo y andar en `/usr/local/lib/python3.12/
site-packages`: un `alembic.ini` con ruta relativa sólo funciona parado en la
raíz del repo, que es justo donde el contenedor no está.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: El directorio de migraciones **dentro del paquete**. Es lo que hace que esto
#: funcione desde una instalación de pip y no sólo desde el repo.
DIRECTORIO = Path(__file__).parent / "migrations"


class SinURL(RuntimeError):
    """No hay contra qué migrar: ni destino explícito ni variables del entorno."""


class SinAlembic(RuntimeError):
    """Falta el extra `[migrations]`, que es quien trae alembic."""


def _comandos():
    """`alembic.command`, importado tarde y con un error que dice qué falta.

    🔴 **Alembic no es dependencia de LibraCore**, es el extra `[migrations]`:
    los tres productos sin cadena propia —Contalibra, Restolibra, VentaLibra—
    hoy **no lo tienen en su contenedor** (medido). Sin este envoltorio, el
    console script muere con un `ModuleNotFoundError: No module named 'alembic'`
    en medio de un deploy, que manda a buscar el problema en el lugar
    equivocado.
    """
    try:
        from alembic import command
        from alembic.config import Config
    except ModuleNotFoundError as e:  # pragma: no cover - depende del entorno
        raise SinAlembic(
            "Falta alembic: LibraCore no lo declara como dependencia, viene en "
            "el extra `[migrations]`. Instalá `libracore[migrations]` en la "
            "imagen del producto antes de declarar este comando en el deploy."
        ) from e
    return command, Config


def normalizar_url(destino: str) -> str:
    """Deja el destino tal como lo espera `migrations/env.py`.

    **No traduce a URL de SQLAlchemy**: el `env.py` de este repo acepta a
    propósito las dos formas en que LibraCore nombra una base —URL PostgreSQL o
    **ruta de archivo** SQLite— y es él quien decide el bind. Traducir acá
    rompería el caso SQLite, que sigue vivo en LibraEdge y en instancias
    viejas.

    Lo único que se corrige es el prefijo pelado, por el mismo motivo que en
    LibraGenda: SQLAlchemy resuelve `postgresql://` a **psycopg2** y este
    paquete instala `psycopg` 3.
    """
    if destino.startswith("postgresql://"):
        return "postgresql+psycopg://" + destino[len("postgresql://"):]
    return destino


def url_de_core(prefijo: str | None = None, entorno=None) -> str:
    """La base **de LibraCore** de esta instancia, que no siempre es la del dominio.

    🔴 **Esta función es la mitad del valor de este módulo.** El comando corre
    dentro del contenedor del producto, donde `DATABASE_URL` apunta a la base
    **del dominio**. En los tres productos que llevan el schema del core en una
    base aparte —Gestiolibra, MedLibra y LibraClub— un `upgrade` que tomara
    `DATABASE_URL` migraría la base equivocada **sin fallar**: crearía las
    tablas del core al lado de las del dominio y dejaría la base real sin tocar.

    El orden es:

    1. `LIBRACORE_MIGRAR_URL`, que es la salida de emergencia explícita.
    2. Con `prefijo`: `<PREFIJO>_LIBRACORE_DATABASE_URL` y sus nombres
       históricos, vía `url_de_instancia(..., core=True)`.
    3. Con `prefijo` y sin lo anterior: la del **dominio**, porque en los
       productos de una sola base el schema del core vive ahí. Es la misma
       regla que `ProductConfig.db_urls`, que deriva `base_core` del prefijo y
       la hace igual a la del dominio cuando `base_core_separada` es falso.
    4. Sin `prefijo`: `DATABASE_URL`, que es el caso de un script en el host.

    El paso 3 es el que hay que mirar con cuidado: **cae al dominio sólo cuando
    la variable del core no existe**, que es exactamente la señal de que el
    producto no separa las bases. Si existiera y estuviera vacía,
    `url_de_instancia` la trata como no puesta — ver su docstring.
    """
    env = os.environ if entorno is None else entorno

    explicita = (env.get("LIBRACORE_MIGRAR_URL") or "").strip()
    if explicita:
        return explicita

    if prefijo:
        from .db.url_de_instancia import url_de_instancia

        del_core = url_de_instancia(prefijo, core=True, entorno=env)
        if del_core:
            return del_core
        del_dominio = url_de_instancia(prefijo, core=False, entorno=env)
        if del_dominio:
            return del_dominio
        raise SinURL(
            f"No hay base de LibraCore para el prefijo '{prefijo}': ni "
            f"{prefijo.upper()}_LIBRACORE_DATABASE_URL ni "
            f"{prefijo.upper()}_DATABASE_URL (ni sus nombres históricos) están "
            "definidas en este entorno."
        )

    del_entorno = (env.get("DATABASE_URL") or "").strip()
    if del_entorno:
        return del_entorno

    raise SinURL(
        "Falta el destino: pasá --prefijo <producto> para que salga de las "
        "variables de la instancia, o definí LIBRACORE_MIGRAR_URL o "
        "DATABASE_URL. Sin eso no hay base contra la cual migrar."
    )


def configuracion(destino: str):
    """El `Config` de Alembic apuntado al paquete instalado.

    No lee `alembic.ini`: ese archivo es del repo y no viaja en el wheel. Se
    arma en memoria con las dos opciones que importan.
    """
    _, Config = _comandos()
    cfg = Config()
    cfg.set_main_option("script_location", str(DIRECTORIO))
    normalizado = normalizar_url(destino)
    # 🔴 **La que manda de verdad.** `env.py` prefiere `DATABASE_URL` del
    # entorno por sobre `sqlalchemy.url`, así que sin esta opción propia el
    # destino explícito **se ignoraría en silencio**: adentro de un contenedor
    # de Gestiolibra, `upgrade(url_del_core)` migraría la del dominio y
    # devolvería éxito. Es el mismo defecto que LibraGenda encontró con un test
    # que migra una base real, no con uno unitario.
    cfg.set_main_option("libracore.url", normalizado)
    cfg.set_main_option("sqlalchemy.url", normalizado)
    return cfg


def upgrade(destino: str, revision: str = "head") -> None:
    """Aplica las migraciones. Es lo que corre el deploy de un consumidor.

    Sobre correrlo contra una instancia viva: la baseline llama a
    `init_core_schema()`, que es **idempotente**, así que aplicarla sobre una
    base que ya tiene el schema hace lo mismo que un arranque de la app y además
    registra la versión. Aun así, **backup antes**: es una operación de schema.
    """
    command, _ = _comandos()
    command.upgrade(configuracion(destino), revision)


def stamp(destino: str, revision: str = "head") -> None:
    """Marca la base en una revisión **sin ejecutar** las migraciones.

    🔴 Casi nunca es lo que hace falta acá, y por eso está documentado en
    negativo: como la baseline es idempotente, una base que ya tiene el schema
    se pone al día con `upgrade`, que además **agrega lo que falte**. Estampar
    declara «esta base está en esta revisión» sin mirar si es cierto: si no lo
    está, la próxima migración corre sobre un esquema que no es el que espera.
    """
    command, _ = _comandos()
    command.stamp(configuracion(destino), revision)


def current(destino: str) -> None:
    command, _ = _comandos()
    command.current(configuracion(destino))


def heads(destino: str) -> None:
    command, _ = _comandos()
    command.heads(configuracion(destino))


def _parsear(argv: list[str]) -> tuple[str, str | None, str | None]:
    """`(accion, prefijo, revision)` — sin argparse, para no cambiar la forma.

    `--prefijo X` o `--prefijo=X`; lo que quede suelto es la revisión.
    """
    accion = argv[0] if argv else "upgrade"
    prefijo = None
    revision = None
    resto = argv[1:]
    i = 0
    while i < len(resto):
        arg = resto[i]
        if arg == "--prefijo":
            i += 1
            prefijo = resto[i] if i < len(resto) else None
        elif arg.startswith("--prefijo="):
            prefijo = arg.split("=", 1)[1]
        else:
            revision = arg
        i += 1
    return accion, prefijo, revision


def main(argv: list[str] | None = None) -> int:
    """CLI: `libracore-migrar [upgrade|stamp|current|heads] [--prefijo P] [rev]`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    accion, prefijo, revision = _parsear(argv)
    acciones = {"upgrade": upgrade, "stamp": stamp, "current": current, "heads": heads}

    if accion in ("-h", "--help") or accion not in acciones:
        print(main.__doc__)
        print(f"  migraciones en: {DIRECTORIO}")
        print("  --prefijo resuelve la base de LibraCore de esa instancia, que "
              "NO siempre es DATABASE_URL: ver libracore.migrar.url_de_core")
        # 🔑 Código 0 si la pidieron, 2 si el comando no existe: un typo no
        # puede leerse como éxito desde un pipeline.
        return 0 if accion in ("-h", "--help") else 2

    try:
        destino = url_de_core(prefijo)
        if accion in ("upgrade", "stamp") and revision:
            acciones[accion](destino, revision)
        else:
            acciones[accion](destino)
    except (SinURL, SinAlembic) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
