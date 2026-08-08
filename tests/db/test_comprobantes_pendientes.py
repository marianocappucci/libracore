"""La capa de datos de la bandeja: idempotencia y resolución.

Lo que se fija acá es lo que separa una bandeja de una tabla cualquiera: que
reenviar lo mismo no duplique, que un reenvío no pueda revertir lo que una
persona ya resolvió, y que el total lo calcule el motor y no el que manda.
"""
import pytest

from libracore.db import comprobantes_pendientes as cp
from libracore.db import core
from libracore.db.schema import init_core_schema


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "bandeja.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


@pytest.fixture
def factura(conn):
    """Una factura real a la que apuntar.

    `comprobantes_pendientes.factura_id` es una FK a `facturas` **y la base la
    hace cumplir** (`get_connection()` prende `foreign_keys`). Un id inventado
    acá no falla en el assert: falla en el `UPDATE`, que es lo que pasó la
    primera vez que corrió esta suite.
    """
    with core.get_connection() as c:
        cur = c.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, items, "
            "subtotal, iva_amount, total) VALUES (11, 1, 1, '2026-09-05', "
            "'[]', 1000.0, 0.0, 1000.0)"
        )
        c.commit()
        return cur.lastrowid


def _depositar(**kwargs):
    base = dict(
        origen_producto="libradesk",
        origen_instancia="compulibra",
        origen_tipo=cp.ORIGEN_CUOTA_CONTRATO,
        origen_id="42",
        cliente_razon="Ferretería San Martín",
        cliente_cuit="30-71234567-9",
        items=[{"description": "Alquiler impresora — agosto", "qty": 1,
                "unit_price": 45000.0, "iva_rate": 0.21}],
    )
    base.update(kwargs)
    return cp.upsert_comprobante(**base)


# ── Idempotencia ─────────────────────────────────────────────────────────────

def test_el_alta_devuelve_creado_true_la_primera_vez(conn):
    comprobante_id, creado = _depositar()
    assert creado is True
    assert cp.get_comprobante(comprobante_id)["origen_id"] == "42"


def test_reenviar_lo_mismo_no_duplica(conn):
    primero, _ = _depositar()
    segundo, creado = _depositar()

    assert segundo == primero
    assert creado is False
    assert len(cp.list_por_estado(cp.ESTADO_PENDIENTE)) == 1


def test_reenviar_actualiza_los_datos_mientras_siga_pendiente(conn):
    comprobante_id, _ = _depositar()
    _depositar(items=[{"description": "Alquiler impresora — agosto", "qty": 1,
                       "unit_price": 55000.0, "iva_rate": 0.21}])

    comprobante = cp.get_comprobante(comprobante_id)
    assert comprobante["items"][0]["unit_price"] == 55000.0
    assert comprobante["total"] == pytest.approx(66550.0)


def test_dos_origenes_distintos_del_mismo_cliente_son_dos_filas(conn):
    _depositar(origen_id="42")
    _depositar(origen_id="43")
    assert len(cp.list_por_estado(cp.ESTADO_PENDIENTE)) == 2


def test_el_mismo_id_en_dos_instancias_no_choca(conn):
    """Dos instancias del mismo producto numeran desde 1 cada una. Sin
    `origen_instancia` en la clave, la cuota 42 de un cliente taparía la del
    otro — y esta bandeja es por instancia, pero la clave tiene que aguantar
    igual."""
    _depositar(origen_instancia="compulibra")
    _, creado = _depositar(origen_instancia="otro-cliente")
    assert creado is True
    assert len(cp.list_por_estado(cp.ESTADO_PENDIENTE)) == 2


# ── Lo que un reenvío no puede hacer ─────────────────────────────────────────

def test_un_reenvio_no_revive_lo_facturado(conn, factura):
    comprobante_id, _ = _depositar()
    cp.marcar_facturado(comprobante_id, factura_id=factura, usuario="mariano")

    with pytest.raises(cp.ComprobanteYaResuelto):
        _depositar()

    comprobante = cp.get_comprobante(comprobante_id)
    assert comprobante["estado"] == cp.ESTADO_FACTURADO
    assert comprobante["factura_id"] == factura
    assert comprobante["resuelto_por"] == "mariano"


def test_un_reenvio_no_revive_lo_descartado(conn):
    comprobante_id, _ = _depositar()
    cp.descartar(comprobante_id, motivo="ya se cobró por fuera")

    with pytest.raises(cp.ComprobanteYaResuelto):
        _depositar()

    assert cp.get_comprobante(comprobante_id)["estado"] == cp.ESTADO_DESCARTADO


def test_marcar_dos_veces_no_pisa_la_factura_de_la_primera(conn, factura):
    otra = conn.execute(
        "INSERT INTO facturas (tipo, punto_venta, numero, fecha, items, "
        "subtotal, iva_amount, total) VALUES (11, 1, 2, '2026-09-05', '[]', "
        "1.0, 0.0, 1.0)"
    ).lastrowid
    conn.commit()

    comprobante_id, _ = _depositar()
    assert cp.marcar_facturado(comprobante_id, factura_id=factura) is True
    assert cp.marcar_facturado(comprobante_id, factura_id=otra) is False
    assert cp.get_comprobante(comprobante_id)["factura_id"] == factura


def test_descartar_algo_ya_facturado_no_hace_nada(conn, factura):
    comprobante_id, _ = _depositar()
    cp.marcar_facturado(comprobante_id, factura_id=factura)
    assert cp.descartar(comprobante_id, motivo="me arrepentí") is False
    assert cp.get_comprobante(comprobante_id)["estado"] == cp.ESTADO_FACTURADO


def test_no_se_puede_marcar_con_una_factura_que_no_existe(conn):
    """La FK a `facturas` está y la base la hace cumplir. Sin esto, un bug del
    caller dejaría comprobantes "facturados" apuntando a una factura que nunca
    se emitió — y esa es justo la fila con la que después se rastrea qué se le
    cobró a quién."""
    import sqlite3

    comprobante_id, _ = _depositar()
    with pytest.raises(sqlite3.IntegrityError):
        cp.marcar_facturado(comprobante_id, factura_id=99999)
    assert cp.get_comprobante(comprobante_id)["estado"] == cp.ESTADO_PENDIENTE


# ── El total lo calcula el motor ─────────────────────────────────────────────

def test_el_total_sale_de_los_items_con_iva(conn):
    comprobante_id, _ = _depositar(items=[
        {"description": "Alquiler", "qty": 2, "unit_price": 1000.0, "iva_rate": 0.21},
        {"description": "Service", "qty": 1, "unit_price": 500.0, "iva_rate": 0.105},
    ])
    assert cp.get_comprobante(comprobante_id)["total"] == pytest.approx(2972.5)


def test_el_total_no_se_puede_mandar_de_afuera(conn):
    """`upsert_comprobante` no acepta `total`. Si algún día alguien se lo
    agrega, este test lo hace explícito antes de que dos sistemas empiecen a
    tener opinión sobre cuánto sale lo mismo."""
    with pytest.raises(TypeError):
        _depositar(total=1.0)


# ── Validación ───────────────────────────────────────────────────────────────

def test_un_origen_tipo_desconocido_no_entra(conn):
    with pytest.raises(ValueError, match="origen_tipo"):
        _depositar(origen_tipo="lo_que_sea")


def test_sin_origen_id_no_entra(conn):
    with pytest.raises(ValueError, match="origen_id"):
        _depositar(origen_id="")


def test_sin_origen_producto_no_entra(conn):
    with pytest.raises(ValueError, match="origen_producto"):
        _depositar(origen_producto="   ")


# ── Listados ─────────────────────────────────────────────────────────────────

def test_la_bandeja_separa_por_estado_y_cuenta_los_pendientes(conn, factura):
    facturado, _ = _depositar(origen_id="1")
    descartado, _ = _depositar(origen_id="2")
    _depositar(origen_id="3")
    cp.marcar_facturado(facturado, factura_id=factura)
    cp.descartar(descartado)

    assert cp.contar_pendientes() == 1
    assert len(cp.list_por_estado(cp.ESTADO_PENDIENTE)) == 1
    assert len(cp.list_por_estado(cp.ESTADO_FACTURADO)) == 1
    assert len(cp.list_por_estado(cp.ESTADO_DESCARTADO)) == 1


def test_get_comprobantes_trae_varios_y_parsea_los_items(conn):
    uno, _ = _depositar(origen_id="1")
    dos, _ = _depositar(origen_id="2")
    comprobantes = cp.get_comprobantes([dos, uno])
    assert [c["id"] for c in comprobantes] == [uno, dos]
    assert isinstance(comprobantes[0]["items"], list)


def test_get_comprobantes_sin_ids_no_consulta(conn):
    assert cp.get_comprobantes([]) == []
