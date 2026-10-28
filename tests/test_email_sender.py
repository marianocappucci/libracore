"""Tests para libracore.email_sender — extraído 2026-07-26 de
Contalibra/Restolibra (activo, sin test propio en ninguno de los dos
repos, que no tienen suite propia). Ver
wiki/analyses/auditoria-duplicacion-familia-libra.md."""
from unittest.mock import MagicMock, patch

import pytest

from libracore.email_sender import enviar_comprobante, enviar_documento


@pytest.fixture
def pdf_path(tmp_path):
    path = tmp_path / "factura.pdf"
    path.write_bytes(b"%PDF-1.4 fake pdf content")
    return str(path)


def _kwargs(pdf_path, **overrides):
    base = dict(
        to_email="cliente@example.com",
        to_name="Cliente Ejemplo",
        pdf_path=pdf_path,
        empresa_nombre="Mi Empresa",
        factura_label="Factura A-0001",
        total=1234.5,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="secret",
        from_email="facturacion@example.com",
        from_name="Mi Empresa",
    )
    base.update(overrides)
    return base


@patch("libracore.email_sender.smtplib.SMTP")
def test_enviar_comprobante_sends_via_starttls(mock_smtp_cls, pdf_path):
    mock_server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_server

    enviar_comprobante(**_kwargs(pdf_path))

    mock_smtp_cls.assert_called_once_with("smtp.example.com", 587)
    mock_server.ehlo.assert_called_once()
    mock_server.starttls.assert_called_once()
    mock_server.login.assert_called_once_with("user@example.com", "secret")
    mock_server.send_message.assert_called_once()


@patch("libracore.email_sender.smtplib.SMTP")
def test_default_subject_and_body_are_generated(mock_smtp_cls, pdf_path):
    mock_server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_server

    enviar_comprobante(**_kwargs(pdf_path))

    sent_msg = mock_server.send_message.call_args[0][0]
    assert sent_msg["Subject"] == "Factura A-0001 - Mi Empresa"
    body = sent_msg.get_body(preferencelist=("plain",)).get_content()
    assert "Cliente Ejemplo" in body
    assert "Factura A-0001" in body
    assert "1,234.50" in body


@patch("libracore.email_sender.smtplib.SMTP")
def test_custom_subject_and_body_are_respected(mock_smtp_cls, pdf_path):
    mock_server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_server

    enviar_comprobante(**_kwargs(pdf_path, asunto="Asunto custom", cuerpo="Cuerpo custom"))

    sent_msg = mock_server.send_message.call_args[0][0]
    assert sent_msg["Subject"] == "Asunto custom"
    body = sent_msg.get_body(preferencelist=("plain",)).get_content()
    assert body.strip() == "Cuerpo custom"


@patch("libracore.email_sender.smtplib.SMTP")
def test_pdf_is_attached_from_disk(mock_smtp_cls, pdf_path):
    mock_server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_server

    enviar_comprobante(**_kwargs(pdf_path))

    sent_msg = mock_server.send_message.call_args[0][0]
    attachments = list(sent_msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_filename() == "factura.pdf"
    assert attachments[0].get_content() == b"%PDF-1.4 fake pdf content"


@patch("libracore.email_sender.smtplib.SMTP")
def test_from_and_to_headers_without_display_name(mock_smtp_cls, pdf_path):
    mock_server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_server

    enviar_comprobante(**_kwargs(pdf_path, to_name="", from_name=""))

    sent_msg = mock_server.send_message.call_args[0][0]
    assert sent_msg["From"] == "facturacion@example.com"
    assert sent_msg["To"] == "cliente@example.com"


@patch("libracore.email_sender.smtplib.SMTP")
def test_enviar_documento_adjunta_un_pdf_que_esta_en_memoria(mock_smtp_cls):
    """`pdf_bytes` manda el PDF sin pasarlo por un archivo (la pre factura se genera al momento)."""
    mock_server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_server

    enviar_documento(
        to_email="cliente@example.com", to_name="Cliente Ejemplo", pdf_path="",
        asunto="Pre factura PF-0001", cuerpo="Adjunto.", smtp_host="smtp.example.com", smtp_port=587,
        smtp_user="user@example.com", smtp_password="secret", from_email="facturacion@example.com",
        from_name="Mi Empresa", filename="PF-0001.pdf", pdf_bytes=b"%PDF-1.4 en memoria")

    adjunto = next(mock_server.send_message.call_args[0][0].iter_attachments())
    assert adjunto.get_filename() == "PF-0001.pdf"
    assert adjunto.get_content() == b"%PDF-1.4 en memoria"


@patch("libracore.email_sender.smtplib.SMTP")
def test_enviar_documento_en_memoria_sin_nombre_usa_uno_por_defecto(mock_smtp_cls):
    mock_server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_server
    enviar_documento(
        to_email="cliente@example.com", to_name="", pdf_path="", asunto="x", cuerpo="y",
        smtp_host="smtp.example.com", smtp_port=587, smtp_user="u", smtp_password="p",
        from_email="f@example.com", from_name="", pdf_bytes=b"%PDF-1.4")
    adjunto = next(mock_server.send_message.call_args[0][0].iter_attachments())
    assert adjunto.get_filename() == "documento.pdf"


# ── Una demo no manda mails (ADR-090, addendum de v1.203.0) ───────────────────

_SMTP = dict(smtp_host="smtp.example.com", smtp_port=587, smtp_user="yo@example.com", smtp_password="clave-de-prueba",
             from_email="yo@example.com", from_name="Yo")


def test_en_una_demo_ninguna_funcion_de_envio_sale_a_la_red(monkeypatch, tmp_path, smtp_que_no_debe_salir):
    from libracore import email_sender

    monkeypatch.setenv("DEMO_MODE", "1")
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    llamadas = [
        lambda: email_sender.enviar_comprobante(to_email="a@b.example", to_name="A", pdf_path=str(pdf), empresa_nombre="E",
                                                factura_label="Factura C 1", total=1.0, **_SMTP),
        lambda: email_sender.enviar_documento(to_email="a@b.example", to_name="A", pdf_path=str(pdf), asunto="x", cuerpo="y", **_SMTP),
        lambda: email_sender.enviar_documento(to_email="a@b.example", to_name="A", pdf_path="", asunto="x", cuerpo="y",
                                              pdf_bytes=b"%PDF-1.4", **_SMTP),
        lambda: email_sender.enviar_texto(to_email="a@b.example", asunto="x", cuerpo="y", **_SMTP),
    ]
    for enviar in llamadas:
        with pytest.raises(email_sender.MailsDeshabilitadosEnDemo, match="En la demo los mails no se envían"):
            enviar()
    assert smtp_que_no_debe_salir == []


def test_el_error_de_demo_es_un_OSError_y_no_pisa_a_uno_de_verdad(monkeypatch):
    """Lo que hoy atrapa un fallo de red (`except OSError`) lo atrapa también; y fuera de una demo la guarda no existe."""
    from libracore import email_sender

    monkeypatch.setenv("DEMO_MODE", "1")
    assert issubclass(email_sender.MailsDeshabilitadosEnDemo, OSError)
    monkeypatch.setenv("DEMO_MODE", "0")
    email_sender.exigir_envio_real()  # no levanta
    monkeypatch.delenv("DEMO_MODE")
    email_sender.exigir_envio_real()


def test_fuera_de_una_demo_el_mismo_envio_si_abre_la_sesion(monkeypatch, tmp_path, smtp_que_no_debe_salir):
    """El control: el fixture detecta la llamada. Sin esto, «no se llamó» podría ser un fixture roto."""
    from libracore import email_sender

    monkeypatch.delenv("DEMO_MODE", raising=False)
    with pytest.raises(AssertionError, match="salió a la red"):
        email_sender.enviar_texto(to_email="a@b.example", asunto="x", cuerpo="y", **_SMTP)
    assert len(smtp_que_no_debe_salir) == 1
