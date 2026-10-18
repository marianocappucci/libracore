"""`HEAD` en la raíz y en la SPA contesta como `GET`, sin cuerpo (ADR-069).

Los monitores de uptime (uptime-kuma) y algunos navegadores y proxies sondean con
`HEAD`. El catch-all estaba declarado con `@app.get`, y FastAPI **no** deriva el
`HEAD` del `GET` (Starlette sí, en sus rutas; `APIRoute` no): `HEAD /` daba 405
con `Allow: GET` y una instancia sana figuraba caída. Lo midió LibraClub
(libraclub#411) y lo tenían los seis productos que montan la SPA.

Se prueba `montar_spa` sobre una `FastAPI` limpia y un `dist` de mentira, como
`test_spa_cache.py`: no se importa la app de ningún producto.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from libracore.spa import PARA_SIEMPRE, SIN_CACHE, montar_spa


@pytest.fixture
def cliente(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (dist / "manifest.webmanifest").write_text("{}", encoding="utf-8")
    app = FastAPI()
    # Una ruta real, montada antes que el catch-all, como en los productos.
    api = APIRouter(prefix="/api")

    @api.get("/existe")
    def existe():
        return {"ok": True}

    app.include_router(api)
    montar_spa(app, dist)
    return TestClient(app)


@pytest.mark.parametrize("ruta", ["/", "/reservas", "/una/ruta/profunda", "/manifest.webmanifest"])
def test_head_responde_como_get_y_sin_cuerpo(cliente, ruta):
    get = cliente.get(ruta)
    head = cliente.head(ruta)
    assert get.status_code == 200
    assert head.status_code == 200, f"HEAD {ruta} dio {head.status_code} (Allow: {head.headers.get('allow')})"
    assert head.content == b""
    assert head.headers["content-type"] == get.headers["content-type"]
    assert head.headers["cache-control"] == SIN_CACHE


def test_head_de_un_asset_con_hash_lleva_la_cache_larga(cliente):
    """`/assets` lo sirve `StaticFiles`, que ya contestaba HEAD: queda cubierto."""
    head = cliente.head("/assets/app-abc123.js")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["cache-control"] == PARA_SIEMPRE


@pytest.mark.parametrize("ruta", ["/api/lo-que-sea", "/api", "/api/"])
def test_head_de_una_ruta_de_api_inexistente_sigue_siendo_404(cliente, ruta):
    """El control: HEAD no abre un camino por el que la API devuelva el `index.html`."""
    assert cliente.get(ruta).status_code == 404
    assert cliente.head(ruta).status_code == 404


def test_head_de_una_ruta_de_api_declarada_solo_con_get_no_da_el_index(cliente):
    """Lo declarado antes del catch-all se resuelve antes con `GET`; con `HEAD` la ruta
    de la API (sólo `GET`) no coincide y cae al catch-all, que en `/api` da 404 y no el
    `index.html`. Antes era 405: ambos son un fallo para el monitor, ninguno es un 200
    falso. Quien quiera `HEAD` en un endpoint de la API lo declara con `api_route`."""
    assert cliente.get("/api/existe").status_code == 200
    head = cliente.head("/api/existe")
    assert head.status_code == 404
    assert head.content == b""


def test_otros_metodos_siguen_sin_entrar_a_la_spa(cliente):
    """Sólo `GET` y `HEAD`: un `POST` a una ruta de la SPA sigue dando 405."""
    assert cliente.post("/reservas").status_code == 405
