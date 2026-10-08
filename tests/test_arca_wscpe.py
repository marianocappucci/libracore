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


# ══ Emitir (ADR-035) ═══════════════════════════════════════════════════════

from datetime import UTC  # noqa: E402

AR = timezone(timedelta(hours=-3))
SOLICITANTE = "30444444445"


@pytest.fixture
def arca_ops(monkeypatch, tmp_path):
    """ARCA de mentira que contesta por operación (`SOAPAction`): cada una tiene su cola de respuestas.

    Una respuesta es el nombre de un fixture, un texto, o una excepción de `httpx` que se levanta.
    """
    monkeypatch.setenv("ARCA_TA_DIR", str(tmp_path))
    colas: dict[str, list] = {}
    pedidos: list[tuple[str, ET.Element]] = []

    async def handler(request):
        op = request.headers["SOAPAction"].strip('"').rsplit("/", 1)[-1]
        pedidos.append((op, _body(request)))
        # Cede el control como lo haría la red: sin esto dos emisiones simultáneas no se pisan nunca y el
        # test del cerrojo pasa aunque el cerrojo no esté.
        await asyncio.sleep(0.01)
        respuesta = colas[op].pop(0) if len(colas.get(op, [])) > 1 else colas[op][0]
        if isinstance(respuesta, Exception):
            raise respuesta
        texto = respuesta if respuesta.lstrip().startswith("<") or " " in respuesta else _respuesta(respuesta)
        return httpx.Response(200, text=texto)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(**kwargs)

    monkeypatch.setattr(w.httpx, "AsyncClient", factory)

    def contestar(op, *respuestas):
        colas[op] = list(respuestas)
        return pedidos

    return contestar


def _autorizada(ctg=10100000001, nro_orden=1):
    """La respuesta de una autorización: la misma forma que la consulta (`DetalleAutomotorRespuesta`)."""
    return (_respuesta("cpe_activa").replace("ConsultarCPEAutomotorResp", "AutorizarCPEAutomotorResp")
            .replace("<nroOrden>72413</nroOrden>", f"<nroOrden>{nro_orden}</nroOrden>")
            .replace("<nroCTG>10100000001</nroCTG>", f"<nroCTG>{ctg}</nroCTG>"))


def _solicitud(**cambios):
    base = dict(
        cuit_solicitante=SOLICITANTE, sucursal=1, origen=w.OrigenPlanta(12, 5321, 77),
        cod_grano=23, cosecha=2526, peso_bruto=45200, peso_tara=15900,
        destino=w.DestinoSolicitud(cuit="30666666667", cod_provincia=12, cod_localidad=4211, planta=1234),
        cuit_destinatario="30-66666666-7",
        transporte=w.TransporteSolicitud(
            cuit_transportista="30222222223", dominios=("aa123bb", "AC456DD"),
            fecha_hora_partida=datetime(2026, 10, 8, 12, 30, tzinfo=UTC), km=310, cuit_chofer="20777777778",
            cuit_pagador_flete="30444444445", tarifa=Decimal("65207.39")),
        intervinientes={"cuitCorredorVentaPrimaria": "30555555556",
                        "cuitRemitenteComercialVentaPrimaria": "30444444445"},
        observaciones="Viaje de prueba",
    )
    base.update(cambios)
    return w.SolicitudCpe(**base)


def _emitir(solicitud=None, cuit=SOLICITANTE):
    return asyncio.run(w.emitir_cpe(cuit, "TKN", "SGN", solicitud or _solicitud(), ambiente="homologacion"))


# ── Cómo se pide ───────────────────────────────────────────────────────────

def test_emitir_pide_el_ultimo_numero_y_autoriza_el_siguiente(arca_ops):
    arca_ops("consultarUltNroOrden", "ult_nro_orden")
    pedidos = arca_ops("autorizarCPEAutomotor", _autorizada())
    cpe = _emitir()
    assert (cpe.nro_ctg, cpe.numero, cpe.pdf) == (10100000001, "00001-00000001", b"%PDF-1.4 carta de porte de prueba")
    assert [op for op, _ in pedidos] == ["consultarUltNroOrden", "autorizarCPEAutomotor"]
    ult = pedidos[0][1].find("solicitud")
    assert [(h.tag, h.text) for h in ult] == [("sucursal", "1"), ("tipoCPE", "74")]
    assert pedidos[0][1].findtext("auth/cuitRepresentada") == SOLICITANTE


def test_la_solicitud_va_en_el_orden_del_esquema(arca_ops):
    arca_ops("consultarUltNroOrden", "ult_nro_orden")
    pedidos = arca_ops("autorizarCPEAutomotor", _autorizada())
    _emitir()
    raiz = pedidos[1][1]
    assert raiz.tag == f"{{{NS}}}AutorizarCPEAutomotorReq"
    s = raiz.find("solicitud")
    assert [h.tag for h in s] == ["cabecera", "origen", "correspondeRetiroProductor", "esSolicitanteCampo",
                                  "intervinientes", "datosCarga", "destino", "destinatario", "transporte",
                                  "observaciones"]
    assert [(h.tag, h.text) for h in s.find("cabecera")] == [
        ("tipoCP", "74"), ("cuitSolicitante", SOLICITANTE), ("sucursal", "1"), ("nroOrden", "1")]
    assert [(h.tag, h.text) for h in s.find("origen/operador")] == [
        ("codProvincia", "12"), ("codLocalidad", "5321"), ("planta", "77")]
    assert (s.findtext("correspondeRetiroProductor"), s.findtext("esSolicitanteCampo")) == ("false", "false")
    assert [h.tag for h in s.find("intervinientes")] == [
        "cuitRemitenteComercialVentaPrimaria", "cuitCorredorVentaPrimaria"], "el orden del esquema, no el del dict"
    assert [(h.tag, h.text) for h in s.find("datosCarga")] == [
        ("codGrano", "23"), ("cosecha", "2526"), ("pesoBruto", "45200"), ("pesoTara", "15900")]
    assert [(h.tag, h.text) for h in s.find("destino")] == [
        ("cuit", "30666666667"), ("esDestinoCampo", "false"), ("codProvincia", "12"), ("codLocalidad", "4211"),
        ("planta", "1234")]
    assert s.findtext("destinatario/cuit") == "30666666667"
    assert [(h.tag, h.text) for h in s.find("transporte")] == [
        ("cuitTransportista", "30222222223"), ("dominio", "AA123BB"), ("dominio", "AC456DD"),
        ("fechaHoraPartida", "2026-10-08T09:30:00"), ("kmRecorrer", "310"), ("cuitChofer", "20777777778"),
        ("tarifa", "65207.39"), ("cuitPagadorFlete", "30444444445"), ("mercaderiaFumigada", "false")]
    assert s.findtext("observaciones") == "Viaje de prueba"


def test_origen_en_campo_va_con_solicitante_campo_y_renspa(arca_ops):
    """Medido: con origen en campo, `esSolicitanteCampo=false` da 949. Sale del origen, no se elige."""
    arca_ops("consultarUltNroOrden", "ult_nro_orden")
    pedidos = arca_ops("autorizarCPEAutomotor", _autorizada())
    _emitir(_solicitud(origen=w.OrigenCampo(12, 14, renspa="01.001.0.00001/01"), intervinientes=None,
                       cuit_remitente_comercial_productor="20111111112"))
    s = pedidos[1][1].find("solicitud")
    assert [(h.tag, h.text) for h in s.find("origen/productor")] == [
        ("codProvincia", "12"), ("codLocalidad", "14"), ("nroRenspa", "01.001.0.00001/01")]
    assert (s.findtext("correspondeRetiroProductor"), s.findtext("esSolicitanteCampo")) == ("true", "true")
    assert s.findtext("retiroProductor/cuitRemitenteComercialProductor") == "20111111112"
    assert s.find("intervinientes") is None


def test_un_interviniente_que_el_esquema_no_tiene_no_sale(arca_ops):
    pedidos = arca_ops("consultarUltNroOrden", "ult_nro_orden")
    with pytest.raises(ValueError, match="cuitInventado"):
        _emitir(_solicitud(intervinientes={"cuitInventado": "30555555556"}))
    assert [op for op, _ in pedidos] == ["consultarUltNroOrden"], "falla al armar, antes de autorizar"


# ── Antes de llamar ────────────────────────────────────────────────────────

def test_se_emite_en_nombre_del_solicitante(arca_ops):
    pedidos = arca_ops("consultarUltNroOrden", "ult_nro_orden")
    with pytest.raises(ValueError, match="solicitante"):
        _emitir(cuit="30222222223")
    assert pedidos == []


@pytest.mark.parametrize(("cambios", "texto"), [
    ({"peso_bruto": 90000}, "88.000"),
    ({"peso_tara": 46000}, "tara tiene que ser menor"),
    ({"cosecha": 20252026}, "cosecha"),
    ({"cuit_destinatario": "3066"}, "destinatario"),
])
def test_lo_que_el_esquema_rechaza_se_dice_antes(arca_ops, cambios, texto):
    pedidos = arca_ops("consultarUltNroOrden", "ult_nro_orden")
    with pytest.raises(w.SolicitudInvalida, match=texto):
        _emitir(_solicitud(**cambios))
    assert pedidos == []


def test_dominios_y_km_fuera_de_rango():
    t = w.TransporteSolicitud(cuit_transportista="30222222223", dominios=("A1", "B", "C", "D"),
                              fecha_hora_partida=datetime(2026, 10, 8, tzinfo=UTC), km=0,
                              cuit_chofer="20777777778", cuit_pagador_flete="30444444445",
                              tarifa=Decimal("100000"))
    problemas = _solicitud(transporte=t).problemas()
    assert any("entre 1 y 3 dominios" in p for p in problemas)
    assert any("«A1»" in p for p in problemas)
    assert any("kilómetros" in p for p in problemas)
    assert any("tarifa" in p for p in problemas)


# ── Lo que contesta ARCA ───────────────────────────────────────────────────

@pytest.mark.parametrize(("fixture", "codigo"), [("autorizar_rechazo_949", 949), ("autorizar_rechazo_2008", 2008)])
def test_los_rechazos_reales_de_homologacion(arca_ops, fixture, codigo):
    """Medidos el 2026-10-08: 949 (productor informado sin corresponder) y 2008 (solicitante sin SISA)."""
    arca_ops("consultarUltNroOrden", "ult_nro_orden")
    arca_ops("autorizarCPEAutomotor", fixture)
    with pytest.raises(w.ErrorWscpe) as e:
        _emitir()
    assert e.value.codigos[0][0] == codigo
    assert not isinstance(e.value, w.CpeNoEncontrada)


# ── Si ARCA no contesta al autorizar ───────────────────────────────────────

def test_sin_respuesta_pero_la_carta_esta_se_devuelve(arca_ops):
    arca_ops("consultarUltNroOrden", "ult_nro_orden")
    arca_ops("autorizarCPEAutomotor", httpx.ReadTimeout("lento"))
    pedidos = arca_ops("consultarCPEAutomotor", _autorizada().replace("AutorizarCPEAutomotorResp",
                                                                       "ConsultarCPEAutomotorResp"))
    cpe = _emitir()
    assert cpe.nro_ctg == 10100000001
    assert [op for op, _ in pedidos] == ["consultarUltNroOrden", "autorizarCPEAutomotor", "consultarCPEAutomotor"]
    consulta = pedidos[2][1].find("solicitud")
    assert [(h.tag, h.text) for h in consulta.find("cartaPorte")] == [
        ("tipoCPE", "74"), ("sucursal", "1"), ("nroOrden", "1")], "consulta el número que se pidió"
    assert [op for op, _ in pedidos].count("autorizarCPEAutomotor") == 1, "no reintenta"


def test_sin_respuesta_y_la_carta_no_esta_se_dice_que_no_se_emitio(arca_ops):
    arca_ops("consultarUltNroOrden", "ult_nro_orden")
    arca_ops("autorizarCPEAutomotor", "BL 502 Bad Gateway")
    arca_ops("consultarCPEAutomotor", "cpe_inexistente")
    with pytest.raises(RuntimeError, match="no se emitió") as e:
        _emitir()
    assert not isinstance(e.value, w.EmisionIncierta)


def test_sin_respuesta_y_sin_poder_consultar_es_incierta(arca_ops):
    arca_ops("consultarUltNroOrden", "ult_nro_orden")
    pedidos = arca_ops("autorizarCPEAutomotor", httpx.ConnectError("caido"))
    arca_ops("consultarCPEAutomotor", httpx.ReadTimeout("caido"))
    with pytest.raises(w.EmisionIncierta, match="No reintentar") as e:
        _emitir()
    assert (e.value.sucursal, e.value.nro_orden, e.value.tipo_cpe) == (1, 1, 74)
    assert [op for op, _ in pedidos].count("autorizarCPEAutomotor") == 1


def test_dos_emisiones_a_la_vez_no_piden_el_mismo_numero(arca_ops):
    """El cerrojo por (solicitante, sucursal, tipo, ambiente): la segunda pide el número cuando la primera terminó."""
    pedidos = arca_ops("consultarUltNroOrden", "ult_nro_orden")
    arca_ops("autorizarCPEAutomotor", _autorizada())

    async def dos():
        return await asyncio.gather(*(w.emitir_cpe(SOLICITANTE, "TKN", "SGN", _solicitud(), ambiente="homologacion")
                                      for _ in range(2)))

    asyncio.run(dos())
    assert [op for op, _ in pedidos] == ["consultarUltNroOrden", "autorizarCPEAutomotor"] * 2


# ── Catálogos, número y anulación ──────────────────────────────────────────

def test_catalogos(arca_ops):
    arca_ops("consultarTiposGrano", "tipos_grano")
    pedidos = arca_ops("consultarLocalidadesPorProvincia", "localidades")
    assert asyncio.run(w.tipos_grano(SOLICITANTE, "TKN", "SGN")) == {31: "Algodón", 30: "Alpiste", 35: "Arroz"}
    locs = asyncio.run(w.localidades(SOLICITANTE, "TKN", "SGN", cod_provincia=12))
    assert list(locs.items())[0] == (14, "22 DE MAYO")
    assert pedidos[-1][1].findtext("solicitud/codProvincia") == "12"


def test_sin_plantas_es_una_lista_vacia(arca_ops):
    """Medido: ARCA contesta 800 cuando el CUIT no tiene plantas."""
    pedidos = arca_ops("consultarPlantas", "plantas_sin_plantas")
    assert asyncio.run(w.plantas(SOLICITANTE, "TKN", "SGN", cuit="30-66666666-7")) == []
    assert pedidos[0][1].findtext("solicitud/cuit") == "30666666667"


def test_ultimo_nro_orden(arca_ops):
    arca_ops("consultarUltNroOrden", "ult_nro_orden")
    assert asyncio.run(w.ultimo_nro_orden(SOLICITANTE, "TKN", "SGN", sucursal=1)) == 0


def test_anular(arca_ops):
    anulada = _respuesta("cpe_activa").replace("ConsultarCPEAutomotorResp", "AnularCPEResp").replace(
        "<estado>AC</estado>", "<estado>AN</estado>")
    pedidos = arca_ops("anularCPE", anulada)
    assert asyncio.run(w.anular_cpe(SOLICITANTE, "TKN", "SGN", sucursal=1, nro_orden=7, observaciones="Error")) == "AN"
    s = pedidos[0][1].find("solicitud")
    assert [h.tag for h in s] == ["cartaPorte", "anulacionObservaciones"]
    assert [(h.tag, h.text) for h in s.find("cartaPorte")] == [("tipoCPE", "74"), ("sucursal", "1"), ("nroOrden", "7")]


def test_anular_una_que_no_existe(arca_ops):
    """Medido: 1302 «No existe una CPE con los parámetros indicados»."""
    arca_ops("anularCPE", "anular_inexistente")
    with pytest.raises(w.ErrorWscpe) as e:
        asyncio.run(w.anular_cpe(SOLICITANTE, "TKN", "SGN", sucursal=1, nro_orden=99999))
    assert e.value.codigos[0][0] == 1302


def test_anular_con_observaciones_largas_no_sale(arca_ops):
    pedidos = arca_ops("anularCPE", "anular_inexistente")
    with pytest.raises(ValueError, match="100"):
        asyncio.run(w.anular_cpe(SOLICITANTE, "TKN", "SGN", sucursal=1, nro_orden=1, observaciones="x" * 101))
    assert pedidos == []
