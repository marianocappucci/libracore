"""
Envío de comprobantes por email vía SMTP (STARTTLS). Extraído 2026-07-26 de
Contalibra/Restolibra, donde `email_sender.py` era byte-idéntico y activo
(usado por `web/routers/webhooks.py`, `web/helpers/email_helper.py`,
`web/api/mp_bandeja.py` y `mp_facturacion.py` en ambos) — ver
wiki/analyses/auditoria-duplicacion-familia-libra.md.
"""
import os
import smtplib
from email.message import EmailMessage


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

    with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
