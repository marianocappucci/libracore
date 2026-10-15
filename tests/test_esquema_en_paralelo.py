"""`init_core_schema()` con varios procesos a la vez (ADR-053): ni deadlock ni claves duplicadas del catálogo.

🔴 **Por qué existe.** `init_core_schema()` es una transacción larga de DDL idempotente que cada producto corre en su
arranque (y las revisiones de Alembic al migrar). Con `uvicorn --workers 2` la corren dos procesos juntos contra la misma
base, y PostgreSQL contesta de dos maneras según el estado de la base:

- **Base ya armada** (la de un deploy: sólo cambia el código): `deadlock detected`. Los dos corren el
  `UPDATE caja_movimientos SET caja_id=… WHERE caja_id IS NULL` (toma `ROW EXCLUSIVE`, y lo guarda hasta el `commit`) y
  después `CREATE INDEX IF NOT EXISTS idx_caja_movimientos_fecha` (pide `SHARE`, que choca con el `ROW EXCLUSIVE` del otro):
  cada uno espera al otro.
- **Base vacía** (un cliente nuevo): `duplicate key value violates unique constraint "pg_class_relname_nsp_index"` (o
  `pg_type_typname_nsp_index`): los dos hacen `CREATE TABLE IF NOT EXISTS` de la misma tabla al mismo tiempo.

Estos tests levantan **procesos de verdad** (`libracore.testing.esquema_en_paralelo`) que arrancan a la vez, y comprueban
las dos cosas. El de la base armada repite la tanda: el deadlock depende de cómo se entrelacen los dos, así que una sola
vuelta limpia no prueba nada (sin el arreglo fallaba 9 de cada 10).

Se saltea sin `LIBRACORE_POSTGRES_URL`. Tarda unos 20 s.
"""
import subprocess
import sys
import threading
import time

import pytest

from libracore import candados
from libracore.db import core
from libracore.db.schema import CANDADO_DEL_ESQUEMA, init_core_schema

pytestmark = pytest.mark.varios_procesos

#: Cuánto tarda un subproceso en importar el motor antes de poder arrancar. La cita (`t0`) va más allá.
CITA = 3.0


def _crear_esquema_en_paralelo(url: str, procesos: int) -> list[tuple[int, str]]:
    """`procesos` procesos que arrancan `init_core_schema()` a la vez sobre `url`: `[(código de salida, salida)]`."""
    t0 = time.time() + CITA
    hijos = [
        subprocess.Popen([sys.executable, "-m", "libracore.testing.esquema_en_paralelo", url, str(t0)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(procesos)
    ]
    resultados = []
    for hijo in hijos:
        salida, _ = hijo.communicate(timeout=120)
        resultados.append((hijo.returncode, salida.strip()))
    return resultados


def _armar(url: str) -> None:
    """La base como la deja un deploy: con todo el esquema, antes de que arranque ningún worker."""
    conn = core.conectar(url)
    try:
        init_core_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _tablas(url: str) -> int:
    conn = core.conectar(url)
    try:
        return conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = current_schema()").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def url(bases):
    return bases.nueva()


def test_tres_procesos_sobre_una_base_armada_no_dan_deadlock(url):
    _armar(url)
    for vuelta in range(3):
        resultados = _crear_esquema_en_paralelo(url, 3)
        fallaron = [salida for codigo, salida in resultados if codigo != 0]
        assert not fallaron, f"vuelta {vuelta + 1}: {len(fallaron)} de 3 procesos fallaron al crear el esquema:\n" + "\n".join(fallaron)


def test_tres_procesos_sobre_una_base_vacia_crean_el_esquema_una_sola_vez(url):
    resultados = _crear_esquema_en_paralelo(url, 3)
    fallaron = [salida for codigo, salida in resultados if codigo != 0]
    assert not fallaron, f"{len(fallaron)} de 3 procesos fallaron al crear el esquema:\n" + "\n".join(fallaron)
    assert _tablas(url) > 30, "el esquema no quedó completo"


def test_init_core_schema_espera_a_quien_ya_lo_esta_corriendo(url):
    """El mecanismo, sin carreras: con el candado del esquema tomado por otra conexión, `init_core_schema()` no empieza
    (no crea ni una tabla) hasta que esa conexión termina su transacción."""
    otra = core.conectar(url)
    propia = core.conectar(url)
    terminado = threading.Event()
    errores: list[BaseException] = []

    def correr():
        try:
            init_core_schema(propia)
            propia.commit()
        except BaseException as e:  # noqa: BLE001 - se devuelve al hilo principal
            errores.append(e)
        finally:
            terminado.set()

    try:
        candados.esperar_candado_de_transaccion(otra, CANDADO_DEL_ESQUEMA)
        hilo = threading.Thread(target=correr)
        hilo.start()
        assert not terminado.wait(1.5), "init_core_schema() corrió con el candado del esquema tomado por otro"
        assert otra.execute("SELECT to_regclass('clients')").fetchone()[0] is None, "ya había empezado a crear tablas"
        otra.commit()
        assert terminado.wait(30), "no terminó al soltarse el candado"
        hilo.join()
        assert not errores, errores
        assert otra.execute("SELECT to_regclass('clients')").fetchone()[0] == "clients"
    finally:
        otra.close()
        propia.close()
