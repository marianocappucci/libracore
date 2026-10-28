"""Los proveedores de avisos (ADR-090): SMTP, Evolution API, Registro y el apagado.

Cumplen `mensaje.Proveedor`. **Ninguno levanta por una falla de red o de credencial**: la devuelven como `Resultado` con un `detalle`
que una persona pueda leer. Y ninguno pone en ese `detalle`, ni en un log, ni en el `repr`, una credencial o el cuerpo del mensaje.

## Evolution API (WhatsApp)

El único WhatsApp que habla hoy la familia es el de n8n del HOMEI contra Evolution: `POST {base}/message/sendText/{instancia}`, header
`apikey`, cuerpo `{"number", "text"}`. Evolution no pide plantillas ni ventana de 24 horas. Este proveedor hace exactamente eso, con
`httpx` y **timeout** (un barrido no puede colgarse esperando a un servidor caído), y distingue lo que se reintenta de lo que no:

| Respuesta | Resultado |
|---|---|
| 200 / 201 | `enviado` (`id_externo` = `key.id`) |
| 400 con `exists: false` | `rechazado`: el número no tiene WhatsApp. Reintentar no lo arregla |
| 400 sin eso, 401, 403, 404, 429, 5xx, otra | `fallido`: se reintenta (una apikey o una instancia mal puestas se corrigen en la configuración y el aviso sale después) |
| timeout / no conecta | `fallido` |

El 401 de Evolution **no se parece al 401 de nuestra API**: el router lo devuelve como 502, porque un 401 hace que el kit mande al
usuario a la pantalla de login.

No se sigue ninguna redirección (`httpx` no lo hace por defecto): un 30x hacia otro host se llevaría la `apikey` en el header.
"""
from __future__ import annotations

import smtplib
from collections.abc import Callable
from urllib.parse import quote

import httpx

from libracore import config_manager
from libracore.avisos.mensaje import CANAL_EMAIL, CANAL_WHATSAPP, Mensaje, Resultado
from libracore.avisos.telefono import es_telefono_normalizado

#: Segundos que se espera a Evolution. El primero (`connect`) es corto: un servidor que no atiende no se espera diez segundos.
TIMEOUT_EVOLUTION = 10.0
TIMEOUT_DE_CONEXION = 5.0

#: Segundos que se espera al servidor de correo. Más largos que los de HTTP: STARTTLS y login son tres idas y vueltas.
TIMEOUT_SMTP = 30.0

#: Lo que se guarda de un texto que vino de afuera (un error de SMTP).
_MAX_DE_AFUERA = 200


def _recortar(texto: object) -> str:
    return " ".join(str(texto).split())[:_MAX_DE_AFUERA]


class ProveedorRegistro:
    """No manda nada: deja el aviso `registrado`. Es el «modo prueba» de una instancia que todavía no conectó un proveedor real.

    `simulado = True` es lo que hace que lo registrado **no cierre el tema** para un proveedor real: cuando se conecte Evolution,
    lo que se anotó en modo prueba se manda, no se da por hecho. Nada real sale desde dev, desde una demo ni desde los tests."""

    nombre = "registro"
    simulado = True

    def __init__(self, canal: str = CANAL_WHATSAPP) -> None:
        self.canal = canal

    def disponible(self) -> bool:
        return True

    def enviar(self, mensaje: Mensaje) -> Resultado:
        return Resultado.registrado("Registrado en modo prueba: no se mandó nada.")

    def __repr__(self) -> str:
        return f"ProveedorRegistro(canal={self.canal!r})"


class ProveedorApagado:
    """El canal está apagado: `disponible()` es `False` y el despachador no escribe nada. Existe para que quien avisa no tenga
    que preguntar `if proveedor is None` en cada barrido."""

    nombre = "apagado"
    simulado = False

    def __init__(self, canal: str = CANAL_WHATSAPP, motivo: str = "El canal está apagado.") -> None:
        self.canal = canal
        self.motivo = motivo

    def disponible(self) -> bool:
        return False

    def enviar(self, mensaje: Mensaje) -> Resultado:  # pragma: no cover - el despachador no llega acá
        return Resultado.fallido(self.motivo)

    def __repr__(self) -> str:
        return f"ProveedorApagado(canal={self.canal!r})"


class ProveedorEvolution:
    """WhatsApp por Evolution API (`sendText`). Ver el docstring del módulo.

    `transporte` es un `httpx.BaseTransport`: los tests pasan un `httpx.MockTransport` y no sale nada a la red.

    🔴 **La `apikey` no se imprime nunca.** `__repr__` la tapa, no se loguea, no se pone en un `detalle`, y los mensajes de error
    son frases propias («Evolution rechazó la apikey (401)») y no el texto que devuelve el servidor."""

    canal = CANAL_WHATSAPP
    nombre = "evolution"
    simulado = False

    def __init__(self, url: str, instancia: str, apikey: str, *, timeout: float = TIMEOUT_EVOLUTION,
                 transporte: httpx.BaseTransport | None = None) -> None:
        self._url = (url or "").strip().rstrip("/")
        self._instancia = (instancia or "").strip()
        self._apikey = apikey or ""
        self._timeout = timeout
        self._transporte = transporte

    def __repr__(self) -> str:
        return f"ProveedorEvolution(url={self._url!r}, instancia={self._instancia!r}, apikey=***)"

    def disponible(self) -> bool:
        return bool(self._url and self._instancia and self._apikey)

    def enviar(self, mensaje: Mensaje) -> Resultado:
        numero = (mensaje.destino or "").strip()
        if not es_telefono_normalizado(numero):
            return Resultado.rechazado("El teléfono no está en formato internacional (por ejemplo 5491155551234).")
        texto = (mensaje.cuerpo or mensaje.asunto or "").strip()
        if not texto:
            return Resultado.rechazado("El mensaje está vacío.")
        # `quote` con `safe=""`: el nombre de la instancia es un segmento de la ruta y no puede abrir otra ("a/../b").
        url = f"{self._url}/message/sendText/{quote(self._instancia, safe='')}"
        try:
            timeout = httpx.Timeout(self._timeout, connect=min(TIMEOUT_DE_CONEXION, self._timeout))
            with httpx.Client(timeout=timeout, transport=self._transporte) as cliente:
                r = cliente.post(url, headers={"apikey": self._apikey}, json={"number": numero, "text": texto})
        except httpx.TimeoutException:
            return Resultado.fallido(f"Evolution no respondió en {self._timeout:g} segundos.")
        except httpx.HTTPError as e:
            # Sólo la clase: el texto de una excepción de red puede traer la URL entera.
            return Resultado.fallido(f"No se pudo conectar con Evolution ({type(e).__name__}).")
        return self._interpretar(r)

    @staticmethod
    def _interpretar(r: httpx.Response) -> Resultado:
        estado = r.status_code
        if estado in (200, 201):
            return Resultado.enviado(_id_del_mensaje(r))
        if estado == 400 and _numero_inexistente(r):
            return Resultado.rechazado("El número no tiene WhatsApp.")
        if estado in (401, 403):
            return Resultado.fallido(f"Evolution rechazó la apikey ({estado}). Revisala en la configuración de avisos.")
        if estado == 404:
            return Resultado.fallido("Evolution no encontró la instancia (404). Revisá el nombre en la configuración de avisos.")
        return Resultado.fallido(f"Evolution respondió {estado}.")


def _json(r: httpx.Response):
    try:
        return r.json()
    except ValueError:
        return None


def _id_del_mensaje(r: httpx.Response) -> str:
    cuerpo = _json(r)
    clave = cuerpo.get("key") if isinstance(cuerpo, dict) else None
    ident = clave.get("id") if isinstance(clave, dict) else None
    return _recortar(ident) if isinstance(ident, str) else ""


def _numero_inexistente(r: httpx.Response) -> bool:
    """Evolution contesta 400 con `{"response": {"message": [{"exists": false, "number": ...}]}}` cuando el número no tiene
    WhatsApp. Cualquier otro 400 (la instancia desconectada, un cuerpo mal armado) no dice nada del número."""
    cuerpo = _json(r)
    detalle = cuerpo.get("response") if isinstance(cuerpo, dict) else None
    mensajes = detalle.get("message") if isinstance(detalle, dict) else None
    return isinstance(mensajes, list) and any(isinstance(m, dict) and m.get("exists") is False for m in mensajes)


class ProveedorSmtp:
    """Mail por el SMTP que la instancia **ya tiene configurado**. No trae servidor propio.

    Reusa `facturas_router.smtp_efectivo`: la misma resolución que el envío de comprobantes, el de presupuestos, la recuperación de
    contraseña y el botón «Probar conexión» (`smtp_router`). Un segundo lugar donde cargar un servidor de correo sería uno más donde
    el dueño puede cargarlo mal. `smtp_config` es el resolver del producto (`lambda: resolver_smtp_config(fabrica)`): se llama **en
    cada envío**, porque la pantalla de Configuración lo edita mientras el proceso corre. Sin resolver, sale `config.json`.

    🔴 **Una demo (`DEMO_MODE=1`) nunca manda.** Con el SMTP cargado, `enviar` no toca la red: devuelve `registrado`, igual que
    WhatsApp en una demo (`configuracion.proveedor_whatsapp`), y `simulado` pasa a ser `True` para que ese `registrado` cierre el
    tema. Una demo con SMTP real mandaría los recordatorios a los mails ficticios de su semilla. La guarda está **acá y no en la
    fábrica** para que valga también para quien arma `ProveedorSmtp(...)` a mano."""

    canal = CANAL_EMAIL
    nombre = "smtp"

    @property
    def simulado(self) -> bool:
        return config_manager.es_demo()

    def __init__(self, smtp_config: Callable | None = None, *, timeout: float = TIMEOUT_SMTP,
                 enviar_texto: Callable | None = None) -> None:
        self._smtp_config = smtp_config
        self._timeout = timeout
        self._enviar_texto = enviar_texto

    def disponible(self) -> bool:
        from libracore.facturas_router import smtp_configurado

        return smtp_configurado(self._smtp_config)

    def enviar(self, mensaje: Mensaje) -> Resultado:
        from libracore import email_sender
        from libracore.facturas_router import smtp_efectivo

        if config_manager.es_demo():
            return Resultado.registrado("Registrado en una demo: no se mandó nada.")
        destino = (mensaje.destino or "").strip()
        if not _parece_un_mail(destino):
            return Resultado.rechazado("La dirección de mail no es válida.")
        smtp = smtp_efectivo(self._smtp_config)
        enviar = self._enviar_texto or email_sender.enviar_texto
        try:
            enviar(
                to_email=destino, asunto=mensaje.asunto or "", cuerpo=mensaje.cuerpo or "",
                smtp_host=smtp["host"], smtp_port=smtp["port"], smtp_user=smtp["user"],
                smtp_password=smtp["password"], from_email=smtp["from_email"], from_name=smtp["from_name"],
                timeout=self._timeout)
        except smtplib.SMTPRecipientsRefused:
            return Resultado.rechazado("El servidor de correo rechazó la dirección.")
        except smtplib.SMTPAuthenticationError:
            return Resultado.fallido("El servidor de correo rechazó el usuario o la contraseña.")
        except (OSError, smtplib.SMTPException) as e:
            return Resultado.fallido(f"No se pudo mandar el mail ({type(e).__name__}: {_recortar(e)}).")
        return Resultado.enviado()

    def __repr__(self) -> str:
        return "ProveedorSmtp()"


def _parece_un_mail(texto: str) -> bool:
    """Una dirección razonable: una sola, sin espacios ni saltos de línea (que en un header serían una inyección)."""
    if not texto or any(c in texto for c in " \t\r\n,;<>"):
        return False
    local, _, dominio = texto.partition("@")
    return bool(local) and "." in dominio and not dominio.startswith(".") and not dominio.endswith(".")
