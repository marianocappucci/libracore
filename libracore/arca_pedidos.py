"""El pedido de certificado pendiente: la clave que espera el `.crt` de ARCA (ADR-036).

Entre que se genera el `.csr` y vuelve el `.crt` pasan horas o días. En ese tiempo la
clave tiene que estar guardada **en algún lado**, y no puede ser el lugar de la clave
vigente: una instancia que ya factura —o que está renovando un certificado por vencer—
dejaría de hacerlo en el acto, con un certificado que sigue siendo bueno y una clave
nueva que no es su pareja. Por eso la clave **pendiente** vive aparte y recién se
promueve a vigente cuando llega un `.crt` que empareja con ella.

## Dónde, y por qué sin tabla

En `CERTS_DIR`, al lado de los pares, con dos archivos por pedido:

    pedido-{servicio}-{ambiente}-{huella}.key     la clave privada, en 0600
    pedido-{servicio}-{ambiente}-{huella}.json    alias, CUIT, razón social, fecha y el `.csr`

Hay **un pedido pendiente** por (servicio, ambiente, empresa), la misma terna con la que
se guarda un par. `{servicio}` es `wsfe` para la facturación. La huella es la de la
empresa, igual que en `_nombres_de_servicio` del router: dos empresas del mismo servicio
no se pisan y el nombre de la empresa, que puede traer cualquier carácter, no entra en la
ruta.

No hay tabla porque no hace falta migración para algo que es un archivo con una vida corta
y que `arca_certs/` ya cubre: el respaldo y la restauración (`directorios_de_datos`) lo
llevan sin tocar nada, que es lo que se necesita de una clave que no se puede regenerar
sin volver a pedirle el certificado a ARCA. Ningún producto guarda este estado en su base.

## El orden de escritura

La clave primero, el `.json` último, y antes de todo se borra el `.json` viejo: un pedido
**existe** sólo si están los dos archivos. Si el proceso muere a la mitad, queda una clave
huérfana que el próximo pedido pisa, no un `.json` que describe un `.csr` cuya clave se
perdió.

🔑 **Nada de acá devuelve la clave salvo `clave()`**, que existe para una sola cosa: ver si
un `.crt` es su pareja y, si lo es, instalarla. El router nunca la pone en una respuesta.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from libracore import arca_certificados

logger = logging.getLogger(__name__)

_AR = timezone(timedelta(hours=-3))  # America/Argentina/Buenos_Aires, sin horario de verano


@dataclass(frozen=True)
class Pedido:
    """Lo que se sabe de un pedido pendiente. **Sin la clave.**"""

    servicio: str
    ambiente: str
    empresa: str
    alias: str
    cuit: str
    razon_social: str
    sujeto: str
    creado: datetime
    csr: bytes

    def como_dict(self, *, con_csr: bool = False) -> dict:
        """La forma que ve la pantalla. El `.csr` va sólo cuando se pide (al generar)."""
        salida = {
            "pendiente": True,
            "servicio": self.servicio,
            "ambiente": self.ambiente,
            "alias": self.alias,
            "cuit": self.cuit,
            "razon_social": self.razon_social,
            "sujeto": self.sujeto,
            # `dd-mm-aaaa`, el formato de la familia, y en hora argentina: un pedido
            # hecho a las 22 no puede figurar con la fecha de mañana.
            "creado": self.creado.astimezone(_AR).strftime("%d-%m-%Y"),
        }
        if con_csr:
            salida["csr"] = self.csr.decode("ascii")
        return salida


def _base(servicio: str, ambiente: str, empresa: str) -> str:
    huella = hashlib.sha1((empresa or "").encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    limpio = re.sub(r"[^a-z0-9]+", "-", (servicio or "").lower()).strip("-")
    return f"pedido-{limpio}-{ambiente}-{huella}"


def _rutas(carpeta: str, servicio: str, ambiente: str, empresa: str) -> tuple[str, str]:
    base = os.path.join(carpeta, _base(servicio, ambiente, empresa))
    return f"{base}.key", f"{base}.json"


def _quitar(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def guardar(carpeta: str, servicio: str, ambiente: str, empresa: str,
            generado: arca_certificados.PedidoDeCertificado, *,
            ahora: datetime | None = None) -> Pedido:
    """Guarda el pedido recién generado como **el** pendiente, reemplazando uno anterior.

    La clave se escribe con `escribir_clave_privada` (0600, sin instante abierta). Reemplazar
    destruye la clave anterior: si ya se había mandado su `.csr` a ARCA, el `.crt` que vuelva
    no va a tener con qué emparejar. Decidir eso es de quien llama (el router pide confirmación).
    """
    ahora = ahora or datetime.now(UTC)
    clave_path, meta_path = _rutas(carpeta, servicio, ambiente, empresa)
    os.makedirs(carpeta, exist_ok=True)
    _quitar(meta_path)
    arca_certificados.escribir_clave_privada(clave_path, generado.clave)

    pedido = Pedido(
        servicio=servicio, ambiente=ambiente, empresa=empresa, alias=generado.alias,
        cuit=generado.cuit, razon_social=generado.razon_social, sujeto=generado.sujeto,
        creado=ahora, csr=generado.csr,
    )
    tmp = f"{meta_path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "alias": pedido.alias, "cuit": pedido.cuit, "razon_social": pedido.razon_social,
                "sujeto": pedido.sujeto, "creado": ahora.astimezone(UTC).isoformat(),
                "csr": pedido.csr.decode("ascii"),
            }, f, ensure_ascii=False)
        os.replace(tmp, meta_path)
    except BaseException:
        _quitar(tmp)
        raise
    return pedido


def leer(carpeta: str, servicio: str, ambiente: str, empresa: str) -> Pedido | None:
    """El pedido pendiente, o `None` si no hay (o si quedó a medias o ilegible)."""
    clave_path, meta_path = _rutas(carpeta, servicio, ambiente, empresa)
    if not (os.path.exists(clave_path) and os.path.exists(meta_path)):
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        return Pedido(
            servicio=servicio, ambiente=ambiente, empresa=empresa,
            alias=meta["alias"], cuit=meta["cuit"], razon_social=meta["razon_social"],
            sujeto=meta["sujeto"], creado=datetime.fromisoformat(meta["creado"]),
            csr=meta["csr"].encode("ascii"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        # Un `.json` roto no tumba la pantalla de configuración: se trata como «sin
        # pedido» y el próximo lo reemplaza. Se deja constancia, sin el contenido.
        logger.warning("El pedido de certificado de %s (%s) no se pudo leer.", servicio, ambiente)
        return None


def clave(carpeta: str, servicio: str, ambiente: str, empresa: str) -> bytes | None:
    """La clave privada pendiente, para emparejarla con un `.crt` o instalarla. Nunca a una respuesta."""
    clave_path, _ = _rutas(carpeta, servicio, ambiente, empresa)
    try:
        with open(clave_path, "rb") as f:
            return f.read()
    except OSError:
        return None


def descartar(carpeta: str, servicio: str, ambiente: str, empresa: str) -> bool:
    """Borra la clave y los datos del pedido. `True` si había algo."""
    clave_path, meta_path = _rutas(carpeta, servicio, ambiente, empresa)
    # El `.json` primero, por el mismo motivo que en `guardar`: sin él no hay pedido.
    habia_meta = _quitar(meta_path)
    habia_clave = _quitar(clave_path)
    return habia_meta or habia_clave
