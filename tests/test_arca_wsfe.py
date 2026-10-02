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
        "cliente_iva_cond": 5,
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


# ── Condición de IVA del receptor (RG 5616) ─────────────────────────────────


@pytest.mark.parametrize("cod, esperado", [
    (1, 1), (4, 4), (5, 5), (6, 6),
    (3, 15),    # «IVA No Responsable» de la base → «No Alcanzado» de ARCA
    (13, 13),   # un id de ARCA que la base nunca guardó pasa tal cual
])
def test_condicion_receptor_traduce_el_codigo_de_la_base(cod, esperado):
    assert arca_wsfe.condicion_iva_receptor_id(
        _factura_base(tipo=1, cliente_iva_cond=cod)) == esperado


@pytest.mark.parametrize("cond", [0, None])
def test_sin_cuit_ni_condicion_un_b_o_c_va_a_consumidor_final(cond):
    for tipo in (6, 11):
        assert arca_wsfe.condicion_iva_receptor_id(
            _factura_base(tipo=tipo, cliente_iva_cond=cond, cliente_cuit="")) == 5


@pytest.mark.parametrize("tipo, cuit", [(1, ""), (1, "20123456789"), (6, "20123456789")])
def test_sin_condicion_no_se_adivina(tipo, cuit):
    with pytest.raises(RuntimeError, match="condición de IVA del cliente"):
        arca_wsfe.condicion_iva_receptor_id(
            _factura_base(tipo=tipo, cliente_iva_cond=0, cliente_cuit=cuit))


def test_solicitar_cae_manda_la_condicion_del_receptor(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = request.content.decode()
        return httpx.Response(200, text=(
            "<soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>"
            "<soap:Body><FECAESolicitarResponse><FECAESolicitarResult><FeDetResp>"
            "<FECAEDetResponse><Resultado>A</Resultado><CAE>1</CAE><CAEFchVto>20260101</CAEFchVto>"
            "</FECAEDetResponse></FeDetResp></FECAESolicitarResult></FECAESolicitarResponse>"
            "</soap:Body></soap:Envelope>"))

    _patch_client(monkeypatch, handler)
    asyncio.run(arca_wsfe.solicitar_cae(
        _factura_base(tipo=1, cliente_iva_cond=1), "20-12345678-9", "TKN", "SGN", "homologacion"))
    assert "<CondicionIVAReceptorId>1</CondicionIVAReceptorId>" in captured["body"]


# ── FCE MiPyME (medido en homologación el 2026-10-02) ───────────────────────


def _capturar(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = request.content.decode()
        return httpx.Response(200, text=(
            "<soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>"
            "<soap:Body><FECAESolicitarResponse><FECAESolicitarResult><FeDetResp>"
            "<FECAEDetResponse><Resultado>A</Resultado><CAE>1</CAE><CAEFchVto>20260101</CAEFchVto>"
            "</FECAEDetResponse></FeDetResp></FECAESolicitarResult></FECAESolicitarResponse>"
            "</soap:Body></soap:Envelope>"))

    _patch_client(monkeypatch, handler)
    return captured


def _fce(**extra):
    return _factura_base(
        tipo=201, cliente_iva_cond=1, fch_vto_pago="2026-08-30",
        fce_cbu="0" * 22, fce_transmision="SCA", **extra)


def _emitir(factura):
    return asyncio.run(arca_wsfe.solicitar_cae(factura, "20-12345678-9", "T", "S", "homologacion"))


def test_la_fce_manda_vencimiento_cbu_y_transmision(monkeypatch):
    cap = _capturar(monkeypatch)
    _emitir(_fce())
    cuerpo = cap["body"]
    assert "<CbteTipo>201</CbteTipo>" in cuerpo
    # 🔴 aunque el concepto sea Productos, que es cuando otro comprobante no la manda
    assert "<FchVtoPago>20260830</FchVtoPago>" in cuerpo
    assert "<Id>2101</Id><Valor>" + "0" * 22 in cuerpo
    assert "<Id>27</Id><Valor>SCA</Valor>" in cuerpo
    assert "<DocTipo>80</DocTipo>" in cuerpo


def test_una_factura_comun_no_manda_vencimiento_con_concepto_productos(monkeypatch):
    cap = _capturar(monkeypatch)
    _emitir(_factura_base())
    assert "FchVtoPago" not in cap["body"] and "Opcionales" not in cap["body"]


@pytest.mark.parametrize("faltante, mensaje", [
    ({"fch_vto_pago": ""}, "vencimiento de pago"),
    ({"fce_cbu": "123"}, "CBU"),
    ({"fce_cbu": ""}, "CBU"),
    ({"fce_transmision": ""}, "transmisión"),
    ({"fce_transmision": "XXX"}, "transmisión"),
    ({"cliente_cuit": ""}, "CUIT del receptor"),
])
def test_a_la_fce_le_falta_algo_y_falla_antes_de_ir_a_arca(monkeypatch, faltante, mensaje):
    cap = _capturar(monkeypatch)
    with pytest.raises(RuntimeError, match=mensaje):
        _emitir({**_fce(), **faltante})
    assert "body" not in cap, "no tiene que haber salido ningún pedido"


def test_la_fce_c_no_lleva_iva(monkeypatch):
    cap = _capturar(monkeypatch)
    _emitir({**_fce(), "tipo": 211})
    assert "<Iva>" not in cap["body"] and "<ImpOpEx>0.00</ImpOpEx>" in cap["body"]


def test_una_nota_de_fce_lleva_la_fecha_del_asociado_y_solo_el_opcional_22(monkeypatch):
    cap = _capturar(monkeypatch)
    _emitir(_factura_base(
        tipo=203, cliente_iva_cond=1, cbte_asoc_tipo=201, cbte_asoc_pv=1, cbte_asoc_nro=9,
        cbte_asoc_fecha="2026-07-13", fce_anulacion="N",
        # estos NO tienen que salir en una nota: ARCA contesta 10172
        fce_cbu="0" * 22, fce_transmision="SCA"))
    cuerpo = cap["body"]
    assert "<CbteFch>20260713</CbteFch>" in cuerpo
    assert "<Opcional><Id>22</Id><Valor>N</Valor></Opcional>" in cuerpo
    assert "2101" not in cuerpo and "<Id>27</Id>" not in cuerpo


def test_una_nota_de_fce_exige_decir_si_anula(monkeypatch):
    _capturar(monkeypatch)
    with pytest.raises(RuntimeError, match="anula"):
        _emitir(_factura_base(tipo=203, cliente_iva_cond=1, fce_anulacion=""))
