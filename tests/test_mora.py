"""`libracore.mora`: el punitorio de un pago fuera de término. Puro: una tabla alcanza.

Los días no hábiles salen del catálogo real de `libracore.feriados` (2026 está cubierto). Calendario que usan los casos:

    agosto 2026:   do 9 · LU 10 · ma 11 · mi 12 · ju 13 · vi 14 · SA 15 · do 16 · FERIADO lu 17 · ma 18 · mi 19
    julio 2026:    ju 9 FERIADO · vi 10 PUENTE · sa 11 · do 12 · lu 13
    mayo 2026:     vi 1 FERIADO · sa 2 · do 3 · lu 4
"""
from datetime import date
from decimal import Decimal

import pytest

from libracore.feriados import FueraDeCobertura
from libracore.mora import (
    BASES,
    calcular_punitorio,
    es_dia_habil,
    proximo_dia_habil,
)

D = Decimal
IMPORTE = D("100000")


def punitorio(vencimiento, pago, **kw):
    kw.setdefault("tasa", D("0.5"))
    return calcular_punitorio(IMPORTE, vencimiento, pago, **kw)


# ── Dentro de la gracia no hay punitorio; después, sí ──────────────────────

@pytest.mark.parametrize("pago, gracia, atraso, computados, esperado", [
    (date(2026, 8, 5), 3, 0, 0, "0.00"),        # pagó antes
    (date(2026, 8, 10), 3, 0, 0, "0.00"),       # pagó el día del vencimiento
    (date(2026, 8, 11), 3, 1, 0, "0.00"),       # dentro de la gracia
    (date(2026, 8, 13), 3, 3, 0, "0.00"),       # el último día de gracia, inclusive
    (date(2026, 8, 14), 3, 4, 4, "2000.00"),    # un día después: corre desde el VENCIMIENTO, los 4 días
    (date(2026, 8, 20), 3, 10, 10, "5000.00"),
    (date(2026, 8, 11), 0, 1, 1, "500.00"),     # sin gracia, un día de atraso
    (date(2026, 8, 10), 0, 0, 0, "0.00"),       # sin gracia, el mismo día
])
def test_gracia_y_dias_de_atraso_desde_el_vencimiento(pago, gracia, atraso, computados, esperado):
    r = punitorio(date(2026, 8, 10), pago, dias_gracia=gracia)
    assert (r["dias_de_atraso"], r["dias_computados"]) == (atraso, computados)
    assert r["punitorio"] == D(esperado)
    assert r["total"] == IMPORTE + D(esperado)
    assert (r["interes_desde"] is None) == (computados == 0)


@pytest.mark.parametrize("pago, computados, esperado", [
    (date(2026, 8, 13), 0, "0.00"),          # último día de gracia
    (date(2026, 8, 14), 1, "500.00"),        # corre sólo desde que termina la gracia
    (date(2026, 8, 20), 7, "3500.00"),
])
def test_el_interes_puede_correr_desde_el_fin_de_la_gracia(pago, computados, esperado):
    r = punitorio(date(2026, 8, 10), pago, dias_gracia=3, desde_fin_de_gracia=True)
    assert r["dias_computados"] == computados
    assert r["punitorio"] == D(esperado)
    if computados:
        assert r["interes_desde"] == date(2026, 8, 13)
    # El atraso real es el mismo con las dos reglas: lo que cambia es sobre cuántos días se cobra.
    assert r["dias_de_atraso"] == (pago - date(2026, 8, 10)).days


def test_desde_el_vencimiento_es_la_regla_por_defecto():
    r = punitorio(date(2026, 8, 10), date(2026, 8, 14), dias_gracia=3)
    assert r["interes_desde"] == date(2026, 8, 10)


# ── El vencimiento en día no hábil ─────────────────────────────────────────

@pytest.mark.parametrize("vencimiento, efectivo", [
    (date(2026, 8, 10), date(2026, 8, 10)),    # lunes hábil: no se mueve
    (date(2026, 8, 9), date(2026, 8, 10)),     # domingo -> lunes
    (date(2026, 8, 15), date(2026, 8, 18)),    # sábado -> domingo -> lunes feriado -> martes
    (date(2026, 8, 16), date(2026, 8, 18)),    # domingo -> lunes feriado -> martes
    (date(2026, 8, 17), date(2026, 8, 18)),    # el feriado mismo
    (date(2026, 7, 9), date(2026, 7, 13)),     # feriado + puente + fin de semana
    (date(2026, 5, 1), date(2026, 5, 4)),      # feriado un viernes
])
def test_el_vencimiento_se_corre_al_siguiente_habil(vencimiento, efectivo):
    r = punitorio(vencimiento, date(2026, 9, 30))
    assert r["vencimiento_efectivo"] == efectivo
    assert r["corrido"] is (efectivo != vencimiento)
    assert r["vencimiento"] == vencimiento


def test_vencimiento_en_domingo_pagado_el_lunes_no_tiene_punitorio():
    r = punitorio(date(2026, 8, 9), date(2026, 8, 10))
    assert (r["dias_de_atraso"], r["dias_computados"], r["punitorio"]) == (0, 0, D("0.00"))
    assert r["corrido"] is True


def test_vencimiento_en_domingo_pagado_el_martes_cuenta_un_dia_desde_el_lunes():
    r = punitorio(date(2026, 8, 9), date(2026, 8, 11))
    assert (r["dias_de_atraso"], r["dias_computados"], r["punitorio"]) == (1, 1, D("500.00"))


def test_sin_corrimiento_el_domingo_cuenta():
    r = punitorio(date(2026, 8, 9), date(2026, 8, 10), correr_dia_no_habil=False)
    assert r["vencimiento_efectivo"] == date(2026, 8, 9)
    assert r["corrido"] is False
    assert (r["dias_computados"], r["punitorio"]) == (1, D("500.00"))


def test_vencimiento_en_feriado_y_fin_de_semana_largo():
    """Sábado 15 + domingo 16 + lunes 17 feriado: se puede pagar hasta el martes 18 sin mora."""
    assert punitorio(date(2026, 8, 15), date(2026, 8, 18))["punitorio"] == D("0.00")
    assert punitorio(date(2026, 8, 15), date(2026, 8, 19))["punitorio"] == D("500.00")


def test_la_gracia_se_cuenta_desde_el_vencimiento_ya_corrido():
    """Vence el domingo 9, pasa al lunes 10; con 3 de gracia se paga sin mora hasta el jueves 13."""
    r = punitorio(date(2026, 8, 9), date(2026, 8, 13), dias_gracia=3)
    assert r["fin_de_gracia"] == date(2026, 8, 13)
    assert r["punitorio"] == D("0.00")
    r = punitorio(date(2026, 8, 9), date(2026, 8, 14), dias_gracia=3)
    assert (r["dias_computados"], r["interes_desde"]) == (4, date(2026, 8, 10))


def test_el_producto_puede_sumar_sus_propios_dias_no_habiles():
    # El 11 de agosto es martes hábil; si la inmobiliaria cierra, el vencimiento pasa al miércoles.
    sin = punitorio(date(2026, 8, 11), date(2026, 8, 12))
    con = punitorio(date(2026, 8, 11), date(2026, 8, 12), dias_no_habiles=[date(2026, 8, 11)])
    assert (sin["vencimiento_efectivo"], sin["dias_computados"]) == (date(2026, 8, 11), 1)
    assert (con["vencimiento_efectivo"], con["dias_computados"]) == (date(2026, 8, 12), 0)


@pytest.mark.parametrize("dia, habil", [
    (date(2026, 8, 10), True),     # lunes
    (date(2026, 8, 14), True),     # viernes
    (date(2026, 8, 15), False),    # sábado
    (date(2026, 8, 16), False),    # domingo
    (date(2026, 8, 17), False),    # feriado (San Martín trasladado)
    (date(2026, 10, 12), False),   # feriado
    (date(2026, 12, 25), False),   # Navidad
    (date(2026, 7, 10), False),    # puente turístico
])
def test_es_dia_habil(dia, habil):
    assert es_dia_habil(dia) is habil


def test_proximo_dia_habil():
    assert proximo_dia_habil(date(2026, 8, 12)) == date(2026, 8, 12)
    assert proximo_dia_habil(date(2026, 8, 15)) == date(2026, 8, 18)


def test_si_no_hay_dia_habil_cerca_es_un_error_y_no_un_bucle():
    todos = [date.fromordinal(date(2026, 8, 10).toordinal() + n) for n in range(40)]
    with pytest.raises(ValueError, match="hábil"):
        proximo_dia_habil(date(2026, 8, 10), todos)


def test_un_anio_fuera_del_catalogo_levanta_en_vez_de_decir_que_no_es_feriado():
    with pytest.raises(FueraDeCobertura):
        punitorio(date(2035, 1, 3), date(2035, 1, 10))
    # Quien cobre fuera de la ventana puede optar por no correr el vencimiento.
    assert punitorio(date(2035, 1, 3), date(2035, 1, 10), correr_dia_no_habil=False)["dias_computados"] == 7


# ── Tasa diaria contra mensual ─────────────────────────────────────────────

@pytest.mark.parametrize("tasa, base, dias, esperado, tasa_diaria", [
    ("0.1", "diaria", 10, "1000.00", "0.1"),
    ("3", "mensual", 10, "1000.00", "0.1"),          # 3 % mensual = 0,1 % por día de 30
    ("3", "diaria", 10, "30000.00", "3"),            # y 3 % diario es otra cosa
    ("5", "mensual", 4, "666.67", None),             # 100000 × 5 % × 4/30 = 666,666… -> 666,67
    ("5", "mensual", 30, "5000.00", None),           # un mes entero de atraso = la tasa mensual
    ("5", "mensual", 60, "10000.00", None),          # interés simple: no capitaliza
    ("0", "diaria", 10, "0.00", "0"),                # sin tasa pactada
])
def test_base_diaria_y_mensual(tasa, base, dias, esperado, tasa_diaria):
    pago = date.fromordinal(date(2026, 8, 10).toordinal() + dias)
    r = punitorio(date(2026, 8, 10), pago, tasa=D(tasa), base=base)
    assert r["dias_computados"] == dias
    assert r["punitorio"] == D(esperado)
    assert (r["base"], r["tasa"]) == (base, D(tasa))
    if tasa_diaria is not None:
        assert r["tasa_diaria"] == D(tasa_diaria)


def test_la_mensual_usa_30_dias_fijos_en_cualquier_mes():
    """Febrero (28 días) y enero (31) dan el mismo interés por el mismo atraso."""
    enero = punitorio(date(2026, 1, 12), date(2026, 1, 22), tasa=D("3"), base="mensual")
    febrero = punitorio(date(2026, 2, 9), date(2026, 2, 19), tasa=D("3"), base="mensual")
    assert enero["punitorio"] == febrero["punitorio"] == D("1000.00")


def test_la_base_por_defecto_es_diaria():
    assert punitorio(date(2026, 8, 10), date(2026, 8, 11))["base"] == "diaria"
    assert BASES == ("diaria", "mensual")


# ── El redondeo y los tipos ────────────────────────────────────────────────

def test_el_punitorio_se_redondea_half_up_una_sola_vez():
    """12.5 × 1 % × 1 día = 0.125: HALF_UP da 0.13; el banquero daría 0.12."""
    r = calcular_punitorio(D("12.5"), date(2026, 8, 10), date(2026, 8, 11), D("1"))
    assert r["punitorio"] == D("0.13")


def test_se_redondea_sobre_el_total_y_no_dia_por_dia():
    """0,5 centavo por día durante 3 días: 3 × 0.005 = 0.015 -> 0.02; redondeando cada día daría 3 × 0.01 = 0.03."""
    r = calcular_punitorio(D("0.5"), date(2026, 8, 10), date(2026, 8, 13), D("1"))
    assert r["punitorio"] == D("0.02")


@pytest.mark.parametrize("importe, tasa", [(100000, 0.5), ("100000", "0.5"), (100000.0, 0.5), (D("100000"), D("0.5"))])
def test_acepta_int_str_float_y_decimal(importe, tasa):
    r = calcular_punitorio(importe, date(2026, 8, 10), date(2026, 8, 14), tasa)
    assert r["punitorio"] == D("2000.00")
    assert isinstance(r["punitorio"], Decimal)
    assert r["importe"] == IMPORTE


def test_importe_cero_no_genera_punitorio():
    r = calcular_punitorio(0, date(2026, 8, 10), date(2026, 9, 10), D("0.5"))
    assert r["punitorio"] == D("0.00")
    assert r["total"] == D("0.00")


def test_el_detalle_trae_todo_lo_que_hace_falta_para_el_recibo():
    r = punitorio(date(2026, 8, 9), date(2026, 8, 14), dias_gracia=3)
    assert r == {
        "importe": D("100000"),
        "vencimiento": date(2026, 8, 9),
        "fecha_pago": date(2026, 8, 14),
        "dias_gracia": 3,
        "vencimiento_efectivo": date(2026, 8, 10),
        "corrido": True,
        "fin_de_gracia": date(2026, 8, 13),
        "dias_de_atraso": 4,
        "dias_computados": 4,
        "interes_desde": date(2026, 8, 10),
        "base": "diaria",
        "tasa": D("0.5"),
        "tasa_diaria": D("0.5"),
        "punitorio": D("2000.00"),
        "total": D("102000.00"),
    }


# ── Entradas inválidas ─────────────────────────────────────────────────────

@pytest.mark.parametrize("kw, mensaje", [
    ({"base": "semanal"}, "semanal"),
    ({"tasa": D("-1")}, "tasa"),
    ({"dias_gracia": -1}, "gracia"),
])
def test_entradas_invalidas_levantan_value_error(kw, mensaje):
    with pytest.raises(ValueError, match=mensaje):
        punitorio(date(2026, 8, 10), date(2026, 8, 14), **kw)


def test_un_importe_negativo_levanta_value_error():
    with pytest.raises(ValueError, match="importe"):
        calcular_punitorio(D("-1"), date(2026, 8, 10), date(2026, 8, 14), D("0.5"))
