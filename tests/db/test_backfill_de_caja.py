"""El relleno de `caja_id` sobre una base donde la columna YA existia.

🔴 **Es el caso que el relleno no cubria, y es el unico que lo necesita.** El
`UPDATE` vivia adentro del `if "caja_id" not in cols`, o sea que corria solo
cuando la columna se acababa de crear --- y una base recien creada no tiene filas
viejas que rellenar. En toda base donde la columna venia de una version anterior,
las filas se quedaban en `NULL` para siempre.

Lo destapo LibraClub el 2026-08-28: su pantalla mostraba *"Turno abierto --- sin
caja asignada"* sobre turnos de una semana antes. Lo reporto el humano.

El test simula esa base **sacandole el relleno a mano** despues de inicializar y
volviendo a inicializar: es la forma de reproducir "la columna ya estaba" sin
depender de un dump de una version vieja.
"""

from __future__ import annotations

import pytest

from libracore.db import core
from libracore.db.schema import init_core_schema


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "backfill.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def _caja_por_defecto(conn) -> int:
    fila = conn.execute("SELECT id FROM cajas WHERE es_default=1 LIMIT 1").fetchone()
    return fila[0]


def test_una_fila_vieja_sin_caja_se_rellena_al_reinicializar(conn):
    """La columna ya existe y hay filas en NULL: el relleno tiene que alcanzarlas."""
    conn.execute(
        "INSERT INTO usuarios (username, nombre, password_hash, role)"
        " VALUES (?,?,?,?)",
        ("ana", "Ana", "x", "admin"),
    )
    conn.execute(
        "INSERT INTO turnos_caja (usuario_id, apertura, monto_inicial, estado, caja_id)"
        " VALUES (1, '2026-08-21 22:16:21', 1000, 'abierto', NULL)"
    )
    conn.execute(
        "INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, caja_id)"
        " VALUES ('2026-08-21', 'ingreso', 'Turno viejo', 14000, NULL)"
    )
    conn.commit()

    # El control: antes de reinicializar, siguen en NULL. Sin esto el test
    # pasaria con un INSERT que ya hubiera puesto la caja.
    assert conn.execute(
        "SELECT COUNT(*) FROM turnos_caja WHERE caja_id IS NULL"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM caja_movimientos WHERE caja_id IS NULL"
    ).fetchone()[0] == 1

    # Segunda inicializacion: la columna YA existe, que es el caso que fallaba.
    init_core_schema(conn)
    conn.commit()

    esperada = _caja_por_defecto(conn)
    assert conn.execute("SELECT caja_id FROM turnos_caja").fetchone()[0] == esperada, (
        "el turno viejo tiene que quedar con la caja por defecto, no en NULL"
    )
    assert conn.execute(
        "SELECT caja_id FROM caja_movimientos"
    ).fetchone()[0] == esperada


def test_el_relleno_no_pisa_una_caja_ya_asignada(conn):
    """El control que impide arreglarlo con un `UPDATE` sin `WHERE`.

    Un relleno que escribe sobre todas las filas le cambiaria la caja a los
    turnos que SI la tienen --- y con eso el arqueo de una sede pasaria a
    contarse en la otra. Es peor que el defecto que viene a arreglar.
    """
    conn.execute(
        "INSERT INTO usuarios (username, nombre, password_hash, role)"
        " VALUES (?,?,?,?)",
        ("ana", "Ana", "x", "admin"),
    )
    otra = conn.execute(
        "INSERT INTO cajas (nombre, descripcion, medios_pago, activo, es_default)"
        " VALUES ('Buffet', '', '[]', 1, 0)"
    ).lastrowid
    conn.execute(
        "INSERT INTO turnos_caja (usuario_id, apertura, monto_inicial, estado, caja_id)"
        " VALUES (1, '2026-08-28 09:00:00', 0, 'abierto', ?)",
        (otra,),
    )
    conn.commit()

    init_core_schema(conn)
    conn.commit()

    assert conn.execute("SELECT caja_id FROM turnos_caja").fetchone()[0] == otra, (
        "el relleno no puede tocar una fila que ya tenia caja"
    )
    assert otra != _caja_por_defecto(conn), (
        "el control del control: si fueran la misma caja, el assert de arriba "
        "se cumpliria por casualidad"
    )
