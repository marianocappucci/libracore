"""Lo que la pantalla de configuración de ARCA tiene que rechazar al subir.

Los tres errores de armado que se ven siempre no se detectan mirando la
extensión del archivo, así que cada uno tiene su test con el archivo **real**
armado al vuelo: un `.csr` de verdad, una clave con passphrase de verdad, y un
par cruzado de verdad.
"""

import datetime

import pytest
from conftest import make_expired_cert_key, make_mismatched_key, make_valid_cert_key
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from libracore import arca_certificados as ac


def _par(tmp_path):
    cert_path, key_path = make_valid_cert_key(tmp_path)
    return open(cert_path, "rb").read(), open(key_path, "rb").read()


def _csr() -> bytes:
    """El pedido de certificado — lo que ARCA recibe, no lo que devuelve."""
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pedido = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pedido")]))
        .sign(clave, hashes.SHA256())
    )
    return pedido.public_bytes(serialization.Encoding.PEM)


def _clave_con_passphrase() -> bytes:
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return clave.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.BestAvailableEncryption(b"secreta"),
    )


# ── 1. el .csr en vez del .crt ───────────────────────────────────────────────

def test_el_csr_no_pasa_por_certificado():
    """🔑 Es el error más común y el más engañoso: el `.csr` **es** un PEM
    válido, así que cualquier chequeo de forma lo deja pasar."""
    with pytest.raises(ac.ArchivoInvalido, match=r"\.csr"):
        ac.leer_certificado(_csr())


# ── 2. los dos archivos cambiados de campo ───────────────────────────────────

def test_la_clave_en_el_campo_del_certificado(tmp_path):
    certificado, clave = _par(tmp_path)
    with pytest.raises(ac.ArchivoInvalido):
        ac.leer_certificado(clave)


def test_el_certificado_en_el_campo_de_la_clave(tmp_path):
    certificado, clave = _par(tmp_path)
    with pytest.raises(ac.ArchivoInvalido):
        ac.leer_clave(certificado)


def test_la_clave_con_passphrase_se_rechaza_al_subirla():
    """Se acepta hoy y falla al emitir: no hay nadie para escribir la
    contraseña cuando se pide el ticket de acceso."""
    with pytest.raises(ac.ArchivoInvalido, match="contraseña"):
        ac.leer_clave(_clave_con_passphrase())


# ── 3. el par cruzado ────────────────────────────────────────────────────────

def test_son_pareja_distingue_el_par_bueno_del_cruzado(tmp_path):
    """Las dos mitades en el mismo test **a propósito**.

    🔑 Un `assert son_pareja(...) is False` solo pasaría igual con una función
    que devuelva `False` siempre — que es el modo en que este chequeo se rompe
    sin que nadie lo note. El positivo es lo que lo hace un chequeo.
    """
    bueno_cert, bueno_clave = _par(tmp_path)
    cert_path, clave_ajena_path = make_mismatched_key(tmp_path)
    cert_de_a = open(cert_path, "rb").read()
    clave_de_b = open(clave_ajena_path, "rb").read()

    assert ac.son_pareja(bueno_cert, bueno_clave) is True, "el par correcto tiene que dar True"
    assert ac.son_pareja(cert_de_a, clave_de_b) is False, "la clave de otro par tiene que dar False"


# ── Los datos que la pantalla muestra ────────────────────────────────────────

def test_datos_del_certificado_vigente(tmp_path):
    certificado, _ = _par(tmp_path)
    datos = ac.leer_certificado(certificado)
    assert datos.vencido is False
    assert datos.todavia_no_vale is False
    assert 360 <= datos.dias_para_vencer <= 365
    assert "test-valid" in datos.sujeto
    assert int(datos.numero_de_serie, 16) > 0


def test_certificado_vencido(tmp_path):
    cert_path, _ = make_expired_cert_key(tmp_path)
    datos = ac.leer_certificado(open(cert_path, "rb").read())
    assert datos.vencido is True
    assert datos.dias_para_vencer < 0


def test_certificado_que_todavia_no_vale():
    """Fecha de inicio en el futuro. Sin este chequeo se ve igual que uno
    vigente: `vencido` da `False` y la pantalla lo da por bueno."""
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "futuro")])
    manana = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)
    cert = (
        x509.CertificateBuilder()
        .subject_name(nombre).issuer_name(nombre)
        .public_key(clave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(manana)
        .not_valid_after(manana + datetime.timedelta(days=365))
        .sign(clave, hashes.SHA256())
    )
    datos = ac.leer_certificado(cert.public_bytes(serialization.Encoding.PEM))
    assert datos.vencido is False, "no está vencido, está al revés"
    assert datos.todavia_no_vale is True


# ── revisar_par: lo que va a la pantalla ─────────────────────────────────────

def test_revisar_par_no_encuentra_nada_en_un_par_bueno(tmp_path):
    certificado, clave = _par(tmp_path)
    assert ac.revisar_par(certificado, clave) == []


def test_revisar_par_marca_el_cruce(tmp_path):
    cert_path, clave_ajena = make_mismatched_key(tmp_path)
    errores = ac.revisar_par(
        open(cert_path, "rb").read(), open(clave_ajena, "rb").read()
    )
    assert any("no corresponde" in e.lower() for e in errores), errores


def test_revisar_par_marca_el_vencimiento(tmp_path):
    cert_path, key_path = make_expired_cert_key(tmp_path)
    errores = ac.revisar_par(
        open(cert_path, "rb").read(), open(key_path, "rb").read()
    )
    assert any("vencido" in e.lower() for e in errores), errores


def test_revisar_par_con_certificado_ilegible_no_sigue(tmp_path):
    """🔴 Un solo error, no dos.

    Si el certificado no se puede ni leer, el chequeo de pareja sobre ese mismo
    archivo explotaría con un `Exception` crudo y taparía la causa real con una
    segunda línea de ruido.
    """
    _, clave = _par(tmp_path)
    errores = ac.revisar_par(b"no es un certificado", clave)
    assert len(errores) == 1, errores
    assert "certificado" in errores[0].lower()


def test_revisar_par_con_clave_ilegible_no_llega_a_comparar(tmp_path):
    certificado, _ = _par(tmp_path)
    errores = ac.revisar_par(certificado, b"tampoco es una clave")
    assert len(errores) == 1, errores
    assert "clave" in errores[0].lower()


# ── La puerta de entrada por ruta, que es la que usan cinco de los seis ──────

def test_revisar_par_de_archivos_par_bueno(tmp_path):
    cert_path, key_path = make_valid_cert_key(tmp_path)
    assert ac.revisar_par_de_archivos(cert_path, key_path) == []


def test_revisar_par_de_archivos_distingue_cual_falta(tmp_path):
    """Cuál de los dos falta, no "falta algo": son dos campos distintos de la
    pantalla y el mensaje tiene que decir en cuál está el problema."""
    cert_path, key_path = make_valid_cert_key(tmp_path)
    falta = str(tmp_path / "no-existe")

    sin_cert = ac.revisar_par_de_archivos(falta, key_path)
    assert sin_cert == ["Archivo de certificado no encontrado"]

    sin_clave = ac.revisar_par_de_archivos(cert_path, falta)
    assert sin_clave == ["Archivo de clave privada no encontrado"]
