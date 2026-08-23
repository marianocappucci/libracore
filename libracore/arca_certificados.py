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

from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.x509 import load_pem_x509_certificate


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
