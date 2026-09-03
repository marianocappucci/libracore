"""
Autenticación WSAA (Web Service de Autenticación y Autorización) de ARCA/AFIP.
Implementa el flujo: TRA → firma CMS → llamada SOAP → token+sign.
"""

import base64
import contextlib
import random
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta, timezone

from libracore import arca_certificados

WSAA_URL = {
    "homologacion": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
    "produccion":   "https://wsaa.afip.gov.ar/ws/services/LoginCms",
}


def validar_archivos(cert_path, key_path):
    """
    Verifica que el certificado y la clave sean válidos y coincidan.
    Devuelve lista de errores (vacía = todo OK).

    Sigue acá por compatibilidad —es la firma que llaman las pantallas de
    configuración de la familia— pero **la implementación vive en
    `arca_certificados`**, que además sabe trabajar sobre `bytes` para los
    productos que guardan el par en la base y no en el volumen.
    """
    return arca_certificados.revisar_par_de_archivos(cert_path, key_path)


def info_certificado(cert_path):
    """Devuelve dict con información del certificado.

    Mismo caso que `validar_archivos`: la firma se mantiene, el criptográfico
    lo pone `arca_certificados`.
    """
    try:
        datos = arca_certificados.leer_certificado_de_archivo(cert_path)
    except arca_certificados.ArchivoInvalido as e:
        return {"error": str(e)}
    return {
        "subject":        datos.sujeto,
        "issuer":         datos.emisor,
        "vencimiento":    datos.vence.strftime("%d-%m-%Y"),
        "vencido":        datos.vencido,
        "dias_restantes": max(0, datos.dias_para_vencer),
        "serial":         str(int(datos.numero_de_serie, 16)),
    }


def _generar_tra(servicio="wsfe"):
    ahora = datetime.now(UTC)
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
    import os
    import subprocess
    import tempfile

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


# ── El par en memoria, para el producto que no lo guarda en el volumen ──────


@contextlib.contextmanager
def par_en_disco(certificado: bytes, clave: bytes):
    """Deja el par en dos archivos temporales mientras dure el bloque.

    🔑 **Hace falta porque la firma del TRA la hace `openssl` por subproceso**,
    y openssl lee de archivos. No es una comodidad: no hay forma de firmar el
    TRA sin que el par toque el disco, aunque sea un instante.

    Existe acá y no en cada producto porque los productos guardan el par en
    lugares distintos —[[libracargo]] lo tiene **en la base**, para que entre en
    el dump del backup— y cada uno improvisando su propio temporal es cada uno
    improvisando sus propios permisos y su propia limpieza.

    ⚠️ **La clave privada se escribe con permisos 0600 y se borra siempre**,
    también si el bloque explota. `mkstemp` ya crea con 0600; se vuelve a fijar
    explícitamente para que el día que alguien cambie la forma de crear el
    archivo, el permiso siga siendo una decisión escrita y no un default
    heredado.
    """
    import os
    import stat
    import tempfile

    caminos = []
    try:
        for contenido, sufijo in ((certificado, ".crt"), (clave, ".key")):
            fd, camino = tempfile.mkstemp(suffix=sufijo)
            caminos.append(camino)
            with os.fdopen(fd, "wb") as f:
                f.write(contenido)
            os.chmod(camino, stat.S_IRUSR | stat.S_IWUSR)
        yield caminos[0], caminos[1]
    finally:
        for camino in caminos:
            try:
                os.unlink(camino)
            except OSError:
                pass


async def autenticar_con_bytes(certificado: bytes, clave: bytes,
                               ambiente="homologacion", servicio="wsfe"):
    """`autenticar()` para el producto que tiene el par en memoria.

    Es la misma función: escribe el par, delega, y lo borra pase lo que pase.
    """
    with par_en_disco(certificado, clave) as (cert_path, key_path):
        return await autenticar(cert_path, key_path, ambiente, servicio)
