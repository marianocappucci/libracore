from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from libracore.security_headers import CSP, CSP_SPA, SecurityHeadersMiddleware


def _make_app():
    async def home(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", home)])
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def test_all_security_headers_present():
    client = TestClient(_make_app())
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "geolocation=()" in r.headers["Permissions-Policy"]
    assert r.headers["Content-Security-Policy"] == CSP
    assert "max-age=31536000" in r.headers["Strict-Transport-Security"]


def test_csp_restricts_object_and_frame_ancestors():
    assert "object-src 'none'" in CSP
    assert "frame-ancestors 'none'" in CSP


# ── CSP_SPA: la politica de los productos con frontend React/Vite ─────────────
#
# Existe porque darles la CSP de las apps Jinja2 les habilitaria
# `cdn.jsdelivr.net`, que no usan. Los tests de abajo fijan las dos cosas que
# hacen que valga la pena tener dos politicas y no una.


def _make_app_spa():
    async def home(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", home)])
    app.add_middleware(SecurityHeadersMiddleware, csp=CSP_SPA)
    return app


def test_la_spa_no_habilita_ningun_cdn():
    """El motivo de que exista `CSP_SPA`. Si alguien la 'arregla' copiando la
    de Jinja, esto se pone rojo."""
    assert "jsdelivr" not in CSP_SPA
    assert "jsdelivr" in CSP, "la de Jinja SI lo necesita — control positivo"


def test_la_spa_no_permite_scripts_inline():
    """La diferencia que de verdad defiende: Vite emite el JS como archivos del
    propio origen, asi que la SPA no necesita ejecutar scripts inline. Las
    plantillas Jinja si, y por eso la otra politica lo permite."""
    assert "script-src 'self';" in CSP_SPA
    assert "'unsafe-inline'" not in CSP_SPA.split("script-src")[1].split(";")[0]
    assert "'unsafe-inline'" in CSP.split("script-src")[1].split(";")[0], "control positivo"


def test_la_spa_si_permite_estilos_inline():
    """Contraprueba de la de arriba: los componentes inyectan estilos, asi que
    quitarlo tambien de `style-src` rompe la pantalla."""
    assert "'unsafe-inline'" in CSP_SPA.split("style-src")[1].split(";")[0]


def test_el_middleware_sirve_la_politica_que_se_le_pasa():
    client = TestClient(_make_app_spa())
    r = client.get("/")
    assert r.headers["Content-Security-Policy"] == CSP_SPA
    # Y el resto de los headers no cambia por elegir otra CSP.
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "max-age=31536000" in r.headers["Strict-Transport-Security"]


def test_sin_argumento_el_middleware_hace_lo_de_siempre():
    """Las cuatro apps que ya lo montaban no cambian de comportamiento."""
    client = TestClient(_make_app())
    assert client.get("/").headers["Content-Security-Policy"] == CSP
