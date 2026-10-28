"""Los tipos del motor de avisos (ADR-090): el mensaje, lo que contesta un proveedor y el contrato de un proveedor."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

CANAL_EMAIL = "email"
CANAL_WHATSAPP = "whatsapp"
CANALES = (CANAL_EMAIL, CANAL_WHATSAPP)

#: Cómo termina un intento de mandar, según el proveedor. Son los estados del registro que un proveedor puede decidir; los demás
#: (`enviando`, `omitido`) los decide el despachador.
ENVIADO = "enviado"
REGISTRADO = "registrado"
FALLIDO = "fallido"
RECHAZADO = "rechazado"


@dataclass(frozen=True, slots=True)
class Mensaje:
    """Lo que hay que decirle a alguien. Sirve para cualquier canal.

    - `destino`: la dirección de mail, o el teléfono **ya normalizado** (`telefono_para_whatsapp`).
    - `asunto`: lo usa el mail; WhatsApp no tiene.
    - `cuerpo`: el texto. En WhatsApp es el mensaje entero (la primera línea, en negrita, hace de título).
    - `plantilla` y `variables`: **existen desde el primer día por la API de Meta**, que para un mensaje iniciado por el negocio
      exige una plantilla aprobada y sus variables, no un texto libre. Evolution API no las pide y las ignora; el proveedor de
      Meta las va a necesitar. Se guardan en el registro igual: así un aviso se puede reconstruir y mandar por otro proveedor.
    """

    destino: str
    asunto: str = ""
    cuerpo: str = ""
    plantilla: str = ""
    variables: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Resultado:
    """Lo que un proveedor contesta después de intentar mandar un mensaje.

    - `enviado`: el proveedor lo aceptó para entregarlo (`id_externo` es el que le puso, si lo dio).
    - `registrado`: un proveedor **simulado** lo anotó sin mandar nada (modo prueba).
    - `fallido`: no salió y puede salir después (timeout, servidor caído, credencial a corregir): se reintenta.
    - `rechazado`: el destino no sirve (número sin WhatsApp, casilla inexistente) y reintentar no lo arregla.

    `detalle` es lo que ve una persona en la pantalla de avisos. **Nunca lleva una credencial ni el cuerpo del mensaje.**
    """

    estado: str
    detalle: str = ""
    id_externo: str = ""

    @classmethod
    def enviado(cls, id_externo: str = "") -> Resultado:
        return cls(ENVIADO, id_externo=id_externo)

    @classmethod
    def registrado(cls, detalle: str = "") -> Resultado:
        return cls(REGISTRADO, detalle)

    @classmethod
    def fallido(cls, detalle: str) -> Resultado:
        return cls(FALLIDO, detalle)

    @classmethod
    def rechazado(cls, detalle: str) -> Resultado:
        return cls(RECHAZADO, detalle)


class Proveedor(Protocol):
    """Cómo sale un mensaje por un canal. Un objeto por canal y por forma de mandarlo.

    - `canal`: `email` o `whatsapp`.
    - `nombre`: `smtp`, `evolution`, `registro`...; queda en el registro de cada aviso.
    - `simulado`: `True` si **no manda nada** (`ProveedorRegistro`). Importa por una regla: un aviso `registrado` por un proveedor
      simulado cierra el tema sólo para otro simulado; cuando se conecta uno real, lo registrado en modo prueba se manda.
    """

    canal: str
    nombre: str
    simulado: bool

    def disponible(self) -> bool:
        """Si hay con qué mandar. Se pregunta **antes** de despachar: sin esto, una instancia sin SMTP acumularía un fallo por
        aviso, y a los tres intentos dejaría de mandarle a ese cliente por una configuración que a lo mejor se completa
        mañana."""

    def enviar(self, mensaje: Mensaje) -> Resultado:
        """Manda, o dice por qué no. Un proveedor **no levanta** por una falla de red: la devuelve como `Resultado`."""
