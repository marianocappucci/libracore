"""La capa de datos del recibo: numeración, snapshot y anulación.

Lo que se fija acá es lo que separa un recibo de un PDF armado al vuelo: que
el número sea único y correlativo aunque dos cobros caigan juntos, que los
pagos guardados no cambien nunca más, y que anular no borre.
"""
import json
import sqlite3

import pytest

from libracore.db import core, recibos
from libracore.db.schema import init_core_schema


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "recibos.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def _crear(conn, **kwargs):
    base = dict(
        fecha="2026-08-04",
        cliente_razon="Municipalidad de Suipacha",
        origen_tipo=recibos.ORIGEN_CC_PAGO,
        total=10500.0,
        pagos=[{"fecha": "2026-08-04", "medio_pago": "transferencia",
                "referencia": "", "monto": 10500.0}],
    )
    base.update(kwargs)
    return recibos.create_recibo(**base)


# ── Numeración ───────────────────────────────────────────────────────────────

def test_la_numeracion_arranca_en_uno_y_es_correlativa(conn):
    assert recibos.get_next_recibo_numero() == 1
    primero = recibos.get_recibo(_crear(conn, origen_id=1))
    segundo = recibos.get_recibo(_crear(conn, origen_id=2))
    assert (primero["numero"], segundo["numero"]) == (1, 2)


def test_cada_punto_de_venta_lleva_su_propio_correlativo(conn):
    uno = recibos.get_recibo(_crear(conn, origen_id=1, punto_venta=1))
    cinco = recibos.get_recibo(_crear(conn, origen_id=2, punto_venta=5))
    assert uno["numero"] == 1
    assert cinco["numero"] == 1
    assert (uno["punto_venta"], cinco["punto_venta"]) == (1, 5)


def test_un_numero_ya_usado_no_rompe_el_alta_sino_que_se_recalcula(conn):
    """El caller pasa un número que quedó viejo — el caso de dos cobros
    simultáneos. Tiene que resolverse solo, no explotar."""
    _crear(conn, origen_id=1)                       # se lleva el 1
    recibo_id = _crear(conn, origen_id=2, numero=1)  # pide el 1 de nuevo
    assert recibos.get_recibo(recibo_id)["numero"] == 2


def test_no_se_puede_repetir_numero_dentro_del_mismo_punto_de_venta(conn):
    _crear(conn, origen_id=1)
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO recibos (punto_venta, numero, fecha, cliente_razon,"
            " origen_tipo, total, pagos) VALUES (1, 1, '2026-08-04', 'X', 'venta', 1, '[]')"
        )
        conn.commit()


def test_un_origen_desconocido_se_rechaza(conn):
    with pytest.raises(ValueError, match="origen_tipo"):
        _crear(conn, origen_tipo="cheque_diferido", origen_id=1)


def test_un_cliente_que_no_esta_en_el_padron_falla_rapido(conn):
    """La FK tiene que cortar en el primer intento. El retry de numeración
    atrapa `IntegrityError`, y si no distinguiera cuál, este caso daría cinco
    vueltas para terminar reportando un problema de numeración que no existe."""
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _crear(conn, origen_id=1, cliente_id=9999)


# ── Snapshot ─────────────────────────────────────────────────────────────────

def test_los_pagos_vuelven_como_lista_y_no_como_texto(conn):
    recibo = recibos.get_recibo(_crear(conn, origen_id=1))
    assert recibo["pagos"] == [{"fecha": "2026-08-04", "medio_pago": "transferencia",
                                "referencia": "", "monto": 10500.0}]


def test_el_snapshot_no_se_mueve_aunque_cambie_el_origen(conn):
    """El punto entero de guardar los pagos en vez de calcularlos."""
    recibo_id = _crear(conn, origen_id=7, origen_tipo=recibos.ORIGEN_FACTURA)
    antes = recibos.get_recibo(recibo_id)
    # Simula un cobro posterior sobre la misma factura: en el modelo viejo esto
    # cambiaba el PDF del recibo ya entregado.
    conn.execute(
        "INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, factura_id, medio_pago)"
        " VALUES ('2026-08-05', 'ingreso', 'Cobro', 5000, 7, 'efectivo')"
    )
    conn.commit()
    assert recibos.get_recibo(recibo_id)["pagos"] == antes["pagos"]
    assert recibos.get_recibo(recibo_id)["total"] == antes["total"]


def test_el_acento_del_cliente_sobrevive_al_json(conn):
    recibo = recibos.get_recibo(_crear(conn, origen_id=1, cliente_razon="Almacén Ñandú",
                                       pagos=[{"referencia": "depósito", "monto": 1.0}]))
    assert recibo["cliente_razon"] == "Almacén Ñandú"
    assert recibo["pagos"][0]["referencia"] == "depósito"


# ── Búsqueda por origen ──────────────────────────────────────────────────────

def test_los_recibos_de_un_origen_salen_del_mas_viejo_al_mas_nuevo(conn):
    a = _crear(conn, origen_tipo=recibos.ORIGEN_FACTURA, origen_id=3)
    b = _crear(conn, origen_tipo=recibos.ORIGEN_FACTURA, origen_id=3)
    encontrados = recibos.get_recibos_de_origen(recibos.ORIGEN_FACTURA, 3)
    assert [r["id"] for r in encontrados] == [a, b]


def test_un_origen_no_ve_los_recibos_de_otro_tipo_con_el_mismo_id(conn):
    _crear(conn, origen_tipo=recibos.ORIGEN_VENTA, origen_id=3)
    assert recibos.get_recibos_de_origen(recibos.ORIGEN_FACTURA, 3) == []


def test_un_recibo_anulado_deja_de_cubrir_su_origen(conn):
    recibo_id = _crear(conn, origen_tipo=recibos.ORIGEN_FACTURA, origen_id=3)
    recibos.anular_recibo(recibo_id, motivo="cargado dos veces")
    assert recibos.get_recibos_de_origen(recibos.ORIGEN_FACTURA, 3) == []
    incluidos = recibos.get_recibos_de_origen(recibos.ORIGEN_FACTURA, 3,
                                              incluir_anulados=True)
    assert [r["id"] for r in incluidos] == [recibo_id]


# ── Anulación ────────────────────────────────────────────────────────────────

def test_anular_marca_y_no_borra(conn):
    recibo_id = _crear(conn, origen_id=1)
    assert recibos.anular_recibo(recibo_id, motivo="error de carga") is True
    recibo = recibos.get_recibo(recibo_id)
    assert recibo["anulado"] is True
    assert recibo["anulado_motivo"] == "error de carga"
    assert recibo["anulado_at"]


def test_anular_dos_veces_avisa_que_ya_estaba(conn):
    recibo_id = _crear(conn, origen_id=1)
    recibos.anular_recibo(recibo_id)
    assert recibos.anular_recibo(recibo_id, motivo="otro motivo") is False
    assert recibos.get_recibo(recibo_id)["anulado_motivo"] == ""


def test_anular_no_libera_el_numero(conn):
    """El papel ya salió impreso con ese número. Un correlativo con huecos es
    una pregunta sin respuesta; reusarlo es peor."""
    primero = _crear(conn, origen_id=1)
    recibos.anular_recibo(primero)
    segundo = recibos.get_recibo(_crear(conn, origen_id=2))
    assert segundo["numero"] == 2


# ── Listado ──────────────────────────────────────────────────────────────────

def test_el_listado_sale_mas_nuevos_primero(conn):
    a = _crear(conn, origen_id=1)
    b = _crear(conn, origen_id=2)
    assert [r["id"] for r in recibos.get_recibos()] == [b, a]


def test_el_listado_filtra_por_fecha_texto_y_anulados(conn):
    _crear(conn, origen_id=1, fecha="2026-07-01", cliente_razon="Panaderia Sol")
    b = _crear(conn, origen_id=2, fecha="2026-08-04", cliente_razon="Ferreteria Luna")
    recibos.anular_recibo(_crear(conn, origen_id=3, fecha="2026-08-04"))

    assert [r["id"] for r in recibos.get_recibos(desde="2026-08-01", q="Luna")] == [b]
    assert recibos.contar_recibos(desde="2026-08-01") == 2
    assert recibos.contar_recibos(desde="2026-08-01", incluir_anulados=False) == 1


def test_el_listado_pagina(conn):
    ids = [_crear(conn, origen_id=i) for i in range(1, 6)]
    pagina = recibos.get_recibos(limit=2, offset=2)
    assert [r["id"] for r in pagina] == [ids[2], ids[1]]


def test_el_numero_buscado_como_texto_encuentra_el_recibo(conn):
    _crear(conn, origen_id=1)
    encontrados = recibos.get_recibos(q="1")
    assert len(encontrados) == 1


def test_los_pagos_se_guardan_como_json_valido_en_la_columna(conn):
    """Contra el `str(lista)` de Python, que parece JSON y no lo es."""
    recibo_id = _crear(conn, origen_id=1)
    raw = conn.execute("SELECT pagos FROM recibos WHERE id=?", (recibo_id,)).fetchone()[0]
    assert isinstance(json.loads(raw), list)
