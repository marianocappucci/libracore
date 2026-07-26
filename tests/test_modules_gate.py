"""Tests para libracore.modules_gate — extraído 2026-07-26 de
Gestiolibra/MedLibra/VentaLibra, donde era byte-idéntico salvo comentarios.
Ver wiki/analyses/auditoria-duplicacion-familia-libra.md."""
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from libracore.modules_gate import require_module


class _FakeModules:
    def __init__(self, enabled: set):
        self._enabled = enabled

    def is_enabled(self, modulo: str) -> bool:
        return modulo in self._enabled


def _make_app(enabled: set):
    app = FastAPI()
    app.state.modules = _FakeModules(enabled)

    @app.get("/gated", dependencies=[Depends(require_module("facturacion"))])
    def gated():
        return {"ok": True}

    return app


def test_require_module_allows_when_enabled():
    client = TestClient(_make_app({"facturacion"}))
    assert client.get("/gated").status_code == 200


def test_require_module_blocks_with_403_when_disabled():
    client = TestClient(_make_app(set()))
    response = client.get("/gated")
    assert response.status_code == 403
    assert "facturacion" in response.json()["detail"]


def test_require_module_duck_types_any_repository_with_is_enabled():
    """No importa la tecnología de persistencia detrás (SQLAlchemy en
    Gestiolibra/MedLibra, sqlite3 crudo en VentaLibra) — alcanza con
    is_enabled(modulo) -> bool."""

    class _MinimalRepo:
        def is_enabled(self, modulo):
            return True

    app = FastAPI()
    app.state.modules = _MinimalRepo()

    @app.get("/gated", dependencies=[Depends(require_module("cualquiera"))])
    def gated():
        return {"ok": True}

    assert TestClient(app).get("/gated").status_code == 200
