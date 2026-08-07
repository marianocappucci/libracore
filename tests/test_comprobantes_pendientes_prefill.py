"""El armado del formulario a partir de los pendientes elegidos.

La decisión que fija esta suite es la de agrupar: el productor manda granular y
**la persona elige** qué se factura junto. Lo que se prueba es que agrupar no
invente datos — ni el receptor, ni el período, ni la alícuota.
"""
import pytest

from libracore import comprobantes_pendientes as dominio


def _comprobante(**kwargs):
    base = dict(
        id=1,
        cliente_id=None,
        cliente_cuit="30-71234567-9",
        cliente_razon="Ferretería San Martín",
        cliente_domicilio="Mitre 100",
        periodo_desde="2026-08-01",
        periodo_hasta="2026-08-31",
        concepto="Alquiler de equipos",
        condicion_venta="Cuenta Corriente",
        observaciones="",
        fecha_sugerida="2026-09-10",
        items=[{"description": "Alquiler impresora", "qty": 1,
                "unit_price": 45000.0, "iva_rate": 0.21}],
    )
    base.update(kwargs)
    return base


# ── El receptor ──────────────────────────────────────────────────────────────

def test_no_se_pueden_facturar_juntos_dos_clientes():
    with pytest.raises(dominio.ClientesMezclados):
        dominio.armar_prefill([
            _comprobante(id=1, cliente_cuit="30-71234567-9"),
            _comprobante(id=2, cliente_cuit="27-99999999-4",
                         cliente_razon="Otra SRL"),
        ])


def test_el_mismo_cuit_con_guiones_distintos_es_el_mismo_cliente():
    """El productor puede mandar el CUIT formateado de otra forma. Rechazar
    eso como "clientes distintos" sería un falso positivo que bloquea agrupar
    justo cuando más se necesita."""
    prefill = dominio.armar_prefill([
        _comprobante(id=1, cliente_cuit="30-71234567-9"),
        _comprobante(id=2, cliente_cuit="30712345679"),
    ])
    assert prefill["comprobantes_ids"] == [1, 2]


def test_sin_cuit_se_agrupa_por_razon_social():
    prefill = dominio.armar_prefill([
        _comprobante(id=1, cliente_cuit=""),
        _comprobante(id=2, cliente_cuit=""),
    ])
    assert len(prefill["items"]) == 2


def test_sin_cuit_y_con_razones_distintas_no_se_agrupa():
    with pytest.raises(dominio.ClientesMezclados):
        dominio.armar_prefill([
            _comprobante(id=1, cliente_cuit="", cliente_razon="Una SRL"),
            _comprobante(id=2, cliente_cuit="", cliente_razon="Otra SRL"),
        ])


# ── Los ítems y el período ───────────────────────────────────────────────────

def test_los_items_se_concatenan_en_orden():
    prefill = dominio.armar_prefill([
        _comprobante(id=1, items=[{"description": "Alquiler", "qty": 1,
                                   "unit_price": 100.0, "iva_rate": 0.21}]),
        _comprobante(id=2, items=[{"description": "Service", "qty": 2,
                                   "unit_price": 50.0, "iva_rate": 0.21}]),
    ])
    assert [i["description"] for i in prefill["items"]] == ["Alquiler", "Service"]
    assert prefill["items"][1]["qty"] == 2


def test_el_periodo_abarca_todos_los_comprobantes():
    prefill = dominio.armar_prefill([
        _comprobante(id=1, periodo_desde="2026-08-01", periodo_hasta="2026-08-31"),
        _comprobante(id=2, periodo_desde="2026-07-01", periodo_hasta="2026-07-31"),
    ])
    assert prefill["fch_serv_desde"] == "2026-07-01"
    assert prefill["fch_serv_hasta"] == "2026-08-31"


def test_el_periodo_no_es_hoy():
    """La diferencia de fondo con `facturas_borrador`: la factura de agosto se
    emite en septiembre y el período tiene que seguir diciendo agosto."""
    prefill = dominio.armar_prefill([_comprobante()], hoy="2026-09-05")
    assert prefill["fecha"] == "2026-09-05"
    assert prefill["fch_serv_hasta"] == "2026-08-31"


def test_sin_periodo_las_fechas_de_servicio_quedan_vacias():
    prefill = dominio.armar_prefill(
        [_comprobante(periodo_desde="", periodo_hasta="")], hoy="2026-09-05")
    assert prefill["fch_serv_desde"] == ""
    assert prefill["fch_serv_hasta"] == ""


# ── El IVA ───────────────────────────────────────────────────────────────────

def test_una_sola_alicuota_pasa_sin_aviso():
    prefill = dominio.armar_prefill([_comprobante()])
    assert prefill["tax_rate"] == 0.21
    assert prefill["avisos"] == []


def test_dos_alicuotas_se_aplastan_a_la_mas_alta_y_avisan():
    """La factura de Contalibra lleva **una** tasa. Promediar en silencio sería
    emitir un IVA que nadie eligió; el aviso pone la decisión donde tiene que
    estar, que es la pantalla."""
    prefill = dominio.armar_prefill([
        _comprobante(id=1, items=[{"description": "A", "qty": 1,
                                   "unit_price": 100.0, "iva_rate": 0.21}]),
        _comprobante(id=2, items=[{"description": "B", "qty": 1,
                                   "unit_price": 100.0, "iva_rate": 0.105}]),
    ])
    assert prefill["tax_rate"] == 0.21
    assert len(prefill["avisos"]) == 1
    assert "alícuota" in prefill["avisos"][0]


def test_condiciones_de_venta_distintas_avisan():
    prefill = dominio.armar_prefill([
        _comprobante(id=1, condicion_venta="Contado"),
        _comprobante(id=2, condicion_venta="Cuenta Corriente"),
    ])
    assert any("condiciones de venta" in a for a in prefill["avisos"])


# ── Lo que el motor no decide ────────────────────────────────────────────────

def test_el_prefill_no_elige_tipo_ni_punto_de_venta():
    """Qué letra se emite depende de la condición de IVA del emisor y del
    receptor, y eso lo sabe el producto. Que el motor no lo mande es la
    diferencia entre prefillear un formulario y emitir por su cuenta."""
    prefill = dominio.armar_prefill([_comprobante()])
    assert "tipo" not in prefill
    assert "punto_venta" not in prefill


def test_sin_comprobantes_no_hay_prefill():
    with pytest.raises(ValueError):
        dominio.armar_prefill([])
