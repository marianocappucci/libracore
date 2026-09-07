"""Logs, reportes y libros IVA como factories (P9-M4): el log de actividad con
partes componibles, los reportes con su puerto, los exports fuera de `/api/` y
los generadores REGINFO."""

from __future__ import annotations

import datetime
import importlib

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore import libros_iva as li
from libracore.db import core
from libracore.db import logs as db_logs
from libracore.db.schema import init_core_schema
from libracore.libros_iva_router import build_libros_iva_export_router, build_libros_iva_router
from libracore.logs_router import TIPO_META, build_logs_export_router, build_logs_router
from libracore.reportes import MEDIO_LABEL, PuertoDeReportes, pivot_caja_medios, totales_por_medio
from libracore.reportes_router import build_reportes_export_router, build_reportes_router

HOY = datetime.date.today().isoformat()
ADMIN = {"x-rol": "admin"}


def _gate_admin(x_rol: str = Header(default="")):
    if x_rol != "admin":
        raise HTTPException(403, "solo administradores")


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as cm
    importlib.reload(cm)
    core.configure(db_path=str(tmp_path / "lrl.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.execute("INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (7, 'ana', 'Ana', 'x', 'admin')")
    conn.execute("INSERT INTO ventas (numero, fecha, items, subtotal, descuento, total, cliente_nombre, usuario_id, estado) "
                 "VALUES ('V-1', ?, '[]', 100, 0, 100, 'Cli', 7, 'cobrada')", (HOY,))
    conn.execute("INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, medio_pago, usuario_id) VALUES (?, 'ingreso', 'Venta V-1', 100, 'efectivo', 7)", (HOY,))
    conn.execute("INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, medio_pago) VALUES (?, 'egreso', 'Hielo', 30, 'efectivo')", (HOY,))
    conn.execute("INSERT INTO turnos_caja (usuario_id, apertura, monto_inicial) VALUES (7, ?, 500)", (HOY + " 08:00:00",))
    conn.commit()
    conn.close()
    cm.save({"empresa_cuit": "20111111112"})
    yield cm
    core._db_path = None


# ── db.logs: partes componibles ──────────────────────────────────────────


def test_las_partes_del_log_se_componen(entorno):
    filas = db_logs.get_actividad_log()
    tipos = sorted({f["tipo"] for f in filas})
    assert tipos == ["caja", "turno", "venta"]
    # Sólo las partes del core: la venta desaparece.
    solo_core = db_logs.get_actividad_log(partes=db_logs.PARTES_CORE)
    assert sorted({f["tipo"] for f in solo_core}) == ["caja", "turno"]
    # La fecha del turno es TEXT (substr), no DATE: filtra como las otras.
    assert db_logs.get_actividad_log(tipos=["turno"], desde=HOY, hasta=HOY)[0]["fecha"] == HOY
    assert db_logs.get_actividad_log(usuario_id=7, tipos=["caja"])[0]["descripcion"] == "ingreso: Venta V-1"
    assert db_logs.get_actividad_log(usuario_id=99) == []
    assert db_logs.get_actividad_count(turno_id=1) == 1
    with core.get_connection() as conn:
        assert db_logs.get_actividad_count(conn=conn, partes=db_logs.PARTES_CORE) == 3


# ── logs router ──────────────────────────────────────────────────────────


def test_logs_router_y_export(entorno):
    vistas = []

    def actividad(**kw):
        vistas.append(kw)
        return db_logs.get_actividad_log(**kw)

    app = FastAPI()
    app.include_router(build_logs_router(usuarios=lambda: [{"id": 7, "nombre": "Ana"}], actividad=actividad,
                                         actividad_count=lambda **kw: 250))
    app.include_router(build_logs_export_router(solo_admin=_gate_admin, nombre_archivo="logs_x.csv", actividad=actividad))
    client = TestClient(app)
    r = client.get("/api/logs?page=2&tipo=caja,venta&usuario_id=7").json()
    assert r["page"] == 2 and r["total"] == 250 and r["total_pages"] == 3
    assert r["tipo_meta"] == TIPO_META and r["usuarios"] == [{"id": 7, "nombre": "Ana"}] and "auth_log" in r
    assert vistas[-1]["offset"] == 100 and vistas[-1]["tipos"] == ["caja", "venta"] and vistas[-1]["usuario_id"] == 7
    assert client.get("/admin/logs/export").status_code == 403
    r = client.get("/admin/logs/export?tipo=caja", headers=ADMIN)
    assert r.status_code == 200 and r.headers["content-disposition"] == "attachment; filename=logs_x.csv"
    assert r.text.splitlines()[0].startswith("Fecha,Tipo") and "Hielo" in r.text
    assert vistas[-1]["limit"] == 5000


# ── reportes ─────────────────────────────────────────────────────────────


def test_pivot_y_totales():
    rows = [
        {"caja_id": 1, "caja_nombre": "A", "medio": "efectivo", "tipo": "ingreso", "operaciones": 2, "total": 100},
        {"caja_id": 1, "caja_nombre": "A", "medio": "efectivo", "tipo": "egreso", "operaciones": 1, "total": 30},
        {"caja_id": 2, "caja_nombre": "B", "medio": "transferencia", "tipo": "ingreso", "operaciones": 1, "total": 50},
    ]
    p = pivot_caja_medios(rows)
    assert p[0]["saldo"] == 70 and p[0]["medios"]["efectivo"]["ingresos_ops"] == 2 and p[1]["total_ingresos"] == 50
    t = totales_por_medio(p)
    assert list(t) == ["efectivo", "transferencia"] and t["efectivo"]["egresos"] == 30
    assert MEDIO_LABEL["sin_especificar"] == "Sin especificar" and MEDIO_LABEL["efectivo"] == "Efectivo"


def test_reportes_router_con_el_puerto_del_producto(entorno):
    limites = []

    def productos_top(d, h, limit=20):
        limites.append(limit)
        return [{"nombre": "Yerba", "cantidad": 2, "total": 100.0}]

    puerto = PuertoDeReportes(
        ventas=lambda d, h, a: [{"periodo": d, "cantidad": 1, "total": 100.0}],
        medios_pago=lambda d, h: [{"medio": "efectivo", "operaciones": 1, "total": 100.0}],
        productos_top=productos_top,
        stock_bajo=lambda: [{"id": 1, "nombre": "Yerba", "stock_actual": 0, "stock_minimo": 5}],
        resumen=lambda d, h: {"ventas_cantidad": 1, "ventas_total": 100.0},
    )
    app = FastAPI()
    app.include_router(build_reportes_router(reportes=puerto))
    app.include_router(build_reportes_export_router(sesion=_gate_admin, reportes=puerto))
    client = TestClient(app)
    r = client.get("/api/reportes?desde=2026-09-01&hasta=2026-09-30&agrupacion=mes").json()
    assert r["ventas_ts"][0]["periodo"] == "2026-09-01" and r["agrupacion"] == "mes"
    assert r["caja"][0]["tipo"] in ("ingreso", "egreso") and r["stock_bajo"][0]["nombre"] == "Yerba"
    assert r["medio_label"]["efectivo"] == "Efectivo" and r["resumen"]["ventas_total"] == 100.0
    # Sin fechas, el mes en curso.
    assert client.get("/api/reportes").json()["hasta"] == HOY
    cm = client.get("/api/reportes/caja-medios").json()
    assert cm["cajas"][0]["saldo"] == 70.0 and cm["totales"]["efectivo"]["ingresos"] == 100.0 and cm["cajas_config"][0]["nombre"]
    assert client.get("/reportes/export/ventas").status_code == 403
    r = client.get("/reportes/export/ventas?desde=2026-09-01&hasta=2026-09-30", headers=ADMIN)
    assert r.headers["content-disposition"].endswith('ventas_2026-09-01_2026-09-30.csv"') and "periodo,cantidad,total" in r.text
    assert "efectivo,1,100.0" in client.get("/reportes/export/medios", headers=ADMIN).text
    r = client.get("/reportes/export/productos", headers=ADMIN)
    assert "Yerba,2,100.0" in r.text and limites[-1] == 500  # el export pide 500, la pantalla 20
    r = client.get("/reportes/caja-medios/export", headers=ADMIN)
    assert "Caja,Medio de cobro" in r.text and "Efectivo" in r.text


def test_los_reportes_por_default_leen_las_tablas_del_core(entorno):
    app = FastAPI()
    app.include_router(build_reportes_router())
    r = TestClient(app).get("/api/reportes").json()
    assert r["resumen"]["ventas_cantidad"] == 1 and r["ventas_ts"][0]["total"] == 100.0


# ── libros IVA ───────────────────────────────────────────────────────────


def test_generadores_reginfo():
    fac = {"tipo": 6, "punto_venta": 5, "numero": 11, "fecha": "2026-09-06", "cliente_cuit": "20-11111111-2",
           "cliente_razon": "Ana", "subtotal": 1000.0, "iva_amount": 210.0, "total": 1210.0, "items": []}
    nc = {**fac, "tipo": 8, "numero": 12}
    cbte = li._ventas_cbte([fac, nc]).splitlines()
    assert cbte[0].startswith("20260906|06|00005|00000011|00000011|80|20111111112|Ana|1210.00|")
    assert "|-1210.00|" in cbte[1] and cbte[0].split("|")[17:19] == ["1", " "]
    ali = li._ventas_alicuotas([fac, {**fac, "tipo": 11}]).splitlines()
    assert ali == ["06|00005|00000011|5|1000.00|21.00|210.00"]
    rv = li._resumen_ventas([fac, nc])
    assert rv["cbtes"] == 2 and rv["total"] == 0.0 and rv["por_tasa"][21.0]["cbtes"] == 2
    egr = {"tipo_comprobante": "factura", "numero": "0003-00001842", "fecha": "2026-09-06", "proveedor_cuit": "30712345678",
           "proveedor_nombre": "Acme", "monto_neto": 1000.0, "iva_monto": 105.0, "iva_pct": 0.105, "total": 1105.0}
    assert li._alicuota_de_egreso(1000.0, 105.0) == 10.5 and li._alicuota_de_egreso(0, 5) == 0.0
    cc = li._compras_cbte([egr, {**egr, "tipo_comprobante": "ticket", "numero": "77", "iva_monto": 0}]).splitlines()
    assert cc[0].startswith("20260906|01|00003|00001842|00001842|80|30712345678|Acme|1105.00|") and "|1|" in cc[0]
    assert cc[1].startswith("20260906|11|00001|00000077|") and "|0|N|" in cc[1]
    assert li._compras_alicuotas([egr]).splitlines() == ["01|00003|00001842|4|1000.00|10.50|105.00"]
    rc = li._resumen_compras([egr])
    assert rc["por_tasa"][10.5]["neto"] == 1000.0 and rc["total"] == 1105.0
    assert li._parse_num_egreso("abc") == (1, 0) and li._parse_num_egreso("x-y") == (1, 0)
    assert li._cod_doc("12345678") == "96" and li._cod_doc("") == "99"
    assert li._get_iva_rate({"items": [{"iva_pct": 10.5}]}) == 10.5 and li._get_iva_rate({}) == 0.0


def test_libros_iva_router_y_exports(entorno):
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, cliente_iva_cond, "
            "items, subtotal, iva_amount, total, ambiente, cae) VALUES (6, 1, 1, ?, '20111111112', 'Ana', 1, '[]', 1000, 210, 1210, 'produccion', '123')", (HOY,))
        conn.execute(
            "INSERT INTO egresos (fecha, concepto, total, tipo_comprobante, numero, monto_neto, iva_pct, iva_monto) "
            "VALUES (?, 'Harina', 1210, 'factura', '0001-00000009', 1000, 0.21, 210)", (HOY,))
    app = FastAPI()
    app.include_router(build_libros_iva_router())
    app.include_router(build_libros_iva_export_router(solo_admin=_gate_admin))
    client = TestClient(app)
    r = client.get("/api/libros-iva").json()
    assert r["empresa_cuit"] == "20111111112" and r["hasta"] == HOY
    assert r["resumen_v"]["cbtes"] == len(r["facturas"]) and r["resumen_c"]["total"] == 1210.0
    assert client.get("/libros-iva/export/ventas-cbte").status_code == 403
    for ruta, prefijo in (("ventas-cbte", "REGINFO_CV_VENTAS_CBTE_"), ("ventas-alicuotas", "REGINFO_CV_VENTAS_ALICUOTAS_"),
                          ("compras-cbte", "REGINFO_CV_COMPRAS_CBTE_"), ("compras-alicuotas", "REGINFO_CV_COMPRAS_ALICUOTAS_")):
        r = client.get(f"/libros-iva/export/{ruta}", headers=ADMIN)
        assert r.status_code == 200 and prefijo in r.headers["content-disposition"]
    assert "0001|00000009" in client.get("/libros-iva/export/compras-alicuotas", headers=ADMIN).text
