"""La cadena de Alembic de LibraCore, EJECUTADA contra los dos motores.

Lo que estos tests cuidan es que **las dos rutas al schema converjan**: una
instalación nueva (base vacía → `upgrade head`) y una instancia viva (schema
puesto por `init_core_schema()` en el arranque → `upgrade head`) tienen que
terminar en el MISMO schema. Si se separan, la suite corre contra una forma de
la base y producción contra otra.

| | SQLite | PostgreSQL |
|---|---|---|
| base **vacía** → head | convergen entre sí, y quedan en head | ídem |
| base **que ya existe** → head | ídem, y **recibe** lo que agregaron las revisiones | ídem |

> ⚠️ **Hasta el 2026-08-12 el invariante era otro**: "la baseline y
> `init_core_schema()` no se separan nunca", verificado comparando el resultado
> del `upgrade` contra la fixture del schema congelado. Eso valía sólo mientras
> `0001` era la única revisión — que es justamente el estado que Alembic existe
> para dejar atrás. La **primera revisión real** (`0002`, las cuatro columnas de
> `clients`) lo puso en rojo por diseño, no por regresión: una base migrada
> tiene que tener MÁS que `init_core_schema()`, si no la revisión no hizo nada.
>
> La fixture sigue congelando `init_core_schema()` — ese es el trabajo de
> `test_schema_congelado.py` y no cambió. Lo que se dejó de exigir acá es que el
> resultado de la cadena entera sea igual a esa función.

El segundo caso es el que decide la operación real: las instancias vivas **se
migran, no se estampan a ciegas**. Como `init_core_schema()` es idempotente, el
`upgrade` reaplica lo que ya está, agrega lo que falta y registra la versión.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from libracore.db import core
from libracore.db.schema import init_core_schema
from libracore.db.schema_dump import volcar_schema

RAIZ = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"


def _alembic(destino: str, *args: str):
    """Corre alembic como PROCESO, no por API.

    Es como lo invoca `scripts/run_migrations.sh` en el pipeline de deploy: si
    el `env.py` o el `alembic.ini` estuvieran mal, llamar a la API desde el
    test lo taparía.
    """
    entorno = {**os.environ, "DATABASE_URL": destino}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=RAIZ,
        env=entorno,
        capture_output=True,
        text=True,
    )


def _sin_alembic(volcado: str) -> list[str]:
    """El volcado sin la tabla de versiones ni las cabeceras con el conteo.

    `alembic_version` es de la herramienta, no del schema del core: la fixture
    se genera sin ella, y los conteos de las cabeceras se mueven con ella.
    """
    return [
        linea
        for linea in volcado.splitlines()
        if "alembic_version" not in linea and not linea.startswith("## ")
    ]


def _revision_head() -> str:
    """El id de la revisión de la punta, leído de la cadena.

    Hardcodearlo obliga a editar estos tests en cada revisión nueva, que es
    justo el mantenimiento que hace que un test se termine ajustando hasta que
    pase en vez de leerse.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini"))).get_current_head()


def _liberar():
    core._db_path = None
    core._database_url = None


def _volcar(destino: str) -> str:
    core.configure(destino)
    conn = core.get_connection()
    try:
        return volcar_schema(conn)
    finally:
        conn.close()
        _liberar()


def _url_postgres() -> str:
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("LIBRACORE_POSTGRES_URL no está definida en CI")
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


def _limpiar_postgres(url: str):
    core.configure(url)
    conn = core.get_connection()
    try:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()
        _liberar()


def _crear_base_ya_existente(destino: str):
    """Una base como la de una instancia viva: con el schema puesto por el
    arranque de la app, y sin `alembic_version`."""
    core.configure(destino)
    conn = core.get_connection()
    try:
        init_core_schema(conn)
        conn.commit()
    finally:
        conn.close()
        _liberar()


# --------------------------------------------------------------------- SQLite


def test_las_dos_rutas_al_schema_convergen_sqlite(tmp_path):
    """Instalación nueva y instancia migrada terminan en el mismo schema.

    Es el invariante que reemplaza a la comparación contra la fixture: no
    depende de cuántas revisiones haya en la cadena, así que no hay que
    regenerar nada al agregar la próxima.
    """
    nueva = str(tmp_path / "nueva.db")
    assert _alembic(nueva, "upgrade", "head").returncode == 0

    viva = str(tmp_path / "viva.db")
    _crear_base_ya_existente(viva)
    assert _alembic(viva, "upgrade", "head").returncode == 0

    assert _sin_alembic(_volcar(nueva)) == _sin_alembic(_volcar(viva))
    assert _revision_head() in _alembic(nueva, "current").stdout
    assert _revision_head() in _alembic(viva, "current").stdout


def test_una_instancia_viva_recibe_lo_que_agregan_las_revisiones(tmp_path):
    """El caso de la operación real, con control negativo.

    El test de convergencia de arriba pasaría igual si la cadena entera no
    hiciera nada: dos bases idénticas también convergen. Esto fija que el
    `upgrade` sobre una instancia viva **cambia algo**, y qué.
    """
    destino = str(tmp_path / "viva.db")
    _crear_base_ya_existente(destino)
    antes = _sin_alembic(_volcar(destino))

    assert _alembic(destino, "upgrade", "head").returncode == 0
    despues = _sin_alembic(_volcar(destino))

    assert despues != antes, (
        "el upgrade no cambió el schema: o la cadena quedó sin revisiones "
        "después de la baseline, o ninguna aplicó"
    )
    agregadas = set(despues) - set(antes)
    # Las cuatro de `0002`, que son las que hacen posible que LibraDesk deje
    # su tabla `clientes` propia y adopte este módulo.
    assert {linea.split("|")[1] for linea in agregadas if linea.startswith("clients|")} == {
        "empresa", "ciudad", "observaciones", "tipo_facturacion",
    }, agregadas


# ----------------------------------------------------------------- PostgreSQL


def test_las_dos_rutas_al_schema_convergen_postgres():
    """El mismo invariante que en SQLite, contra el motor de producción.

    No es redundante: los `CHECK` sólo se ven por introspección acá, y los
    tipos y defaults se escriben distinto en cada motor — una revisión puede
    converger en SQLite y separarse en PostgreSQL.
    """
    url = _url_postgres()

    _limpiar_postgres(url)
    assert _alembic(url, "upgrade", "head").returncode == 0
    nueva = _sin_alembic(_volcar(url))

    _limpiar_postgres(url)
    _crear_base_ya_existente(url)
    assert _alembic(url, "upgrade", "head").returncode == 0
    viva = _sin_alembic(_volcar(url))

    assert nueva == viva
    assert _revision_head() in _alembic(url, "current").stdout


def test_una_instancia_viva_recibe_lo_que_agregan_las_revisiones_postgres():
    url = _url_postgres()
    _limpiar_postgres(url)
    _crear_base_ya_existente(url)
    antes = _sin_alembic(_volcar(url))

    assert _alembic(url, "upgrade", "head").returncode == 0
    despues = _sin_alembic(_volcar(url))

    assert despues != antes
    agregadas = set(despues) - set(antes)
    assert {linea.split("|")[1] for linea in agregadas if linea.startswith("clients|")} == {
        "empresa", "ciudad", "observaciones", "tipo_facturacion",
    }, agregadas


# ------------------------------------------------------------------ La cadena


def test_los_ids_de_revision_entran_en_la_columna_de_alembic():
    """`alembic_version.version_num` es `VARCHAR(32)`.

    PostgreSQL rechaza un id más largo; SQLite lo acepta igual. O sea que una
    revisión con nombre largo pasaría toda la suite local y **fallaría recién
    contra el motor de producción**. Se chequea acá y no allá.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini")))
    largos = {rev.revision: len(rev.revision) for rev in script.walk_revisions()}
    assert largos, "la cadena no tiene ninguna revisión"
    assert all(largo <= 32 for largo in largos.values()), largos


def test_el_modo_offline_falla_en_vez_de_mentir(tmp_path):
    """`--sql` no puede generar esta baseline: ejecuta Python que inspecciona
    la base. Que falle es la respuesta correcta; emitir algo sería peor."""
    resultado = _alembic(str(tmp_path / "offline.db"), "upgrade", "head", "--sql")
    assert resultado.returncode != 0
    assert "offline" in (resultado.stderr + resultado.stdout).lower()
