import asyncio

import httpx
import pytest

from libracore import arca_wspadron

_RealAsyncClient = httpx.AsyncClient


def _client_factory(handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(**kwargs)
    return factory


def _patch_client(monkeypatch, handler):
    monkeypatch.setattr(arca_wspadron.httpx, "AsyncClient", _client_factory(handler))


def _persona_juridica_response():
    return (
        "<soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>"
        "<soap:Body><ns:getPersonaResponse xmlns:ns='http://a13.soap.ws.server.puc.sr/'>"
        "<personaReturn><persona>"
        "<tipoPersona>JURIDICA</tipoPersona>"
        "<razonSocial>Acme SA</razonSocial>"
        "<estadoClave>ACTIVO</estadoClave>"
        "<domicilio><tipoDomicilio>FISCAL</tipoDomicilio>"
        "<calle>Av Siempreviva</calle><numero>742</numero>"
        "<localidad>Springfield</localidad><descripcionProvincia>Buenos Aires</descripcionProvincia>"
        "</domicilio>"
        "<impuesto><idImpuesto>32</idImpuesto><estado>ACTIVO</estado></impuesto>"
        "</persona></personaReturn>"
        "</ns:getPersonaResponse></soap:Body></soap:Envelope>"
    )


def test_consultar_persona_juridica(monkeypatch):
    def handler(request):
        return httpx.Response(200, text=_persona_juridica_response())

    _patch_client(monkeypatch, handler)
    result = asyncio.run(
        arca_wspadron.consultar_persona("20111111112", "30222222223", "TKN", "SGN", "homologacion")
    )
    assert result["nombre"] == "Acme SA"
    assert result["estado"] == "ACTIVO"
    assert result["iva_condition"] == "Responsable Inscripto"
    assert "Av Siempreviva 742" in result["domicilio"]


def _persona_fisica_response():
    return (
        "<soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>"
        "<soap:Body><ns:getPersonaResponse xmlns:ns='http://a13.soap.ws.server.puc.sr/'>"
        "<personaReturn><persona>"
        "<tipoPersona>FISICA</tipoPersona>"
        "<apellido>Perez</apellido><nombre>Juan</nombre>"
        "<estadoClave>ACTIVO</estadoClave>"
        "<categoriaMonotributo>2</categoriaMonotributo>"
        "</persona></personaReturn>"
        "</ns:getPersonaResponse></soap:Body></soap:Envelope>"
    )


def test_consultar_persona_fisica_monotributo(monkeypatch):
    def handler(request):
        return httpx.Response(200, text=_persona_fisica_response())

    _patch_client(monkeypatch, handler)
    result = asyncio.run(
        arca_wspadron.consultar_persona("20111111112", "20333333334", "TKN", "SGN", "homologacion")
    )
    assert result["nombre"] == "Perez, Juan"
    assert result["iva_condition"] == "Monotributista"


def test_consultar_persona_no_encontrado(monkeypatch):
    def handler(request):
        body = (
            "<soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>"
            "<soap:Body><soap:Fault><faultstring>CUIT inexistente en el padron</faultstring></soap:Fault>"
            "</soap:Body></soap:Envelope>"
        )
        return httpx.Response(200, text=body)

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="no encontrado en el padrón"):
        asyncio.run(
            arca_wspadron.consultar_persona("20111111112", "20999999999", "TKN", "SGN", "homologacion")
        )


def test_consultar_persona_fault_generico(monkeypatch):
    def handler(request):
        body = (
            "<soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>"
            "<soap:Body><soap:Fault><faultstring>Token expirado</faultstring></soap:Fault>"
            "</soap:Body></soap:Envelope>"
        )
        return httpx.Response(200, text=body)

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="Token expirado"):
        asyncio.run(
            arca_wspadron.consultar_persona("20111111112", "20999999999", "TKN", "SGN", "homologacion")
        )
