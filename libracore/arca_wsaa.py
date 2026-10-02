"""
Autenticación WSAA (Web Service de Autenticación y Autorización) de ARCA/AFIP.
Implementa el flujo: TRA → firma CMS → llamada SOAP → token+sign.
"""

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import random
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta, timezone

from libracore import arca_certificados

logger = logging.getLogger(__name__)

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


async def _pedir_ticket(cert_path, key_path, ambiente="homologacion", servicio="wsfe"):
    """
    Hace el login contra WSAA y devuelve dict con token, sign y expiracion.
    Lanza RuntimeError con mensaje legible ante cualquier falla.

    Es el login **crudo**, sin caché: quien llama de afuera usa `autenticar`.
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


# ── La caché del ticket ──────────────────────────────────────────────────────
#
# 🔴 **El ticket dura 12 horas y WSAA no entrega otro mientras haya uno vigente**:
# contesta `coe.alreadyAuthenticated`. Pedir uno por emisión, como se hizo hasta
# acá, deja emitir **una factura cada 12 horas** por certificado y servicio
# (medido en homologación el 2026-10-02).
#
# El ticket se guarda **en disco** y no en memoria porque la instancia corre con
# varios workers y cada uno tiene la suya: con la caché en memoria el segundo
# worker pide otro ticket y se choca con el del primero.

#: Se renueva cuando faltan menos de estos segundos. Un ticket que vence en medio
#: de un pedido a WSFE es un rechazo que no se explica solo.
MARGEN_DE_RENOVACION = 5 * 60
#: Cuánto se espera a que otro proceso termine de pedir el ticket.
ESPERA_DEL_CERROJO = 45


def _dir_de_tickets() -> str:
    """Dónde viven los tickets: `ARCA_TA_DIR`, o `$DATA_DIR/arca_ta`.

    No va junto al certificado porque los productos que guardan el par en la base
    lo escriben en un temporal que se borra: el ticket tiene que sobrevivirlo.
    """
    return (os.environ.get("ARCA_TA_DIR")
            or os.path.join(os.environ.get("DATA_DIR", os.getcwd()), "arca_ta"))


def _ruta_del_ticket(cert_path, ambiente, servicio) -> str:
    """Un archivo por (ambiente, servicio, certificado).

    🔑 **La clave incluye la huella del certificado**: si el cliente sube uno
    nuevo, el ticket del viejo no se reusa. Con sólo ambiente+servicio, cambiar
    de certificado seguiría firmando con un ticket que ya no corresponde.
    """
    with open(cert_path, "rb") as f:
        huella = hashlib.sha256(f.read()).hexdigest()[:16]
    return os.path.join(_dir_de_tickets(), f"ta-{ambiente}-{servicio}-{huella}.json")


def _vigente(ticket) -> bool:
    try:
        vence = datetime.fromisoformat(ticket["expiracion"])
    except (KeyError, TypeError, ValueError):
        return False
    if vence.tzinfo is None:
        return False
    restan = (vence - datetime.now(UTC)).total_seconds()
    return restan > MARGEN_DE_RENOVACION and bool(ticket.get("token") and ticket.get("sign"))


def _leer_ticket(ruta):
    """El ticket guardado si sigue vigente; `None` si no hay, está roto o venció."""
    try:
        with open(ruta, encoding="utf-8") as f:
            ticket = json.load(f)
    except (OSError, ValueError):
        return None
    return ticket if isinstance(ticket, dict) and _vigente(ticket) else None


def _guardar_ticket(ruta, ticket) -> None:
    """Escribe con permisos 0600 y reemplazo atómico: token+sign son credenciales
    y un lector concurrente nunca tiene que ver el archivo a medio escribir."""
    tmp = f"{ruta}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(ticket, f)
    os.replace(tmp, ruta)


@contextlib.asynccontextmanager
async def _cerrojo(ruta):
    """Un solo login a la vez por ticket, entre tareas y entre procesos.

    `flock` sin bloquear y con espera por `asyncio.sleep`: un `flock` bloqueante
    congelaría el loop de todo el worker mientras otro proceso hace el login.
    """
    fd = os.open(f"{ruta}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        limite = time.monotonic() + ESPERA_DEL_CERROJO
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > limite:
                    raise RuntimeError(
                        "WSAA: otro proceso está pidiendo el ticket y no terminó. "
                        "Reintentá en un momento.")
                await asyncio.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


async def autenticar(cert_path, key_path, ambiente="homologacion", servicio="wsfe"):
    """
    Devuelve dict con token, sign y expiracion, **reusando el ticket vigente**.

    Pide uno nuevo sólo si no hay, venció o vence en menos de
    `MARGEN_DE_RENOVACION`. Lanza RuntimeError con mensaje legible ante falla.
    """
    try:
        ruta = _ruta_del_ticket(cert_path, ambiente, servicio)
        os.makedirs(os.path.dirname(ruta), mode=0o700, exist_ok=True)
    except OSError as e:
        # Sin certificado legible el login crudo da el error de siempre; sin
        # directorio escribible se emite sin caché, que es lo de antes.
        logger.warning("WSAA sin caché de ticket (%s): %s", ambiente, e)
        return await _pedir_ticket(cert_path, key_path, ambiente, servicio)

    ticket = _leer_ticket(ruta)
    if ticket:
        return ticket

    async with _cerrojo(ruta):
        # Otro proceso pudo dejar el ticket mientras esperábamos el cerrojo.
        ticket = _leer_ticket(ruta)
        if ticket:
            return ticket
        try:
            ticket = await _pedir_ticket(cert_path, key_path, ambiente, servicio)
        except RuntimeError as e:
            if "alreadyAuthenticated" not in str(e):
                raise
            ticket = _leer_ticket(ruta)
            if ticket:
                return ticket
            raise RuntimeError(
                "WSAA: ARCA ya emitió un ticket vigente para este certificado y "
                "servicio, pero no está en la caché de esta instancia (lo pidió "
                "otro sistema o se perdió el archivo). No se puede pedir otro "
                "hasta que venza, dentro de 12 horas como máximo."
            ) from e
        try:
            _guardar_ticket(ruta, ticket)
        except OSError as e:
            logger.warning("WSAA: no se pudo guardar el ticket (%s): %s", ambiente, e)
        return ticket


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
