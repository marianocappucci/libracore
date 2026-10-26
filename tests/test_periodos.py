"""`libracore.periodos`: la aritmética de períodos de un contrato. Pura: una tabla alcanza.

Los casos vienen de `tests/test_cuotas.py` de LibraDesk (devengado, 2026-08-15), donde estaban escritos contra la API y la base; acá son
los mismos números sin nada de eso. Lo que se fija es lo que sale mal una vez al año: los meses cortos, el bisiesto, el prorrateo con
divisor real y el redondeo que no es el del banquero.
"""
from datetime import date
from decimal import Decimal

import pytest

from libracore.periodos import (
    PERIODICIDADES,
    CuotaPropuesta,
    Periodo,
    _sumar_meses,
    clave_de_periodo,
    periodo_de,
    periodo_por_cadencia,
    proponer_cuota,
    redondear,
    vencimiento_de,
    ya_generada,
)

D = Decimal


# ── El redondeo ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("valor, esperado", [
    ("0.125", "0.13"),   # el banquero daría 0.12: una factura argentina dice 0.13
    ("0.135", "0.14"),
    ("0.124", "0.12"),
    ("2.675", "2.68"),   # exacto en Decimal; con float daría 2.67
    ("0.5", "0.50"),
    ("10", "10.00"),
    ("-0.125", "-0.13"),  # HALF_UP de Decimal redondea lejos del cero
    ("0", "0.00"),
])
def test_redondear_es_half_up_a_centavos(valor, esperado):
    assert redondear(D(valor)) == D(esperado)
    assert str(redondear(D(valor))) == esperado  # y deja los dos decimales


# ── Sumar meses ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("origen, meses, esperado", [
    (date(2026, 1, 31), 1, date(2026, 2, 28)),    # el caso que revienta sin el recorte
    (date(2024, 1, 31), 1, date(2024, 2, 29)),    # bisiesto
    (date(2026, 3, 31), 1, date(2026, 4, 30)),
    (date(2026, 1, 30), 1, date(2026, 2, 28)),
    (date(2026, 1, 29), 1, date(2026, 2, 28)),
    (date(2026, 1, 28), 1, date(2026, 2, 28)),
    (date(2026, 11, 30), 3, date(2027, 2, 28)),   # cruza de año
    (date(2026, 12, 15), 1, date(2027, 1, 15)),
    (date(2026, 8, 20), 12, date(2027, 8, 20)),
    (date(2026, 8, 20), 0, date(2026, 8, 20)),
    (date(2028, 2, 29), 12, date(2029, 2, 28)),   # el 29 de febrero un año después
])
def test_sumar_meses_recorta_el_dia_al_ultimo_del_mes_destino(origen, meses, esperado):
    assert _sumar_meses(origen, meses) == esperado


# ── El período que contiene al ancla ───────────────────────────────────────

@pytest.mark.parametrize("periodicidad, ancla, desde, hasta, dias", [
    ("mensual", date(2026, 8, 25), date(2026, 8, 1), date(2026, 8, 31), 31),
    ("mensual", date(2026, 9, 1), date(2026, 9, 1), date(2026, 9, 30), 30),
    ("mensual", date(2026, 2, 10), date(2026, 2, 1), date(2026, 2, 28), 28),
    ("mensual", date(2024, 2, 10), date(2024, 2, 1), date(2024, 2, 29), 29),  # bisiesto
    ("mensual", date(2026, 12, 31), date(2026, 12, 1), date(2026, 12, 31), 31),
    ("bimestral", date(2026, 3, 15), date(2026, 3, 1), date(2026, 4, 30), 61),
    ("bimestral", date(2026, 2, 1), date(2026, 1, 1), date(2026, 2, 28), 59),
    ("trimestral", date(2026, 1, 1), date(2026, 1, 1), date(2026, 3, 31), 90),
    ("trimestral", date(2024, 3, 31), date(2024, 1, 1), date(2024, 3, 31), 91),  # primer trimestre bisiesto
    ("trimestral", date(2026, 5, 15), date(2026, 4, 1), date(2026, 6, 30), 91),
    ("trimestral", date(2026, 12, 31), date(2026, 10, 1), date(2026, 12, 31), 92),
    ("semestral", date(2026, 6, 30), date(2026, 1, 1), date(2026, 6, 30), 181),
    ("semestral", date(2026, 8, 1), date(2026, 7, 1), date(2026, 12, 31), 184),
    ("anual", date(2026, 8, 1), date(2026, 1, 1), date(2026, 12, 31), 365),
    ("anual", date(2024, 8, 1), date(2024, 1, 1), date(2024, 12, 31), 366),
])
def test_el_periodo_es_de_calendario_y_los_multimes_se_alinean_al_anio(periodicidad, ancla, desde, hasta, dias):
    p = periodo_de(periodicidad, ancla)
    assert (p.desde, p.hasta, p.dias) == (desde, hasta, dias)
    assert p.desde <= ancla <= p.hasta


@pytest.mark.parametrize("periodicidad", PERIODICIDADES)
def test_cualquier_dia_del_periodo_da_el_mismo_periodo(periodicidad):
    p = periodo_de(periodicidad, date(2026, 8, 15))
    for dia in (p.desde, p.hasta, date(2026, 8, 15)):
        assert periodo_de(periodicidad, dia) == p


@pytest.mark.parametrize("periodicidad", PERIODICIDADES)
def test_dos_periodos_contiguos_no_se_pisan_ni_dejan_hueco(periodicidad):
    p = periodo_de(periodicidad, date(2026, 8, 15))
    siguiente = periodo_de(periodicidad, date.fromordinal(p.hasta.toordinal() + 1))
    assert siguiente.desde.toordinal() == p.hasta.toordinal() + 1


def test_periodo_de_y_periodo_por_cadencia_son_la_misma_cuenta():
    for periodicidad in PERIODICIDADES:
        assert periodo_de(periodicidad, date(2026, 8, 15)) == periodo_por_cadencia(periodicidad, date(2026, 8, 15))


def test_una_periodicidad_desconocida_es_un_error_claro():
    with pytest.raises(ValueError, match="quincenal"):
        periodo_de("quincenal", date(2026, 8, 15))


@pytest.mark.parametrize("inicio, fin, esperado", [
    (date(2026, 1, 1), None, Periodo(date(2026, 8, 1), date(2026, 8, 31))),
    (date(2026, 8, 20), None, Periodo(date(2026, 8, 20), date(2026, 8, 31))),
    (date(2026, 1, 1), date(2026, 8, 10), Periodo(date(2026, 8, 1), date(2026, 8, 10))),
    (date(2026, 8, 5), date(2026, 8, 5), Periodo(date(2026, 8, 5), date(2026, 8, 5))),
    (date(2026, 8, 31), None, Periodo(date(2026, 8, 31), date(2026, 8, 31))),
    (date(2026, 9, 1), None, None),                    # todavía no empezó
    (date(2026, 1, 1), date(2026, 7, 31), None),       # ya terminó
])
def test_recortar_el_periodo_a_la_vigencia(inicio, fin, esperado):
    assert Periodo(date(2026, 8, 1), date(2026, 8, 31)).recortado(inicio, fin) == esperado


# ── El vencimiento ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("dia, periodo, esperado", [
    (10, Periodo(date(2026, 8, 1), date(2026, 8, 31)), date(2026, 8, 10)),
    (1, Periodo(date(2026, 8, 1), date(2026, 8, 31)), date(2026, 8, 1)),
    (31, Periodo(date(2026, 2, 1), date(2026, 2, 28)), date(2026, 2, 28)),   # no vence un 31 de febrero
    (31, Periodo(date(2024, 2, 1), date(2024, 2, 29)), date(2024, 2, 29)),
    (31, Periodo(date(2026, 4, 1), date(2026, 4, 30)), date(2026, 4, 30)),
    (31, Periodo(date(2026, 1, 1), date(2026, 3, 31)), date(2026, 1, 31)),   # trimestral: el mes en que arranca
    (30, Periodo(date(2026, 2, 1), date(2026, 2, 28)), date(2026, 2, 28)),
    # 🔴 Comportamiento heredado de LibraDesk: en la cuota prorrateada cuenta el mes del primer día cubierto, y puede quedar ANTES de
    # la emisión. Está fijado para que un cambio sea una decisión y no un accidente.
    (10, Periodo(date(2026, 8, 20), date(2026, 8, 31)), date(2026, 8, 10)),
])
def test_el_vencimiento_se_recorta_al_ultimo_dia_del_mes(dia, periodo, esperado):
    assert vencimiento_de(dia, periodo) == esperado


def test_sin_dia_de_vencimiento_no_se_inventa_ninguno():
    assert vencimiento_de(None, Periodo(date(2026, 8, 1), date(2026, 8, 31))) is None


@pytest.mark.parametrize("dia", [0, 32, -1])
def test_un_dia_de_vencimiento_fuera_de_rango_es_un_error(dia):
    with pytest.raises(ValueError):
        vencimiento_de(dia, Periodo(date(2026, 8, 1), date(2026, 8, 31)))


# ── La cuota propuesta: prorrateo y vigencia ───────────────────────────────

@pytest.mark.parametrize("inicio, fin, periodicidad, importe, ancla, desde, hasta, cubiertos, del_periodo, total", [
    # mes entero
    (date(2026, 1, 1), None, "mensual", "31000", date(2026, 8, 15), date(2026, 8, 1), date(2026, 8, 31), 31, 31, "31000.00"),
    # arranca el 20: 12 de 31
    (date(2026, 8, 20), None, "mensual", "31000", date(2026, 8, 25), date(2026, 8, 20), date(2026, 8, 31), 12, 31, "12000.00"),
    # termina el 10: 10 de 31
    (date(2026, 1, 1), date(2026, 8, 10), "mensual", "31000", date(2026, 8, 5), date(2026, 8, 1), date(2026, 8, 10), 10, 31, "10000.00"),
    # las dos puntas en el mismo mes: del 10 al 20 son 11 días
    (date(2026, 8, 10), date(2026, 8, 20), "mensual", "31000", date(2026, 8, 15), date(2026, 8, 10), date(2026, 8, 20), 11, 31, "11000.00"),
    # febrero: el divisor es 28 y no 30 (con 30 daría 13066.67)
    (date(2026, 2, 15), None, "mensual", "28000", date(2026, 2, 20), date(2026, 2, 15), date(2026, 2, 28), 14, 28, "14000.00"),
    # febrero bisiesto: 15 de 29
    (date(2024, 2, 15), None, "mensual", "29000", date(2024, 2, 20), date(2024, 2, 15), date(2024, 2, 29), 15, 29, "15000.00"),
    # arranca el 31 de enero: un día de enero, y febrero entero
    (date(2026, 1, 31), None, "mensual", "31000", date(2026, 1, 15), date(2026, 1, 31), date(2026, 1, 31), 1, 31, "1000.00"),
    (date(2026, 1, 31), None, "mensual", "31000", date(2026, 2, 15), date(2026, 2, 1), date(2026, 2, 28), 28, 28, "31000.00"),
    # trimestral que arranca el 1 de febrero: 59 de 90 el primero, entero desde abril
    (date(2026, 2, 1), None, "trimestral", "90000", date(2026, 3, 15), date(2026, 2, 1), date(2026, 3, 31), 59, 90, "59000.00"),
    (date(2026, 2, 1), None, "trimestral", "90000", date(2026, 5, 15), date(2026, 4, 1), date(2026, 6, 30), 91, 91, "90000.00"),
    # semestral y anual
    (date(2026, 10, 1), None, "semestral", "184000", date(2026, 11, 1), date(2026, 10, 1), date(2026, 12, 31), 92, 184, "92000.00"),
    (date(2026, 7, 2), None, "anual", "365000", date(2026, 9, 1), date(2026, 7, 2), date(2026, 12, 31), 183, 365, "183000.00"),
    # bimestral entero
    (date(2026, 1, 1), None, "bimestral", "61000", date(2026, 3, 20), date(2026, 3, 1), date(2026, 4, 30), 61, 61, "61000.00"),
    # el contrato termina justo el último día del período: no se prorratea
    (date(2026, 1, 1), date(2026, 8, 31), "mensual", "31000", date(2026, 8, 15), date(2026, 8, 1), date(2026, 8, 31), 31, 31, "31000.00"),
    # un solo día de contrato
    (date(2026, 8, 15), date(2026, 8, 15), "mensual", "31000", date(2026, 8, 15), date(2026, 8, 15), date(2026, 8, 15), 1, 31, "1000.00"),
])
def test_tabla_de_cuotas(inicio, fin, periodicidad, importe, ancla, desde, hasta, cubiertos, del_periodo, total):
    cuota = proponer_cuota(
        fecha_inicio=inicio, fecha_fin=fin, periodicidad=periodicidad, importe=D(importe), ancla=ancla,
    )
    assert isinstance(cuota, CuotaPropuesta)
    assert (cuota.periodo.desde, cuota.periodo.hasta) == (desde, hasta)
    assert (cuota.dias_cubiertos, cuota.dias_del_periodo) == (cubiertos, del_periodo)
    assert cuota.dias_cubiertos == cuota.periodo.dias
    assert cuota.importe == D(total)
    assert str(cuota.importe) == total  # a centavos, siempre con dos decimales
    assert cuota.prorrateada == (cubiertos < del_periodo)
    # Mes adelantado: se emite el primer día que el contrato cubre.
    assert cuota.fecha_emision == desde
    assert cuota.fecha_vencimiento is None


def test_el_prorrateo_redondea_half_up_y_no_a_la_par():
    """3.875 × 1/31 = 0.125 exacto: HALF_UP da 0.13; el banquero daría 0.12."""
    cuota = proponer_cuota(
        fecha_inicio=date(2026, 8, 31), fecha_fin=None, periodicidad="mensual",
        importe=D("3.875"), ancla=date(2026, 8, 31),
    )
    assert cuota.importe == D("0.13")


def test_un_importe_completo_tambien_se_redondea_a_centavos():
    cuota = proponer_cuota(
        fecha_inicio=date(2026, 1, 1), fecha_fin=None, periodicidad="mensual",
        importe=D("100.125"), ancla=date(2026, 8, 15),
    )
    assert cuota.importe == D("100.13")


@pytest.mark.parametrize("importe", [31000, "31000", "31000.00", 31000.0, D("31000")])
def test_el_importe_puede_venir_como_int_str_float_o_decimal(importe):
    cuota = proponer_cuota(
        fecha_inicio=date(2026, 8, 20), fecha_fin=None, periodicidad="mensual", importe=importe, ancla=date(2026, 8, 25),
    )
    assert cuota.importe == D("12000.00")
    assert isinstance(cuota.importe, Decimal)


def test_un_float_no_arrastra_su_error_binario():
    """`Decimal(0.1)` son 55 dígitos; pasado por `str` es 0.1. 0.1 × 12 / 31 = 0.0387... → 0.04."""
    cuota = proponer_cuota(
        fecha_inicio=date(2026, 8, 20), fecha_fin=None, periodicidad="mensual", importe=0.1, ancla=date(2026, 8, 25),
    )
    assert cuota.importe == D("0.04")


@pytest.mark.parametrize("inicio, fin", [
    (date(2026, 9, 1), None),                    # todavía no empezó
    (date(2026, 1, 1), date(2026, 7, 31)),       # ya terminó
    (date(2026, 8, 20), date(2026, 8, 10)),      # fechas al revés: no devenga nada
])
def test_si_el_contrato_no_toca_el_periodo_no_hay_cuota_y_no_una_cuota_en_cero(inicio, fin):
    assert proponer_cuota(
        fecha_inicio=inicio, fecha_fin=fin, periodicidad="mensual", importe=D("31000"), ancla=date(2026, 8, 15),
    ) is None


def test_el_ancla_es_una_fecha_cualquiera_del_periodo():
    propuestas = {
        proponer_cuota(
            fecha_inicio=date(2026, 1, 1), fecha_fin=None, periodicidad="mensual", importe=D("31000"), ancla=date(2026, 8, dia),
        )
        for dia in (1, 5, 15, 31)
    }
    assert len(propuestas) == 1


def test_una_periodicidad_desconocida_en_la_cuota_es_un_error():
    with pytest.raises(ValueError, match="semanal"):
        proponer_cuota(
            fecha_inicio=date(2026, 1, 1), fecha_fin=None, periodicidad="semanal", importe=D("1"), ancla=date(2026, 8, 15),
        )


# ── El vencimiento dentro de la cuota ──────────────────────────────────────

@pytest.mark.parametrize("ancla, dia, esperado", [
    (date(2026, 8, 15), 10, date(2026, 8, 10)),
    (date(2026, 2, 10), 31, date(2026, 2, 28)),   # un contrato con vencimiento el 31 no vence el 31 de febrero
    (date(2024, 2, 10), 31, date(2024, 2, 29)),
    (date(2026, 8, 15), None, None),              # sin día pactado no se inventa ninguno
])
def test_el_vencimiento_de_la_cuota(ancla, dia, esperado):
    cuota = proponer_cuota(
        fecha_inicio=date(2020, 1, 1), fecha_fin=None, periodicidad="mensual", importe=D("1000"), ancla=ancla, dia_vencimiento=dia,
    )
    assert cuota.fecha_vencimiento == esperado


def test_el_vencimiento_de_una_cuota_prorrateada_queda_en_el_mes_del_primer_dia_cubierto():
    cuota = proponer_cuota(
        fecha_inicio=date(2026, 8, 20), fecha_fin=None, periodicidad="mensual", importe=D("31000"),
        ancla=date(2026, 8, 25), dia_vencimiento=10,
    )
    assert cuota.fecha_emision == date(2026, 8, 20)
    assert cuota.fecha_vencimiento == date(2026, 8, 10)  # anterior a la emisión: ver `vencimiento_de`


# ── La clave de idempotencia ───────────────────────────────────────────────

def test_la_clave_es_contrato_y_desde():
    assert clave_de_periodo(7, Periodo(date(2026, 8, 1), date(2026, 8, 31))) == (7, date(2026, 8, 1))


def test_cualquier_ancla_del_mismo_periodo_da_la_misma_clave():
    claves = {
        clave_de_periodo(7, proponer_cuota(
            fecha_inicio=date(2026, 1, 1), fecha_fin=None, periodicidad="mensual", importe=D("1"), ancla=date(2026, 8, dia),
        ).periodo)
        for dia in (1, 10, 31)
    }
    assert claves == {(7, date(2026, 8, 1))}


def test_la_clave_distingue_contrato_y_periodo():
    p = Periodo(date(2026, 8, 1), date(2026, 8, 31))
    existentes = [clave_de_periodo(7, p)]
    assert ya_generada(clave_de_periodo(7, p), existentes)
    assert not ya_generada(clave_de_periodo(8, p), existentes)                                          # otro contrato
    assert not ya_generada(clave_de_periodo(7, Periodo(date(2026, 9, 1), date(2026, 9, 30))), existentes)  # otro período
    assert not ya_generada(clave_de_periodo(7, p), [])


def test_la_clave_de_la_cuota_prorrateada_es_su_desde():
    """Un contrato que arranca el 20 emite el 20: ésa es su clave, y vale una sola vez."""
    cuota = proponer_cuota(
        fecha_inicio=date(2026, 8, 20), fecha_fin=None, periodicidad="mensual", importe=D("31000"), ancla=date(2026, 8, 25),
    )
    assert clave_de_periodo("C-1", cuota.periodo) == ("C-1", date(2026, 8, 20))
