import asyncio

import httpx
import pytest

from libracore import mp_api

_RealAsyncClient = httpx.AsyncClient


def _client_factory(handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(**kwargs)
    return factory


def _patch_client(monkeypatch, handler):
    monkeypatch.setattr(mp_api.httpx, "AsyncClient", _client_factory(handler))


def test_obtener_usuario_info(monkeypatch):
    def handler(request):
        assert request.url.path == "/users/me"
        assert request.headers["Authorization"] == "Bearer TOKEN"
        return httpx.Response(200, json={"id": "123", "email": "a@b.com"})

    _patch_client(monkeypatch, handler)
    result = asyncio.run(mp_api.obtener_usuario_info("TOKEN"))
    assert result == {"id": "123", "email": "a@b.com"}


def test_obtener_pago_ok(monkeypatch):
    def handler(request):
        assert request.url.path == "/v1/payments/999"
        return httpx.Response(200, json={"id": 999, "status": "approved"})

    _patch_client(monkeypatch, handler)
    result = asyncio.run(mp_api.obtener_pago("999", "TOKEN"))
    assert result["status"] == "approved"


def test_obtener_pago_404_raises_value_error(monkeypatch):
    def handler(request):
        return httpx.Response(404, json={"message": "not found"})

    _patch_client(monkeypatch, handler)
    with pytest.raises(ValueError):
        asyncio.run(mp_api.obtener_pago("999", "TOKEN"))


def test_obtener_movimientos_paginates_until_total_reached(monkeypatch):
    pages = [
        {"results": [{"id": 1}, {"id": 2}], "paging": {"total": 3}},
        {"results": [{"id": 3}], "paging": {"total": 3}},
    ]
    calls = []

    def handler(request):
        calls.append(request.url.params.get("offset"))
        return httpx.Response(200, json=pages[len(calls) - 1])

    _patch_client(monkeypatch, handler)
    result = asyncio.run(mp_api.obtener_movimientos("TOKEN", "2026-01-01", "2026-01-31"))
    assert [r["id"] for r in result] == [1, 2, 3]
    assert calls == ["0", "2"]


def test_obtener_movimientos_stops_when_no_results(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"results": [], "paging": {"total": 0}})

    _patch_client(monkeypatch, handler)
    result = asyncio.run(mp_api.obtener_movimientos("TOKEN", "2026-01-01", "2026-01-31"))
    assert result == []


def test_buscar_pago_por_referencia_found(monkeypatch):
    def handler(request):
        assert request.url.params["external_reference"] == "venta-42"
        return httpx.Response(200, json={"results": [{"id": 1, "status": "approved"}]})

    _patch_client(monkeypatch, handler)
    result = asyncio.run(mp_api.buscar_pago_por_referencia("venta-42", "TOKEN"))
    assert result == {"id": 1, "status": "approved"}


def test_buscar_pago_por_referencia_not_found(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"results": []})

    _patch_client(monkeypatch, handler)
    result = asyncio.run(mp_api.buscar_pago_por_referencia("venta-42", "TOKEN"))
    assert result is None


def test_crear_orden_qr_success(monkeypatch):
    def handler(request):
        assert request.method == "PUT"
        assert request.url.path == "/instore/qrs/merchant/stores/default/pos/POS1/orders"
        return httpx.Response(200, json={"in_store_order_id": "abc123"})

    _patch_client(monkeypatch, handler)
    items = [{"nombre": "Cafe", "qty": 1, "precio": 1000.0, "subtotal": 1000.0, "producto_id": 7}]
    result = asyncio.run(
        mp_api.crear_orden_qr("U1", "POS1", "TOKEN", "venta-1", "Venta 1", items, 1000.0)
    )
    assert result == {"in_store_order_id": "abc123"}


def test_crear_orden_qr_raises_runtime_error_on_failure(monkeypatch):
    def handler(request):
        return httpx.Response(400, text="bad request")

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError):
        asyncio.run(mp_api.crear_orden_qr("U1", "POS1", "TOKEN", "venta-1", "Venta 1", [], 0.0))


def test_eliminar_orden_qr(monkeypatch):
    def handler(request):
        assert request.method == "DELETE"
        assert request.url.path == "/instore/qrs/merchant/stores/default/pos/POS1/orders"
        return httpx.Response(200)

    _patch_client(monkeypatch, handler)
    asyncio.run(mp_api.eliminar_orden_qr("POS1", "TOKEN"))
