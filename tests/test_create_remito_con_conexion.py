"""`create_remito(conn=...)` escribe por la conexión que le dan y no commitea (ADR-068).

Hasta v1.178.0 `create_remito` abría siempre su propia conexión y commiteaba. Un producto que
tiene que atar el remito a su origen (reclamo, cuota, venta, presupuesto) en UNA transacción lo
copiaba a mano (el INSERT de LibraDesk, con un test que comparaba las dos filas para enterarse si
el motor sumaba una columna). Ahora el motor ofrece `conn=`, como `create_cc_pago`.

Los tests corren en SQLite y PostgreSQL, y miran la base por una conexión APARTE: lo que ve una
sesión que no es la de la escritura es lo que quedó commiteado de verdad. El último arma el caso
real de un producto con SQLAlchemy sobre la misma base PostgreSQL.
"""
import json

import pytest

from libracore.db import core
from libracore.db import remitos_presupuestos as rp
from libracore.db.schema import init_core_schema

ITEMS = [{"description": "Trabajo ñandú", "qty": 2, "unit_price": 100.5}]


@pytest.fixture(params=["sqlite", "postgres"])
def destino(request, tmp_path):
    destino = str(tmp_path / "remitos.db") if request.param == "sqlite" \
        else request.getfixturevalue("bases").nueva()
    core.configure(db_path=destino)
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    conn.close()
    yield destino
    core._db_path = None
    core._database_url = None


def _remito(numero="0001-00000001", **extra):
    valores = dict(
        number=numero, date="2026-10-18", client_id=None, client_name="Cliente de Prueba",
        client_address="Calle 1", client_cuit="30-66513816-6", client_email="a@b.com",
        client_phone="123", items=ITEMS, subtotal=201.0, tax_rate=0.21, tax_amount=42.21,
        total=243.21, observations="obs", pdf_path="", usuario_id=None,
    )
    valores.update(extra)
    return valores


def _numeros_vistos_desde_afuera():
    """Los remitos que ve una conexión que no es la de la escritura: sólo lo commiteado."""
    with core.get_connection() as conn:
        return [r["number"] for r in conn.execute("SELECT number FROM remitos ORDER BY id")]


def test_sin_conn_abre_su_conexion_y_commitea(destino):
    remito_id = rp.create_remito(**_remito())

    assert remito_id
    assert _numeros_vistos_desde_afuera() == ["0001-00000001"]
    guardado = rp.get_remito(remito_id)
    assert guardado["items"] == ITEMS
    assert guardado["total"] == pytest.approx(243.21)


def test_con_conn_escribe_sin_commitear_y_el_commit_es_de_quien_la_abrio(destino):
    conn = core.get_connection()
    try:
        remito_id = rp.create_remito(**_remito(), conn=conn)
        assert remito_id

        # Por la conexión de la escritura se ve; desde afuera todavía no.
        assert conn.execute("SELECT number FROM remitos WHERE id=?", (remito_id,)).fetchone()
        assert _numeros_vistos_desde_afuera() == []

        conn.commit()
    finally:
        conn.close()

    assert _numeros_vistos_desde_afuera() == ["0001-00000001"]


def test_con_conn_y_rollback_no_queda_nada(destino):
    conn = core.get_connection()
    try:
        rp.create_remito(**_remito(), conn=conn)
        rp.create_remito(**_remito("0001-00000002"), conn=conn)
        conn.rollback()
    finally:
        conn.close()

    assert _numeros_vistos_desde_afuera() == []


def test_con_conn_no_cierra_la_conexion(destino):
    conn = core.get_connection()
    try:
        rp.create_remito(**_remito(), conn=conn)
        # Sigue usable: la conexión es de quien la pasó.
        rp.create_remito(**_remito("0001-00000002"), conn=conn)
        assert conn.execute("SELECT COUNT(*) FROM remitos").fetchone()[0] == 2
        conn.commit()
    finally:
        conn.close()

    assert _numeros_vistos_desde_afuera() == ["0001-00000001", "0001-00000002"]


def test_con_y_sin_conn_escriben_la_misma_fila(destino):
    """Las dos rutas comparten el INSERT: la fila es idéntica salvo lo que genera la base."""
    sin = rp.create_remito(**_remito("0001-00000001"))
    conn = core.get_connection()
    try:
        con = rp.create_remito(**_remito("0001-00000002"), conn=conn)
        conn.commit()
    finally:
        conn.close()

    generado = {"id", "number", "created_at"}
    a = {k: v for k, v in rp.get_remito(sin).items() if k not in generado}
    b = {k: v for k, v in rp.get_remito(con).items() if k not in generado}
    assert a == b
    assert json.dumps(a["items"], sort_keys=True) == json.dumps(b["items"], sort_keys=True)


# ── El caso de un producto con SQLAlchemy sobre la misma base PostgreSQL ───


@pytest.fixture
def sesion_sqlalchemy(bases):
    """Una sesión de SQLAlchemy y `libracore.db.core` sobre la MISMA base PostgreSQL."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    url = bases.nueva()
    core.configure(db_path=url)
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    conn.close()
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        yield lambda: Session(engine)
    finally:
        engine.dispose()
        core._db_path = None
        core._database_url = None


def _presupuesto():
    return rp.create_presupuesto(
        number="PRES-00000001", date="2026-10-18", valid_until="2026-11-18", client_id=None,
        client_name="Cliente de Prueba", client_address="", client_cuit="", client_email="",
        client_phone="", items=ITEMS, subtotal=201.0, tax_rate=0.21, tax_amount=42.21,
        total=243.21,
    )


def _con_vinculo(sesion, presupuesto_id):
    """Emite el remito por la conexión de la sesión y escribe el vínculo por SQLAlchemy."""
    from sqlalchemy import text

    from libracore.db.migraciones import conexion_libracore

    remito_id = rp.create_remito(**_remito(), conn=conexion_libracore(sesion.connection()))
    sesion.execute(text("UPDATE presupuestos SET remito_id = :r WHERE id = :p"),
                   {"r": remito_id, "p": presupuesto_id})
    return remito_id


def test_remito_y_vinculo_de_una_sesion_de_sqlalchemy_se_commitean_juntos(sesion_sqlalchemy):
    presupuesto_id = _presupuesto()

    with sesion_sqlalchemy() as sesion:
        remito_id = _con_vinculo(sesion, presupuesto_id)
        assert _numeros_vistos_desde_afuera() == [], "nada commiteado antes del commit de la sesión"
        sesion.commit()

    assert _numeros_vistos_desde_afuera() == ["0001-00000001"]
    assert rp.get_presupuesto(presupuesto_id)["remito_id"] == remito_id


def test_si_la_sesion_se_cae_despues_del_remito_no_queda_ni_el_remito_ni_el_vinculo(sesion_sqlalchemy):
    presupuesto_id = _presupuesto()

    class Caida(RuntimeError):
        pass

    with pytest.raises(Caida):
        with sesion_sqlalchemy() as sesion:
            _con_vinculo(sesion, presupuesto_id)
            raise Caida("el proceso cae antes del commit")

    assert _numeros_vistos_desde_afuera() == []
    assert rp.get_presupuesto(presupuesto_id)["remito_id"] is None
