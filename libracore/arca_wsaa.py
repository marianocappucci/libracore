"""
Autenticación WSAA (Web Service de Autenticación y Autorización) de ARCA/AFIP.
Implementa el flujo: TRA → firma CMS → llamada SOAP → token+sign.
"""

import base64
import random
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import Encoding

WSAA_URL = {
    "homologacion": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
    "produccion":   "https://wsaa.afip.gov.ar/ws/services/LoginCms",
}


def validar_archivos(cert_path, key_path):
    """
    Verifica que el certificado y la clave sean válidos y coincidan.
    Devuelve lista de errores (vacía = todo OK).
    """
    errores = []

    cert = None
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        ahora = datetime.now(timezone.utc)
        vto = cert.not_valid_after_utc
        if vto < ahora:
            errores.append(f"Certificado vencido el {vto.strftime('%d/%m/%Y')}")
        elif cert.not_valid_before_utc > ahora:
            errores.append("Certificado aún no es válido (fecha de inicio futura)")
    except FileNotFoundError:
        errores.append("Archivo de certificado no encontrado")
        return errores
    except Exception as e:
        errores.append(f"Error al leer certificado: {e}")
        return errores

    key = None
    try:
        with open(key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
    except FileNotFoundError:
        errores.append("Archivo de clave privada no encontrado")
        return errores
    except Exception as e:
        errores.append(f"Error al leer clave privada: {e}")
        return errores

    # Verificar que cert y clave se corresponden
    try:
        pub_cert = cert.public_key().public_bytes(
            Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        pub_key = key.public_key().public_bytes(
            Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        if pub_cert != pub_key:
            errores.append("La clave privada no corresponde al certificado")
    except Exception as e:
        errores.append(f"Error al comparar clave y certificado: {e}")

    return errores


def info_certificado(cert_path):
    """Devuelve dict con información del certificado."""
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        ahora = datetime.now(timezone.utc)
        vto   = cert.not_valid_after_utc
        return {
            "subject":  cert.subject.rfc4514_string(),
            "issuer":   cert.issuer.rfc4514_string(),
            "vencimiento": vto.strftime("%d/%m/%Y"),
            "vencido":  vto < ahora,
            "dias_restantes": max(0, (vto - ahora).days),
            "serial":   str(cert.serial_number),
        }
    except Exception as e:
        return {"error": str(e)}


def _generar_tra(servicio="wsfe"):
    ahora = datetime.now(timezone.utc)
    exp   = ahora + timedelta(minutes=10)
    fmt   = "%Y-%m-%dT%H:%M:%S+00:00"
    uid   = random.randint(1, 2**31)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<loginTicketRequest version="1.0">'
        f'<header>'
        f'<uniqueId>{uid}</uniqueId>'
        f'<generationTime>{ahora.strftime(fmt)}</generationTime>'
        f'<expirationTime>{exp.strftime(fmt)}</expirationTime>'
        f'</header>'
        f'<service>{servicio}</service>'
        f'</loginTicketRequest>'
    ).encode()


def _firmar_tra(tra_bytes, cert_path, key_path):
    """Firma el TRA con openssl smime (SHA1, DER, contenido embebido)."""
    import subprocess
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
        f.write(tra_bytes)
        tra_file = f.name

    try:
        result = subprocess.run(
            [
                "openssl", "smime", "-sign",
                "-in",      tra_file,
                "-signer",  cert_path,
                "-inkey",   key_path,
                "-outform", "DER",
                "-nodetach",
                "-md",      "sha1",
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode().strip())
        return base64.b64encode(result.stdout).decode()
    finally:
        os.unlink(tra_file)


async def autenticar(cert_path, key_path, ambiente="homologacion", servicio="wsfe"):
    """
    Autentica contra WSAA y devuelve dict con token, sign y expiracion.
    Lanza RuntimeError con mensaje legible ante cualquier falla.
    """
    import httpx

    tra = _generar_tra(servicio)
    try:
        cms = _firmar_tra(tra, cert_path, key_path)
    except Exception as e:
        raise RuntimeError(f"Error al firmar TRA: {e}")

    url  = WSAA_URL.get(ambiente, WSAA_URL["homologacion"])
    soap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<SOAP-ENV:Envelope '
        '  xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"'
        '  xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
        '  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<SOAP-ENV:Body>'
        '<loginCms xmlns="http://wsaa.view.sua.dvadac.desein.afip.gov">'
        f'<in0>{cms}</in0>'
        '</loginCms>'
        '</SOAP-ENV:Body>'
        '</SOAP-ENV:Envelope>'
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                content=soap.encode(),
                headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": ""},
            )
    except httpx.TimeoutException:
        raise RuntimeError("Tiempo de espera agotado al conectar con WSAA")
    except Exception as e:
        raise RuntimeError(f"Error de red al conectar con WSAA: {e}")

    if resp.status_code != 200:
        # Intentar extraer faultcode + faultstring del XML antes de truncar
        try:
            root_err = ET.fromstring(resp.text)
            fc  = next((e.text or "" for e in root_err.iter() if e.tag.endswith("faultcode")),   "")
            fs  = next((e.text or "" for e in root_err.iter() if e.tag.endswith("faultstring")), "")
            if fs:
                raise RuntimeError(f"WSAA error [{fc}]: {fs}")
        except RuntimeError:
            raise
        except Exception:
            pass
        raise RuntimeError(f"WSAA respondio HTTP {resp.status_code}: {resp.text[:500]}")

    # SOAP fault check
    if "<faultstring>" in resp.text:
        try:
            root  = ET.fromstring(resp.text)
            fault = next((e.text for e in root.iter() if "faultstring" in e.tag), resp.text[:200])
        except Exception:
            fault = resp.text[:200]
        raise RuntimeError(f"WSAA rechazo la solicitud: {fault}")

    # Extraer loginCmsReturn
    try:
        root = ET.fromstring(resp.text)
        ret_elem = next(
            (e for e in root.iter() if e.tag.endswith("loginCmsReturn") or e.tag.endswith("return")),
            None,
        )
        if ret_elem is None or not ret_elem.text:
            raise ValueError("loginCmsReturn no encontrado en respuesta")
        cred  = ET.fromstring(ret_elem.text)
        token = cred.findtext(".//token") or ""
        sign  = cred.findtext(".//sign")  or ""
        exp   = cred.findtext(".//expirationTime") or ""
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Error al parsear respuesta de WSAA: {e}\n{resp.text[:400]}")

    return {"token": token, "sign": sign, "expiracion": exp}
