"""Emitir un comprobante dentro de la transacción del producto (`conn=`, M2 del diseño de LibraCargo).

Un producto que cierra su operación junto con el comprobante (LibraCargo: las
órdenes de carga y el movimiento de su cuenta corriente, ADR-024 de ese
producto) necesita **todo o nada**: número, comprobante, CAE y lo suyo, en una
sola transacción. Lo que se fija acá, contra los dos motores:

- Con `conn`, nada se confirma solo: si el producto revierte, no queda ni el
  comprobante ni su CAE; si confirma, queda todo.
- La numeración ve lo que la transacción ya escribió.
- 🔴 **El reintento ante un número repetido no mata la transacción.** En
  PostgreSQL un error la aborta entera; sin el `SAVEPOINT`, el segundo intento de
  `create_factura` y todo lo que el producto escribiera después morirían con
  «current transaction is aborted». Es el caso que este archivo existe para
  cubrir, y por eso corre contra PostgreSQL además de SQLite.

`ENV=development`: el motor numera local y simula el CAE.
"""
import asyncio
import os

import pytest

from libracore import arca_facturacion
from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db.schema import init_core_schema

FECHA = "2026-10-05"


@pytest.fixture(params=["sqlite", "postgres"])
def base(request, tmp_path, monkeypatch):
    if request.param == "postgres":
        url = os.environ.get("LIBRACORE_POSTGRES_URL")
        if not url:
            pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
        import psycopg

        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True) as c:
            c.execute("DROP SCHEMA IF EXISTS public CASCADE")
            c.execute("CREATE SCHEMA public")
        core.configure(db_path=url)
    else:
        core.configure(db_path=str(tmp_path / "transaccion.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    # La tabla propia del producto: lo que cierra junto con el comprobante.
    conn.execute("CREATE TABLE ordenes_prueba (id INTEGER PRIMARY KEY, factura_id INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("ENV", "development")
    yield request.param
    core._db_path = None
    core._database_url = None


def _emitir(conn, numero=None):
    """El camino de un producto: número, comprobante y CAE, todo con `conn`."""
    n, ta, arca = asyncio.run(arca_facturacion.get_next_numero_with_arca(1, 11, conn=conn))
    factura_id = db_facturas.create_factura(
        11, 1, numero or n, FECHA, "30555555556", "Cerealera SA", 1, [], 100, 0, 100,
        ambiente="produccion", conn=conn)
    factura = db_facturas.get_factura(factura_id, conn=conn)
    asyncio.run(arca_facturacion.solicitar_cae(factura_id, factura, ta, arca, conn=conn))
    return factura_id


def _contar(tabla):
    with core.get_connection() as c:
        return c.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


def test_si_el_producto_revierte_no_queda_nada(base):
    conn = core.get_connection()
    factura_id = _emitir(conn)
    assert db_facturas.get_factura(factura_id, conn=conn)["cae"]  # adentro, con su CAE
    conn.execute("INSERT INTO ordenes_prueba (id, factura_id) VALUES (1, ?)", (factura_id,))
    conn.rollback()
    conn.close()
    assert (_contar("facturas"), _contar("ordenes_prueba")) == (0, 0)


def test_si_el_producto_confirma_queda_todo(base):
    conn = core.get_connection()
    factura_id = _emitir(conn)
    conn.execute("INSERT INTO ordenes_prueba (id, factura_id) VALUES (1, ?)", (factura_id,))
    conn.commit()
    conn.close()
    factura = db_facturas.get_factura(factura_id)
    assert factura["cae"] and factura["numero"] == 1
    assert _contar("ordenes_prueba") == 1


def test_nada_se_confirma_antes_que_el_producto(base):
    """Desde otra conexión, mientras la transacción sigue abierta, no se ve el comprobante."""
    conn = core.get_connection()
    _emitir(conn)
    assert _contar("facturas") == 0
    conn.rollback()
    conn.close()


def test_la_numeracion_ve_lo_que_la_transaccion_ya_escribio(base):
    conn = core.get_connection()
    ids = [_emitir(conn), _emitir(conn)]
    assert [db_facturas.get_factura(i, conn=conn)["numero"] for i in ids] == [1, 2]
    conn.rollback()
    conn.close()


def test_un_numero_repetido_se_reintenta_sin_matar_la_transaccion(base):
    """🔴 El caso de PostgreSQL: el `INSERT` choca, el reintento sigue y el producto escribe después."""
    with core.get_connection() as c:
        db_facturas.create_factura(11, 1, 1, FECHA, "", "x", 1, [], 1, 0, 1, ambiente="produccion", conn=c)
    conn = core.get_connection()
    factura_id = _emitir(conn, numero=1)  # el 1 ya está: tiene que reintentar
    conn.execute("INSERT INTO ordenes_prueba (id, factura_id) VALUES (1, ?)", (factura_id,))
    conn.commit()
    conn.close()
    assert db_facturas.get_factura(factura_id)["numero"] == 2
    assert _contar("ordenes_prueba") == 1


def test_un_registro_repetido_tampoco_mata_la_transaccion(base, monkeypatch):
    """La carrera del registro manual: lo frena el índice único dentro de la transacción y se sigue."""
    with core.get_connection() as c:
        db_facturas.registrar_comprobante(1, 3, 10, FECHA, "", "x", 1, [], 1, 0, 1, conn=c)
    conn = core.get_connection()
    respuestas = iter([False, True])  # antes del INSERT «no está»; después, sí
    monkeypatch.setattr(db_facturas, "_ya_existe", lambda *a, **k: next(respuestas))
    with pytest.raises(db_facturas.NumeroYaRegistrado):
        db_facturas.registrar_comprobante(1, 3, 10, FECHA, "", "x", 1, [], 1, 0, 1, conn=conn)
    conn.execute("INSERT INTO ordenes_prueba (id, factura_id) VALUES (1, NULL)")
    conn.commit()
    conn.close()
    assert (_contar("facturas"), _contar("ordenes_prueba")) == (1, 1)


def test_la_anulacion_tambien_se_revierte_con_el_producto(base):
    with core.get_connection() as c:
        factura_id = db_facturas.create_factura(
            11, 1, 1, FECHA, "", "x", 1, [], 1, 0, 1, ambiente="produccion", conn=c)
    conn = core.get_connection()
    assert db_facturas.anular_factura(factura_id, motivo="prueba", conn=conn)["anulada_en"]
    conn.rollback()
    conn.close()
    assert db_facturas.get_factura(factura_id)["anulada_en"] is None


def test_sin_conn_cada_funcion_confirma_la_suya_como_siempre(base):
    factura_id = db_facturas.create_factura(11, 1, 1, FECHA, "", "x", 1, [], 1, 0, 1, ambiente="produccion")
    db_facturas.update_factura_cae(factura_id, "123", "2026-10-15")
    assert db_facturas.get_factura(factura_id)["cae"] == "123"
