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
