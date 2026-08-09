"""La cadena de Alembic de LibraCore, EJECUTADA contra los dos motores.

Lo que estos tests cuidan no es que Alembic corra: es que **la baseline y
`init_core_schema()` no se separen nunca**. La `0001` llama a la función en vez
de re-expresarla justamente para que no haya dos fuentes de verdad, y estos
tests son la prueba de que sigue siendo cierto — contra la misma fixture que
congela el schema (`test_schema_congelado.py`).

Los cuatro caminos que importan, y los cuatro se ejercitan acá:

| | SQLite | PostgreSQL |
|---|---|---|
| base **vacía** | el schema resultante == fixture | ídem |
| base **que ya existe** | el upgrade no cambia nada y queda estampada | ídem |

El segundo es el que decide la operación real: las instancias vivas **se
migran, no se estampan a ciegas**. Como `init_core_schema()` es idempotente, el
`upgrade` hace lo mismo que un arranque de la app y además registra la versión.
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


def test_upgrade_sobre_base_vacia_sqlite(tmp_path):
    destino = str(tmp_path / "nueva.db")
    resultado = _alembic(destino, "upgrade", "head")
    assert resultado.returncode == 0, resultado.stderr

    esperado = (FIXTURES / "schema_sqlite.txt").read_text(encoding="utf-8")
    assert _sin_alembic(_volcar(destino)) == _sin_alembic(esperado)

    actual = _alembic(destino, "current")
    assert "0001_baseline" in actual.stdout


def test_upgrade_sobre_base_que_ya_existe_sqlite(tmp_path):
    """El caso de la operación real: una instancia viva se migra, no se estampa."""
    destino = str(tmp_path / "viva.db")
    _crear_base_ya_existente(destino)
    antes = _volcar(destino)

    resultado = _alembic(destino, "upgrade", "head")
    assert resultado.returncode == 0, resultado.stderr

    assert _sin_alembic(_volcar(destino)) == _sin_alembic(antes)
    assert "0001_baseline" in _alembic(destino, "current").stdout


# ----------------------------------------------------------------- PostgreSQL


def test_upgrade_sobre_base_vacia_postgres():
    url = _url_postgres()
    _limpiar_postgres(url)

    resultado = _alembic(url, "upgrade", "head")
    assert resultado.returncode == 0, resultado.stderr

    esperado = (FIXTURES / "schema_postgres.txt").read_text(encoding="utf-8")
    assert _sin_alembic(_volcar(url)) == _sin_alembic(esperado)
    assert "0001_baseline" in _alembic(url, "current").stdout


def test_upgrade_sobre_base_que_ya_existe_postgres():
    url = _url_postgres()
    _limpiar_postgres(url)
    _crear_base_ya_existente(url)
    antes = _volcar(url)

    resultado = _alembic(url, "upgrade", "head")
    assert resultado.returncode == 0, resultado.stderr

    assert _sin_alembic(_volcar(url)) == _sin_alembic(antes)


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
