"""`libracore.abono.calcular_abono`: el abono = base + adicionales contratadas × precio (ADR-041). Puro: una tabla alcanza."""
import pytest

from libracore.abono import calcular_abono

BASE, INCLUIDAS, ADICIONAL = 39900, 1, 19950


@pytest.mark.parametrize("contratadas, adicionales, extra, total", [
    (1, 0, 0, 39900),
    (2, 1, 19950, 59850),
    (3, 2, 39900, 79800),
    (10, 9, 179550, 219450),
    (0, 0, 0, 39900),  # nunca por debajo de la base
    (None, 0, 0, 39900),  # sin dato cargado: sólo la base
])
def test_tabla_del_abono_de_ventalibra(contratadas, adicionales, extra, total):
    r = calcular_abono(BASE, INCLUIDAS, ADICIONAL, contratadas)
    assert (r["adicionales"], r["extra"], r["total"]) == (adicionales, extra, total)
    assert r["base"] == BASE
    assert r["contratadas"] == contratadas


def test_sin_dato_se_distingue_de_una_contratada():
    assert calcular_abono(BASE, INCLUIDAS, ADICIONAL, None)["contratadas"] is None
    assert calcular_abono(BASE, INCLUIDAS, ADICIONAL, 1)["contratadas"] == 1


def test_devuelve_tambien_lo_que_se_necesita_para_mostrar_la_cuenta():
    r = calcular_abono(BASE, INCLUIDAS, ADICIONAL, 3)
    assert r == {"contratadas": 3, "incluidas": 1, "base": 39900, "adicionales": 2, "precio_adicional": 19950, "extra": 39900, "total": 79800}


def test_con_mas_unidades_incluidas_solo_se_cobra_lo_que_pasa():
    assert calcular_abono(10000, 3, 1000, 5)["total"] == 12000
    assert calcular_abono(10000, 3, 1000, 2)["total"] == 10000
