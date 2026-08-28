"""La caja como mostrador de una sucursal, y el turno abierto sobre una caja.

Lo estrena LibraClub, que es el único producto de la familia con sedes adentro de
una instancia. Los otros cinco siguen con la caja por default y no ven nada de
esto — que es lo que los dos controles de acá verifican.

🔴 **`cajas.sucursal_id` no lleva FK a propósito.** Las sucursales viven en la
base del PRODUCTO y las cajas en la de LibraCore: no hay integridad referencial
que declarar entre dos bases. Es el mismo caso que `reservas.factura_id`.
"""

from __future__ import annotations

import pytest

from libracore.db import caja as db_caja
from libracore.db import core
from libracore.db import turnos as db_turnos
from libracore.db.schema import init_core_schema


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "caja_sucursal.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def test_una_caja_pertenece_a_una_sucursal(conn):
    cid = db_caja.create_caja_config("Mostrador", "", ["efectivo"], sucursal_id=7)
    assert db_caja.get_caja_config(cid)["sucursal_id"] == 7


def test_el_listado_filtra_por_sucursal(conn):
    """Y **una sede puede tener más de una caja**, que es la decisión del
    humano del 2026-08-28: mostrador y buffet son dos cajones distintos."""
    db_caja.create_caja_config("Mostrador centro", "", [], sucursal_id=1)
    db_caja.create_caja_config("Buffet centro", "", [], sucursal_id=1)
    db_caja.create_caja_config("Mostrador norte", "", [], sucursal_id=2)

    del_centro = db_caja.get_all_cajas(sucursal_id=1)
    assert {c["nombre"] for c in del_centro} == {"Mostrador centro", "Buffet centro"}

    # El control por el otro lado: la de la otra sede no se cuela.
    assert [c["nombre"] for c in db_caja.get_all_cajas(sucursal_id=2)] == ["Mostrador norte"]


def test_una_caja_SIN_sucursal_no_aparece_en_ninguna(conn):
    """🔴 Es la caja por default de los productos que no tienen sedes.

    Si el filtro la trajera, aparecería en **todas** las sucursales de LibraClub
    —una caja que no es de ninguna—, y el arqueo de una sede incluiría plata de
    otra.
    """
    # El schema ya siembra una «Caja Principal» sin sucursal: ES el caso.
    db_caja.create_caja_config("Mostrador", "", [], sucursal_id=1)

    assert [c["nombre"] for c in db_caja.get_all_cajas(sucursal_id=1)] == ["Mostrador"]
    # Y el control: sin filtro sí está, que es como la ven los otros cinco
    # productos.
    todas = {c["nombre"] for c in db_caja.get_all_cajas()}
    assert "Mostrador" in todas
    assert any(c["sucursal_id"] is None for c in db_caja.get_all_cajas()), (
        "la caja sembrada por el schema no tiene sucursal, y es la que no puede "
        "aparecer en el filtro de una sede"
    )


def test_el_turno_se_abre_sobre_una_caja(conn, crear_usuario):
    """`turnos_caja.caja_id` **ya existía** —lo agrega un ALTER defensivo del
    schema— y nadie la escribía: `create_turno` no la recibía, así que todo
    turno nuevo nacía sin caja. Eso es lo que se arregla."""
    uid = crear_usuario("cajero")
    cid = db_caja.create_caja_config("Mostrador", "", ["efectivo"], sucursal_id=1)

    tid = db_turnos.create_turno(uid, 0.0, caja_id=cid)

    assert db_turnos.get_turno(tid)["caja_id"] == cid


def test_sin_caja_el_turno_sigue_abriendo(conn, crear_usuario):
    """El control de los otros cinco productos: no pasan `caja_id` y su turno
    tiene que seguir funcionando igual que antes."""
    uid = crear_usuario("cajero")
    tid = db_turnos.create_turno(uid, 1000.0)

    turno = db_turnos.get_turno(tid)
    assert turno["caja_id"] is None
    assert turno["monto_inicial"] == 1000.0
