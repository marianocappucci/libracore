"""
Tests de libracore.npm_api. httpx.get/post/put/delete se mockean
directamente (NPMClient los llama como funciones sueltas, no vía un
httpx.Client persistente, así que no hace falta httpx.MockTransport).
"""
import json

import httpx
import pytest

from libracore import npm_api


@pytest.fixture(autouse=True)
def _reset_config():
    npm_api._config_file = None
    yield
    npm_api._config_file = None


def test_config_file_sin_configurar_lanza_runtime_error():
    with pytest.raises(RuntimeError):
        npm_api.config_file()


def test_configure_fija_config_file(tmp_path):
    path = tmp_path / ".npm_config.json"
    npm_api.configure(config_file=path)
    assert npm_api.config_file() == path


def test_load_config_inexistente_devuelve_none(tmp_path):
    npm_api.configure(config_file=tmp_path / ".npm_config.json")
    assert npm_api.load_config() is None


def test_save_y_load_config_roundtrip(tmp_path):
    npm_api.configure(config_file=tmp_path / ".npm_config.json")
    npm_api.save_config({"npm_url": "http://npm:81", "npm_email": "a@b.com"})
    assert npm_api.load_config() == {"npm_url": "http://npm:81", "npm_email": "a@b.com"}


def test_client_from_config_sin_config_devuelve_none(tmp_path):
    npm_api.configure(config_file=tmp_path / ".npm_config.json")
    assert npm_api.client_from_config() is None


def test_client_from_config_con_config(tmp_path):
    npm_api.configure(config_file=tmp_path / ".npm_config.json")
    npm_api.save_config({
        "npm_url": "http://npm:81", "npm_email": "a@b.com", "npm_password": "secret",
    })
    client = npm_api.client_from_config()
    assert isinstance(client, npm_api.NPMClient)
    assert client.base_url == "http://npm:81"


def test_forward_host_y_le_email_from_config_defaults(tmp_path):
    npm_api.configure(config_file=tmp_path / ".npm_config.json")
    assert npm_api.forward_host_from_config() == "172.17.0.1"
    assert npm_api.le_email_from_config() == ""


def test_forward_host_y_le_email_from_config_valores_guardados(tmp_path):
    npm_api.configure(config_file=tmp_path / ".npm_config.json")
    npm_api.save_config({"forward_host": "10.0.0.5", "le_email": "ssl@test.com"})
    assert npm_api.forward_host_from_config() == "10.0.0.5"
    assert npm_api.le_email_from_config() == "ssl@test.com"


def _patch_httpx(monkeypatch, get=None, post=None, put=None, delete=None):
    if get is not None:
        monkeypatch.setattr(npm_api.httpx, "get", get)
    if post is not None:
        monkeypatch.setattr(npm_api.httpx, "post", post)
    if put is not None:
        monkeypatch.setattr(npm_api.httpx, "put", put)
    if delete is not None:
        monkeypatch.setattr(npm_api.httpx, "delete", delete)


def test_authenticate_ok_guarda_token(monkeypatch):
    def fake_post(url, **kwargs):
        assert url.endswith("/api/tokens")
        return httpx.Response(200, json={"token": "TOK123"})

    _patch_httpx(monkeypatch, post=fake_post)
    client = npm_api.NPMClient("http://npm:81", "a@b.com", "secret")
    assert client._authenticate() == "TOK123"
    assert client._token == "TOK123"


def test_authenticate_fallida_lanza_npm_error(monkeypatch):
    def fake_post(url, **kwargs):
        return httpx.Response(401, text="bad credentials")

    _patch_httpx(monkeypatch, post=fake_post)
    client = npm_api.NPMClient("http://npm:81", "a@b.com", "wrong")
    with pytest.raises(npm_api.NPMError):
        client._authenticate()


def test_list_proxy_hosts_reautentica_en_401(monkeypatch):
    calls = {"auth": 0, "get": 0}

    def fake_post(url, **kwargs):
        calls["auth"] += 1
        return httpx.Response(200, json={"token": f"TOK{calls['auth']}"})

    def fake_get(url, **kwargs):
        calls["get"] += 1
        if calls["get"] == 1:
            return httpx.Response(401)
        return httpx.Response(200, json=[{"id": 1, "domain_names": ["a.test"]}])

    _patch_httpx(monkeypatch, get=fake_get, post=fake_post)
    client = npm_api.NPMClient("http://npm:81", "a@b.com", "secret")
    client._token = "STALE"
    hosts = client.list_proxy_hosts()
    assert hosts[0]["domain_names"] == ["a.test"]
    assert calls["get"] == 2  # 1 falla con 401, reautentica, reintenta


def test_get_proxy_host_by_domain_encuentra_y_no_encuentra(monkeypatch):
    def fake_post(url, **kwargs):
        return httpx.Response(200, json={"token": "TOK"})

    def fake_get(url, **kwargs):
        return httpx.Response(200, json=[{"id": 5, "domain_names": ["existe.test"]}])

    _patch_httpx(monkeypatch, get=fake_get, post=fake_post)
    client = npm_api.NPMClient("http://npm:81", "a@b.com", "secret")
    assert client.get_proxy_host_by_domain("existe.test")["id"] == 5
    assert client.get_proxy_host_by_domain("no-existe.test") is None


def test_create_proxy_host_con_ssl_pide_certificado(monkeypatch):
    captured = {}

    def fake_post(url, json=None, **kwargs):
        if url.endswith("/api/tokens"):
            return httpx.Response(200, json={"token": "TOK"})
        captured["body"] = json
        return httpx.Response(201, json={"id": 7})

    _patch_httpx(monkeypatch, post=fake_post)
    client = npm_api.NPMClient("http://npm:81", "a@b.com", "secret")
    host = client.create_proxy_host(
        domain="cliente.test", forward_host="172.17.0.1", forward_port=8080,
        le_email="ssl@test.com",
    )
    assert host["id"] == 7
    assert captured["body"]["certificate_id"] == "new"
    assert captured["body"]["meta"]["letsencrypt_email"] == "ssl@test.com"


def test_delete_proxy_host(monkeypatch):
    def fake_post(url, **kwargs):
        return httpx.Response(200, json={"token": "TOK"})

    def fake_delete(url, **kwargs):
        assert url.endswith("/api/nginx/proxy-hosts/9")
        return httpx.Response(200)

    _patch_httpx(monkeypatch, post=fake_post, delete=fake_delete)
    client = npm_api.NPMClient("http://npm:81", "a@b.com", "secret")
    assert client.delete_proxy_host(9) is True


def test_ping_ok_y_error(monkeypatch):
    def fake_post_ok(url, **kwargs):
        return httpx.Response(200, json={"token": "TOK"})

    _patch_httpx(monkeypatch, post=fake_post_ok)
    client = npm_api.NPMClient("http://npm:81", "a@b.com", "secret")
    assert client.ping() is True

    def fake_post_fail(url, **kwargs):
        return httpx.Response(401)

    _patch_httpx(monkeypatch, post=fake_post_fail)
    client2 = npm_api.NPMClient("http://npm:81", "a@b.com", "wrong")
    assert client2.ping() is False
