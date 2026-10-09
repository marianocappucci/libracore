"""`libracore.fechas.rango_por_dia` (ADR-037): el rango se mide por **día**, con o sin hora.

Las columnas `fecha` del motor son TEXT libre. `fecha <= '2026-10-09'` deja afuera
`'2026-10-09 13:00:00'` (como texto es mayor), y el parche viejo de tesorería
(`hasta + " 23:59:59"`) dejaba afuera `'2026-10-09T13:00'` ('T' > ' '). Acá se fija la
función pura; que cada consulta del motor la use lo fija `tests/db/test_rango_por_dia.py`.
"""
import pytest

from libracore.fechas import dia_iso, rango_por_dia


def test_desde_y_hasta_sin_hora():
    assert rango_por_dia("fecha", "2026-10-01", "2026-10-09") == (
        ["fecha >= ?", "fecha < ?"], ["2026-10-01", "2026-10-10"])


def test_el_rango_de_un_solo_dia_incluye_ese_dia():
    # [d, d] es el día d entero: >= d y < d+1.
    assert rango_por_dia("m.fecha", "2026-10-09", "2026-10-09") == (
        ["m.fecha >= ?", "m.fecha < ?"], ["2026-10-09", "2026-10-10"])


@pytest.mark.parametrize("hora", ["2026-10-09 13:00:00", "2026-10-09T13:00", "2026-10-09T13:00:00.123", "2026-10-09 "])
def test_los_extremos_con_hora_se_miden_por_dia(hora):
    # El `desde` con hora no recorta el día; el `hasta` con hora no deja afuera el resto del día.
    assert rango_por_dia("fecha", hora, hora) == (
        ["fecha >= ?", "fecha < ?"], ["2026-10-09", "2026-10-10"])


def test_fin_de_mes_y_de_anio():
    assert rango_por_dia("fecha", None, "2026-10-31")[1] == ["2026-11-01"]
    assert rango_por_dia("fecha", None, "2026-12-31")[1] == ["2027-01-01"]
    assert rango_por_dia("fecha", None, "2028-02-28")[1] == ["2028-02-29"]  # bisiesto
    assert rango_por_dia("fecha", None, "2027-02-28")[1] == ["2027-03-01"]


def test_desde_solo_y_hasta_solo():
    assert rango_por_dia("fecha", "2026-10-09", None) == (["fecha >= ?"], ["2026-10-09"])
    assert rango_por_dia("fecha", None, "2026-10-09") == (["fecha < ?"], ["2026-10-10"])


@pytest.mark.parametrize("vacio", [None, ""])
def test_vacio_o_none_no_pone_condicion(vacio):
    assert rango_por_dia("fecha", vacio, vacio) == ([], [])


@pytest.mark.parametrize("raro", ["mañana", "2026-13-40", "2026-02-30", "20261009", "2026-10-09x", "09/10/2026"])
def test_un_extremo_que_no_es_fecha_iso_se_usa_tal_cual(raro):
    # No se rompe a un llamador raro: queda el comportamiento de antes, `>=` / `<=` sobre el texto.
    assert rango_por_dia("fecha", raro, raro) == (["fecha >= ?", "fecha <= ?"], [raro, raro])


def test_un_extremo_valido_y_el_otro_no():
    assert rango_por_dia("fecha", "2026-10-01", "fin") == (
        ["fecha >= ?", "fecha <= ?"], ["2026-10-01", "fin"])


def test_el_ultimo_dia_representable_no_tiene_siguiente():
    # `date.max + 1 día` desborda: se queda en `<=` sobre el día, no revienta.
    assert rango_por_dia("fecha", None, "9999-12-31") == (["fecha <= ?"], ["9999-12-31"])


def test_los_placeholders_son_signos_de_pregunta_y_hay_uno_por_parametro():
    # El estilo del repo: `?` (el adaptador de PostgreSQL los traduce). Nunca un valor pegado al SQL.
    conds, params = rango_por_dia("fecha", "2026-10-01", "2026-10-09")
    assert all(c.count("?") == 1 and "2026" not in c for c in conds)
    assert len(params) == sum(c.count("?") for c in conds)


@pytest.mark.parametrize("valor,esperado", [
    ("2026-10-09", (2026, 10, 9)),
    ("2026-10-09 13:00:00", (2026, 10, 9)),
    ("2026-10-09T13:00", (2026, 10, 9)),
    ("  2026-10-09 ", (2026, 10, 9)),
])
def test_dia_iso_lee_el_dia(valor, esperado):
    d = dia_iso(valor)
    assert (d.year, d.month, d.day) == esperado


@pytest.mark.parametrize("valor", [None, "", "2026-10", "2026-10-9", "20261009", "2026-10-09x", "2026-02-30", 20261009])
def test_dia_iso_devuelve_none_si_no_es_una_fecha(valor):
    assert dia_iso(valor) is None
