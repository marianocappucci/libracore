"""Qué letra de factura corresponde: la regla única, medida contra ARCA."""
import pytest

from libracore.arca_facturacion import tipo_de_comprobante
from libracore.venta_facturacion import _tipo_comprobante


@pytest.mark.parametrize("receptor, esperado", [
    ("Responsable Inscripto", 1),
    ("IVA Responsable Inscripto", 1),
    ("Monotributista", 1),            # 🔴 antes daba B, que ARCA rechaza (10243)
    ("Responsable Monotributo", 1),
    ("Consumidor Final", 6),
    ("IVA Exento", 6),
    ("No Alcanzado", 6),
    ("", 6),
])
def test_un_emisor_inscripto_elige_la_letra_segun_el_receptor(receptor, esperado):
    assert tipo_de_comprobante("Responsable Inscripto", receptor) == esperado


@pytest.mark.parametrize("receptor", ["Responsable Inscripto", "Monotributista", "Consumidor Final"])
def test_un_monotributista_emite_c_a_cualquiera(receptor):
    assert tipo_de_comprobante("Monotributista", receptor) == 11


def test_la_facturacion_de_ventas_usa_la_misma_regla():
    assert _tipo_comprobante("Responsable Inscripto", "Monotributista") == 1


# ── Los tipos, en un solo lugar ─────────────────────────────────────────────


def test_cada_nota_sale_de_una_factura_y_conserva_la_letra_y_si_es_fce():
    from libracore import tipos_comprobante as t
    assert set(t.TIPO_NC) == set(t.TIPO_ND) == set(t.FACTURAS)
    assert set(t.TIPO_NC.values()) == set(t.NC) and set(t.TIPO_ND.values()) == set(t.ND)
    for factura in t.FACTURAS:
        for nota in (t.TIPO_NC[factura], t.TIPO_ND[factura]):
            assert t.LETRA[nota] == t.LETRA[factura]
            assert (nota in t.FCE) == (factura in t.FCE)


def test_las_fce_estan_en_todos_los_listados_que_miran_facturas_y_notas(tmp_path):
    from libracore import tipos_comprobante as t
    from libracore.db import facturas as db_facturas
    assert set(t.FCE_FACTURA) <= set(db_facturas._TIPOS_FACTURA)
    assert set(t.FCE_NOTA) <= set(db_facturas._TIPOS_NC) | set(db_facturas._TIPOS_ND)


def test_el_pdf_nombra_todos_los_tipos():
    from libracore import pdf_generator as pdf
    from libracore import tipos_comprobante as t
    for tipo in t.LETRA:
        assert tipo in pdf._TIPO_LABELS and tipo in pdf._TIPO_COD and tipo in pdf._TIPO_NOMBRE_DOC
