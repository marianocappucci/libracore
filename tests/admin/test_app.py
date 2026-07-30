"""
Tests de integración de libracore.admin.app::create_admin_app +
libracore.admin.routers.clientes::create_clientes_router, con
TestClient (Fase 4 de LibraCore, backoffice compartido — ver
wiki/entities/libracore.md). services, templates y **auth** mínimos de
prueba (los templates HTML reales viven en cada producto, forkeados, nunca
migran a LibraCore).

> El auth era `libracore.auth.AdminAuth` hasta el 2026-07-30, cuando el auth
> salió del motor a `libraauth`. `create_admin_app()` **recibe el objeto de
> auth inyectado** — no lo importa —, así que acá va un doble que implementa
> ese contrato: mantiene lo que estos tests prueban (rutas, redirects, gateo
> del endpoint de docs) sin que LibraCore tenga que depender de libraauth para
> correr sus tests. El comportamiento real de `AdminAuth` (firma de cookie,
> fail-closed, rate limit) tiene sus propios 18 tests en libraauth.
"""
import types

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from libracore.admin.app import create_admin_app
from libracore.admin.routers.clientes import create_clientes_router
from libracore.admin.templates_config import create_templates


class FakeAdminAuth:
    """Doble del contrato que `create_admin_app`/`create_clientes_router`
    esperan del objeto de auth. Sin sesión válida por defecto: es el escenario
    que ejercitan estos tests."""

    def __init__(self):
        self.intentos_fallidos = []
        self.usuario = None

    def current_user(self, request: Request):
        return self.usuario

    def require_login(self, request: Request) -> str:
        # La anotacion `Request` es parte del contrato, no decoracion: se usa
        # como `Depends(auth.require_login)` y sin ella FastAPI toma `request`
        # como query param y devuelve 422 en vez de redirigir. Y el redirect
        # es un 307 con `Location`, no una RedirectResponse (ver
        # libraauth/admin_auth.py, de donde salio este comportamiento).
        if self.usuario is None:
            raise HTTPException(status_code=307, headers={"Location": "/login"})
        return self.usuario

    def rate_limit_excedido(self, ip):
        return False

    def check_credentials(self, username, password):
        return username == "admin" and password == "correcta"

    def registrar_intento_fallido(self, ip):
        self.intentos_fallidos.append(ip)

    def create_session_cookie(self, resp, username):
        self.usuario = username

    def clear_session_cookie(self, resp):
        self.usuario = None


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "autoused-test-secret")


@pytest.fixture
def templates_dir(tmp_path):
    d = tmp_path / "templates"
    (d / "clientes").mkdir(parents=True)
    (d / "login.html").write_text(
        '<html><body>Login {{ error }}</body></html>', encoding="utf-8"
    )
    (d / "clientes" / "list.html").write_text(
        '<html><body>{% for c in clientes %}{{ c.slug }}{% endfor %}</body></html>',
        encoding="utf-8",
    )
    (d / "clientes" / "form.html").write_text(
        '<html><body>Nuevo cliente</body></html>', encoding="utf-8"
    )
    (d / "clientes" / "detail.html").write_text(
        '<html><body>{{ c.slug }}</body></html>', encoding="utf-8"
    )
    return str(d)


@pytest.fixture
def fake_services():
    services = types.SimpleNamespace()
    services._clientes = [
        {"slug": "demo1", "nombre": "Demo Uno", "domain": "demo1.test",
         "estado": "running", "plan": "basico"},
    ]

    class ServiceError(Exception):
        pass

    services.ServiceError = ServiceError
    services.listar_clientes = lambda: services._clientes
    services.get_cliente = lambda slug: next(
        (c for c in services._clientes if c["slug"] == slug), None
    )
    services.planes_info = lambda: [{"key": "basico", "label": "Básico"}]

    def crear_cliente(**kwargs):
        return {"slug": kwargs.get("slug") or "nuevo", "admin_password": "genpass"}

    services.crear_cliente = crear_cliente
    return services


@pytest.fixture
def app(templates_dir, fake_services):
    auth = FakeAdminAuth()
    templates = create_templates(templates_dir)
    router = create_clientes_router(auth, fake_services, templates)
    return create_admin_app(
        product_name="TestProduct", auth=auth, services=fake_services,
        templates=templates, clientes_router=router,
    )


@pytest.fixture
def client(app):
    return TestClient(app)


def test_health_no_requiere_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_listar_clientes_sin_sesion_redirige_a_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_login_form_renderiza(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Login" in r.text


def test_login_credenciales_invalidas_redirige_con_error(client):
    r = client.post("/login", data={"username": "nadie", "password": "x"}, follow_redirects=False)
    assert r.status_code == 303
    assert "error=1" in r.headers["location"]


def test_api_clientes_publicos_sin_secret_devuelve_401(client, monkeypatch):
    monkeypatch.delenv("DOCS_AUTH_SECRET", raising=False)
    r = client.get("/api/clientes-publicos")
    assert r.status_code == 401


def test_api_clientes_publicos_con_secret_devuelve_clientes(templates_dir, fake_services, monkeypatch):
    monkeypatch.setenv("DOCS_AUTH_SECRET", "shared-secret")
    auth = FakeAdminAuth()
    templates = create_templates(templates_dir)
    router = create_clientes_router(auth, fake_services, templates)
    app = create_admin_app(
        product_name="TestProduct", auth=auth, services=fake_services,
        templates=templates, clientes_router=router,
    )
    client = TestClient(app)
    r = client.get("/api/clientes-publicos", headers={"x-internal-auth": "shared-secret"})
    assert r.status_code == 200
    assert r.json()["clientes"] == [{"slug": "demo1", "nombre": "Demo Uno", "domain": "demo1.test"}]
