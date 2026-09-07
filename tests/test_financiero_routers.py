"""Las factories financieras de P9-M4 (clientes, proveedores, egresos, tesorería,
cuenta corriente, dashboard): el contrato HTTP que los dos productos ya fijaban
en sus suites, sin el gate por módulo."""

from __future__ import annotations

import datetime

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracore.clientes_router import build_clientes_router
from libracore.cuenta_corriente_router import build_cuenta_corriente_router
from libracore.dashboard_router import build_dashboard_router
from libracore.db import core
from libracore.db.schema import init_core_schema
from libracore.egresos_router import build_egresos_router, build_proveedores_router
from libracore.tesoreria_router import build_tesoreria_router

HOY = datetime.date.today().isoformat()
USUARIO = {"id": 7, "username": "cajero", "role": "admin"}


def _usuario():
    return USUARIO


def _solo_admin(user: dict = Depends(_usuario)):
    if user.get("role") != "admin":
        raise HTTPException(403, "solo administradores")


@pytest.fixture
def entorno(tmp_path):
    core.configure(db_path=str(tmp_path / "fin.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?,?,?,?,?)",
        (USUARIO["id"], USUARIO["username"], "Cajero", "x", "admin"))
    conn.commit()
    conn.close()
    USUARIO["role"] = "admin"
    yield
    core._db_path = None


@pytest.fixture
def client(entorno, request):
    app = FastAPI()
    app.include_router(build_clientes_router())
    app.include_router(build_proveedores_router())
    app.include_router(build_egresos_router(usuario_actual=_usuario))
    app.include_router(build_tesoreria_router(usuario_actual=_usuario))
    app.include_router(build_cuenta_corriente_router(usuario_actual=_usuario, solo_admin=_solo_admin,
                                                     con_recibos=getattr(request, "param", False)))
    app.include_router(build_dashboard_router(usuario_actual=_usuario))
    return TestClient(app)


# ── Clientes ─────────────────────────────────────────────────────────────


def test_clientes_crud_alias_y_baja(client):
    r = client.post("/api/clientes", json={"name": " Ana ", "cuit_dni": "20111111112", "iva_condition": "Monotributista"})
    assert r.status_code == 200 and r.json()["name"] == "Ana"
    cid = r.json()["id"]
    assert client.post("/api/clientes", json={"name": "  "}).status_code == 422
    # El CUIT repetido rebota con el mensaje del motor.
    assert client.post("/api/clientes", json={"name": "Otra", "cuit_dni": "20111111112"}).status_code == 422
    d = client.get(f"/api/clientes/{cid}").json()
    assert d["name"] == "Ana" and d["facturas"] == [] and d["presupuestos"] == [] and d["remitos"] == [] and d["alias_facturacion"] == []
    assert client.get("/api/clientes/999").status_code == 404
    r = client.put(f"/api/clientes/{cid}", json={"name": "Ana P", "email": "a@b.c", "auto_facturar": True})
    assert r.json()["email"] == "a@b.c" and r.json()["auto_facturar"] == 1
    assert client.put(f"/api/clientes/{cid}", json={"name": " "}).status_code == 422
    assert client.put("/api/clientes/999", json={"name": "X"}).status_code == 404
    assert client.post(f"/api/clientes/{cid}/toggle-auto-facturar").json()["auto_facturar"] == 0
    alias = client.post(f"/api/clientes/{cid}/alias-facturacion", json={"tipo": "email", "valor": "pago@x.com"}).json()
    assert len(alias) == 1 and alias[0]["valor"] == "pago@x.com"
    assert client.post(f"/api/clientes/{cid}/alias-facturacion", json={"tipo": "raro", "valor": "x"}).status_code == 422
    assert client.delete(f"/api/clientes/{cid}/alias-facturacion/{alias[0]['id']}").json() == []
    assert client.post(f"/api/clientes/{cid}/desactivar").json()["activo"] == 0
    assert len(client.get("/api/clientes").json()) == 1  # incluye inactivos
    assert client.post(f"/api/clientes/{cid}/activar").json()["activo"] == 1
    for ruta in ("toggle-auto-facturar", "desactivar", "activar"):
        assert client.post(f"/api/clientes/999/{ruta}").status_code == 404
    assert client.post("/api/clientes/999/alias-facturacion", json={"tipo": "email", "valor": "x"}).status_code == 404
    assert client.delete("/api/clientes/999/alias-facturacion/1").status_code == 404


def test_no_se_desactiva_un_cliente_con_presupuestos_aprobados(client):
    cid = client.post("/api/clientes", json={"name": "Ana"}).json()["id"]
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO presupuestos (number, date, valid_until, client_id, client_name, status, items, subtotal, tax_amount, total) "
            "VALUES ('P-1', ?, ?, ?, 'Ana', 'aceptado', '[]', 10, 0, 10)", (HOY, HOY, cid))
    r = client.post(f"/api/clientes/{cid}/desactivar")
    assert r.status_code == 422 and "presupuestos aprobados" in r.json()["detail"]


# ── Proveedores y egresos ────────────────────────────────────────────────


def test_proveedores_y_egresos(client):
    assert client.post("/api/proveedores", json={"nombre": " "}).status_code == 422
    p = client.post("/api/proveedores", json={"nombre": "Acme", "cuit_dni": "30", "email": "a@acme"}).json()
    assert p["nombre"] == "Acme"
    assert client.get("/api/proveedores").json()[0]["id"] == p["id"]
    assert client.get("/api/proveedores?q=acm").json()[0]["id"] == p["id"]
    assert client.get("/api/proveedores?q=zzz").json() == []
    assert client.put(f"/api/proveedores/{p['id']}", json={"nombre": "Acme SA"}).json()["nombre"] == "Acme SA"
    assert client.put(f"/api/proveedores/{p['id']}", json={"nombre": " "}).status_code == 422
    assert client.put("/api/proveedores/999", json={"nombre": "X"}).status_code == 404
    assert client.delete("/api/proveedores/999").status_code == 404

    assert client.get("/api/egresos/tipos-comprobante").json()[0]["id"] == "factura"
    assert client.post("/api/egresos/categorias", json={"nombre": " "}).status_code == 422
    cats = client.post("/api/egresos/categorias", json={"nombre": "Insumos"}).json()
    # El schema siembra las categorías de siempre; la nueva se suma.
    insumos = [c for c in cats if c["nombre"] == "Insumos"]
    assert len(insumos) == 1
    assert client.get("/api/egresos/cajas").json()[0]["nombre"]
    assert client.post("/api/egresos", json={"concepto": " "}).status_code == 422
    assert client.post("/api/egresos", json={"concepto": "Nada", "monto_neto": 0}).status_code == 422
    e = client.post("/api/egresos", json={
        "concepto": "Harina", "categoria": "Insumos", "tipo_comprobante": "factura", "numero": "0001-00000009",
        "proveedor_id": p["id"], "monto_neto": 1000, "iva_pct": 0.21,
    }).json()
    assert e["total"] == 1210.0 and e["proveedor_nombre"] == "Acme SA" and e["iva_monto"] == 210.0
    listado = client.get("/api/egresos").json()
    assert len(listado["items"]) == 1 and listado["resumen"]["total_periodo"] == 1210.0
    assert len(client.get(f"/api/egresos?proveedor_id={p['id']}").json()["items"]) == 1
    assert client.get("/api/egresos?proveedor_id=999").json()["items"] == []
    assert client.get(f"/api/egresos/{e['id']}/pagos").json() == []
    assert client.get("/api/egresos/999/pagos").status_code == 404
    r = client.post(f"/api/egresos/{e['id']}/pagar", json={"medio_pago": "efectivo", "referencia": "r1"})
    assert r.status_code == 200 and r.json()["estado"] == "pagado"
    assert client.get(f"/api/egresos/{e['id']}/pagos").json()[0]["monto"] == 1210.0
    with core.get_connection() as conn:
        mov = conn.execute("SELECT tipo, concepto, monto, usuario_id FROM caja_movimientos").fetchone()
    assert mov["tipo"] == "egreso" and mov["concepto"].startswith("Pago Factura 0001-00000009") and mov["monto"] == 1210.0 and mov["usuario_id"] == 7
    assert client.post("/api/egresos/999/pagar", json={}).status_code == 404
    # El proveedor con egresos no se borra; borrar el egreso sí.
    assert client.delete(f"/api/proveedores/{p['id']}").status_code == 422
    assert client.delete(f"/api/egresos/{e['id']}").json() == {"ok": True}
    assert client.delete(f"/api/egresos/{e['id']}").status_code == 404
    assert client.delete(f"/api/proveedores/{p['id']}").json() == {"ok": True}
    assert not [c for c in client.delete(f"/api/egresos/categorias/{insumos[0]['id']}").json() if c["nombre"] == "Insumos"]


# ── Tesorería ────────────────────────────────────────────────────────────


def test_tesoreria(client):
    assert client.post("/api/tesoreria/cuentas", json={"nombre": " "}).status_code == 422
    a = client.post("/api/tesoreria/cuentas", json={"nombre": "Banco", "saldo_inicial": 1000}).json()
    b = client.post("/api/tesoreria/cuentas", json={"nombre": "Caja fuerte", "tipo": "efectivo"}).json()
    r = client.get("/api/tesoreria").json()
    assert len(r["cuentas"]) == 2 and "resumen" in r and "movimientos" in r
    assert client.get("/api/tesoreria/cuentas/999").status_code == 404
    assert client.put(f"/api/tesoreria/cuentas/{a['id']}", json={"nombre": "Banco Nación"}).json()["nombre"] == "Banco Nación"
    assert client.put(f"/api/tesoreria/cuentas/{a['id']}", json={"nombre": " "}).status_code == 422
    assert client.put("/api/tesoreria/cuentas/999", json={"nombre": "X"}).status_code == 404
    for cuerpo in ({"tipo": "ingreso", "monto": 0, "concepto": "x", "fecha": HOY}, {"tipo": "ingreso", "monto": 5, "concepto": " ", "fecha": HOY}):
        assert client.post(f"/api/tesoreria/cuentas/{a['id']}/movimiento", json=cuerpo).status_code == 422
    assert client.post("/api/tesoreria/cuentas/999/movimiento", json={"tipo": "ingreso", "monto": 5, "concepto": "x", "fecha": HOY}).status_code == 404
    r = client.post(f"/api/tesoreria/cuentas/{a['id']}/movimiento", json={"tipo": "ingreso", "monto": 500, "concepto": "Cobro", "fecha": HOY})
    assert r.status_code == 200
    assert client.post("/api/tesoreria/transferencia", json={"cuenta_origen_id": a["id"], "cuenta_destino_id": a["id"], "monto": 1, "fecha": HOY}).status_code == 422
    assert client.post("/api/tesoreria/transferencia", json={"cuenta_origen_id": a["id"], "cuenta_destino_id": b["id"], "monto": 0, "fecha": HOY}).status_code == 422
    assert client.post("/api/tesoreria/transferencia", json={"cuenta_origen_id": a["id"], "cuenta_destino_id": 999, "monto": 1, "fecha": HOY}).status_code == 404
    assert client.post("/api/tesoreria/transferencia", json={"cuenta_origen_id": a["id"], "cuenta_destino_id": b["id"], "monto": 200, "fecha": HOY}).json() == {"ok": True}
    det = client.get(f"/api/tesoreria/cuentas/{b['id']}").json()
    # La transferencia aparece en las dos cuentas: la salida de A y la entrada de B.
    assert det["cuenta"]["id"] == b["id"] and {m["tipo"] for m in det["movimientos"]} == {"transferencia_entrada", "transferencia_salida"}
    mid = [m for m in det["movimientos"] if m["cuenta_id"] == b["id"]][0]["id"]
    assert client.delete(f"/api/tesoreria/movimientos/{mid}").json() == {"ok": True}
    assert client.delete(f"/api/tesoreria/cuentas/{b['id']}").json() == {"ok": True}
    assert client.delete("/api/tesoreria/cuentas/999").status_code == 404


# ── Cuenta corriente ─────────────────────────────────────────────────────


def _cliente_con_deuda(client, monto=1000.0):
    cid = client.post("/api/clientes", json={"name": "Deudor"}).json()["id"]
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO cc_debitos (cliente_id, monto, fecha, concepto) VALUES (?, ?, ?, 'Fiado')", (cid, monto, HOY))
    return cid


@pytest.mark.parametrize("client", [False], indirect=True)
def test_cuenta_corriente_sin_recibos(client):
    cid = _cliente_con_deuda(client)
    r = client.get("/api/cuenta-corriente").json()
    assert r["total_deuda"] == 1000.0 and r["clientes"][0]["id"] == cid
    assert client.get("/api/cuenta-corriente/cajas").json()[0]["nombre"]
    assert client.get("/api/cuenta-corriente/999").status_code == 404
    assert client.get(f"/api/cuenta-corriente/{cid}").json()["saldo"] == 1000.0
    assert client.post("/api/cuenta-corriente/999/pagar", json={"monto": 1, "fecha": HOY}).status_code == 404
    caja = client.get("/api/cuenta-corriente/cajas").json()[0]["id"]
    r = client.post(f"/api/cuenta-corriente/{cid}/pagar", json={"monto": 400, "fecha": HOY, "caja_id": caja, "medio_pago": "efectivo"}).json()
    assert r["saldo"] == 600.0 and "recibo_id" not in r
    with core.get_connection() as conn:
        assert conn.execute("SELECT concepto FROM caja_movimientos").fetchone()["concepto"] == "Pago CC - Deudor"
    # Sin caja no hay movimiento.
    client.post(f"/api/cuenta-corriente/{cid}/pagar", json={"monto": 100, "fecha": HOY})
    with core.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM caja_movimientos").fetchone()[0] == 1
        pago_id = conn.execute("SELECT MAX(id) FROM cc_pagos").fetchone()[0]
    USUARIO["role"] = "operador"
    assert client.delete(f"/api/cuenta-corriente/pagos/{pago_id}").status_code == 403
    USUARIO["role"] = "admin"
    assert client.delete(f"/api/cuenta-corriente/pagos/{pago_id}").json() == {"ok": True}
    assert client.get(f"/api/cuenta-corriente/{cid}").json()["saldo"] == 600.0


@pytest.mark.parametrize("client", [True], indirect=True)
def test_cuenta_corriente_con_recibos(client, monkeypatch, tmp_path):
    from libracore import pdf_generator as pdf_gen

    monkeypatch.setattr(pdf_gen, "FACTURAS_PDF_DIR", str(tmp_path / "pdf"))
    cid = _cliente_con_deuda(client)
    r = client.post(f"/api/cuenta-corriente/{cid}/pagar", json={"monto": 400, "fecha": HOY}).json()
    assert r["recibo_id"], "el recibo sale con el cobro"
    with core.get_connection() as conn:
        pago_id = conn.execute("SELECT MAX(id) FROM cc_pagos").fetchone()[0]
        assert conn.execute("SELECT anulado FROM recibos WHERE id=?", (r["recibo_id"],)).fetchone()[0] == 0
    assert client.delete(f"/api/cuenta-corriente/pagos/{pago_id}").json() == {"ok": True}
    with core.get_connection() as conn:
        assert conn.execute("SELECT anulado FROM recibos WHERE id=?", (r["recibo_id"],)).fetchone()[0] == 1
    # Si la emisión falla, el cobro queda igual.
    from libracore import recibos as mod_recibos

    monkeypatch.setattr(mod_recibos, "emitir_recibo_cobranza", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sin pdf")))
    r = client.post(f"/api/cuenta-corriente/{cid}/pagar", json={"monto": 100, "fecha": HOY}).json()
    assert r["recibo_id"] is None and r["saldo"] == 900.0


# ── Dashboard ────────────────────────────────────────────────────────────


def test_dashboard_y_el_extra_del_producto(entorno):
    app = FastAPI()
    app.include_router(build_dashboard_router(usuario_actual=_usuario, extra=lambda hoy: {"salon": hoy}))
    client = TestClient(app)
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, cliente_iva_cond, "
            "items, subtotal, iva_amount, total, ambiente) VALUES (11, 5, 11, ?, '', 'CF', 5, '[]', 100, 0, 100, 'produccion')", (HOY,))
    d = client.get("/api/dashboard").json()
    assert d["mes_hasta"] == HOY and d["salon"] == HOY
    assert d["facturas_sin_cobrar"][0]["letra"] == "C" and d["facturas_sin_cobrar"][0]["label_numero"] == "0005-00000011"
    assert d["facturado_mes"] == 100.0
