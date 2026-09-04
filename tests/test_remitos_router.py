"""El factory del router de remitos, montado como lo montaría un producto.

Verifica el contrato HTTP (los cuatro endpoints) y que las dos aristas inyectadas
lleguen de verdad: la auth (el `usuario_id` que queda en el remito) y el
`generar_pdf` (que corre y su ruta se guarda). Mismo patrón que
`test_comprobantes_router.py`: se monta en una app de prueba con el schema real.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore import remitos_router as rr
from libracore.db import core
from libracore.db.schema import init_core_schema


@pytest.fixture
def client(tmp_path):
    core.configure(db_path=str(tmp_path / "remitos.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    # `remitos.usuario_id` es FK a `usuarios(id)` y la base la hace cumplir, así
    # que hace falta un usuario real (mismo motivo que la factura en
    # test_comprobantes_router).
    cur = conn.execute(
        "INSERT INTO usuarios (username, nombre, email, password_hash, role, activo) "
        "VALUES ('tester', 'Tester', '', 'x', 'admin', 1)"
    )
    uid = cur.lastrowid
    conn.commit()
    conn.close()

    pdfs = []

    def fake_pdf(remito):
        pdfs.append(remito["id"])
        return f"/tmp/remito-{remito['id']}.pdf"

    app = FastAPI()
    app.include_router(rr.build_remitos_router(
        usuario_actual=lambda: {"id": uid, "username": "tester"},
        generar_pdf=fake_pdf,
    ))
    c = TestClient(app)
    c.pdfs = pdfs
    c.uid = uid
    yield c
    core._db_path = None


def test_crear_lista_detalle_eliminar(client):
    resp = client.post("/api/remitos", json={
        "date": "2026-09-04", "client_name": "Cliente Test", "observations": "obs",
        "items": [{"description": "Prod A", "qty": 2}, {"description": "   ", "qty": 1}],
    })
    assert resp.status_code == 200, resp.text
    remito = resp.json()
    rid = remito["id"]
    # el ítem vacío se filtra; queda uno
    assert remito["items"] == [{"description": "Prod A", "qty": 2}]
    # la auth inyectada dejó su usuario_id
    assert remito["usuario_id"] == client.uid
    # el generar_pdf inyectado corrió y su ruta quedó guardada
    assert client.pdfs == [rid]
    assert remito["pdf_path"] == f"/tmp/remito-{rid}.pdf"

    assert client.get(f"/api/remitos/{rid}").json()["id"] == rid
    assert any(r["id"] == rid for r in client.get("/api/remitos").json())
    assert client.delete(f"/api/remitos/{rid}").json() == {"ok": True}
    assert client.get(f"/api/remitos/{rid}").status_code == 404


def test_crear_sin_items_validos_es_422(client):
    resp = client.post("/api/remitos", json={
        "date": "2026-09-04", "client_name": "X",
        "items": [{"description": "   ", "qty": 1}],
    })
    assert resp.status_code == 422


def test_crear_sin_cliente_es_422(client):
    resp = client.post("/api/remitos", json={
        "date": "2026-09-04", "client_name": "",
        "items": [{"description": "A", "qty": 1}],
    })
    assert resp.status_code == 422
