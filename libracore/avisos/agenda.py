"""El adaptador de `libragenda.NotificationPort` a los avisos del motor (ADR-090): lo que reemplaza al `LoggingNotificationPort`
de GestioLibra y MedLibra.

`libragenda` es dueña de una sola cosa: la regla «¿toca recordar?» (`due_reminders`) y un puerto, `send(notification)`, que el
producto implementa. Lo que el puerto no trae —a quién, por qué canal, con qué texto— es del producto. Este adaptador pone lo que
sí es común:

    puerto = NotificacionPorAvisos(
        lambda: proveedores(smtp_config),          # se resuelve en cada envío: la configuración cambia con el proceso andando
        contacto=lambda client_id: {"email": c.email, "telefono": c.telefono},
        redactar=lambda n, canal: Mensaje("", asunto="Recordatorio de turno", cuerpo=f"Tu turno es el {n.starts_at:%d/%m a las %H:%M}"),
    )
    ReminderDispatcher(citas, recordados, puerto, DEFAULT_REMINDER_POLICIES)

- Es **estructural**, como el puerto: no importa `libragenda` (el motor no depende de ella). Le alcanza con que la notificación
  tenga `appointment_id`, `policy_id` y `client_id`.
- **No levanta por un fallo de envío.** `ReminderDispatcher.dispatch` marca el recordatorio como enviado apenas `send` vuelve, y una
  excepción cortaría el resto de la tanda. El fallo queda `fallido` en el registro de avisos y lo reintenta `reintentar_fallidos`
  (5 minutos después del último intento: da igual si va antes o después de `dispatch` en el cron).
- La clave es `agenda:{appointment_id}:{policy_id}`: aunque `dispatch` repita una notificación, **no sale dos veces**.
- Un canal sin destino (el paciente no dejó teléfono) queda `omitido` con su motivo; un canal apagado o sin configurar no escribe nada.
  Si **ninguno** está disponible, `libragenda` igual da el recordatorio por visto (igual que con el `LoggingNotificationPort`): prender un
  canal después no reenvía lo viejo, y un recordatorio de un turno que ya pasó no le sirve a nadie.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping

from libracore.avisos.despachador import Envio, despachar, omitir
from libracore.avisos.mensaje import CANAL_EMAIL, CANAL_WHATSAPP, Mensaje, Proveedor
from libracore.avisos.telefono import telefono_para_whatsapp

TIPO = "recordatorio"


def clave_de_recordatorio(appointment_id: str, policy_id: str) -> str:
    return f"agenda:{appointment_id}:{policy_id}"


class NotificacionPorAvisos:
    """Cumple `libragenda.NotificationPort`. Ver el docstring del módulo.

    - `proveedores`: `{canal: Proveedor}` o un callable que lo devuelve (`libracore.avisos.proveedores(smtp_config)`).
    - `contacto(client_id)`: `{"email": ..., "telefono": ...}` del cliente; lo que falte, vacío.
    - `redactar(notificacion, canal)`: el `Mensaje` de ese canal (el `destino` lo pone el adaptador). `None` = no avisar por ahí.
    - `canales`: en qué orden se prueba; por defecto los dos, y un recordatorio sale por cada uno que tenga destino.
    """

    def __init__(self, proveedores: Mapping[str, Proveedor] | Callable[[], Mapping[str, Proveedor]], *,
                 contacto: Callable[[str], Mapping[str, str]], redactar: Callable[[object, str], Mensaje | None],
                 canales: tuple[str, ...] = (CANAL_WHATSAPP, CANAL_EMAIL), tipo: str = TIPO,
                 nodo: Callable[[], str | None] | None = None) -> None:
        self._proveedores = proveedores if callable(proveedores) else (lambda: proveedores)
        self._contacto = contacto
        self._redactar = redactar
        self._canales = canales
        self._tipo = tipo
        self._nodo = nodo

    def send(self, notification) -> list[Envio]:
        clave = clave_de_recordatorio(notification.appointment_id, notification.policy_id)
        proveedores = self._proveedores()
        contacto = self._contacto(notification.client_id) or {}
        envios: list[Envio] = []
        for canal in self._canales:
            proveedor = proveedores.get(canal)
            if proveedor is None or not proveedor.disponible():
                continue
            destino = (telefono_para_whatsapp(contacto.get("telefono")) if canal == CANAL_WHATSAPP
                       else (contacto.get("email") or "").strip())
            if not destino:
                dato = "un teléfono que WhatsApp reconozca" if canal == CANAL_WHATSAPP else "email"
                envios.append(omitir(canal, clave, motivo=f"El cliente no tiene {dato}.", tipo=self._tipo,
                                    nodo=self._nodo))
                continue
            mensaje = self._redactar(notification, canal)
            if mensaje is None:
                continue
            envios.append(despachar(proveedor, dataclasses.replace(mensaje, destino=destino), clave=clave, tipo=self._tipo,
                                    nodo=self._nodo))
        return envios
