"""Tests para libracore.email_sender — extraído 2026-07-26 de
Contalibra/Restolibra (activo, sin test propio en ninguno de los dos
repos, que no tienen suite propia). Ver
wiki/analyses/auditoria-duplicacion-familia-libra.md."""
from unittest.mock import MagicMock, patch

import pytest

from libracore.email_sender import enviar_comprobante


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
