"""
Smoke tests de comportamiento para los 14 módulos "core puro" migrados a
libracore.db (Fase 3, migración real). Cada uno se verificó ya como
byte-idéntico entre Contalibra y Restolibra durante el split por producto
— acá se prueba que, migrados y corriendo contra el schema componible, se
comportan igual que antes.
"""
import pytest

from libracore.db import core
from libracore.db.schema import init_core_schema
from libracore.db import tesoreria, caja, egresos, modulos as modulos_mod
from libracore.db import listas_precio, turnos, dashboard, logs, arca_config
from libracore.db import cuenta_corriente, libros_iva, reportes, remitos_presupuestos


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "tier1_test.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


# `crear_usuario` es una fixture de tests/db/conftest.py — reemplaza al
# `usuarios.create_usuario` que se fue con el modulo de auth.


def test_tesoreria(conn):
    cid = tesoreria.create_cuenta_tesoreria("Banco Test", "banco", saldo_inicial=1000)
    tesoreria.create_movimiento_tesoreria("2026-07-14", cid, "ingreso", 200, concepto="dep")
    assert tesoreria.get_cuenta_tesoreria(cid)["saldo"] == 1200.0


def test_caja(conn):
    cid = caja.create_caja_config("Caja 1", "", ["efectivo"])
    caja.set_default_caja(cid)
    mid = caja.create_caja_movimiento("2026-07-14", "ingreso", "venta test", 1000, caja_id=cid, medio_pago="efectivo")
    assert mid is not None
    resumen = caja.get_caja_resumen()
    assert resumen["ingresos"] == 1000.0


def test_caja_movimiento_idempotencia_por_referencia_scopeada_a_factura(conn):
    cid = caja.create_caja_config("Caja 1", "", ["transferencia"])
    caja.set_default_caja(cid)

    # Misma referencia bancaria cubriendo el cobro de dos facturas distintas:
    # ambos movimientos deben insertarse (bug real: antes el guard de
    # idempotencia miraba solo la referencia, sin factura_id, y el segundo
    # se descartaba silenciosamente aunque fuera una factura distinta).
    mid1 = caja.create_caja_movimiento(
        "2026-07-18", "ingreso", "Cobro factura 37", 95000, caja_id=cid,
        medio_pago="transferencia", referencia="169339948070", factura_id=48,
    )
    mid2 = caja.create_caja_movimiento(
        "2026-07-18", "ingreso", "Cobro factura 42", 90000, caja_id=cid,
        medio_pago="transferencia", referencia="169339948070", factura_id=54,
    )
    assert mid1 != mid2
    resumen = caja.get_caja_resumen()
    assert resumen["ingresos"] == 185000.0

    # La misma referencia reenviada para la MISMA factura sigue deduplicando
    # (reintento de una notificación ya procesada, comportamiento original).
    mid1_retry = caja.create_caja_movimiento(
        "2026-07-18", "ingreso", "Cobro factura 37", 95000, caja_id=cid,
        medio_pago="transferencia", referencia="169339948070", factura_id=48,
    )
    assert mid1_retry == mid1
    assert caja.get_caja_resumen()["ingresos"] == 185000.0


def test_egresos(conn):
    cid = caja.create_caja_config("Caja", "", ["efectivo"])
    egresos.create_categoria_egreso("Insumos")  # el egreso la referencia por nombre
    pid = egresos.create_proveedor("Proveedor Test", cuit_dni="20111111111")
    eid = egresos.create_egreso("2026-07-14", "compra", 500, proveedor_id=pid,
                                 proveedor_nombre="Proveedor Test", categoria="Insumos", monto_neto=500)
    egresos.create_pago_egreso(eid, "2026-07-14", 500, caja_id=cid, medio_pago="efectivo")
    assert egresos.get_egreso(eid)["estado"] == "pagado"


def test_modulos(conn):
    # El seed de la lista de módulos es específico de cada producto (vive en
    # su propio init_db(), no en init_core_schema) — acá se simula.
    conn.execute("INSERT INTO modulos (modulo, habilitado, plan) VALUES ('clientes', 1, 'basico')")
    conn.commit()
    m = modulos_mod.get_modulos()
    assert m.get("clientes") is True


def test_listas_precio(conn):
    lid = listas_precio.create_lista_precio("Lista test")
    assert lid in [l["id"] for l in listas_precio.get_all_listas_precio()]


def test_turnos(conn, crear_usuario):
    uid = crear_usuario("mozo2")
    tid = turnos.create_turno(uid, 1000)
    assert turnos.get_turno_activo(uid) is not None
    turnos.cerrar_turno(tid, 1000)
    assert turnos.get_turno(tid)["estado"] == "cerrado"


def test_dashboard(conn):
    d = dashboard.get_dashboard_data("2026-01-01", "2026-12-31")
    assert "facturado_mes" in d


def test_logs(conn):
    logs.registrar_auth_event("login_fallido", "mozo1", ip="1.2.3.4")
    assert logs.contar_login_fallidos_recientes("1.2.3.4") == 1
    assert logs.get_actividad_count() >= 0


def test_arca_config(conn):
    arca_config.crear_arca_config("MiEmpresa", "20111111111", 1, "/tmp/clave.key", "/tmp/cert.crt")
    assert arca_config.obtener_arca_config("MiEmpresa") is not None


def test_cuenta_corriente(conn):
    cid = caja.create_caja_config("Caja", "", ["efectivo"])
    with conn as c:
        c.execute(
            "INSERT INTO clients (name, cuit_dni) VALUES (?,?)",
            ("Cliente CC", "20222222222"),
        )
        clid = c.execute("SELECT id FROM clients WHERE name='Cliente CC'").fetchone()[0]
    cuenta_corriente.create_cc_pago(clid, 500, "2026-07-14", "Pago a cuenta", "", "efectivo", cid, None)
    assert cuenta_corriente.get_cc_saldo(clid) == -500.0


def test_libros_iva(conn):
    assert libros_iva.get_facturas_para_iva("2026-01-01", "2026-12-31") == []
    assert libros_iva.get_egresos_para_iva("2026-01-01", "2026-12-31") == []


def test_reportes(conn):
    r = reportes.get_reporte_resumen()
    assert r["ventas_cantidad"] == 0
    assert reportes.get_reporte_stock_bajo() == []


def test_remitos_presupuestos(conn):
    with conn as c:
        c.execute("INSERT INTO clients (name, cuit_dni) VALUES (?,?)", ("Cliente R", "20333333333"))
        clid = c.execute("SELECT id FROM clients WHERE name='Cliente R'").fetchone()[0]
    rid = remitos_presupuestos.create_remito(
        remitos_presupuestos.get_next_remito_number(), "2026-07-14", clid, "Cliente R",
        "Calle 123", "20333333333", "r@x.com", "", [{"nombre": "Item", "qty": 1, "precio": 100}],
        100, 0.21, 21, 121,
    )
    assert remitos_presupuestos.get_remito(rid)["number"].startswith("0001-")

    pid = remitos_presupuestos.create_presupuesto(
        remitos_presupuestos.get_next_presupuesto_number(), "2026-07-14", "2026-08-14", clid,
        "Cliente R", "Calle 123", "20333333333", "r@x.com", "",
        [{"nombre": "Item", "qty": 1, "precio": 100}], 100, 0.21, 21, 121,
    )
    assert remitos_presupuestos.get_presupuesto(pid)["status"] == "borrador"
    remitos_presupuestos.update_presupuesto_status(pid, "aceptado")
    with pytest.raises(ValueError):
        remitos_presupuestos.delete_presupuesto(pid)
