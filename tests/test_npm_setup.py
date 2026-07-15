"""
Tests de libracore.npm_setup.main(). El flujo es interactivo (input()) —
se mockean las respuestas y NPMClient.ping().
"""
import json

import pytest

from libracore import npm_api, npm_setup


@pytest.fixture(autouse=True)
def _reset_config():
    npm_api._config_file = None
    yield
    npm_api._config_file = None


@pytest.fixture
def cfg(tmp_path):
    npm_api.configure(config_file=tmp_path / ".npm_config.json")
    return tmp_path / ".npm_config.json"


def _answers(*vals):
    it = iter(vals)
    return lambda *_: next(it)


def test_main_guarda_config_tras_ping_exitoso(cfg, monkeypatch, capsys):
    monkeypatch.setattr(npm_setup, "input", _answers(
        "http://npm:81",       # npm_url
        "admin@test.com",      # npm_email
        "secret",              # npm_password
        "172.17.0.1",          # forward_host
        "",                    # le_email (usa default = npm_email)
    ), raising=False)
    monkeypatch.setattr(npm_api.NPMClient, "ping", lambda self: True)

    npm_setup.main(product_name="TESTPROD")

    saved = json.loads(cfg.read_text())
    assert saved["npm_url"] == "http://npm:81"
    assert saved["npm_email"] == "admin@test.com"
    assert saved["le_email"] == "admin@test.com"

    out = capsys.readouterr().out
    assert "TESTPROD — Configuración Nginx Proxy Manager" in out
    assert "Config guardada" in out


def test_main_ping_fallido_no_guarda_config(cfg, monkeypatch):
    monkeypatch.setattr(npm_setup, "input", _answers(
        "http://npm:81", "admin@test.com", "wrong", "172.17.0.1", "",
    ), raising=False)
    monkeypatch.setattr(npm_api.NPMClient, "ping", lambda self: False)

    with pytest.raises(SystemExit):
        npm_setup.main(product_name="TESTPROD")

    assert not cfg.exists()


def test_main_usa_config_existente_como_default(cfg, monkeypatch):
    npm_api.save_config({
        "npm_url": "http://viejo:81", "npm_email": "old@test.com",
        "npm_password": "oldpass", "forward_host": "10.0.0.1", "le_email": "old@test.com",
    })
    # Todas las respuestas vacías → debe conservar los defaults existentes.
    monkeypatch.setattr(npm_setup, "input", _answers("", "", "", "", ""), raising=False)
    monkeypatch.setattr(npm_api.NPMClient, "ping", lambda self: True)

    npm_setup.main(product_name="TESTPROD")

    saved = json.loads(cfg.read_text())
    assert saved["npm_url"] == "http://viejo:81"
    assert saved["forward_host"] == "10.0.0.1"
