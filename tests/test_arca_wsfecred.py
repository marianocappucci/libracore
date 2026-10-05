"""`libracore.arca_wsfecred` contra respuestas REALES de homologación (`tests/fixtures_wsfecred/`, ver su README).

Lo que se fija acá es lo medido el 2026-10-05: cómo hay que pedir (namespace sólo en la raíz, orden de los campos,
`dummy` con el Body vacío) y qué devuelve ARCA (la cuenta corriente de una FCE con su saldo, la obligación de recepción
con `montoDesde`, los errores `1102`/`1105`).
"""

import asyncio
import datetime
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from libracore import arca_wsfecred as w

FIXTURES = Path(__file__).parent / "fixtures_wsfecred"
_RealAsyncClient = httpx.AsyncClient
HOY = datetime.date(2026, 10, 5)


def _respuesta(nombre: str) -> str:
    return (FIXTURES / f"{nombre}.xml").read_text()


@pytest.fixture
def arca(monkeypatch):
    """ARCA de mentira: contesta con el fixture que se le diga y guarda lo que se le pidió."""
    pedidos = []
    estado = {"respuesta": "", "status": 200}

    def handler(request):
        pedidos.append(request)
        return httpx.Response(estado["status"], text=estado["respuesta"])

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(**kwargs)

    monkeypatch.setattr(w.httpx, "AsyncClient", factory)

    def contestar(nombre=None, *, texto=None, status=200):
        estado["respuesta"] = texto if texto is not None else _respuesta(nombre)
        estado["status"] = status
        return pedidos

    return contestar


def _body(pedido) -> ET.Element:
    """El primer hijo del Body del pedido (o `None` si va vacío)."""
    root = ET.fromstring(pedido.content)
    body = next(e for e in root.iter() if e.tag.endswith("Body"))
    return next(iter(body), None)


# ── Cómo se pide ───────────────────────────────────────────────────────────

def test_dummy_va_con_el_body_vacio(arca):
    """Medido: con un elemento adentro, el balanceador devuelve una línea suelta `BL… 500` y no un SOAP."""
    pedidos = arca("dummy")
    assert asyncio.run(w.dummy("homologacion")) == {"appserver": "OK", "authserver": "OK", "dbserver": "OK"}
    assert _body(pedidos[0]) is None
    assert pedidos[0].headers["SOAPAction"] == "http://ar.gob.afip.wsfecred/FECredService/dummy"


def test_el_namespace_va_solo_en_la_raiz_y_los_campos_en_orden(arca):
    pedidos = arca("monto_obligado_si")
    asyncio.run(w.monto_obligado("20-11111111-2", "30-33333333-4", HOY, "TKN", "SGN", "homologacion"))
    raiz = _body(pedidos[0])
    assert raiz.tag == "{http://ar.gob.afip.wsfecred/FECredService/}consultarMontoObligadoRecepcionRequest"
    assert [h.tag for h in raiz] == ["authRequest", "cuitConsultada", "fechaEmision"], "sin namespace y en orden"
    auth = raiz.find("authRequest")
    assert [(h.tag, h.text) for h in auth] == [("token", "TKN"), ("sign", "SGN"), ("cuitRepresentada", "20111111112")]
    assert (raiz.findtext("cuitConsultada"), raiz.findtext("fechaEmision")) == ("30333333334", "2026-10-05")
    assert pedidos[0].headers["SOAPAction"].endswith("/consultarMontoObligadoRecepcion")


def test_la_url_sale_del_ambiente(arca):
    pedidos = arca("dummy")
    asyncio.run(w.dummy("homologacion"))
    asyncio.run(w.dummy("produccion"))
    assert str(pedidos[0].url) == "https://fwshomo.afip.gob.ar/wsfecred/FECredService"
    assert str(pedidos[1].url) == "https://serviciosjava.afip.gov.ar/wsfecred/FECredService"


# ── La obligación de recepción ─────────────────────────────────────────────

def test_un_receptor_obligado_trae_desde_que_monto(arca):
    arca("monto_obligado_si")
    m = asyncio.run(w.monto_obligado("20111111112", "30333333334", HOY, "T", "S", "homologacion"))
    assert m == w.MontoObligado(obligado=True, monto_desde=Decimal("3958316"))
    assert m.corresponde("3958316.00") and m.corresponde(5_000_000)
    assert not m.corresponde("3958315.99"), "por debajo del monto no corresponde"


def test_un_receptor_no_obligado_nunca_corresponde(arca):
    arca("monto_obligado_no")
    m = asyncio.run(w.monto_obligado("20111111112", "30222222223", HOY, "T", "S", "homologacion"))
    assert m == w.MontoObligado(obligado=False, monto_desde=None)
    assert not m.corresponde(10**9)


# ── El estado de una FCE ───────────────────────────────────────────────────

def test_el_estado_de_una_fce_trae_su_cuenta_corriente_y_el_saldo(arca):
    """Medido: FCE A 1-12 por 1210 con una nota de crédito parcial de 121 → saldo 1089 (el `saldo_acreditable` del motor)."""
    pedidos = arca("ctacte_por_factura")
    e = asyncio.run(w.estado_de_fce("20111111112", 201, 1, 12, "T", "S", "homologacion"))
    assert (e.cuenta, e.estado, e.estado_cuenta) == (450109, "PendienteRecepcion", "Modificable")
    assert (e.importe_inicial, e.notas, e.saldo) == (Decimal("1210.00"), Decimal("-121.00"), Decimal("1089.00"))
    assert e.notas_asociadas == (w.NotaAsociada(203, 1, 4, Decimal("121.00"), False),)
    assert not e.rechazada
    # Se pide por la factura (el `choice` de IdCtaCteType), no por el código de la cuenta.
    id_ = _body(pedidos[0]).find("idCtaCte/idFactura")
    assert [(h.tag, h.text) for h in id_] == [("CUITEmisor", "20111111112"), ("codTipoCmp", "201"),
                                             ("ptoVta", "1"), ("nroCmp", "12")]


def test_una_fce_rechazada_lo_dice(arca):
    """El rechazo no se pudo medir todavía (hace falta un segundo certificado, del comprador): se arma desde la
    respuesta real cambiando sólo el estado, que es el valor de la enumeración del WSDL."""
    texto = _respuesta("ctacte_por_factura").replace(
        "<estado>PendienteRecepcion</estado>", "<estado>Rechazado</estado>", 1)
    arca(texto=texto)
    assert asyncio.run(w.estado_de_fce("20111111112", 201, 1, 12, "T", "S", "homologacion")).rechazada


def test_una_fce_que_arca_no_tiene_da_fce_no_registrada(arca):
    arca("ctacte_inexistente")
    with pytest.raises(w.FceNoRegistrada) as e:
        asyncio.run(w.estado_de_fce("20111111112", 201, 1, 99999, "T", "S", "homologacion"))
    assert e.value.codigos == [(1102, "No existe la cuenta corriente indicada")]


# ── El historial ───────────────────────────────────────────────────────────

def test_el_historial_trae_los_estados_con_su_hora(arca):
    arca("historial")
    assert asyncio.run(w.historial("20111111112", 201, 1, 12, "T", "S", "homologacion")) == [
        ("PendienteRecepcion", "2026-10-05T01:52:56")]


def test_el_historial_de_un_comprobante_inexistente_da_fce_no_registrada(arca):
    arca("historial_inexistente")
    with pytest.raises(w.FceNoRegistrada, match=r"\[1105\]"):
        asyncio.run(w.historial("20111111112", 201, 1, 99999, "T", "S", "homologacion"))


# ── Cuando ARCA no contesta lo esperado ────────────────────────────────────

def test_un_fault_de_soap_se_dice_tal_cual(arca):
    arca(texto=("<S:Envelope xmlns:S='http://schemas.xmlsoap.org/soap/envelope/'><S:Body><S:Fault>"
                "<faultcode>S:Server</faultcode><faultstring>Token vencido</faultstring></S:Fault></S:Body></S:Envelope>"),
         status=500)
    with pytest.raises(RuntimeError, match="WSFECRED: Token vencido"):
        asyncio.run(w.dummy("homologacion"))


def test_una_respuesta_que_no_es_soap_no_explota_con_un_parse_error(arca):
    """Medido: el balanceador devuelve `BL… 500` (texto plano) ante un pedido mal formado."""
    arca(texto="BL9939339626552 2026-10-05 08:35:49 500", status=500)
    with pytest.raises(RuntimeError, match="no es SOAP"):
        asyncio.run(w.dummy("homologacion"))


def test_un_error_que_no_es_de_registro_es_error_wsfecred_pero_no_fce_no_registrada(arca):
    texto = _respuesta("ctacte_inexistente").replace("1102", "1001").replace(
        "No existe la cuenta corriente indicada", "Otro error")
    arca(texto=texto)
    with pytest.raises(w.ErrorWsfecred) as e:
        asyncio.run(w.estado_de_fce("20111111112", 201, 1, 1, "T", "S", "homologacion"))
    assert not isinstance(e.value, w.FceNoRegistrada)


# ── El aviso al emitir: `corresponde_fce` (ADR-019, decisión 2) ────────────

CFG = {"cuit": "20111111112", "ambiente": "homologacion"}


@pytest.fixture
def con_credenciales(monkeypatch):
    """Un certificado en disco y un WSAA que da ticket para `wsfecred` (y anota para qué servicio se lo pidieron)."""
    servicios = []

    async def autenticar(cert, clave, ambiente, servicio="wsfe"):
        servicios.append(servicio)
        return {"token": "T", "sign": "S"}

    monkeypatch.setattr(w.arca_credenciales, "paths_en_disco", lambda cfg: ("/c.crt", "/c.key"))
    monkeypatch.setattr(w.arca_wsaa, "autenticar", autenticar)
    return servicios


def test_corresponde_fce_desde_el_monto_del_registro(arca, con_credenciales):
    arca("monto_obligado_si")
    assert asyncio.run(w.corresponde_fce(CFG, "30333333334", "4000000.00", HOY)) == {
        "disponible": True, "corresponde": True, "obligado": True, "monto_desde": "3958316"}
    assert con_credenciales == ["wsfecred"], "el ticket es el del registro de FCE, no el de WSFE"
    assert asyncio.run(w.corresponde_fce(CFG, "30333333334", "100000.00", HOY))["corresponde"] is False


def test_a_un_receptor_no_obligado_no_le_corresponde(arca, con_credenciales):
    arca("monto_obligado_no")
    r = asyncio.run(w.corresponde_fce(CFG, "30222222223", "99999999", HOY))
    assert (r["disponible"], r["corresponde"], r["obligado"], r["monto_desde"]) == (True, False, False, None)


def test_sin_arca_configurado_no_se_puede_preguntar_y_no_falla():
    r = asyncio.run(w.corresponde_fce(None, "30222222223", 1, HOY))
    assert r["disponible"] is False and "ARCA no está configurado" in r["motivo"]


def test_un_certificado_sin_wsfecred_lo_dice_y_no_frena(monkeypatch):
    """Medido el 2026-10-05: WSAA contesta `coe.notAuthorized` si el certificado no tiene el servicio."""
    async def autenticar(*a, **kw):
        raise RuntimeError("WSAA error [ns1:coe.notAuthorized]: Computador no autorizado a acceder al servicio")

    monkeypatch.setattr(w.arca_credenciales, "paths_en_disco", lambda cfg: ("/c.crt", "/c.key"))
    monkeypatch.setattr(w.arca_wsaa, "autenticar", autenticar)
    r = asyncio.run(w.corresponde_fce(CFG, "30222222223", 1, HOY))
    assert r["disponible"] is False
    assert "no tiene autorizado el servicio wsfecred" in r["motivo"]


def test_arca_caido_no_frena(arca, con_credenciales):
    arca(texto="BL9939339626552 2026-10-05 08:35:49 500", status=500)
    r = asyncio.run(w.corresponde_fce(CFG, "30222222223", 1, HOY))
    assert r["disponible"] is False and "no es SOAP" in r["motivo"]
