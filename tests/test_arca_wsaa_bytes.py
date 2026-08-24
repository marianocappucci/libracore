"""El par en memoria: `libracargo` lo guarda en la base, no en el volumen.

Lo que se prueba acá es lo que **no se ve desde afuera**: que el archivo
temporal exista sólo mientras dura la llamada, que la clave privada no quede
legible para otros usuarios de la máquina, y que las dos cosas valgan también
cuando la firma explota.
"""

import asyncio
import os
import stat

import httpx
import pytest
from conftest import make_valid_cert_key

from libracore import arca_wsaa

_RealAsyncClient = httpx.AsyncClient


def _par(tmp_path):
    cert_path, key_path = make_valid_cert_key(tmp_path)
    return open(cert_path, "rb").read(), open(key_path, "rb").read()


def _respuesta_ok():
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<soapenv:Envelope xmlns:soapenv='http://schemas.xmlsoap.org/soap/envelope/'>"
        "<soapenv:Body><loginCmsResponse><loginCmsReturn>"
        "&lt;credentials&gt;&lt;token&gt;TKN&lt;/token&gt;"
        "&lt;sign&gt;SGN&lt;/sign&gt;"
        "&lt;expirationTime&gt;2027-01-01T00:00:00-03:00&lt;/expirationTime&gt;"
        "&lt;/credentials&gt;"
        "</loginCmsReturn></loginCmsResponse></soapenv:Body></soapenv:Envelope>"
    )


def _mockear_http(monkeypatch):
    def handler(request):
        return httpx.Response(200, text=_respuesta_ok())

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


# ── El contexto ─────────────────────────────────────────────────────────────

def test_el_par_existe_adentro_y_no_afuera(tmp_path):
    """Las dos mitades. Sólo "no existe después" pasaría igual con una función
    que no escribe nada."""
    certificado, clave = _par(tmp_path)
    with arca_wsaa.par_en_disco(certificado, clave) as (cert_path, key_path):
        assert os.path.exists(cert_path)
        assert os.path.exists(key_path)
        with open(cert_path, "rb") as f:
            assert f.read() == certificado
        with open(key_path, "rb") as f:
            assert f.read() == clave
        guardados = (cert_path, key_path)
    assert not os.path.exists(guardados[0])
    assert not os.path.exists(guardados[1])


def test_la_clave_no_queda_legible_para_otros(tmp_path):
    """🔑 Es la identidad fiscal del contribuyente en un directorio compartido.
    Un `/tmp` con la clave en 0644 la deja al alcance de cualquier proceso de
    la máquina mientras dura la firma."""
    certificado, clave = _par(tmp_path)
    with arca_wsaa.par_en_disco(certificado, clave) as (_, key_path):
        modo = stat.S_IMODE(os.stat(key_path).st_mode)
        assert modo == 0o600, f"la clave quedó en {oct(modo)}"


def test_se_borra_aunque_el_bloque_explote(tmp_path):
    """🔴 La mitad que importa: si el par sobrevive a un error, queda una copia
    de la clave privada en `/tmp` sin que nadie se entere."""
    certificado, clave = _par(tmp_path)
    guardados = []
    with pytest.raises(RuntimeError):
        with arca_wsaa.par_en_disco(certificado, clave) as caminos:
            guardados.extend(caminos)
            raise RuntimeError("algo salió mal en el medio")
    assert guardados, "el contexto tenía que haber entregado los dos caminos"
    for camino in guardados:
        assert not os.path.exists(camino), f"{camino} sobrevivió al error"


# ── La autenticación ────────────────────────────────────────────────────────

def test_autenticar_con_bytes_da_lo_mismo_que_con_rutas(tmp_path, monkeypatch):
    """El control que ata las dos entradas: mismo par, mismo resultado."""
    cert_path, key_path = make_valid_cert_key(tmp_path)
    with open(cert_path, "rb") as f:
        certificado = f.read()
    with open(key_path, "rb") as f:
        clave = f.read()

    _mockear_http(monkeypatch)
    por_ruta = asyncio.run(arca_wsaa.autenticar(cert_path, key_path, "homologacion"))
    por_bytes = asyncio.run(
        arca_wsaa.autenticar_con_bytes(certificado, clave, "homologacion")
    )
    assert por_bytes == por_ruta
    assert por_bytes["token"] == "TKN"


def test_no_deja_temporales_despues_de_autenticar(tmp_path, monkeypatch):
    certificado, clave = _par(tmp_path)
    vistos = []
    original = arca_wsaa._firmar_tra

    def espiar(tra, cert_path, key_path):
        vistos.extend([cert_path, key_path])
        return original(tra, cert_path, key_path)

    monkeypatch.setattr(arca_wsaa, "_firmar_tra", espiar)
    _mockear_http(monkeypatch)
    asyncio.run(arca_wsaa.autenticar_con_bytes(certificado, clave, "homologacion"))

    assert len(vistos) == 2, "no se llegó a firmar: el test no probó nada"
    for camino in vistos:
        assert not os.path.exists(camino), f"{camino} quedó en el disco"


def test_un_par_invalido_tampoco_deja_temporales(tmp_path, monkeypatch):
    """Con bytes que no son un par, `openssl` falla — y el par igual tiene que
    desaparecer."""
    vistos = []
    original = arca_wsaa._firmar_tra

    def espiar(tra, cert_path, key_path):
        vistos.extend([cert_path, key_path])
        return original(tra, cert_path, key_path)

    monkeypatch.setattr(arca_wsaa, "_firmar_tra", espiar)
    with pytest.raises(Exception):
        asyncio.run(arca_wsaa.autenticar_con_bytes(b"no soy un cert", b"ni yo"))
    assert len(vistos) == 2
    for camino in vistos:
        assert not os.path.exists(camino)
