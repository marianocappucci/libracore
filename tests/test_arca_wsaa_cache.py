"""La caché del ticket de WSAA: un login por ticket, no uno por emisión."""
import asyncio
import json
import os
import stat
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import make_valid_cert_key

from libracore import arca_wsaa

_RealAsyncClient = httpx.AsyncClient


def _respuesta(vence: datetime, token="TKN", sign="SGN"):
    exp = vence.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return httpx.Response(200, text=(
        "<soapenv:Envelope xmlns:soapenv='http://schemas.xmlsoap.org/soap/envelope/'>"
        "<soapenv:Body><loginCmsResponse><loginCmsReturn>"
        f"&lt;credentials&gt;&lt;token&gt;{token}&lt;/token&gt;&lt;sign&gt;{sign}&lt;/sign&gt;"
        f"&lt;expirationTime&gt;{exp}&lt;/expirationTime&gt;&lt;/credentials&gt;"
        "</loginCmsReturn></loginCmsResponse></soapenv:Body></soapenv:Envelope>"))


def _wsaa(monkeypatch, handler):
    llamadas = []

    def h(request):
        llamadas.append(request)
        return handler(request)

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(h)
        return _RealAsyncClient(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return llamadas


def _en(horas):
    return datetime.now(UTC) + timedelta(hours=horas)


def test_la_segunda_autenticacion_reusa_el_ticket(tmp_path, monkeypatch):
    cert, key = make_valid_cert_key(tmp_path)
    llamadas = _wsaa(monkeypatch, lambda r: _respuesta(_en(12)))
    a = asyncio.run(arca_wsaa.autenticar(cert, key, "homologacion"))
    b = asyncio.run(arca_wsaa.autenticar(cert, key, "homologacion"))
    assert a == b and len(llamadas) == 1


def test_un_ticket_por_servicio_y_por_ambiente(tmp_path, monkeypatch):
    cert, key = make_valid_cert_key(tmp_path)
    llamadas = _wsaa(monkeypatch, lambda r: _respuesta(_en(12)))
    asyncio.run(arca_wsaa.autenticar(cert, key, "homologacion", "wsfe"))
    asyncio.run(arca_wsaa.autenticar(cert, key, "homologacion", "wscpe"))
    asyncio.run(arca_wsaa.autenticar(cert, key, "produccion", "wsfe"))
    assert len(llamadas) == 3


def test_un_certificado_nuevo_no_reusa_el_ticket_del_viejo(tmp_path, monkeypatch):
    llamadas = _wsaa(monkeypatch, lambda r: _respuesta(_en(12)))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    c1, k1 = make_valid_cert_key(tmp_path / "a")
    c2, k2 = make_valid_cert_key(tmp_path / "b")
    asyncio.run(arca_wsaa.autenticar(c1, k1, "homologacion"))
    asyncio.run(arca_wsaa.autenticar(c2, k2, "homologacion"))
    assert len(llamadas) == 2


def test_un_ticket_por_vencer_se_renueva(tmp_path, monkeypatch):
    cert, key = make_valid_cert_key(tmp_path)
    vence = iter([_en(0.05), _en(12)])   # el primero vence en 3 min: menos que el margen
    llamadas = _wsaa(monkeypatch, lambda r: _respuesta(next(vence)))
    asyncio.run(arca_wsaa.autenticar(cert, key, "homologacion"))
    asyncio.run(arca_wsaa.autenticar(cert, key, "homologacion"))
    assert len(llamadas) == 2


def test_un_archivo_roto_se_ignora_y_se_pide_otro(tmp_path, monkeypatch):
    cert, key = make_valid_cert_key(tmp_path)
    llamadas = _wsaa(monkeypatch, lambda r: _respuesta(_en(12)))
    asyncio.run(arca_wsaa.autenticar(cert, key, "homologacion"))
    ruta = arca_wsaa._ruta_del_ticket(cert, "homologacion", "wsfe")
    open(ruta, "w").write("{esto no es json")
    asyncio.run(arca_wsaa.autenticar(cert, key, "homologacion"))
    assert len(llamadas) == 2


def test_el_ticket_se_guarda_con_permisos_0600(tmp_path, monkeypatch):
    cert, key = make_valid_cert_key(tmp_path)
    _wsaa(monkeypatch, lambda r: _respuesta(_en(12)))
    asyncio.run(arca_wsaa.autenticar(cert, key, "homologacion"))
    ruta = arca_wsaa._ruta_del_ticket(cert, "homologacion", "wsfe")
    assert stat.S_IMODE(os.stat(ruta).st_mode) == 0o600
    assert json.load(open(ruta))["token"] == "TKN"


def test_ya_autenticado_sin_cache_da_un_mensaje_que_explica(tmp_path, monkeypatch):
    cert, key = make_valid_cert_key(tmp_path)
    _wsaa(monkeypatch, lambda r: httpx.Response(500, text=(
        "<soapenv:Envelope xmlns:soapenv='http://schemas.xmlsoap.org/soap/envelope/'>"
        "<soapenv:Body><soapenv:Fault><faultcode>ns1:coe.alreadyAuthenticated</faultcode>"
        "<faultstring>El CEE ya posee un TA valido</faultstring>"
        "</soapenv:Fault></soapenv:Body></soapenv:Envelope>")))
    with pytest.raises(RuntimeError, match="no está en la caché"):
        asyncio.run(arca_wsaa.autenticar(cert, key, "homologacion"))


def test_dos_pedidos_simultaneos_hacen_un_solo_login(tmp_path, monkeypatch):
    cert, key = make_valid_cert_key(tmp_path)
    llamadas = _wsaa(monkeypatch, lambda r: _respuesta(_en(12)))

    async def dos():
        return await asyncio.gather(
            arca_wsaa.autenticar(cert, key, "homologacion"),
            arca_wsaa.autenticar(cert, key, "homologacion"))

    a, b = asyncio.run(dos())
    assert a == b and len(llamadas) == 1


def test_sin_directorio_escribible_se_emite_sin_cache(tmp_path, monkeypatch):
    cert, key = make_valid_cert_key(tmp_path)
    bloqueo = tmp_path / "archivo"
    bloqueo.write_text("no soy un directorio")
    monkeypatch.setenv("ARCA_TA_DIR", str(bloqueo / "ta"))
    llamadas = _wsaa(monkeypatch, lambda r: _respuesta(_en(12)))
    asyncio.run(arca_wsaa.autenticar(cert, key, "homologacion"))
    asyncio.run(arca_wsaa.autenticar(cert, key, "homologacion"))
    assert len(llamadas) == 2
