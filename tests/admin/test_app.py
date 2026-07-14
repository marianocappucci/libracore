"""
Tests de integración de libracore.admin.app::create_admin_app +
libracore.admin.routers.clientes::create_clientes_router, con
TestClient (Fase 4 de LibraCore, backoffice compartido — ver
wiki/entities/libracore.md). auth real de libracore.auth.AdminAuth;
services y templates mínimos de prueba (los templates HTML reales viven
en cada producto, forkeados, nunca migran a LibraCore).
"""
import types

import pytest
from fastapi.testclient import TestClient

from libracore.auth import AdminAuth
from libracore.admin.app import create_admin_app
from libracore.admin.routers.clientes import create_clientes_router
from libracore.admin.templates_config import create_templates


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
    auth = AdminAuth(dev_secret_fallback="test-admin-secret")
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
    auth = AdminAuth(dev_secret_fallback="test-admin-secret")
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
