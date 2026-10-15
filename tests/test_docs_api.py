"""El Swagger de la API sólo se sirve en desarrollo (ADR-054, `libracore.docs_api`).

Medido el 2026-10-15: `/docs`, `/redoc` y `/openapi.json` daban 200 sin sesión en clientes reales, demos
y backoffices. Estos tests fijan lo contrario con `TestClient` sobre una app de verdad, incluida una con el
catch-all de la SPA montado (que contesta `index.html` con 200 a lo que no existe: es la trampa de cerrar
con `docs_url=None`).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore.docs_api import VARIABLE_DOCS, cerrar_docs_de_la_api, docs_abiertos
from libracore.spa import montar_spa

RUTAS_DEL_SWAGGER = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")


@pytest.fixture(autouse=True)
def _sin_marcas_del_shell(monkeypatch):
    """Ni el `ENV` del shell de quien corre la suite ni un `LIBRA_DOCS_API` ajeno deciden el resultado."""
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv(VARIABLE_DOCS, raising=False)
    monkeypatch.delenv("LIBRA_ENTORNO", raising=False)


def _app(cerrar=True) -> FastAPI:
    app = FastAPI(title="Producto de prueba")
    if cerrar:
        cerrar_docs_de_la_api(app)

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    return app


def _estados(client) -> dict[str, int]:
    return {ruta: client.get(ruta).status_code for ruta in RUTAS_DEL_SWAGGER}


# ── Cerrado: producción, demo, y el «no sé» ──────────────────────────────────

@pytest.mark.parametrize("entorno", ["cliente", "demo", "", "desconocido"])
def test_fuera_de_desarrollo_las_cuatro_rutas_dan_404(monkeypatch, entorno):
    """Un cliente real (`cliente`), una demo (`demo`) y una instancia sin la variable: todas cerradas."""
    monkeypatch.setenv("LIBRA_ENTORNO", entorno)
    estados = _estados(TestClient(_app()))
    assert estados == dict.fromkeys(RUTAS_DEL_SWAGGER, 404), estados


def test_sin_ninguna_variable_esta_cerrado():
    """El valor por omisión es cerrar: lo que se abre hay que pedirlo."""
    assert _estados(TestClient(_app())) == dict.fromkeys(RUTAS_DEL_SWAGGER, 404)


def test_el_404_no_trae_el_esquema_ni_la_pagina(monkeypatch):
    monkeypatch.setenv("LIBRA_ENTORNO", "cliente")
    client = TestClient(_app())
    for ruta in RUTAS_DEL_SWAGGER:
        r = client.get(ruta)
        assert r.json() == {"detail": "Not Found"}, ruta
        assert "/api/ping" not in r.text and "swagger" not in r.text.lower(), ruta


def test_un_head_tampoco_lo_filtra():
    client = TestClient(_app())
    assert client.head("/openapi.json").status_code == 404


def test_con_el_catch_all_de_la_spa_sigue_dando_404_y_no_el_index(tmp_path, monkeypatch):
    """🔴 El caso que importa: con la SPA montada, `docs_url=None` daría `index.html` con 200."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>la SPA</html>")
    monkeypatch.setenv("LIBRA_ENTORNO", "cliente")
    app = _app()
    montar_spa(app, tmp_path)
    client = TestClient(app)

    assert _estados(client) == dict.fromkeys(RUTAS_DEL_SWAGGER, 404)
    assert "la SPA" not in client.get("/docs").text
    # El control: la SPA sigue atendiendo lo suyo.
    assert "la SPA" in client.get("/clientes").text
    assert client.get("/api/ping").json() == {"ok": True}


def test_el_control_de_la_trampa_docs_url_none_con_la_spa_da_200(tmp_path):
    """Por qué no alcanza con apagar las rutas: la SPA contesta 200 al `/docs` que ya no existe."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>la SPA</html>")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    montar_spa(app, tmp_path)
    assert TestClient(app).get("/docs").status_code == 200


# ── Abierto: desarrollo y el interruptor explícito ───────────────────────────

def test_en_desarrollo_las_tres_paginas_siguen_andando(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    client = TestClient(_app())

    esquema = client.get("/openapi.json")
    assert esquema.status_code == 200
    assert "/api/ping" in esquema.json()["paths"]
    docs = client.get("/docs")
    assert docs.status_code == 200 and "swagger" in docs.text.lower()
    redoc = client.get("/redoc")
    assert redoc.status_code == 200 and "redoc" in redoc.text.lower()
    assert client.get("/docs/oauth2-redirect").status_code == 200


def test_el_interruptor_explicito_abre_una_instancia(monkeypatch):
    monkeypatch.setenv("LIBRA_ENTORNO", "cliente")
    monkeypatch.setenv(VARIABLE_DOCS, "1")
    assert _estados(TestClient(_app())) == dict.fromkeys(RUTAS_DEL_SWAGGER, 200)


@pytest.mark.parametrize("valor", ["0", "", "no", "false", "si", "development", "yes"])
def test_el_interruptor_con_cualquier_otro_valor_no_abre(monkeypatch, valor):
    monkeypatch.setenv(VARIABLE_DOCS, valor)
    assert docs_abiertos() is False
    assert TestClient(_app()).get("/openapi.json").status_code == 404


@pytest.mark.parametrize("valor", ["Development", "dev", "production", "prod", "1", " development"])
def test_env_distinto_de_development_no_abre(monkeypatch, valor):
    """`ENV` se compara exacto, como en `arca_facturacion` y libraauth: ante la duda, cerrado."""
    monkeypatch.setenv("ENV", valor)
    assert docs_abiertos() is False


def test_se_decide_en_cada_pedido_no_al_crear_la_app(monkeypatch):
    """La misma app contesta según el entorno de cada pedido: la prueba de un producto no depende del import."""
    client = TestClient(_app())
    assert client.get("/openapi.json").status_code == 404
    monkeypatch.setenv("ENV", "development")
    assert client.get("/openapi.json").status_code == 200
    monkeypatch.delenv("ENV")
    assert client.get("/openapi.json").status_code == 404


# ── Lo que no se toca ────────────────────────────────────────────────────────

def test_sin_la_llamada_el_swagger_esta_abierto_el_control():
    """El control de los tests de arriba: una app sin `cerrar_docs_de_la_api` publica todo, sin sesión."""
    client = TestClient(_app(cerrar=False))
    assert _estados(client) == dict.fromkeys(RUTAS_DEL_SWAGGER, 200)


def test_las_demas_rutas_no_se_tocan(monkeypatch):
    monkeypatch.setenv("LIBRA_ENTORNO", "cliente")
    client = TestClient(_app())
    assert client.get("/api/ping").json() == {"ok": True}
    assert client.get("/api/no-existe").status_code == 404


def test_openapi_como_metodo_sigue_andando_en_produccion(monkeypatch):
    """Los tests de rutas de los productos leen `app.openapi()`: lo que se cierra es sólo la publicación HTTP."""
    monkeypatch.setenv("LIBRA_ENTORNO", "cliente")
    app = _app()
    assert "/api/ping" in app.openapi()["paths"]


def test_llamarla_dos_veces_no_rompe_nada(monkeypatch):
    app = _app()
    cerrar_docs_de_la_api(app)
    client = TestClient(app)
    assert client.get("/openapi.json").status_code == 404
    monkeypatch.setenv("ENV", "development")
    assert client.get("/openapi.json").status_code == 200


def test_una_app_sin_swagger_no_tiene_nada_que_guardar():
    """Un producto que ya armó la app con `docs_url=None` y compañía: la llamada es un no-op."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    antes = list(app.router.routes)
    cerrar_docs_de_la_api(app)
    assert app.router.routes == antes


def test_con_una_url_del_swagger_cambiada_se_cierra_la_nueva(monkeypatch):
    """Si un producto movió el Swagger (`docs_url="/ayuda"`), se cierra donde esté, no donde estaba."""
    app = FastAPI(docs_url="/ayuda", openapi_url="/esquema.json")
    cerrar_docs_de_la_api(app)
    client = TestClient(app)
    assert client.get("/ayuda").status_code == 404
    assert client.get("/esquema.json").status_code == 404
    monkeypatch.setenv("ENV", "development")
    assert client.get("/ayuda").status_code == 200


def test_docs_abiertos_acepta_un_entorno_explicito():
    assert docs_abiertos({"ENV": "development"}) is True
    assert docs_abiertos({VARIABLE_DOCS: "true"}) is True
    assert docs_abiertos({"LIBRA_ENTORNO": "demo"}) is False
    assert docs_abiertos({}) is False
