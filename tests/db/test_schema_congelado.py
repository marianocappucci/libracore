"""El gate del schema congelado: `init_core_schema()` no cambia sin que se note.

Por qué existe, que es lo que menos se ve:

`init_core_schema()` **ya es** un mecanismo de migraciones hecho a mano — 34
`ALTER ... ADD COLUMN` idempotentes que corren en cada arranque y que son cómo
las instancias viejas reciben las columnas nuevas. La decisión (2026-08-09) fue
**congelarlo**: la revisión `0001` de Alembic va a llamarlo entero y desde ahí
la función pasa a ser de sólo lectura; todo cambio posterior va como revisión.

Un congelamiento que dependa de que alguien se acuerde no es un congelamiento.
Y congelar el **texto** por hash tampoco sirve: se pone rojo si alguien toca un
comentario y verde si alguien cambia un `DEFAULT` en un ALTER de abajo. Lo que
se congela acá es el **resultado**: el schema que la función produce contra una
base vacía, volcado por `libracore.db.schema_dump` y comparado contra una
fixture por motor.

**Las dos fixtures se regeneran juntas.** No son redundantes: los `CHECK` sólo
se ven en la de PostgreSQL (SQLite no los expone por introspección), y los
tipos y defaults se escriben distinto en cada motor. La cobertura es del par.

Regenerar, después de un cambio deliberado:

    python -m libracore.db.schema_dump /tmp/gate.db --init \\
        > tests/db/fixtures/schema_sqlite.txt
    python -m libracore.db.schema_dump "$LIBRACORE_POSTGRES_URL" --init \\
        > tests/db/fixtures/schema_postgres.txt

Un diff en la fixture es la pregunta "¿esto no debería ser una revisión de
Alembic?" puesta donde no se puede ignorar.
"""
import difflib
import os
from pathlib import Path

import pytest

from libracore.db import core
from libracore.db.schema import init_core_schema
from libracore.db.schema_dump import volcar_schema

FIXTURES = Path(__file__).parent / "fixtures"

_COMO_REGENERAR = (
    "Si el cambio es deliberado, regenerá LAS DOS fixtures (ver el docstring "
    "de este archivo) y preguntate si no tendría que ser una revisión de "
    "Alembic en vez de un cambio a init_core_schema()."
)


def _liberar():
    core._db_path = None
    core._database_url = None


def _comparar_con_fixture(actual: str, nombre: str):
    esperado = (FIXTURES / nombre).read_text(encoding="utf-8")
    if actual == esperado:
        return
    diff = list(
        difflib.unified_diff(
            esperado.splitlines(),
            actual.splitlines(),
            fromfile=f"fixtures/{nombre} (congelado)",
            tofile="init_core_schema() (ahora)",
            lineterm="",
        )
    )
    recorte = "\n".join(diff[:40])
    if len(diff) > 40:
        recorte += f"\n... y {len(diff) - 40} líneas más de diferencia"
    pytest.fail(f"El schema cambió respecto de la fixture.\n\n{recorte}\n\n{_COMO_REGENERAR}")


def _url_postgres() -> str:
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        # Un skip acá sería un falso verde del CI: la mitad de la cobertura
        # (los CHECK, los tipos reales, los largos de varchar) vive sólo en
        # este motor. Si la variable falta, es un problema del workflow.
        pytest.fail(
            "LIBRACORE_POSTGRES_URL no está definida en CI — el gate de "
            "PostgreSQL no se saltea acá"
        )
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


def test_schema_sqlite_congelado(tmp_path):
    core.configure(str(tmp_path / "gate.db"))
    conn = core.get_connection()
    try:
        init_core_schema(conn)
        conn.commit()
        _comparar_con_fixture(volcar_schema(conn), "schema_sqlite.txt")
    finally:
        conn.close()
        _liberar()


def test_schema_postgres_congelado():
    url = _url_postgres()
    core.configure(url)
    conn = core.get_connection()
    try:
        # Slate limpia: este servicio de PostgreSQL lo comparten todos los
        # tests del archivo, y el volcado mira el schema `public` entero.
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        init_core_schema(conn)
        conn.commit()
        _comparar_con_fixture(volcar_schema(conn), "schema_postgres.txt")
    finally:
        conn.close()
        _liberar()


def test_el_volcado_ve_una_columna_agregada(tmp_path):
    """Contraprueba: un gate que no puede ponerse rojo no es un gate.

    Se le agrega una columna a la base **después** de volcar, y el volcado
    tiene que cambiar exactamente en esa línea. Sin esto, un volcado que
    devolviera siempre lo mismo pasaría los dos tests de arriba para siempre.
    """
    core.configure(str(tmp_path / "contraprueba.db"))
    conn = core.get_connection()
    try:
        init_core_schema(conn)
        conn.commit()
        antes = volcar_schema(conn)

        conn.execute("ALTER TABLE clients ADD COLUMN prueba_del_gate TEXT DEFAULT 'x'")
        conn.commit()
        despues = volcar_schema(conn)

        agregadas = set(despues.splitlines()) - set(antes.splitlines())
        # La línea de la columna, y el conteo de la cabecera que se mueve con
        # ella: la cifra base se comparó contra las diez instancias vivas, así
        # que también tiene que ser sensible.
        #
        # 370 desde el 2026-08-30, cuando entró `cajas.punto_venta` (era 369
        # desde el 2026-08-28 con `caja_movimientos.anulado`, que venía de 368
        # con `cajas.sucursal_id`, que venía de 367).
        # Que este número haya que moverlo a mano **es la señal**: si cambia sin
        # que nadie lo decida, el gate se pone rojo y obliga a mirarlo.
        assert agregadas == {"clients|prueba_del_gate|TEXT||'x'", "## tablas (371)"}
    finally:
        conn.close()
        _liberar()


def test_el_volcado_ve_un_indice_borrado(tmp_path):
    """La otra mitad de la contraprueba: los índices también están cubiertos.

    Un volcado que sólo mirara columnas dejaría pasar la pérdida de un UNIQUE
    —que es una regla de negocio, no un detalle— sin decir nada.
    """
    core.configure(str(tmp_path / "contraprueba_idx.db"))
    conn = core.get_connection()
    try:
        init_core_schema(conn)
        conn.commit()
        antes = volcar_schema(conn)

        conn.execute("DROP INDEX idx_facturas_numero_unico")
        conn.commit()
        despues = volcar_schema(conn)

        perdidas = set(antes.splitlines()) - set(despues.splitlines())
        assert any("idx_facturas_numero_unico" in linea for linea in perdidas)
    finally:
        conn.close()
        _liberar()
