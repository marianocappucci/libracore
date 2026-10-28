"""El factory del router de presupuestos, montado como lo montaría un producto.

Verifica el contrato HTTP y que las inyecciones lleguen: crear calcula totales y
llama el PDF; convertir pasa el flag `valorizado` al callback del producto; el
envío de email chequea el SMTP y delega en el envío inyectado.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore import presupuestos_router as pr
from libracore.db import core
from libracore.db.schema import init_core_schema


@pytest.fixture
def app_client(tmp_path):
    core.configure(db_path=str(tmp_path / "pres.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    cur = conn.execute(
        "INSERT INTO usuarios (username, nombre, email, password_hash, role, activo) "
        "VALUES ('t','T','','x','admin',1)"
    )
    uid = cur.lastrowid
    conn.commit()
    conn.close()

    reg = {"pdf": [], "convertir": [], "email": []}
    app = FastAPI()
    app.include_router(pr.build_presupuestos_router(
        usuario_actual=lambda: {"id": uid},
        generar_pdf=lambda p: (reg["pdf"].append(p["id"]) or f"/tmp/p-{p['id']}.pdf"),
        convertir_a_remito=lambda p, valorizado: reg["convertir"].append((p["id"], valorizado)),
        smtp_configurado=lambda: True,
        enviar_comprobante=lambda **kw: reg["email"].append(kw["to_email"]),
        moneda=lambda v: f"{v:.2f}",
    ))
    c = TestClient(app)
    c.reg = reg
    c.uid = uid
    yield c
    core._db_path = None


def _crear(app_client, unit_price=100.0, qty=2):
    return app_client.post("/api/presupuestos", json={
        "date": "2026-09-04", "client_name": "Cliente X", "tax_rate": 0.21,
        "items": [{"description": "A", "qty": qty, "unit_price": unit_price}],
    })


def test_crear_calcula_totales_y_genera_pdf(app_client):
    resp = _crear(app_client)
    assert resp.status_code == 200, resp.text
    p = resp.json()
    assert p["subtotal"] == 200.0
    assert p["total"] == 242.0            # 200 + 21%
    assert app_client.reg["pdf"] == [p["id"]]
    # detalle y lista lo traen
    assert app_client.get(f"/api/presupuestos/{p['id']}").json()["id"] == p["id"]
    assert app_client.get("/api/presupuestos").json()["items"][0]["id"] == p["id"]


def test_convertir_pasa_el_flag_valorizado_al_callback(app_client):
    pid = _crear(app_client).json()["id"]
    resp = app_client.post(f"/api/presupuestos/{pid}/estado",
                           json={"estado": "aceptado", "convertir_remito": True, "valorizado": True})
    assert resp.status_code == 200, resp.text
    assert app_client.reg["convertir"] == [(pid, True)]


def test_enviar_email_chequea_smtp_y_delega(app_client):
    pid = _crear(app_client).json()["id"]
    resp = app_client.post(f"/api/presupuestos/{pid}/enviar-email", json={"email": "a@b.com"})
    assert resp.status_code == 200, resp.text
    assert app_client.reg["email"] == ["a@b.com"]


def _app_con_envio(enviar, uid):
    """Otro router sobre la MISMA base ya configurada por el fixture, con un
    envio propio. Sirve para el caso en que el SMTP falla, que el fixture no
    puede representar porque su envio siempre anda."""
    app = FastAPI()
    app.include_router(pr.build_presupuestos_router(
        usuario_actual=lambda: {"id": uid},
        generar_pdf=lambda p: f"/tmp/p-{p['id']}.pdf",
        convertir_a_remito=lambda p, valorizado: None,
        smtp_configurado=lambda: True,
        enviar_comprobante=enviar,
        moneda=lambda v: f"{v:.2f}",
    ))
    return TestClient(app)


def test_enviar_email_pasa_el_presupuesto_a_enviado(app_client):
    """El estado sigue al hecho: mandarlo por mail ES enviarlo.

    Antes habia que apretar ademas "Marcar como enviado", y el presupuesto
    quedaba en borrador para siempre si nadie se acordaba.
    """
    pid = _crear(app_client).json()["id"]
    assert app_client.get(f"/api/presupuestos/{pid}").json()["status"] == "borrador"

    resp = app_client.post(f"/api/presupuestos/{pid}/enviar-email", json={"email": "a@b.com"})

    assert resp.status_code == 200, resp.text
    # En la base y en la respuesta: la pantalla repinta con lo que devuelve.
    assert resp.json()["status"] == "enviado"
    assert app_client.get(f"/api/presupuestos/{pid}").json()["status"] == "enviado"


def test_reenviar_no_retrocede_un_presupuesto_aceptado(app_client):
    """Reenviar el PDF no es un evento del ciclo: el ciclo solo avanza."""
    pid = _crear(app_client).json()["id"]
    app_client.post(f"/api/presupuestos/{pid}/estado", json={"estado": "aceptado"})

    resp = app_client.post(f"/api/presupuestos/{pid}/enviar-email", json={"email": "a@b.com"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "aceptado"
    # Y se mando igual: no cambiar el estado no es no enviar.
    assert app_client.reg["email"] == ["a@b.com"]


def test_si_el_envio_falla_el_presupuesto_se_queda_en_borrador(app_client):
    """El 502 y el estado tienen que contar la misma historia.

    Si la transicion corriera antes del envio, un SMTP caido dejaria el
    presupuesto marcado como enviado sin que haya salido ningun mail.
    """
    pid = _crear(app_client).json()["id"]

    def _explota(**kw):
        raise RuntimeError("conexion rechazada")

    otro = _app_con_envio(_explota, app_client.uid)
    resp = otro.post(f"/api/presupuestos/{pid}/enviar-email", json={"email": "a@b.com"})

    assert resp.status_code == 502
    assert app_client.get(f"/api/presupuestos/{pid}").json()["status"] == "borrador"


def test_cada_envio_deja_rastro_y_el_listado_lo_marca(app_client):
    """Un reclamo de «no me llegó» se contesta mirando el rastro (ADR-050)."""
    pid = _crear(app_client).json()["id"]
    assert app_client.get(f"/api/presupuestos/{pid}/envios").json() == []
    assert app_client.get("/api/presupuestos").json()["items"][0]["envio_email"] is None

    def _explota(**kw):
        raise RuntimeError("conexion rechazada")

    otro = _app_con_envio(_explota, app_client.uid)
    assert otro.post(f"/api/presupuestos/{pid}/enviar-email", json={"email": "a@b.com"}).status_code == 502
    assert app_client.post(f"/api/presupuestos/{pid}/enviar-email", json={"email": "c@d.com"}).status_code == 200

    envios = app_client.get(f"/api/presupuestos/{pid}/envios").json()
    assert [(e["destinatario"], e["estado"]) for e in envios] == [("c@d.com", "enviado"), ("a@b.com", "error")]
    assert "conexion rechazada" in envios[1]["error"]
    assert envios[0]["documento_label"].startswith("Presupuesto ")

    resumen = app_client.get("/api/presupuestos").json()["items"][0]["envio_email"]
    assert resumen["cantidad"] == 2 and resumen["enviados"] == 1
    assert resumen["ultimo_estado"] == "enviado" and resumen["ultimo_destino"] == "c@d.com"
    assert len(app_client.get(f"/api/presupuestos/{pid}").json()["envios_email"]) == 2
    assert app_client.get("/api/presupuestos/99999/envios").status_code == 404


def test_en_una_demo_el_presupuesto_no_se_manda_y_sigue_en_borrador(app_client, monkeypatch):
    """ADR-090, v1.203.0: ni siquiera se llama al envío inyectado por el producto (que podría no pasar por `email_sender`)."""
    pid = _crear(app_client).json()["id"]
    monkeypatch.setenv("DEMO_MODE", "1")
    resp = app_client.post(f"/api/presupuestos/{pid}/enviar-email", json={"email": "a@b.com"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "En la demo los mails no se envían."
    assert app_client.reg["email"] == []
    assert app_client.get(f"/api/presupuestos/{pid}").json()["status"] == "borrador"
    assert app_client.get(f"/api/presupuestos/{pid}").json()["envios_email"] == []
