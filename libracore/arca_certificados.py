"""Lectura y validación de los archivos de ARCA — **antes** de guardarlos.

Lo que hace este módulo es **rechazar en la pantalla de configuración lo que si
no fallaría recién al emitir el primer comprobante**, con un error de ARCA que
no habla de la causa. Los tres errores de armado que se ven siempre:

1. Subir el `.csr` —el pedido— en vez del `.crt` que ARCA devolvió.
2. Subir el certificado en el campo de la clave, o al revés.
3. 🔑 Subir un certificado y una clave que **no son pareja**, porque se generó
   una clave nueva y se subió el certificado viejo. Son dos archivos válidos,
   se ven perfectos en pantalla, y ARCA rechaza la autenticación con un error
   genérico.

Los tres se detectan leyendo los archivos, y **ninguno se detecta mirando la
extensión**.

## Por qué la entrada son `bytes` y no una ruta

Porque la validación tiene que pasar **antes** de que el archivo se guarde, y
los productos de la familia no guardan en el mismo lugar: cinco escriben el
`.crt` y el `.key` en `CERTS_DIR` del volumen de la instancia, y [[libracargo]]
los guarda **en la base**, como columnas de `configuracion_arca`, para que
entren en el dump del backup. Un validador que abra rutas sólo sirve para los
primeros. Con `bytes` sirve para los dos, y las funciones `*_de_archivo` de
abajo cubren el caso de la ruta sin duplicar una línea de criptografía.

Nace de `app/servicios/arca.py` de LibraCargo —el único producto que tenía el
chequeo de pareja— al normalizar la facturación electrónica de la suite. La
versión vieja de esto en `arca_wsaa.validar_archivos()`/`info_certificado()`
sigue existiendo con su firma de siempre, pero ahora delega acá: **una sola
implementación, dos puertas de entrada**.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509 import load_pem_x509_certificate
from cryptography.x509.oid import NameOID


class ArchivoInvalido(ValueError):
    """El archivo no es lo que dice ser. El mensaje va tal cual a la pantalla."""


@dataclass(frozen=True)
class DatosDelCertificado:
    """Lo que se puede mostrar de un certificado sin exponer nada secreto."""

    sujeto: str
    emisor: str
    vence: datetime
    desde: datetime
    numero_de_serie: str

    @property
    def vencido(self) -> bool:
        return self.vence < datetime.now(UTC)

    @property
    def todavia_no_vale(self) -> bool:
        """Un certificado recién emitido con la fecha de inicio en el futuro.

        Es raro pero pasa, y sin este chequeo se ve idéntico a uno vigente:
        `vencido` da `False` y la pantalla lo da por bueno.
        """
        return self.desde > datetime.now(UTC)

    @property
    def dias_para_vencer(self) -> int:
        return (self.vence - datetime.now(UTC)).days

    @property
    def cuit(self) -> str:
        """El CUIT del titular, si el sujeto lo trae (`serialNumber=CUIT 20…`).

        Los certificados que emite ARCA lo llevan ahí: es el CUIT de **quien se
        autentica**, que en un servicio con delegación (`wscpe`) puede ser una
        persona que actúa en nombre de la empresa. `""` si no está.
        """
        m = re.search(r"CUIT\s*(\d{11})", self.sujeto)
        return m.group(1) if m else ""


def leer_certificado(contenido: bytes) -> DatosDelCertificado:
    """Valida que sea un X.509 PEM y devuelve sus datos legibles.

    El vencimiento es el dato que evita la falla silenciosa: los certificados de
    ARCA duran dos años y el día que vence, la facturación deja de andar sin que
    nadie haya tocado nada.
    """
    try:
        cert = load_pem_x509_certificate(contenido)
    except Exception:
        raise ArchivoInvalido(
            "no parece un certificado PEM. Tiene que ser el .crt que devuelve "
            "ARCA, no el .csr que se le manda"
        ) from None
    return DatosDelCertificado(
        sujeto=cert.subject.rfc4514_string(),
        emisor=cert.issuer.rfc4514_string(),
        vence=cert.not_valid_after_utc,
        desde=cert.not_valid_before_utc,
        numero_de_serie=format(cert.serial_number, "x"),
    )


def leer_clave(contenido: bytes):
    """Valida que sea una clave privada PEM **sin passphrase**.

    Sin passphrase no es una preferencia: el ticket de acceso se pide sin
    intervención de nadie, así que no hay dónde escribirla. Una clave protegida
    se acepta hoy y falla al emitir.
    """
    try:
        return serialization.load_pem_private_key(contenido, password=None)
    except TypeError:
        raise ArchivoInvalido(
            "la clave privada está protegida con contraseña. ARCA se autentica "
            "sin que haya nadie para escribirla: hay que subirla sin passphrase"
        ) from None
    except Exception:
        raise ArchivoInvalido(
            "no parece una clave privada PEM. Es el archivo que se generó junto "
            "con el pedido de certificado, no el certificado"
        ) from None


def _publica(clave_o_cert) -> bytes:
    return clave_o_cert.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def son_pareja(certificado: bytes, clave: bytes) -> bool:
    """Si la clave privada corresponde a la pública del certificado.

    🔑 Es el chequeo que ningún nombre de archivo puede dar. Un certificado
    viejo con una clave nueva se ve perfecto en pantalla —los dos archivos son
    válidos— y ARCA rechaza la autenticación con un error genérico.
    """
    publica_del_cert = load_pem_x509_certificate(certificado).public_key()
    publica_de_la_clave = leer_clave(clave).public_key()
    return _publica(publica_del_cert) == _publica(publica_de_la_clave)


# ── La puerta de entrada por ruta ────────────────────────────────────────────
#
# Es la forma que usan los cinco productos que guardan los archivos en el
# volumen. No repite criptografía: lee el archivo y llama a lo de arriba.


class ArchivoFaltante(ArchivoInvalido):
    """El path no existe. Se distingue de `ArchivoInvalido` porque en pantalla
    son dos cosas distintas: "no lo subiste" y "lo que subiste no sirve"."""


def _leer_bytes(path: str, que: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        raise ArchivoFaltante(f"Archivo de {que} no encontrado") from None
    except OSError as e:
        raise ArchivoInvalido(f"No se pudo leer el archivo de {que}: {e}") from None


def leer_certificado_de_archivo(path: str) -> DatosDelCertificado:
    return leer_certificado(_leer_bytes(path, "certificado"))


def revisar_par(certificado: bytes, clave: bytes) -> list[str]:
    """Los errores del par, en castellano y listos para la pantalla.

    Lista vacía = está todo bien. Devuelve **lista** y no lanza porque la
    pantalla de configuración quiere mostrar todo lo que está mal de una vez,
    no el primero de la lista.

    ⚠️ El orden importa: si el certificado no se puede ni leer, no se sigue.
    Sin ese corte, el chequeo de pareja explotaría con un `Exception` crudo
    sobre el mismo archivo ilegible y taparía la causa real con una segunda
    línea de ruido.
    """
    errores: list[str] = []

    try:
        datos = leer_certificado(certificado)
    except ArchivoInvalido as e:
        return [f"Error al leer certificado: {e}"]

    if datos.vencido:
        errores.append(f"Certificado vencido el {datos.vence.strftime('%d-%m-%Y')}")
    elif datos.todavia_no_vale:
        errores.append("Certificado aún no es válido (fecha de inicio futura)")

    try:
        leer_clave(clave)
    except ArchivoInvalido as e:
        errores.append(f"Error al leer clave privada: {e}")
        return errores

    if not son_pareja(certificado, clave):
        errores.append("La clave privada no corresponde al certificado")

    return errores


def revisar_par_de_archivos(cert_path: str, key_path: str) -> list[str]:
    """`revisar_par` sobre dos rutas. Es lo que llama
    `arca_wsaa.validar_archivos()`, que se mantiene por compatibilidad."""
    try:
        certificado = _leer_bytes(cert_path, "certificado")
    except ArchivoInvalido as e:
        return [str(e)]
    try:
        clave = _leer_bytes(key_path, "clave privada")
    except ArchivoInvalido as e:
        return [str(e)]
    return revisar_par(certificado, clave)


# ── Los permisos de la clave privada ────────────────────────────────────────


def escribir_clave_privada(destino: str, contenido: bytes) -> None:
    """Guarda la clave privada **sólo legible por su dueño** (0600).

    🔴 Hasta el 2026-10-02 la pantalla la escribía con `open(destino, "wb")`, o sea
    con la umask del proceso: **644, legible por cualquiera dentro del contenedor**
    (medido en la instancia dev de LibraCargo). El certificado es público; la
    clave es la identidad fiscal del cliente.

    Se escribe a un temporal creado ya con 0600 y se reemplaza: así **no hay
    instante con la clave abierta**, y un archivo previo en 644 deja de existir
    en vez de conservar su modo (que es lo que pasaría abriéndolo con `os.open`
    sobre el mismo nombre).
    """
    import os
    carpeta = os.path.dirname(destino) or "."
    tmp = os.path.join(carpeta, f".{os.path.basename(destino)}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(contenido)
        os.replace(tmp, destino)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cerrar_permisos_de_la_clave(path: str) -> bool:
    """Deja en 0600 una clave que ya estaba guardada abierta. `True` si la cambió.

    Existe para las instancias vivas, que ya tienen la clave en 644, y para lo
    que la reescribe sin pasar por la pantalla (restaurar un ZIP de respaldo, la
    migración `0008`). **Nunca levanta**: si el archivo es de otro usuario o el
    volumen no deja, la emisión sigue como estaba, que es mejor que dejarla
    caída por un permiso.
    """
    import os
    import stat
    try:
        modo = stat.S_IMODE(os.stat(path).st_mode)
        if modo & 0o077:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            return True
    except OSError:
        pass
    return False


# ── El pedido de certificado: la clave nace en el servidor ──────────────────
#
# Hasta el 2026-10-08 la clave y el `.csr` los generaba a mano quien daba de alta
# la instancia, con `openssl` en su PC, y la clave se **subía como archivo**: pasaba
# por un mail, un chat o un pendrive antes de llegar al servidor. Acá se generan
# adentro y la clave no sale: de este módulo sólo sale el `.csr`, que es público.


class PedidoInvalido(ValueError):
    """Los datos del pedido no sirven. El mensaje va tal cual a la pantalla."""


#: ARCA no admite guiones ni espacios en el alias: letras y números.
_ALIAS = re.compile(r"[A-Za-z0-9]{3,40}")
#: Lo que se deja pasar de la razón social. Tilde y eñe se normalizan; lo demás que
#: no figure acá se rechaza antes de que ARCA devuelva un error que no dice cuál era.
_RAZON_SOCIAL = re.compile(r"[A-Za-z0-9 .,&'-]{1,64}")


@dataclass(frozen=True)
class PedidoDeCertificado:
    """La clave privada nueva y el `.csr` que se le manda a ARCA.

    🔴 `clave` **no** se imprime: está fuera del `repr`, para que un `print`, un
    `logger.debug(pedido)` o el informe de un test fallido no la escriban en un log.
    """

    clave: bytes = field(repr=False)
    csr: bytes
    sujeto: str
    alias: str
    cuit: str
    razon_social: str


def datos_del_pedido(cuit: str, razon_social: str, alias: str) -> tuple[str, str, str]:
    """Normaliza y valida `(cuit, razón social, alias)` del pedido. Levanta `PedidoInvalido`.

    - **CUIT**: 11 dígitos, sin guiones ni espacios (se aceptan escritos con ellos).
    - **Razón social**: sin tildes ni eñe (`Ñ` pasa a `N`), de hasta 64 caracteres, con
      letras, números, espacio y `. , & ' -`. 64 es el tope del campo `O` de X.509.
    - **Alias**: sólo letras y números, de 3 a 40. Es el `CN` del pedido y el nombre con el
      que ARCA lo muestra en su lista.
    """
    c = re.sub(r"[\s-]", "", cuit or "")
    if not (len(c) == 11 and c.isdigit()):
        raise PedidoInvalido("El CUIT tiene 11 dígitos, sin guiones.")

    r = unicodedata.normalize("NFKD", razon_social or "")
    r = "".join(ch for ch in r if not unicodedata.combining(ch))
    r = re.sub(r"\s+", " ", r).strip()
    if not r:
        raise PedidoInvalido("Falta la razón social.")
    if not _RAZON_SOCIAL.fullmatch(r):
        raise PedidoInvalido(
            "La razón social admite hasta 64 caracteres, con letras, números, espacio y "
            "los signos . , & ' - (sin tildes: se escribe «Ñ» como «N»).")

    a = (alias or "").strip()
    if not _ALIAS.fullmatch(a):
        raise PedidoInvalido(
            "El alias lleva sólo letras y números, de 3 a 40 (ARCA no admite guiones ni espacios).")
    return c, r, a


def generar_pedido(cuit: str, razon_social: str, alias: str) -> PedidoDeCertificado:
    """Genera una clave RSA de 2048 bits **sin passphrase** y su pedido de certificado (PKCS#10).

    El sujeto es el que pide ARCA: `C=AR, O=<razón social>, CN=<alias>, serialNumber=CUIT <n>`
    (el mismo que armaba a mano `docs/guia-certificado-arca.md`), firmado con SHA-256.

    Sin passphrase no es una preferencia: el ticket de acceso se pide sin nadie que la escriba
    (ver `leer_clave`). La clave sale en PKCS#8, que es lo que escribe `openssl genrsa` desde la 3.0.

    🔑 **Quien llama guarda `clave` (con `escribir_clave_privada`) y muestra `csr`.** Esta función
    no toca el disco ni la base.
    """
    cuit, razon_social, alias = datos_del_pedido(cuit, razon_social, alias)
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sujeto = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, razon_social),
        x509.NameAttribute(NameOID.COMMON_NAME, alias),
        x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {cuit}"),
    ])
    csr = x509.CertificateSigningRequestBuilder().subject_name(sujeto).sign(clave, hashes.SHA256())
    return PedidoDeCertificado(
        clave=clave.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        csr=csr.public_bytes(serialization.Encoding.PEM),
        sujeto=sujeto.rfc4514_string(),
        alias=alias, cuit=cuit, razon_social=razon_social,
    )
