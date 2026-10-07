"""`libracore.arca_wscpe` contra respuestas de ARCA (`tests/fixtures_wscpe/`, ver su README).

Lo que se fija acá: cómo se pide (namespace sólo en la raíz, `auth` con `cuitRepresentada`, CTG o tipo+sucursal+número),
cómo se lee una CPE (kilos de carga y de descarga, transporte, PDF aparte de lo archivado) y los dos errores que
cambian lo que hace el operador: la CPE que ARCA no devuelve (`800`) y el CUIT que no delegó (`soap:Fault`).
"""

import asyncio
import base64
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from libracore import arca_wscpe as w

FIXTURES = Path(__file__).parent / "fixtures_wscpe"
_RealAsyncClient = httpx.AsyncClient
NS = "https://serviciosjava.afip.gob.ar/wscpe/"


def _respuesta(nombre: str) -> str:
    return (FIXTURES / f"{nombre}.xml").read_text(encoding="utf-8")


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
    root = ET.fromstring(pedido.content)
    body = next(e for e in root.iter() if e.tag.endswith("Body"))
    return next(iter(body))


def _consultar(**kw):
    kw.setdefault("ctg", 10100000001)
    kw.setdefault("ambiente", "produccion")
    return asyncio.run(w.consultar_cpe("30-22222222-3", "TKN", "SGN", **kw))


# ── Cómo se pide ───────────────────────────────────────────────────────────

def test_por_ctg_el_namespace_va_solo_en_la_raiz(arca):
    pedidos = arca("cpe_activa")
    _consultar()
    raiz = _body(pedidos[0])
    assert raiz.tag == f"{{{NS}}}ConsultarCPEAutomotorReq"
    assert [h.tag for h in raiz] == ["auth", "solicitud"], "hijos sin namespace"
    assert [(h.tag, h.text) for h in raiz.find("auth")] == [
        ("token", "TKN"), ("sign", "SGN"), ("cuitRepresentada", "30222222223")]
    assert [(h.tag, h.text) for h in raiz.find("solicitud")] == [("nroCTG", "10100000001")]
    assert pedidos[0].headers["SOAPAction"] == f'"{NS}consultarCPEAutomotor"'
    assert str(pedidos[0].url) == "https://cpea-ws.afip.gob.ar/wscpe/services/soap"


def test_por_numero_de_cpe_y_con_solicitante(arca):
    pedidos = arca("cpe_activa")
    _consultar(ctg=None, tipo_cpe=74, sucursal=1, nro_orden=72413, cuit_solicitante="30-44444444-5",
               ambiente="homologacion")
    solicitud = _body(pedidos[0]).find("solicitud")
    assert [h.tag for h in solicitud] == ["cuitSolicitante", "cartaPorte"], "el orden del sequence"
    assert solicitud.findtext("cuitSolicitante") == "30444444445"
    assert [(h.tag, h.text) for h in solicitud.find("cartaPorte")] == [
        ("tipoCPE", "74"), ("sucursal", "1"), ("nroOrden", "72413")]
    assert str(pedidos[0].url) == "https://cpea-ws-qaext.afip.gob.ar/wscpe/services/soap"


def test_sin_ctg_ni_numero_completo_no_sale_a_arca(arca):
    pedidos = arca("cpe_activa")
    with pytest.raises(ValueError, match="CTG"):
        _consultar(ctg=None, tipo_cpe=74, sucursal=1)
    assert pedidos == []


@pytest.mark.parametrize("cuit", ["", "3022222222", "30-22222222-34"])
def test_un_cuit_representado_que_no_es_cuit_no_sale_a_arca(arca, cuit):
    pedidos = arca("cpe_activa")
    with pytest.raises(ValueError, match="11 dígitos"):
        asyncio.run(w.consultar_cpe(cuit, "TKN", "SGN", ctg=1))
    assert pedidos == []


def test_un_ambiente_desconocido_no_cae_a_produccion(arca):
    pedidos = arca("cpe_activa")
    with pytest.raises(ValueError, match="ambiente"):
        _consultar(ambiente="prueba")
    assert pedidos == []


# ── Cómo se lee una CPE ────────────────────────────────────────────────────

def test_lee_la_cpe_activa(arca):
    arca("cpe_activa")
    cpe = _consultar()
    ar = timezone(timedelta(hours=-3))
    assert (cpe.nro_ctg, cpe.tipo_cpe, cpe.numero) == (10100000001, 74, "00001-00072413")
    assert (cpe.estado, cpe.estado_descripcion) == ("AC", "Activa")
    assert cpe.fecha_emision == datetime(2026, 9, 15, 8, 30, tzinfo=ar), "ARCA manda la hora argentina sin zona"
    assert cpe.fecha_vencimiento == datetime(2026, 9, 17, 23, 59, 59, tzinfo=ar)
    assert (cpe.carga.cod_grano, cpe.carga.cosecha) == (23, 2526)
    assert (cpe.carga.peso_bruto, cpe.carga.peso_tara, cpe.carga.peso_neto) == (45200, 15900, 29300)
    assert cpe.carga.peso_neto_descarga is None and not cpe.tiene_descarga, "hasta el arribo no hay descarga"
    assert (cpe.origen.cuit, cpe.origen.cod_localidad, cpe.origen.renspa) == ("20111111112", 5321, "01.001.0.00001/01")
    assert (cpe.destino.cuit, cpe.destino.planta, cpe.destino.cuit_destinatario) == ("30666666667", 1234, "30666666667")
    t = cpe.transporte
    assert (t.cuit_transportista, t.cuit_chofer, t.cuit_pagador_flete) == ("30222222223", "20777777778", "30444444445")
    assert t.dominios == ("AA123BB", "AC456DD")
    assert (t.km, t.tarifa, t.tarifa_referencia) == (310, Decimal("65207.39"), Decimal("60000.00"))
    assert t.fecha_hora_partida == datetime(2026, 9, 15, 9, 0, tzinfo=ar)
    assert t.cuit_intermediario_flete is None and t.mercaderia_fumigada is False
    assert cpe.intervinientes == {"cuitRemitenteComercialVentaPrimaria": "30444444445",
                                  "cuitCorredorVentaPrimaria": "30555555556"}


def test_lee_los_kilos_de_descarga(arca):
    arca("cpe_descargada")
    cpe = _consultar()
    assert cpe.estado == "CN" and cpe.tiene_descarga
    assert (cpe.carga.peso_bruto_descarga, cpe.carga.peso_tara_descarga, cpe.carga.peso_neto_descarga) == (
        45100, 15800, 29300)


def test_el_pdf_viene_aparte_y_no_se_archiva(arca):
    """El PDF pesa: va decodificado en `pdf` y no se repite en lo que se archiva."""
    arca("cpe_activa")
    cpe = _consultar()
    assert cpe.pdf == b"%PDF-1.4 carta de porte de prueba"
    archivado = ET.fromstring(cpe.respuesta_xml)
    assert archivado.find("pdf") is None
    assert archivado.find("cabecera/nroCTG").text == "10100000001", "el resto se archiva entero"
    assert archivado.find("metadata") is not None


def test_un_estado_que_el_manual_no_nombra_viaja_tal_cual(arca):
    arca(texto=_respuesta("cpe_activa").replace("<estado>AC</estado>", "<estado>ZZ</estado>"))
    assert _consultar().estado_descripcion == "ZZ"


def test_respuesta_sin_cabecera_ni_errores_es_un_error(arca):
    arca(texto=_respuesta("cpe_inexistente").replace(
        _respuesta("cpe_inexistente")[_respuesta("cpe_inexistente").index("<errores>"):
                                      _respuesta("cpe_inexistente").index("<metadata>")], ""))
    with pytest.raises(RuntimeError, match="sin cabecera"):
        _consultar()


# ── Los errores ────────────────────────────────────────────────────────────

def test_la_cpe_que_arca_no_devuelve(arca):
    """Medido en producción: un CTG en el que el CUIT representado no interviene da `800`, igual que uno que no existe."""
    arca("cpe_inexistente")
    with pytest.raises(w.CpeNoEncontrada) as e:
        _consultar()
    assert e.value.codigos == [(800, "No existen solicitudes para los parámetros indicados.")]


def test_otro_error_de_arca_no_es_no_encontrada(arca):
    arca(texto=_respuesta("cpe_inexistente").replace("<codigo>800</codigo>", "<codigo>2037</codigo>"))
    with pytest.raises(w.ErrorWscpe) as e:
        _consultar()
    assert not isinstance(e.value, w.CpeNoEncontrada)
    assert e.value.codigos[0][0] == 2037


def test_el_cuit_que_no_delego(arca):
    """Medido en producción: ARCA contesta un `soap:Fault`, no un `errores`. El mensaje dice qué hacer y cita a ARCA."""
    arca("no_relacionada", status=500)
    with pytest.raises(w.CuitNoRelacionado) as e:
        _consultar()
    mensaje = str(e.value)
    assert "Administrador de Relaciones" in mensaje and "próximo ticket" in mensaje
    assert "no esta relacionada con el conjunto { 20111111112 }" in mensaje


def test_otro_fault_es_un_error_generico(arca):
    arca(texto=_respuesta("no_relacionada").replace(
        _respuesta("no_relacionada")[_respuesta("no_relacionada").index("<faultstring>") + 13:
                                     _respuesta("no_relacionada").index("</faultstring>")],
        "Token vencido"), status=500)
    with pytest.raises(w.ErrorWscpe, match="Token vencido") as e:
        _consultar()
    assert not isinstance(e.value, w.CuitNoRelacionado)


def test_una_respuesta_que_no_es_soap(arca):
    arca(texto="BL 502 Bad Gateway", status=502)
    with pytest.raises(RuntimeError, match="no es SOAP"):
        _consultar()


# ── Provincias: probar una representación ──────────────────────────────────

def test_provincias(arca):
    pedidos = arca("provincias")
    assert asyncio.run(w.provincias("20111111112", "TKN", "SGN")) == {
        1: "BUENOS AIRES", 0: "CAP.FEDERAL", 2: "CATAMARCA"}
    raiz = _body(pedidos[0])
    assert raiz.tag == f"{{{NS}}}ConsultarProvinciasReq"
    assert [h.tag for h in raiz] == ["auth"]
    assert pedidos[0].headers["SOAPAction"] == f'"{NS}consultarProvincias"'


def test_provincias_con_un_cuit_que_no_delego(arca):
    arca("no_relacionada", status=500)
    with pytest.raises(w.CuitNoRelacionado):
        asyncio.run(w.provincias("30222222223", "TKN", "SGN"))


# ── El ticket ──────────────────────────────────────────────────────────────

def _token(relaciones: str) -> str:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><sso version="2.0"><id src="CN=wsaa" dst="CN=wscpe"/>'
        '<operation type="login" value="granted"><login entity="33693450239" service="wscpe" '
        f'uid="SERIALNUMBER=CUIT 20111111112, CN=prueba">{relaciones}</login></operation></sso>'
    )
    return base64.b64encode(xml.encode()).decode()


def test_cuits_habilitados_lee_las_relaciones_del_ticket():
    """Medido en producción: el token trae `<relations><relation key="CUIT" reltype="4"/>`."""
    token = _token('<relations><relation key="30222222223" reltype="4"/>'
                   '<relation key="30444444445" reltype="4"/><relation key="30222222223" reltype="4"/></relations>')
    assert w.cuits_habilitados({"token": token}) == ("30222222223", "30444444445")


@pytest.mark.parametrize("ticket", [{}, {"token": "no es base64 !!"}, {"token": base64.b64encode(b"<a").decode()},
                                    {"token": _token("")}])
def test_cuits_habilitados_sin_relaciones_o_ilegible(ticket):
    assert w.cuits_habilitados(ticket) == ()


# ── De punta a punta, con las credenciales del producto ────────────────────

def test_consultar_por_ctg_usa_el_par_del_servicio_y_el_cuit_explicito(arca, monkeypatch):
    pedidos = arca("cpe_activa")
    pedidos_de_par, logins = [], []

    def par(empresa, servicio, ambiente):
        pedidos_de_par.append((empresa, servicio, ambiente))
        return "/certs/wscpe.crt", "/certs/wscpe.key"

    async def autenticar(cert, clave, ambiente, servicio):
        logins.append((cert, clave, ambiente, servicio))
        return {"token": "TKN", "sign": "SGN", "expiracion": "2099-01-01T00:00:00-03:00"}

    monkeypatch.setattr(w.arca_credenciales, "paths_en_disco_de_servicio", par)
    monkeypatch.setattr(w.arca_wsaa, "autenticar", autenticar)
    cpe = asyncio.run(w.consultar_por_ctg("suitrans", "produccion", cuit_representada="30222222223", ctg=10100000001))
    assert cpe.nro_ctg == 10100000001
    assert pedidos_de_par == [("suitrans", "wscpe", "produccion")]
    assert logins == [("/certs/wscpe.crt", "/certs/wscpe.key", "produccion", "wscpe")]
    assert _body(pedidos[0]).find("auth/cuitRepresentada").text == "30222222223"


def test_consultar_por_ctg_exige_el_cuit_representado():
    with pytest.raises(TypeError):
        asyncio.run(w.consultar_por_ctg("suitrans", "produccion", ctg=1))  # type: ignore[call-arg]


def test_consultar_por_ctg_sin_credenciales(arca, monkeypatch):
    pedidos = arca("cpe_activa")
    monkeypatch.setattr(w.arca_credenciales, "paths_en_disco_de_servicio", lambda *a: ("", ""))
    with pytest.raises(w.SinCredenciales, match="Configuración / ARCA"):
        asyncio.run(w.consultar_por_ctg("suitrans", "homologacion", cuit_representada="30222222223", ctg=1))
    assert pedidos == []


def test_consultar_por_ctg_traduce_el_error_de_wsaa(arca, monkeypatch):
    monkeypatch.setattr(w.arca_credenciales, "paths_en_disco_de_servicio", lambda *a: ("c", "k"))

    async def autenticar(*a, **k):
        raise RuntimeError("WSAA rechazo la solicitud: coe.notAuthorized")

    monkeypatch.setattr(w.arca_wsaa, "autenticar", autenticar)
    with pytest.raises(RuntimeError, match="Administrador de Relaciones.*coe.notAuthorized"):
        asyncio.run(w.consultar_por_ctg("suitrans", "produccion", cuit_representada="30222222223", ctg=1))
