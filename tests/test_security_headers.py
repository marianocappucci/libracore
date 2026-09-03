from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from libracore.security_headers import CSP, SecurityHeadersMiddleware


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
