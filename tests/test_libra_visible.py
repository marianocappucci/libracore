"""«Libra» a la vista (marca blanca, libracore ADR-092): lo que el motor produce para el cliente final del revendedor no nombra la familia.

Se generan un PDF, un ticket y los mails de comprobante y de texto con datos ficticios de una empresa que no es Libra, y se falla si en
el texto aparece `libra`. Es el hermano de `libra-ui/test/libra-visible.test.tsx` (lo que dibuja la pantalla): acá, lo que sale como
archivo o como mensaje.

Lo que NO mide, a propósito: el dominio del remitente que arma el aprovisionamiento (`provisioning/mail_cuentas.py`, `<cliente>@libradesk.com.ar`)
y el nombre de la app que ve el cliente al autorizar su nube; son del alta y del proveedor, no de lo que genera el motor (anotado en el
ADR-094).
"""
import io
import re
from unittest.mock import MagicMock, patch

import pytest
from pypdf import PdfReader

from libracore import config_manager, ticket_generator
from libracore.email_sender import enviar_comprobante, enviar_documento, enviar_texto
from libracore.pdf_generator import generate_pdf_pre_factura

LIBRA = re.compile(r"libra", re.IGNORECASE)
EMPRESA = {"nombre": "Clinica Modelo SRL", "logo_path": ""}


def _texto_pdf(pdf: bytes) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages)


@pytest.fixture(autouse=True)
def _config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(tmp_path / "config.json"))
    config_manager.save({
        "empresa_nombre": "Clinica Modelo SRL", "empresa_cuit": "30-11111111-8", "ticket_ancho_mm": "80", "ticket_pie": "Gracias por venir",
    })


def test_el_pdf_de_una_pre_factura_no_nombra_a_libra():
    pre = {
        "id": 1, "numero_interno": "PF-0001", "fecha_sugerida": "2026-10-06", "cliente_razon": "Acopio Sur SA",
        "cliente_cuit": "30-12345678-1", "tipo_comprobante": 1,
        "items": [{"description": "Flete", "qty": 1, "unit_price": 1000, "iva_rate": 0.21}], "total": 1210,
    }
    texto = _texto_pdf(generate_pdf_pre_factura(pre, empresa=EMPRESA))
    assert "Clinica Modelo SRL" in texto, "control positivo: si el texto no se extrajo, el test no midió nada"
    assert not LIBRA.search(texto), LIBRA.search(texto)


def test_el_ticket_de_venta_no_nombra_a_libra():
    venta = {
        "id": 42, "fecha": "2026-07-28 14:30", "cliente_nombre": "Consumidor final",
        "items": [{"nombre": "Consulta", "cantidad": 1, "precio_unitario": 3900.0}], "total": 3900.0,
        "pagos": [{"medio": "efectivo", "monto": 3900.0}],
    }
    texto = _texto_pdf(ticket_generator.generar_ticket_venta(venta))
    assert "Gracias por venir" in texto, "control positivo: si el texto no se extrajo, el test no midió nada"
    assert not LIBRA.search(texto), LIBRA.search(texto)


def _enviado(mock_smtp_cls) -> str:
    servidor = mock_smtp_cls.return_value.__enter__.return_value
    mensaje = servidor.send_message.call_args[0][0]
    cuerpo = "\n".join(
        parte.get_content() for parte in mensaje.walk() if parte.get_content_type() == "text/plain"
    )
    adjuntos = " ".join(p.get_filename() or "" for p in mensaje.walk())
    return "\n".join([mensaje["Subject"], mensaje["From"], mensaje["To"], cuerpo, adjuntos])


_SMTP = dict(smtp_host="smtp.example.com", smtp_port=587, smtp_user="u@example.com", smtp_password="x", from_email="facturacion@example.com")


@patch("libracore.email_sender.smtplib.SMTP")
def test_los_mails_de_comprobante_documento_y_texto_no_nombran_a_libra(mock_smtp_cls, tmp_path):
    pdf = tmp_path / "factura.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake pdf content")
    salidas = []

    enviar_comprobante(
        to_email="cliente@example.com", to_name="Cliente Ejemplo", pdf_path=str(pdf), empresa_nombre="Clinica Modelo SRL",
        factura_label="Factura A-0001", total=1234.5, from_name="Clinica Modelo SRL", **_SMTP,
    )
    salidas.append(_enviado(mock_smtp_cls))
    mock_smtp_cls.reset_mock()
    enviar_documento(
        to_email="cliente@example.com", to_name="Cliente Ejemplo", pdf_path=str(pdf), asunto="Resumen de cuenta",
        cuerpo="Adjuntamos el resumen.", from_name="Clinica Modelo SRL", **_SMTP,
    )
    salidas.append(_enviado(mock_smtp_cls))
    mock_smtp_cls.reset_mock()
    enviar_texto(
        to_email="cliente@example.com", asunto="Recordatorio de turno", cuerpo="Tiene un turno mañana.",
        from_name="Clinica Modelo SRL", **_SMTP,
    )
    salidas.append(_enviado(mock_smtp_cls))

    assert all("Clinica Modelo SRL" in s or "Recordatorio" in s for s in salidas), "control positivo"
    for s in salidas:
        assert not LIBRA.search(s), s
