import asyncio

import httpx
import pytest
from conftest import make_expired_cert_key, make_mismatched_key, make_valid_cert_key

from libracore import arca_wsaa

_RealAsyncClient = httpx.AsyncClient


def _client_factory(handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(**kwargs)
    return factory


def _patch_client(monkeypatch, handler):
    # arca_wsaa.autenticar() hace `import httpx` local (no a nivel de
    # modulo) — se parchea el modulo global httpx, que es el mismo objeto
    # que resuelve ese import local.
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))


def test_validar_archivos_par_valido(tmp_path):
    cert_path, key_path = make_valid_cert_key(tmp_path)
    assert arca_wsaa.validar_archivos(cert_path, key_path) == []


def test_validar_archivos_certificado_vencido(tmp_path):
    cert_path, key_path = make_expired_cert_key(tmp_path)
    errores = arca_wsaa.validar_archivos(cert_path, key_path)
    assert any("vencido" in e.lower() for e in errores)


def test_validar_archivos_clave_no_corresponde(tmp_path):
    cert_path, key_path = make_mismatched_key(tmp_path)
    errores = arca_wsaa.validar_archivos(cert_path, key_path)
    assert any("no corresponde" in e.lower() for e in errores)


def test_validar_archivos_certificado_faltante(tmp_path):
    errores = arca_wsaa.validar_archivos(str(tmp_path / "nope.crt"), str(tmp_path / "nope.key"))
    assert any("no encontrado" in e.lower() for e in errores)


def test_info_certificado_devuelve_datos(tmp_path):
    cert_path, _ = make_valid_cert_key(tmp_path)
    info = arca_wsaa.info_certificado(cert_path)
    assert "error" not in info
    assert info["vencido"] is False
    assert info["dias_restantes"] > 0
    assert "serial" in info


def test_info_certificado_archivo_invalido(tmp_path):
    bad = tmp_path / "bad.crt"
    bad.write_text("no es un certificado")
    info = arca_wsaa.info_certificado(str(bad))
    assert "error" in info


def test_firmar_tra_produce_base64_valido(tmp_path):
    cert_path, key_path = make_valid_cert_key(tmp_path)
    tra = arca_wsaa._generar_tra("wsfe")
    cms = arca_wsaa._firmar_tra(tra, cert_path, key_path)
    import base64
    base64.b64decode(cms)  # no debe lanzar


def test_firmar_tra_con_clave_invalida_lanza(tmp_path):
    cert_path, _ = make_valid_cert_key(tmp_path)
    tra = arca_wsaa._generar_tra("wsfe")
    with pytest.raises(Exception):
        arca_wsaa._firmar_tra(tra, cert_path, str(tmp_path / "nope.key"))


def test_autenticar_ok(tmp_path, monkeypatch):
    cert_path, key_path = make_valid_cert_key(tmp_path)

    def handler(request):
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<soapenv:Envelope xmlns:soapenv='http://schemas.xmlsoap.org/soap/envelope/'>"
            "<soapenv:Body><loginCmsResponse><loginCmsReturn>"
            "&lt;credentials&gt;&lt;token&gt;TKN123&lt;/token&gt;"
            "&lt;sign&gt;SGN456&lt;/sign&gt;"
            "&lt;expirationTime&gt;2027-01-01T00:00:00-03:00&lt;/expirationTime&gt;"
            "&lt;/credentials&gt;"
            "</loginCmsReturn></loginCmsResponse></soapenv:Body></soapenv:Envelope>"
        )
        return httpx.Response(200, text=body)

    _patch_client(monkeypatch, handler)
    result = asyncio.run(arca_wsaa.autenticar(cert_path, key_path, "homologacion"))
    assert result == {"token": "TKN123", "sign": "SGN456", "expiracion": "2027-01-01T00:00:00-03:00"}


def test_autenticar_soap_fault(tmp_path, monkeypatch):
    cert_path, key_path = make_valid_cert_key(tmp_path)

    def handler(request):
        body = (
            "<soapenv:Envelope xmlns:soapenv='http://schemas.xmlsoap.org/soap/envelope/'>"
            "<soapenv:Body><soapenv:Fault>"
            "<faultcode>soapenv:Server</faultcode>"
            "<faultstring>Certificado revocado</faultstring>"
            "</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
        )
        return httpx.Response(200, text=body)

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="Certificado revocado"):
        asyncio.run(arca_wsaa.autenticar(cert_path, key_path, "homologacion"))


def test_autenticar_http_error(tmp_path, monkeypatch):
    cert_path, key_path = make_valid_cert_key(tmp_path)

    def handler(request):
        return httpx.Response(500, text="Internal Server Error")

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        asyncio.run(arca_wsaa.autenticar(cert_path, key_path, "homologacion"))
