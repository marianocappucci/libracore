"""
Envío de comprobantes por email vía SMTP (STARTTLS). Extraído 2026-07-26 de
Contalibra/Restolibra, donde `email_sender.py` era byte-idéntico y activo
(usado por `web/routers/webhooks.py`, `web/helpers/email_helper.py`,
`web/api/mp_bandeja.py` y `mp_facturacion.py` en ambos) — ver
wiki/analyses/auditoria-duplicacion-familia-libra.md.

## Una demo no manda mails (ADR-090, addendum de v1.204.0)

`_entregar` es el único lugar del motor que abre una sesión SMTP para **mandar**, y `enviar_documento`, `enviar_comprobante` y
`enviar_texto` pasan por ahí. (El otro `smtplib.SMTP` del motor es el de *Probar conexión*, `smtp_router`, que no manda nada y
tiene su propia guarda.) Con `DEMO_MODE=1` **ninguna sale a la red**: levantan `MailsDeshabilitadosEnDemo` (un `OSError`, así que
lo que ya atrapa los errores de red de `smtplib` lo trata como un fallo de envío, con un mensaje que se entiende). La semilla de una
demo trae mails ficticios, y una demo con SMTP cargado mandaba facturas y presupuestos de verdad.

Los routers (`facturas_router`, `presupuestos_router`, `pre_facturas_router`, `mp_bandeja_router`) preguntan antes, con
`exigir_envio_real()`, para contestar «En la demo los mails no se envían» (403) sin generar el PDF ni dejar un error en el rastro
de envíos. La guarda de acá es la red de seguridad de todo lo demás: un producto que llame a `email_sender` por su cuenta también queda
cubierto.
"""
import os
import smtplib
from email.message import EmailMessage

from libracore import config_manager

MENSAJE_DEMO = "En la demo los mails no se envían."


class MailsDeshabilitadosEnDemo(OSError):
    """Se quiso mandar un mail desde una demo. Es un `OSError` a propósito: los llamadores ya tratan así un fallo de red."""


def exigir_envio_real() -> None:
    """Levanta `MailsDeshabilitadosEnDemo` si esta instancia es una demo (`DEMO_MODE=1`); no hace nada si no."""
    if config_manager.es_demo():
        raise MailsDeshabilitadosEnDemo(MENSAJE_DEMO)


def enviar_comprobante(
    to_email: str,
    to_name: str,
    pdf_path: str,
    empresa_nombre: str,
    factura_label: str,
    total: float,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_email: str,
    from_name: str,
    asunto: str = "",
    cuerpo: str = "",
):
    if not asunto:
        asunto = f"{factura_label} - {empresa_nombre}"
    if not cuerpo:
        cuerpo = (
            f"Estimado/a {to_name or to_email},\n\n"
            f"Adjuntamos el comprobante correspondiente.\n\n"
            f"Comprobante: {factura_label}\n"
            f"Total: $ {total:,.2f}\n\n"
            f"Muchas gracias.\n{empresa_nombre}"
        )

    enviar_documento(
        to_email=to_email, to_name=to_name, pdf_path=pdf_path,
        asunto=asunto, cuerpo=cuerpo,
        smtp_host=smtp_host, smtp_port=smtp_port, smtp_user=smtp_user,
        smtp_password=smtp_password, from_email=from_email, from_name=from_name,
    )


def enviar_documento(
    to_email: str,
    to_name: str,
    pdf_path: str,
    asunto: str,
    cuerpo: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_email: str,
    from_name: str,
    filename: str = "",
    pdf_bytes: bytes | None = None,
):
    """Envía un PDF adjunto sin asumir que sea un comprobante fiscal.

    `pdf_bytes` manda un PDF que está en memoria, sin pasarlo por un archivo (la pre
    factura se genera al momento, `libracore.pre_facturas`): en ese caso `pdf_path` se
    ignora (puede ir vacío) y `filename` es el nombre con que llega el adjunto.

    `enviar_comprobante` (que arma asunto/cuerpo con semántica de factura +
    total) quedó como caso particular de ésta. Se separó al agregar el resumen
    automático de cuenta corriente, cuyo adjunto no es un comprobante.
    """
    exigir_envio_real()  # antes de leer el PDF: en una demo no hay nada que armar
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email
    msg.set_content(cuerpo)

    if pdf_bytes is not None:
        contenido, nombre = pdf_bytes, filename or "documento.pdf"
    else:
        with open(pdf_path, "rb") as f:
            contenido = f.read()
        nombre = filename or os.path.basename(pdf_path)
    msg.add_attachment(contenido, maintype="application", subtype="pdf", filename=nombre)

    _entregar(msg, smtp_host, smtp_port, smtp_user, smtp_password)


def enviar_texto(
    to_email: str,
    asunto: str,
    cuerpo: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_email: str,
    from_name: str = "",
    to_name: str = "",
    timeout: float = 30,
):
    """Manda un mail de texto plano, sin adjunto (ADR-090: el proveedor SMTP de `libracore.avisos`).

    Es la misma sesión que `enviar_documento` (STARTTLS y login), con dos diferencias: no hay adjunto y **tiene
    `timeout`**. Un aviso sale desde un barrido, y un servidor que no contesta no puede dejarlo colgado.
    """
    exigir_envio_real()
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email
    msg.set_content(cuerpo)
    _entregar(msg, smtp_host, smtp_port, smtp_user, smtp_password, timeout=timeout)


def _entregar(msg: EmailMessage, smtp_host, smtp_port, smtp_user, smtp_password, **opciones):
    """La sesión SMTP con STARTTLS y login. `opciones` se pasa a `smtplib.SMTP` (`timeout`).

    🔴 Lo único del motor que manda un mail: en una demo no abre la conexión (ver el docstring del módulo)."""
    exigir_envio_real()
    with smtplib.SMTP(smtp_host, int(smtp_port), **opciones) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
