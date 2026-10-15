"""`soltar_fk()`: la costura con la que un producto deja una tabla del motor sin una FK (ADR-056).

Existe por un defecto medido el 2026-10-15 en LibraDesk. Declara `cc_pagos`, `cc_debitos`,
`recibos`, `remitos`, `presupuestos` y `egresos` sin FK hacia `clients` y `proveedores`, pero
desde que `libracore-migrar` corre antes de su arranque la baseline (`init_core_schema()`) las
crea primero, con las FK. Una base **nueva** quedaba con seis FK que una migrada no tiene, y dos
de ellas son `ON DELETE CASCADE`: borrar un cliente borraba su cuenta corriente.

La baseline no se toca (está congelada, y los otros productos conservan esas FK a propósito),
así que lo que se fija acá es la herramienta:

1. 🔴 Suelta la FK de la columna hacia la tabla pedida, y con ella el `CASCADE`: después de soltarla,
   borrar el cliente ya no se lleva sus pagos. Es lo que ponía en rojo a LibraDesk.
2. Es idempotente y no toca lo que no es suyo: otra FK de la misma tabla, la misma columna hacia
   otra tabla, una tabla que no existe.
3. En SQLite no hace nada (no sabe quitar una restricción sin reconstruir la tabla).
"""
import os

import pytest

from libracore.db import core
from libracore.db.fk_de_producto import soltar_fk
from libracore.db.schema import init_core_schema

#: Las seis que la baseline crea y LibraDesk declara sin FK: (tabla, columna, padre).
LAS_SEIS = (
    ("cc_debitos", "cliente_id", "clients"),
    ("cc_pagos", "cliente_id", "clients"),
    ("recibos", "cliente_id", "clients"),
    ("remitos", "client_id", "clients"),
    ("presupuestos", "client_id", "clients"),
    ("egresos", "proveedor_id", "proveedores"),
)


def _liberar():
    core._db_path = None
    core._database_url = None


def _url_postgres() -> str:
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("LIBRACORE_POSTGRES_URL no está definida en CI")
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


def _fk(conn, tabla: str) -> list[str]:
    """Las FK de `tabla`, tal como las renderiza PostgreSQL."""
    return [
        f[0]
        for f in conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = to_regclass(?) AND contype = 'f' ORDER BY 1",
            (tabla,),
        ).fetchall()
    ]


@pytest.fixture
def _servidor_pg():
    """Antes que `bases`: ésta se saltea sin la variable, y en CI eso sería un falso verde."""
    return _url_postgres()


@pytest.fixture
def pg_con_baseline(_servidor_pg, bases):
    """Una base PostgreSQL nueva con la baseline del motor, que es lo que deja `libracore-migrar`."""
    core.configure(bases.nueva())
    conn = core.get_connection()
    try:
        init_core_schema(conn)
        conn.commit()
        yield conn
    finally:
        conn.close()
        _liberar()


def test_la_baseline_deja_las_seis_fk(pg_con_baseline):
    """El supuesto del resto: si la baseline dejara de crearlas, `soltar_fk` sería código muerto."""
    for tabla, columna, padre in LAS_SEIS:
        assert any(f"({columna}) REFERENCES {padre}(id)" in d for d in _fk(pg_con_baseline, tabla)), tabla


def test_suelta_las_seis_y_devuelve_los_nombres(pg_con_baseline):
    conn = pg_con_baseline
    for tabla, columna, padre in LAS_SEIS:
        assert soltar_fk(conn, tabla, columna, hacia=padre) == [f"{tabla}_{columna}_fkey"]
        assert not any(f"({columna}) REFERENCES {padre}(" in d for d in _fk(conn, tabla)), tabla


def test_sin_la_fk_borrar_el_cliente_ya_no_se_lleva_su_cuenta_corriente(pg_con_baseline):
    """El daño que se evita: con el `ON DELETE CASCADE` de la baseline, esto borraba el pago."""
    conn = pg_con_baseline

    def _cliente_con_pago(nombre: str) -> int:
        cid = conn.execute("INSERT INTO clients (name) VALUES (?)", (nombre,)).lastrowid
        conn.execute("INSERT INTO cc_pagos (cliente_id, monto, fecha) VALUES (?, 100, '2026-10-15')", (cid,))
        conn.execute("INSERT INTO cc_debitos (cliente_id, monto, fecha) VALUES (?, 50, '2026-10-15')", (cid,))
        conn.commit()
        return cid

    con_fk = _cliente_con_pago("Con FK")
    conn.execute("DELETE FROM clients WHERE id=?", (con_fk,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM cc_pagos").fetchone()[0] == 0, "la baseline cascadea (así nace)"

    soltar_fk(conn, "cc_pagos", "cliente_id", hacia="clients")
    soltar_fk(conn, "cc_debitos", "cliente_id", hacia="clients")
    sin_fk = _cliente_con_pago("Sin FK")
    conn.execute("DELETE FROM clients WHERE id=?", (sin_fk,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM cc_pagos WHERE cliente_id=?", (sin_fk,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM cc_debitos WHERE cliente_id=?", (sin_fk,)).fetchone()[0] == 1


def test_es_idempotente(pg_con_baseline):
    conn = pg_con_baseline
    assert soltar_fk(conn, "cc_pagos", "cliente_id", hacia="clients") != []
    assert soltar_fk(conn, "cc_pagos", "cliente_id", hacia="clients") == []


def test_no_toca_lo_que_no_pidieron(pg_con_baseline):
    conn = pg_con_baseline
    antes = _fk(conn, "cc_pagos")

    # La misma columna hacia otra tabla: no es la FK que se pidió.
    assert soltar_fk(conn, "cc_pagos", "cliente_id", hacia="proveedores") == []
    # Otra columna de la misma tabla hacia el mismo padre: tampoco.
    assert soltar_fk(conn, "cc_pagos", "usuario_id", hacia="clients") == []
    assert _fk(conn, "cc_pagos") == antes

    soltar_fk(conn, "cc_pagos", "cliente_id", hacia="clients")
    resto = _fk(conn, "cc_pagos")
    assert resto == [d for d in antes if "(cliente_id) REFERENCES clients(" not in d]
    assert any("(usuario_id) REFERENCES usuarios(" in d for d in resto), "la FK a usuarios sigue"
    assert any("(caja_id) REFERENCES cajas(" in d for d in resto), "la FK a cajas sigue"


def test_una_tabla_que_no_existe_no_es_un_error(_servidor_pg, bases):
    core.configure(bases.nueva())
    conn = core.get_connection()
    try:
        assert soltar_fk(conn, "cc_pagos", "cliente_id", hacia="clients") == []
    finally:
        conn.close()
        _liberar()


@pytest.mark.parametrize("malo", ["cc_pagos; DROP TABLE clients", "CC_PAGOS", "", "a.b"])
def test_rechaza_un_identificador_que_no_es_simple(malo):
    with pytest.raises(ValueError):
        soltar_fk(None, malo, "cliente_id", hacia="clients")
    with pytest.raises(ValueError):
        soltar_fk(None, "cc_pagos", malo, hacia="clients")
    with pytest.raises(ValueError):
        soltar_fk(None, "cc_pagos", "cliente_id", hacia=malo)


def test_en_sqlite_no_hace_nada(tmp_path):
    """SQLite no sabe quitar una restricción: el llamado no hace nada, y lo dice devolviendo vacío."""
    core.configure(db_path=str(tmp_path / "sqlite.db"))
    conn = core.get_connection()
    try:
        init_core_schema(conn)
        conn.commit()
        assert soltar_fk(conn, "cc_pagos", "cliente_id", hacia="clients") == []
        fks = {(f[2], f[3]) for f in conn.execute("PRAGMA foreign_key_list(cc_pagos)").fetchall()}
        assert ("clients", "cliente_id") in fks
    finally:
        conn.close()
        _liberar()
