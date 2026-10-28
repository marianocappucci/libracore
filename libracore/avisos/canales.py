"""Por qué canal sale un aviso (ADR-090, addendum de v1.204.0): **uno solo**, WhatsApp si se puede, mail si no.

Hasta v1.202 un aviso a un cliente con teléfono y mail salía por los dos. La política por defecto ahora es `preferir_whatsapp`:

1. Si WhatsApp está disponible (`registro` o `evolution`, no `apagado`) y el cliente tiene un teléfono que WhatsApp reconoce,
   sale **sólo por WhatsApp**.
2. Si no tiene teléfono válido, o WhatsApp está apagado, sale por mail (si hay SMTP y el cliente tiene mail).
3. **Si WhatsApp lo dio por perdido** —`rechazado` (el número no tiene WhatsApp, un caso permanente) o `fallido` con los intentos
   agotados— cae al mail **con la misma clave lógica**: es otro canal, así que la clave no choca, y repetir la llamada no manda
   dos veces por ninguno.
4. **Lo que ya salió por un canal no sale por el otro**, aunque la configuración haya cambiado entre dos corridas (WhatsApp apagado
   en una, prendido en la siguiente; o al revés): antes de elegir se mira si la clave tiene un aviso `enviado` en cualquier canal.

`politica="todos"` es el comportamiento anterior (cada canal con destino, sin mirar los otros) para quien lo pida explícitamente.

Quien tiene un barrido propio (LibraClub) usa `avisar` en lugar de armar su propio ciclo por canal; `canal_para` es la parte
«¿por dónde?» sola, para mostrar o filtrar sin despachar.

Qué **no** cubre: un `fallido` de WhatsApp que se reintenta después con `reintentar_fallidos(whatsapp)` y termina `rechazado` no
cae al mail, porque el reintento no conoce el contacto ni el texto del otro canal. Mientras tanto queda en el registro como
`rechazado` y se ve en el listado. Es un pendiente documentado (`docs/avisos.md`).
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from libracore.avisos.despachador import (
    EN_NODO,
    MAX_INTENTOS,
    NO_DISPONIBLE,
    Envio,
    despachar,
    omitir,
    ya_salio,
)
from libracore.avisos.mensaje import CANAL_EMAIL, CANAL_WHATSAPP, FALLIDO, RECHAZADO, Mensaje, Proveedor
from libracore.avisos.telefono import telefono_para_whatsapp
from libracore.db.avisos_envios import ESTADO_OMITIDO

PREFERIR_WHATSAPP = "preferir_whatsapp"
TODOS = "todos"
POLITICAS = (PREFERIR_WHATSAPP, TODOS)

#: El orden de preferencia: el primero que se pueda, y los siguientes de respaldo.
CANALES_POR_DEFECTO = (CANAL_WHATSAPP, CANAL_EMAIL)


@dataclass(frozen=True, slots=True)
class Via:
    """Un canal por el que se le puede avisar a ese contacto ahora: hay proveedor disponible y hay destino."""

    canal: str
    proveedor: Proveedor
    destino: str


def destino_de(canal: str, contacto: Mapping[str, str] | None) -> str:
    """El destino de ese canal según el contacto (`{"email": ..., "telefono": ...}`): el teléfono normalizado o el mail. Vacío si no hay."""
    contacto = contacto or {}
    if canal == CANAL_WHATSAPP:
        return telefono_para_whatsapp(contacto.get("telefono"))
    return (contacto.get("email") or "").strip()


def _validar_politica(politica: str) -> str:
    if politica not in POLITICAS:
        raise ValueError(f"La política de canales tiene que ser una de: {', '.join(POLITICAS)}.")
    return politica


def canal_para(contacto: Mapping[str, str] | None, proveedores: Mapping[str, Proveedor], *,
               canales: tuple[str, ...] = CANALES_POR_DEFECTO) -> Via | None:
    """La primera vía (por orden de `canales`) con proveedor disponible y destino para ese contacto, o `None` si no hay ninguna.

    Es la decisión de `preferir_whatsapp` sin despachar nada: no escribe en el registro y no mira lo que ya salió."""
    for canal in canales:
        proveedor = proveedores.get(canal)
        if proveedor is None or not proveedor.disponible():
            continue
        destino = destino_de(canal, contacto)
        if destino:
            return Via(canal, proveedor, destino)
    return None


def _definitivamente_sin_salir(envio: Envio) -> bool:
    """Si por ese canal ese aviso **no va a salir nunca**, y entonces corresponde probar el siguiente."""
    if envio.estado in (RECHAZADO, ESTADO_OMITIDO):
        return True
    return envio.estado == FALLIDO and envio.intentos >= MAX_INTENTOS


def avisar(proveedores: Mapping[str, Proveedor], contacto: Mapping[str, str] | None,
           redactar: Callable[[str], Mensaje | None], *, clave: str, tipo: str = "",
           politica: str = PREFERIR_WHATSAPP, canales: tuple[str, ...] = CANALES_POR_DEFECTO,
           nodo: Callable[[], str | None] | None = None) -> list[Envio]:
    """Manda el aviso de esa `clave` según la política y devuelve un `Envio` por cada canal que se tocó. Ver el docstring del módulo.

    - `proveedores`: `{canal: Proveedor}` (`libracore.avisos.proveedores(smtp_config)`).
    - `contacto`: `{"email": ..., "telefono": ...}`; lo que falte, vacío.
    - `redactar(canal)`: el `Mensaje` de ese canal (el `destino` lo pone `avisar`). `None` = por ese canal no se avisa; con
      `preferir_whatsapp` se prueba el siguiente.
    - Un canal apagado o sin configurar no escribe nada. Un canal disponible sin destino queda `omitido` con su motivo.
    - No levanta por un fallo de envío (`despachar` lo devuelve como estado)."""
    _validar_politica(politica)
    if politica == PREFERIR_WHATSAPP:
        previo = ya_salio(clave, canales, nodo=nodo)
        if previo is not None:
            return [previo]

    envios: list[Envio] = []
    for canal in canales:
        proveedor = proveedores.get(canal)
        if proveedor is None or not proveedor.disponible():
            continue
        destino = destino_de(canal, contacto)
        if not destino:
            dato = "un teléfono que WhatsApp reconozca" if canal == CANAL_WHATSAPP else "email"
            envios.append(omitir(canal, clave, motivo=f"El cliente no tiene {dato}.", tipo=tipo, nodo=nodo))
            continue
        mensaje = redactar(canal)
        if mensaje is None:
            continue
        envio = despachar(proveedor, dataclasses.replace(mensaje, destino=destino), clave=clave, tipo=tipo, nodo=nodo)
        envios.append(envio)
        if politica == TODOS or envio.estado == NO_DISPONIBLE:
            continue
        if envio.estado == EN_NODO or not _definitivamente_sin_salir(envio):
            break
    return envios


__all__ = ["CANALES_POR_DEFECTO", "POLITICAS", "PREFERIR_WHATSAPP", "TODOS", "Via", "avisar", "canal_para", "destino_de"]
