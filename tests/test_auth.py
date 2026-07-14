import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from libracore.auth import SessionAuth, AdminAuth


@pytest.fixture(autouse=True)
def _default_secret_key(monkeypatch):
    # La mayoría de los tests no ejercitan el fail-fast/dev-fallback de
    # SECRET_KEY — los tests que sí lo hacen pisan esto con su propio
    # monkeypatch.delenv/setenv.
    monkeypatch.setenv("SECRET_KEY", "autoused-test-secret")


# ── SessionAuth ───────────────────────────────────────────────────────────

_USERS = {
    "admin1":  {"username": "admin1",  "role": "admin",    "_password": "adminpw"},
    "oper1":   {"username": "oper1",   "role": "operador", "_password": "operpw"},
    "cajero1": {"username": "cajero1", "role": "cajero",   "_password": "cajpw"},
}


def _make_session_auth(**overrides):
    def get_user_by_username(username):
        return _USERS.get(username)

    def check_credentials(username, password):
        user = _USERS.get(username)
        if user and user["_password"] == password:
            return user
        return None

    kwargs = dict(
        dev_secret_fallback="test-secret",
        get_user_by_username=get_user_by_username,
        check_credentials=check_credentials,
    )
    kwargs.update(overrides)
    return SessionAuth(**kwargs)


def _make_session_app(session_auth):
    async def protected(request):
        user = session_auth.require_auth(request)
        return PlainTextResponse(f"hello {user}")

    async def admin_only(request):
        user = session_auth.require_admin(request)
        return JSONResponse(user)

    role_dep = session_auth.require_role("admin", "operador")

    async def role_only(request):
        user = role_dep(request)
        return JSONResponse(user)

    async def login(request):
        resp = PlainTextResponse("ok")
        session_auth.create_session_cookie(resp, request.query_params["username"])
        return resp

    async def logout(request):
        resp = PlainTextResponse("ok")
        session_auth.clear_session_cookie(resp)
        return resp

    return Starlette(
        routes=[
            Route("/protected", protected),
            Route("/admin-only", admin_only),
            Route("/role-only", role_only),
            Route("/login", login),
            Route("/logout", logout),
        ]
    )


def _client(session_auth):
    app = _make_session_app(session_auth)
    return TestClient(app, base_url="https://testserver")


def test_require_auth_redirects_without_session():
    client = _client(_make_session_auth())
    r = client.get("/protected", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/login"


def test_login_then_require_auth_succeeds():
    client = _client(_make_session_auth())
    client.get("/login?username=oper1")
    r = client.get("/protected")
    assert r.status_code == 200
    assert r.text == "hello oper1"


def test_logout_clears_session():
    client = _client(_make_session_auth())
    client.get("/login?username=oper1")
    client.get("/logout")
    r = client.get("/protected", follow_redirects=False)
    assert r.status_code == 307


def test_require_admin_redirects_non_admin_to_dashboard():
    client = _client(_make_session_auth())
    client.get("/login?username=oper1")
    r = client.get("/admin-only", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/dashboard"


def test_require_admin_passes_for_admin():
    client = _client(_make_session_auth())
    client.get("/login?username=admin1")
    r = client.get("/admin-only")
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_require_role_accepts_any_listed_role():
    client = _client(_make_session_auth())
    client.get("/login?username=oper1")
    r = client.get("/role-only")
    assert r.status_code == 200


def test_require_role_rejects_role_not_listed():
    client = _client(_make_session_auth())
    client.get("/login?username=cajero1")
    r = client.get("/role-only", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/dashboard"


def test_check_credentials_true_for_valid_password():
    auth = _make_session_auth()
    assert auth.check_credentials("admin1", "adminpw") is True


def test_check_credentials_false_for_invalid_password():
    auth = _make_session_auth()
    assert auth.check_credentials("admin1", "wrong") is False


def test_check_credentials_false_for_unknown_user():
    auth = _make_session_auth()
    assert auth.check_credentials("nadie", "x") is False


def test_tampered_cookie_treated_as_anonymous():
    client = _client(_make_session_auth())
    client.get("/login?username=oper1")
    client.cookies.set("cl_session", client.cookies.get("cl_session") + "tampered")
    r = client.get("/protected", follow_redirects=False)
    assert r.status_code == 307


def test_secret_key_dev_fallback_when_env_development(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("ENV", "development")
    auth = _make_session_auth(dev_secret_fallback="dev-fallback-key")
    assert auth.secret_key == "dev-fallback-key"


def test_secret_key_fail_fast_without_env_development(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY no está seteado"):
        _make_session_auth()


def test_secret_key_from_env_takes_priority(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "from-env")
    monkeypatch.setenv("ENV", "development")
    auth = _make_session_auth(dev_secret_fallback="dev-fallback-key")
    assert auth.secret_key == "from-env"
    monkeypatch.delenv("SECRET_KEY", raising=False)


# ── AdminAuth ─────────────────────────────────────────────────────────────


def _make_admin_auth(monkeypatch, panel_pass="secretpass", **overrides):
    monkeypatch.setenv("SECRET_KEY", "admin-test-secret")
    monkeypatch.setenv("ADMIN_PANEL_USER", "superadmin")
    if panel_pass is not None:
        monkeypatch.setenv("ADMIN_PANEL_PASSWORD", panel_pass)
    else:
        monkeypatch.delenv("ADMIN_PANEL_PASSWORD", raising=False)
    kwargs = dict(dev_secret_fallback="admin-dev-secret")
    kwargs.update(overrides)
    return AdminAuth(**kwargs)


def _make_admin_app(admin_auth):
    async def require_login_view(request):
        user = admin_auth.require_login(request)
        return PlainTextResponse(f"hello {user}")

    async def login(request):
        resp = PlainTextResponse("ok")
        admin_auth.create_session_cookie(resp, request.query_params["username"])
        return resp

    return Starlette(
        routes=[
            Route("/require-login", require_login_view),
            Route("/login", login),
        ]
    )


def test_admin_check_credentials_correct(monkeypatch):
    auth = _make_admin_auth(monkeypatch)
    assert auth.check_credentials("superadmin", "secretpass") is True


def test_admin_check_credentials_wrong_password(monkeypatch):
    auth = _make_admin_auth(monkeypatch)
    assert auth.check_credentials("superadmin", "nope") is False


def test_admin_check_credentials_fail_closed_without_panel_pass(monkeypatch):
    auth = _make_admin_auth(monkeypatch, panel_pass=None)
    assert auth.check_credentials("superadmin", "") is False


def test_admin_rate_limit_under_threshold(monkeypatch):
    auth = _make_admin_auth(monkeypatch)
    for _ in range(4):
        auth.registrar_intento_fallido("1.2.3.4")
    assert auth.rate_limit_excedido("1.2.3.4") is False


def test_admin_rate_limit_over_threshold(monkeypatch):
    auth = _make_admin_auth(monkeypatch)
    for _ in range(5):
        auth.registrar_intento_fallido("1.2.3.4")
    assert auth.rate_limit_excedido("1.2.3.4") is True


def test_admin_rate_limit_ignores_empty_ip(monkeypatch):
    auth = _make_admin_auth(monkeypatch)
    assert auth.rate_limit_excedido("") is False


def test_admin_require_login_redirects_without_session(monkeypatch):
    auth = _make_admin_auth(monkeypatch)
    app = _make_admin_app(auth)
    client = TestClient(app, base_url="https://testserver")
    r = client.get("/require-login", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/login"


def test_admin_require_login_succeeds_after_login(monkeypatch):
    auth = _make_admin_auth(monkeypatch)
    app = _make_admin_app(auth)
    client = TestClient(app, base_url="https://testserver")
    client.get("/login?username=superadmin")
    r = client.get("/require-login")
    assert r.status_code == 200
    assert r.text == "hello superadmin"


def test_admin_secret_key_dev_fallback(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("ENV", "development")
    auth = AdminAuth(dev_secret_fallback="admin-dev-fallback")
    assert auth.secret_key == "admin-dev-fallback"


def test_admin_secret_key_fail_fast_without_env_development(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    with pytest.raises(RuntimeError, match="backoffice de superadmin"):
        AdminAuth(dev_secret_fallback="admin-dev-fallback")
