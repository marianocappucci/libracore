"""Los índices de ajuste (ADR-085): el catálogo, la cuenta, la tabla y la descarga.

Lo que se fija acá:
- la cuenta es exacta (`Decimal`) y redondea a centavos con HALF_UP una sola vez, al final;
- el IPC por niveles y el IPC por variaciones mensuales dan el mismo ajuste con datos coherentes;
- si falta un valor no hay ajuste, y el error nombra **todas** las fechas que faltan;
- el valor se guarda tal cual lo publicó la fuente (once decimales del CER) en los dos motores;
- una descarga no pisa lo cargado a mano, y lo informa;
- la descarga del BCRA pagina, los mensuales se fechan el primero del mes y una fuente caída no frena a las otras;
- la `0030` crea la tabla en una base de antes y no se baja.
"""
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from libracore import indices_ajuste as ia
from libracore import indices_descarga as descarga
from libracore.db import core
from libracore.db import indices_ajuste as db
from libracore.db.schema import init_core_schema

RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["sqlite", "postgres"])
def base(request, tmp_path):
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
        core.configure(db_path=str(tmp_path / "indices.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.commit()
    yield request.param
    core._db_path = None
    core._database_url = None


# ── la cuenta, pura ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("base_, ajuste, esperado", [
    ("1", "37.09", Decimal("37.09")),
    ("36.5", "37.09", Decimal("37.09") / Decimal("36.5")),
])
def test_coeficiente_por_niveles(base_, ajuste, esperado):
    assert ia.coeficiente_por_niveles(base_, ajuste) == esperado


def test_coeficiente_por_niveles_rechaza_cero():
    with pytest.raises(ValueError):
        ia.coeficiente_por_niveles("0", "1")


def test_coeficiente_por_variaciones_compone_los_meses():
    assert ia.coeficiente_por_variaciones(["1.9", "1.7", "2.1"]) == (
        Decimal("1.019") * Decimal("1.017") * Decimal("1.021"))
    assert ia.coeficiente_por_variaciones([]) == 1


@pytest.mark.parametrize("importe, coef, nuevo", [
    ("100000", Decimal("37.09"), Decimal("3709000.00")),
    # HALF_UP y no el redondeo del banquero: 0,125 → 0,13.
    ("1", Decimal("0.125"), Decimal("0.13")),
    ("250000", Decimal("1.0412345"), Decimal("260308.63")),
])
def test_aplicar_redondea_a_centavos_al_final(importe, coef, nuevo):
    r = ia.aplicar(importe, coef)
    assert r["importe_nuevo"] == nuevo
    assert r["coeficiente"] == coef            # sin redondear: quien lo imprime elige los decimales


def test_aplicar_porcentaje():
    r = ia.aplicar_porcentaje("200000", "8.5")
    assert r["importe_nuevo"] == Decimal("217000.00")
    assert r["variacion_pct"] == Decimal("8.50")
    assert r["metodo"] == "porcentaje"


@pytest.mark.parametrize("clave, fecha, esperada", [
    ("icl", "2026-10-26", "2026-10-26"),
    ("ipc", "2026-09-30", "2026-09-01"),
    ("ipc_variacion", "2026-02-28", "2026-02-01"),
])
def test_fecha_del_indice(clave, fecha, esperada):
    assert ia.fecha_del_indice(clave, fecha) == esperada


def test_meses_entre_cruza_el_anio():
    from datetime import date
    assert ia._meses_entre(date(2025, 11, 15), date(2026, 2, 1)) == ["2025-12-01", "2026-01-01", "2026-02-01"]
    assert ia._meses_entre(date(2026, 3, 1), date(2026, 3, 31)) == []


def test_indice_desconocido():
    with pytest.raises(ia.IndiceDesconocido):
        ia.indice("ipc_rosario")


def test_el_catalogo_dice_de_donde_sale_cada_uno():
    assert {c for c, i in ia.INDICES.items() if i.fuente == ia.FUENTE_MANUAL} == {"casa_propia"}
    assert set(descarga.DESCARGABLES) == set(ia.INDICES) - {"casa_propia"}
    assert ia.INDICES["icl"].serie == 40


# ── la tabla ───────────────────────────────────────────────────────────────────────────────────────


def test_guardar_y_leer_exacto(base):
    assert db.guardar("cer", "2026-10-26", "860.18437604432", "bcra") == "insertado"
    fila = db.obtener("cer", "2026-10-26")
    assert fila["valor"] == Decimal("860.18437604432")
    assert db.guardar("cer", "2026-10-26", Decimal("860.184376044320"), "bcra") == "igual"
    assert db.guardar("cer", "2026-10-26", "860.2", "bcra") == "actualizado"
    assert db.obtener("cer", "2026-10-26")["valor"] == Decimal("860.2")


def test_guardar_acepta_coma_decimal(base):
    db.guardar("icl", "2026-10-26", "37,09", db.FUENTE_MANUAL)
    assert db.obtener("icl", "2026-10-26")["valor"] == Decimal("37.09")


@pytest.mark.parametrize("valor", [37.09, "0", "-1", "abc", "NaN"])
def test_guardar_rechaza_lo_que_no_es_un_valor(base, valor):
    with pytest.raises(db.ValorInvalido):
        db.guardar("icl", "2026-10-26", valor, "manual")


def test_lo_manual_no_lo_pisa_una_descarga(base):
    db.guardar("icl", "2026-10-26", "37.10", db.FUENTE_MANUAL)
    assert db.guardar("icl", "2026-10-26", "37.09", "bcra", pisar_manual=False) == "manual_distinto"
    assert db.obtener("icl", "2026-10-26")["valor"] == Decimal("37.10")
    # Una persona sí puede corregirlo.
    assert db.guardar("icl", "2026-10-26", "37.09", db.FUENTE_MANUAL) == "actualizado"


def test_ultimo_hasta_listar_y_borrar(base):
    for f, v in (("2026-10-22", "37.00"), ("2026-10-23", "37.02"), ("2026-10-26", "37.09")):
        db.guardar("icl", f, v, "bcra")
    assert db.ultimo_hasta("icl", "2026-10-25")["fecha"] == "2026-10-23"
    assert db.ultimo_hasta("icl")["fecha"] == "2026-10-26"
    assert db.ultimo_hasta("icl", "2026-01-01") is None
    assert [x["fecha"] for x in db.listar("icl", desde="2026-10-23")] == ["2026-10-23", "2026-10-26"]
    assert db.borrar("icl", "2026-10-23") is True
    assert db.borrar("icl", "2026-10-23") is False


# ── ajustar, contra la tabla ────────────────────────────────────────────────────────────────────────


def test_ajustar_por_icl(base):
    db.guardar("icl", "2025-10-26", "30.50", "bcra")
    db.guardar("icl", "2026-10-26", "37.09", "bcra")
    r = ia.ajustar("icl", "500000", "2025-10-26", "2026-10-26")
    assert r["coeficiente"] == Decimal("37.09") / Decimal("30.50")
    assert r["importe_nuevo"] == Decimal("608032.79")
    assert [v["fecha"] for v in r["valores"]] == ["2025-10-26", "2026-10-26"]
    assert r["indice"] == "icl" and r["metodo"] == "indice"


def test_ajustar_sin_un_valor_nombra_todas_las_fechas_que_faltan(base):
    with pytest.raises(ia.IndiceFaltante) as e:
        ia.ajustar("icl", "500000", "2025-10-26", "2026-10-26")
    assert e.value.fechas == ["2025-10-26", "2026-10-26"]
    assert "2025-10-26" in str(e.value)


def test_ajustar_al_reves_no_se_puede(base):
    with pytest.raises(ValueError):
        ia.ajustar("icl", "1", "2026-10-26", "2026-01-01")


def test_ipc_por_niveles_y_por_variaciones_dan_lo_mismo(base):
    """Con datos coherentes, componer las variaciones de los meses posteriores al de base es dividir los niveles."""
    niveles = {"2026-05-01": Decimal("11000"), "2026-06-01": Decimal("11220"), "2026-07-01": Decimal("11444.4"),
               "2026-08-01": Decimal("11673.288")}
    for f, v in niveles.items():
        db.guardar("ipc", f, v, "datos_gob_ar")
    for f in ("2026-06-01", "2026-07-01", "2026-08-01"):
        db.guardar("ipc_variacion", f, "2", "bcra")             # 2 % cada mes, que es lo que dicen los niveles
    por_nivel = ia.ajustar("ipc", "300000", "2026-05-15", "2026-08-20")
    por_variacion = ia.ajustar("ipc_variacion", "300000", "2026-05-15", "2026-08-20")
    assert por_nivel["importe_nuevo"] == por_variacion["importe_nuevo"] == Decimal("318362.40")
    assert [v["fecha"] for v in por_variacion["valores"]] == ["2026-06-01", "2026-07-01", "2026-08-01"]


def test_ipc_variacion_sin_un_mes(base):
    db.guardar("ipc_variacion", "2026-06-01", "1.9", "bcra")
    with pytest.raises(ia.IndiceFaltante) as e:
        ia.ajustar("ipc_variacion", "1000", "2026-05-01", "2026-08-01")
    assert e.value.fechas == ["2026-07-01", "2026-08-01"]


# ── la descarga, sin red ─────────────────────────────────────────────────────────────────────────────


def _bcra(detalle, count=None):
    return {"status": 200, "metadata": {"resultset": {"count": len(detalle) if count is None else count}},
            "results": [{"idVariable": 40, "detalle": detalle}]}


def test_descarga_del_bcra_pagina_y_guarda(base, monkeypatch):
    monkeypatch.setattr(descarga, "_PAGINA_BCRA", 2)
    paginas = {
        0: _bcra([{"fecha": "2026-10-26", "valor": Decimal("37.09000000000")},
                  {"fecha": "2026-10-25", "valor": Decimal("37.07")}], count=3),
        2: _bcra([{"fecha": "2026-10-24", "valor": Decimal("37.05")}], count=3),
    }
    pedidos = []

    def obtener(url, params):
        pedidos.append((url, params))
        return paginas[params["offset"]]

    r = descarga.sincronizar("icl", desde="2026-10-24", hasta="2026-10-26", obtener=obtener)
    assert pedidos[0][0].endswith("/estadisticas/v4.0/monetarias/40")
    assert [p["offset"] for _, p in pedidos] == [0, 2]
    assert (r["leidos"], r["insertados"], r["iguales"]) == (3, 3, 0)
    assert db.obtener("icl", "2026-10-26")["valor"] == Decimal("37.09")
    # La segunda vez no escribe nada nuevo.
    assert descarga.sincronizar("icl", desde="2026-10-24", hasta="2026-10-26", obtener=obtener)["iguales"] == 3


def test_descarga_mensual_del_bcra_se_fecha_el_primero(base):
    def obtener(url, params):
        return _bcra([{"fecha": "2026-09-30", "valor": Decimal("1.9")}, {"fecha": "2026-08-31", "valor": Decimal("1.7")}])

    descarga.sincronizar("ipc_variacion", desde="2026-08-01", hasta="2026-10-26", obtener=obtener)
    assert [x["fecha"] for x in db.listar("ipc_variacion")] == ["2026-08-01", "2026-09-01"]


def test_descarga_no_pisa_lo_manual_y_lo_informa(base):
    db.guardar("icl", "2026-10-26", "37.10", db.FUENTE_MANUAL)
    r = descarga.sincronizar("icl", desde="2026-10-26", hasta="2026-10-26",
                             obtener=lambda u, p: _bcra([{"fecha": "2026-10-26", "valor": Decimal("37.09")}]))
    assert r["manuales_distintos"] == [{"fecha": "2026-10-26", "manual": Decimal("37.10"),
                                        "publicado": Decimal("37.09")}]
    assert db.obtener("icl", "2026-10-26")["valor"] == Decimal("37.10")


def test_descarga_de_datos_gob_ar_salta_los_vacios(base):
    def obtener(url, params):
        assert params["ids"] == "148.3_INIVELNAL_DICI_M_26"
        return {"data": [["2026-07-01", Decimal("12076.3937")], ["2026-08-01", Decimal("12276.766")],
                         ["2026-09-01", None]]}

    r = descarga.sincronizar("ipc", desde="2026-07-01", hasta="2026-10-26", obtener=obtener)
    assert r["insertados"] == 2
    assert db.obtener("ipc", "2026-08-01")["valor"] == Decimal("12276.766")


@pytest.mark.parametrize("respuesta", [{"results": [{"sin_detalle": []}]}, {"results": "x"}])
def test_una_respuesta_rara_es_fuente_no_disponible(base, respuesta):
    with pytest.raises(descarga.FuenteNoDisponible):
        descarga.sincronizar("icl", desde="2026-10-01", obtener=lambda u, p: respuesta)


def test_casa_propia_no_se_baja(base):
    with pytest.raises(ValueError):
        descarga.sincronizar("casa_propia")


def test_una_fuente_caida_no_frena_a_las_otras(base):
    def obtener(url, params):
        if "datos.gob.ar" in url:
            raise descarga.FuenteNoDisponible("datos.gob.ar contestó 503")
        return _bcra([{"fecha": "2026-10-26", "valor": Decimal("2")}])

    r = descarga.sincronizar_todos(desde="2026-10-26", hasta="2026-10-26", obtener=obtener)
    assert set(r["errores"]) == {"ipc"}
    assert set(r["resultados"]) == set(descarga.DESCARGABLES) - {"ipc"}


def test_main_sale_con_error_si_alguna_fuente_fallo(base, monkeypatch, capsys):
    monkeypatch.setattr(descarga, "_obtener_http", lambda u, p: (_ for _ in ()).throw(
        descarga.FuenteNoDisponible("sin red")))
    assert descarga.main(["icl", "--desde", "2026-10-26"]) == 1
    assert "sin red" in capsys.readouterr().out


# ── la migración ──────────────────────────────────────────────────────────────────────────────────────


def _alembic(destino: str, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=RAIZ, env={**os.environ, "DATABASE_URL": destino, "PYTHONPATH": str(RAIZ)}, capture_output=True, text=True)


def test_la_0030_crea_la_tabla_en_una_base_de_antes_y_no_se_baja(tmp_path):
    destino = str(tmp_path / "antes.db")
    assert _alembic(destino, "upgrade", "0029_cola_arca").returncode == 0
    # La 0001 de hoy ya la crea con `init_core_schema`: se la sacamos para tener una base de antes.
    conn = sqlite3.connect(destino)
    conn.execute("DROP TABLE indices_ajuste")
    conn.commit()
    conn.close()

    assert _alembic(destino, "upgrade", "head").returncode == 0
    conn = sqlite3.connect(destino)
    try:
        columnas = {f[1] for f in conn.execute("PRAGMA table_info(indices_ajuste)").fetchall()}
        assert {"indice", "fecha", "valor", "fuente", "usuario_id", "created_at", "updated_at"} <= columnas
        indices = {f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "idx_indices_ajuste_fecha" in indices
        assert conn.execute("SELECT COUNT(*) FROM indices_ajuste").fetchone()[0] == 0
    finally:
        conn.close()

    assert _alembic(destino, "upgrade", "head").returncode == 0
    bajar = _alembic(destino, "downgrade", "0029_cola_arca")
    assert bajar.returncode != 0 and "No se baja" in bajar.stderr
