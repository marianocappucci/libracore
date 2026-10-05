"""Una ruta de la API que no existe es un 404, no la SPA (ADR-020).

El catch-all de la SPA contestaba `index.html` con 200 a cualquier ruta, también
a `/api/loquesea`. Un cliente que pedía un endpoint mal escrito, o que todavía no
existe en esa versión, recibía HTML con 200 y lo tomaba por un éxito: los deploys
de VentaLibra del 2026-10-04/05 reportaban «`/api/health` 200» y era el
`index.html` (el chequeo real es `/health`).
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from libracore.spa import es_de_la_api, montar_spa


def _app(tmp_path, **kwargs):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    app = FastAPI()
    # Una ruta real de la API, montada antes que el catch-all, como en los productos.
    api = APIRouter(prefix="/api")

    @api.get("/existe")
    def existe():
        return {"ok": True}

    app.include_router(api)
    montar_spa(app, dist, **kwargs)
    return TestClient(app)


@pytest.mark.parametrize("ruta", ["/api/health", "/api/no-existe/123", "/api", "/api/"])
def test_una_ruta_de_la_api_que_no_existe_da_404_en_json(tmp_path, ruta):
    r = _app(tmp_path).get(ruta)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"detail": "Not Found"}


def test_una_ruta_de_la_api_que_existe_contesta_igual(tmp_path):
    r = _app(tmp_path).get("/api/existe")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@pytest.mark.parametrize("ruta", ["/ventas", "/logs", "/apis", "/api-docs", "/configuracion/api"])
def test_las_rutas_de_la_pantalla_siguen_cayendo_en_la_spa(tmp_path, ruta):
    """Sólo `api` como primer tramo: `/apis` o `/configuracion/api` son rutas de la SPA."""
    r = _app(tmp_path).get(ruta)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_un_producto_puede_sumar_prefijos(tmp_path):
    cliente = _app(tmp_path, prefijos_api=("api", "auth"))
    assert cliente.get("/auth/no-existe").status_code == 404
    assert cliente.get("/ventas").status_code == 200


def test_es_de_la_api():
    assert es_de_la_api("api")
    assert es_de_la_api("api/x/y")
    assert not es_de_la_api("")
    assert not es_de_la_api("apis/x")
    assert not es_de_la_api("ventas/api")
