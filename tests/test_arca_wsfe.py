import asyncio

import httpx
import pytest

from libracore import arca_wsfe

_RealAsyncClient = httpx.AsyncClient


def _client_factory(handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(**kwargs)
    return factory


def _patch_client(monkeypatch, handler):
    monkeypatch.setattr(arca_wsfe.httpx, "AsyncClient", _client_factory(handler))


def test_iva_id_mapea_porcentajes_conocidos():
    assert arca_wsfe._iva_id(21) == 5
    assert arca_wsfe._iva_id(10.5) == 4
    assert arca_wsfe._iva_id(0) == 3
    assert arca_wsfe._iva_id(27) == 6


def test_iva_id_desconocido_cae_a_21():
    assert arca_wsfe._iva_id(15) == 5


def test_auth_arma_bloque_con_cuit_sin_guiones():
    xml = arca_wsfe._auth("TKN", "SGN", "20-12345678-9")
    assert "<Cuit>20123456789</Cuit>" in xml
    assert "<Token>TKN</Token>" in xml


def test_cbte_asoc_block_vacio_sin_datos():
    assert arca_wsfe._cbte_asoc_block({}, "20-12345678-9") == ""


def test_cbte_asoc_block_con_datos():
    factura = {"cbte_asoc_tipo": 1, "cbte_asoc_pv": 2, "cbte_asoc_nro": 55}
    xml = arca_wsfe._cbte_asoc_block(factura, "20-12345678-9")
    assert "<Tipo>1</Tipo>" in xml
    assert "<Nro>55</Nro>" in xml
    assert "<Cuit>20123456789</Cuit>" in xml


def test_ultimo_numero_autorizado_parsea_respuesta(monkeypatch):
    def handler(request):
        assert request.method == "POST"
        body = (
            "<soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>"
            "<soap:Body><FECompUltimoAutorizadoResponse>"
            "<FECompUltimoAutorizadoResult><CbteNro>142</CbteNro></FECompUltimoAutorizadoResult>"
            "</FECompUltimoAutorizadoResponse></soap:Body></soap:Envelope>"
        )
        return httpx.Response(200, text=body)

    _patch_client(monkeypatch, handler)
    result = asyncio.run(
        arca_wsfe.ultimo_numero_autorizado(1, 6, "20-12345678-9", "TKN", "SGN", "homologacion")
    )
    assert result == 142


def test_soap_fault_lanza_runtime_error(monkeypatch):
    def handler(request):
        body = (
            "<soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>"
            "<soap:Body><soap:Fault><faultstring>Auth invalida</faultstring></soap:Fault>"
            "</soap:Body></soap:Envelope>"
        )
        return httpx.Response(200, text=body)

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="Auth invalida"):
        asyncio.run(
            arca_wsfe.ultimo_numero_autorizado(1, 6, "20-12345678-9", "TKN", "SGN", "homologacion")
        )


def _factura_base(**overrides):
    factura = {
        "punto_venta": 1,
        "tipo": 6,
        "numero": 100,
        "fecha": "2026-07-13",
        "concepto": 1,
        "subtotal": 100.0,
        "iva_amount": 21.0,
        "total": 121.0,
        "cliente_cuit": "20123456789",
    }
    factura.update(overrides)
    return factura


def test_solicitar_cae_exito(monkeypatch):
    def handler(request):
        body = (
            "<soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>"
            "<soap:Body><FECAESolicitarResponse><FECAESolicitarResult><FeDetResp>"
            "<FECAEDetResponse><Resultado>A</Resultado><CAE>75312345678901</CAE>"
            "<CAEFchVto>20260720</CAEFchVto></FECAEDetResponse>"
            "</FeDetResp></FECAESolicitarResult></FECAESolicitarResponse>"
            "</soap:Body></soap:Envelope>"
        )
        return httpx.Response(200, text=body)

    _patch_client(monkeypatch, handler)
    result = asyncio.run(
        arca_wsfe.solicitar_cae(_factura_base(), "20-12345678-9", "TKN", "SGN", "homologacion")
    )
    assert result == {"cae": "75312345678901", "cae_vto": "20260720"}


def test_solicitar_cae_rechazado_junta_observaciones(monkeypatch):
    def handler(request):
        body = (
            "<soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>"
            "<soap:Body><FECAESolicitarResponse><FECAESolicitarResult><FeDetResp>"
            "<FECAEDetResponse><Resultado>R</Resultado>"
            "<Observaciones><Obs><Code>10016</Code><Msg>Doc invalido</Msg></Obs></Observaciones>"
            "</FECAEDetResponse></FeDetResp></FECAESolicitarResult></FECAESolicitarResponse>"
            "</soap:Body></soap:Envelope>"
        )
        return httpx.Response(200, text=body)

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="10016.*Doc invalido"):
        asyncio.run(
            arca_wsfe.solicitar_cae(_factura_base(), "20-12345678-9", "TKN", "SGN", "homologacion")
        )


def test_solicitar_cae_tipo_c_no_lleva_iva(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = request.content.decode()
        body = (
            "<soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>"
            "<soap:Body><FECAESolicitarResponse><FECAESolicitarResult><FeDetResp>"
            "<FECAEDetResponse><Resultado>A</Resultado><CAE>1</CAE><CAEFchVto>20260101</CAEFchVto>"
            "</FECAEDetResponse></FeDetResp></FECAESolicitarResult></FECAESolicitarResponse>"
            "</soap:Body></soap:Envelope>"
        )
        return httpx.Response(200, text=body)

    _patch_client(monkeypatch, handler)
    asyncio.run(
        arca_wsfe.solicitar_cae(_factura_base(tipo=11), "20-12345678-9", "TKN", "SGN", "homologacion")
    )
    assert "<Iva>" not in captured["body"]
    assert "<ImpOpEx>0.00</ImpOpEx>" in captured["body"]
