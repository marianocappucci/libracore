"""Los proveedores de avisos (ADR-090): Evolution, SMTP, Registro, y el teléfono.

Nada real sale: Evolution va contra un `httpx.MockTransport` y el SMTP contra un doble o contra `smtplib.SMTP` parcheado. Datos
ficticios (números `11 5555-xxxx`, dominios `.example`).

Lo que se fija:
- Evolution manda lo que manda n8n del HOMEI (`POST .../message/sendText/{instancia}`, header `apikey`, `{number, text}`) y traduce
  cada respuesta: 201 enviado, 400 con `exists:false` rechazado (no se reintenta), 401/404/5xx/timeout/sin conexión fallidos;
- 🔴 la `apikey` no aparece en el `repr`, ni en el `detalle`, ni en los logs, ni siquiera cuando el servidor devuelve un cuerpo que
  la repite;
- el teléfono: ante la duda, vacío; y el proveedor no manda a un número sin país;
- el SMTP reusa la configuración que ya tiene la instancia, con timeout, y una dirección con salto de línea no sale.
"""
import json
import logging
import smtplib
from unittest.mock import patch

import httpx
import pytest

from libracore.avisos import (
    Mensaje,
    ProveedorApagado,
    ProveedorEvolution,
    ProveedorRegistro,
    ProveedorSmtp,
    es_telefono_normalizado,
    telefono_para_whatsapp,
)
from libracore.avisos.mensaje import ENVIADO, FALLIDO, RECHAZADO, REGISTRADO

APIKEY = "apikey-ficticia-0123456789abcdef"
URL = "https://evolution.example"
NUMERO = "5491155551234"


def _evolution(handler, **kw):
    return ProveedorEvolution(URL, "club-demo", APIKEY, transporte=httpx.MockTransport(handler), **kw)


def _responde(status, cuerpo=None):
    pedidos = []

    def handler(request: httpx.Request):
        pedidos.append(request)
        return httpx.Response(status, json=cuerpo if cuerpo is not None else {})

    handler.pedidos = pedidos
    return handler


# ── Evolution: lo que manda ───────────────────────────────────────────────────────────────────────────────


def test_evolution_manda_lo_mismo_que_n8n_del_homei():
    handler = _responde(201, {"key": {"id": "BAE5F4A1"}, "status": "PENDING"})
    r = _evolution(handler).enviar(Mensaje(NUMERO, cuerpo="*Turno confirmado*\nMañana 19:00"))

    assert r.estado == ENVIADO and r.id_externo == "BAE5F4A1"
    (pedido,) = handler.pedidos
    assert pedido.method == "POST"
    assert str(pedido.url) == "https://evolution.example/message/sendText/club-demo"
    assert pedido.headers["apikey"] == APIKEY
    assert json.loads(pedido.content) == {"number": NUMERO, "text": "*Turno confirmado*\nMañana 19:00"}


def test_evolution_tolera_la_barra_final_y_escapa_el_nombre_de_la_instancia():
    handler = _responde(200, {})
    p = ProveedorEvolution(URL + "/", "a/../b", APIKEY, transporte=httpx.MockTransport(handler))
    p.enviar(Mensaje(NUMERO, cuerpo="hola"))
    # La instancia es un segmento de la ruta: no puede abrir otra.
    assert handler.pedidos[0].url.raw_path == b"/message/sendText/a%2F..%2Fb"


def test_evolution_sin_cuerpo_manda_el_asunto_y_sin_nada_no_manda():
    handler = _responde(201, {})
    assert _evolution(handler).enviar(Mensaje(NUMERO, asunto="Solo el asunto")).estado == ENVIADO
    assert json.loads(handler.pedidos[0].content)["text"] == "Solo el asunto"

    vacio = _responde(201, {})
    r = _evolution(vacio).enviar(Mensaje(NUMERO, cuerpo="   "))
    assert r.estado == RECHAZADO and vacio.pedidos == []


@pytest.mark.parametrize("numero", ["", "abc", "11 5555-1234", "1155551234", "011155551234", "+5491155551234", "123"])
def test_evolution_no_manda_a_un_numero_que_no_este_normalizado(numero):
    handler = _responde(201, {})
    r = _evolution(handler).enviar(Mensaje(numero, cuerpo="hola"))
    assert r.estado == RECHAZADO and "internacional" in r.detalle
    assert handler.pedidos == [], "ni una llamada a la red con un número dudoso"


# ── Evolution: lo que contesta ────────────────────────────────────────────────────────────────────────────


def test_evolution_401_es_fallido_y_dice_que_revise_la_apikey():
    r = _evolution(_responde(401, {"message": "Unauthorized"})).enviar(Mensaje(NUMERO, cuerpo="hola"))
    assert r.estado == FALLIDO and "apikey" in r.detalle and "401" in r.detalle


def test_evolution_numero_sin_whatsapp_es_rechazado_y_no_se_reintenta():
    cuerpo = {"status": 400, "error": "Bad Request",
              "response": {"message": [{"jid": f"{NUMERO}@s.whatsapp.net", "exists": False, "number": NUMERO}]}}
    r = _evolution(_responde(400, cuerpo)).enviar(Mensaje(NUMERO, cuerpo="hola"))
    assert r.estado == RECHAZADO and "no tiene WhatsApp" in r.detalle


def test_evolution_otro_400_no_dice_nada_del_numero_y_se_reintenta():
    """La instancia desconectada también es un 400: culpar al número lo dejaría sin aviso para siempre."""
    r = _evolution(_responde(400, {"response": {"message": ["Connection Closed"]}})).enviar(Mensaje(NUMERO, cuerpo="hola"))
    assert r.estado == FALLIDO


@pytest.mark.parametrize("status, parte", [(404, "instancia"), (429, "429"), (500, "500"), (503, "503"), (302, "302")])
def test_evolution_otros_estados_son_fallidos(status, parte):
    r = _evolution(_responde(status, {})).enviar(Mensaje(NUMERO, cuerpo="hola"))
    assert r.estado == FALLIDO and parte in r.detalle


def test_evolution_timeout_es_fallido():
    def handler(request):
        raise httpx.ReadTimeout("se colgó", request=request)

    r = _evolution(handler, timeout=7).enviar(Mensaje(NUMERO, cuerpo="hola"))
    assert r.estado == FALLIDO and "7 segundos" in r.detalle


def test_evolution_sin_conexion_es_fallido():
    def handler(request):
        raise httpx.ConnectError("no hay ruta a evolution.example", request=request)

    r = _evolution(handler).enviar(Mensaje(NUMERO, cuerpo="hola"))
    assert r.estado == FALLIDO and "ConnectError" in r.detalle
    assert "evolution.example" not in r.detalle, "el texto de la excepción de red puede traer la URL"


def test_evolution_tiene_timeout_de_verdad(monkeypatch):
    """Sin timeout un barrido se cuelga con un servidor caído: se mira el que le llega a `httpx`."""
    vistos = []
    cliente = httpx.Client

    def espia(*args, **kwargs):
        vistos.append(kwargs.get("timeout"))
        return cliente(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", espia)
    _evolution(_responde(201, {})).enviar(Mensaje(NUMERO, cuerpo="hola"))
    (timeout,) = vistos
    assert timeout.read == 10.0 and timeout.connect == 5.0


def test_evolution_cuerpo_que_no_es_json_no_rompe():
    def handler(request):
        return httpx.Response(201, text="<html>ok</html>")

    r = _evolution(handler).enviar(Mensaje(NUMERO, cuerpo="hola"))
    assert r.estado == ENVIADO and r.id_externo == ""


# ── 🔴 La apikey no se ve ─────────────────────────────────────────────────────────────────────────────────


def test_la_apikey_no_aparece_en_el_repr_ni_en_el_detalle_ni_en_los_logs(caplog):
    """Evolution devuelve un cuerpo que repite la apikey y el mensaje; ni una ni el otro pueden terminar en el detalle ni en un log."""
    cuerpo_malo = {"message": f"apikey {APIKEY} inválida", "text": "contenido-secreto-del-mensaje"}
    with caplog.at_level(logging.DEBUG):
        for status in (401, 500, 400, 201):
            r = _evolution(_responde(status, cuerpo_malo)).enviar(Mensaje(NUMERO, cuerpo="contenido-secreto-del-mensaje"))
            assert APIKEY not in r.detalle and "contenido-secreto" not in r.detalle
        proveedor = _evolution(_responde(401, cuerpo_malo))
        assert APIKEY not in repr(proveedor) and APIKEY not in str(vars(proveedor).get("_url", ""))
        assert "apikey=***" in repr(proveedor)
    assert APIKEY not in caplog.text


def test_evolution_disponible_pide_url_instancia_y_apikey():
    assert ProveedorEvolution(URL, "i", APIKEY).disponible()
    assert not ProveedorEvolution("", "i", APIKEY).disponible()
    assert not ProveedorEvolution(URL, "", APIKEY).disponible()
    assert not ProveedorEvolution(URL, "i", "").disponible()


def test_evolution_no_sigue_redirecciones_con_la_apikey():
    """Un 30x hacia otro host se llevaría el header `apikey`: la respuesta se toma como un fallo y no se sigue."""
    destinos = []

    def handler(request):
        destinos.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://otro-host.example/robo"})

    r = _evolution(handler).enviar(Mensaje(NUMERO, cuerpo="hola"))
    assert r.estado == FALLIDO and destinos == ["https://evolution.example/message/sendText/club-demo"]


# ── Registro y apagado ────────────────────────────────────────────────────────────────────────────────────


def test_registro_es_simulado_y_no_manda_nada():
    p = ProveedorRegistro()
    r = p.enviar(Mensaje(NUMERO, cuerpo="hola"))
    assert p.simulado and p.disponible() and p.canal == "whatsapp"
    assert r.estado == REGISTRADO and "no se mandó nada" in r.detalle


def test_apagado_no_esta_disponible():
    assert not ProveedorApagado().disponible()


# ── SMTP ──────────────────────────────────────────────────────────────────────────────────────────────────


class _Cfg:
    def __init__(self, configurado=True):
        self.configurado = configurado
        self.host, self.port, self.user, self.password = "smtp.ejemplo.example", 587, "avisos@ejemplo.example", "clave-ficticia"
        self.from_email, self.from_name = "avisos@ejemplo.example", "Club Ficticio"


def test_smtp_usa_la_configuracion_que_la_instancia_ya_tiene():
    llamadas = []
    p = ProveedorSmtp(lambda: _Cfg(), enviar_texto=lambda **kw: llamadas.append(kw))
    assert p.disponible()
    r = p.enviar(Mensaje("jugador@ejemplo.example", "Turno confirmado", "Mañana 19:00"))

    assert r.estado == ENVIADO
    (kw,) = llamadas
    assert kw["to_email"] == "jugador@ejemplo.example" and kw["asunto"] == "Turno confirmado"
    assert kw["smtp_host"] == "smtp.ejemplo.example" and kw["from_name"] == "Club Ficticio"
    assert kw["timeout"] == 30.0, "un servidor que no contesta no puede colgar el barrido"


def test_smtp_sin_configurar_no_esta_disponible():
    assert not ProveedorSmtp(lambda: _Cfg(configurado=False)).disponible()


@pytest.mark.parametrize("direccion", ["", "sin-arroba", "a@b", "a b@c.example", "a@c.example\nBcc: x@y.example", "a@c.example,b@c.example",
                                       "<a@c.example>", "a@.example"])
def test_smtp_no_manda_a_una_direccion_dudosa(direccion):
    llamadas = []
    p = ProveedorSmtp(lambda: _Cfg(), enviar_texto=lambda **kw: llamadas.append(kw))
    assert p.enviar(Mensaje(direccion, "x", "y")).estado == RECHAZADO
    assert llamadas == []


@pytest.mark.parametrize("error, estado", [
    (smtplib.SMTPRecipientsRefused({"a@b.example": (550, b"no")}), RECHAZADO),
    (smtplib.SMTPAuthenticationError(535, b"mala clave"), FALLIDO),
    (smtplib.SMTPServerDisconnected("cortó"), FALLIDO),
    (TimeoutError("lento"), FALLIDO),
    (ConnectionRefusedError("cerrado"), FALLIDO),
])
def test_smtp_traduce_los_errores(error, estado):
    def falla(**kw):
        raise error

    r = ProveedorSmtp(lambda: _Cfg(), enviar_texto=falla).enviar(Mensaje("jugador@ejemplo.example", "x", "y"))
    assert r.estado == estado
    assert "clave-ficticia" not in r.detalle


def test_enviar_texto_arma_el_mail_y_usa_timeout(monkeypatch):
    """El camino real de `email_sender.enviar_texto`, con `smtplib.SMTP` doblado: STARTTLS, login y el timeout."""
    from unittest.mock import MagicMock, patch

    from libracore import email_sender

    with patch("libracore.email_sender.smtplib.SMTP") as smtp:
        servidor = smtp.return_value.__enter__.return_value = MagicMock()
        email_sender.enviar_texto("jugador@ejemplo.example", "Turno confirmado", "Mañana 19:00", "smtp.ejemplo.example", 587,
                                  "avisos@ejemplo.example", "clave-ficticia", "avisos@ejemplo.example", "Club Ficticio", timeout=12)
    smtp.assert_called_once_with("smtp.ejemplo.example", 587, timeout=12)
    servidor.starttls.assert_called_once()
    servidor.login.assert_called_once_with("avisos@ejemplo.example", "clave-ficticia")
    (msg,), _ = servidor.send_message.call_args
    assert msg["To"] == "jugador@ejemplo.example" and msg["From"] == "Club Ficticio <avisos@ejemplo.example>"
    assert msg["Subject"] == "Turno confirmado" and "Mañana 19:00" in msg.get_content()
    assert not list(msg.iter_attachments())


def test_enviar_documento_sigue_llamando_a_smtp_sin_timeout():
    """El refactor para compartir la sesión no puede cambiar cómo sale el PDF: mismo `SMTP(host, puerto)`, sin opciones."""
    from unittest.mock import MagicMock, patch

    from libracore import email_sender

    with patch("libracore.email_sender.smtplib.SMTP") as smtp:
        smtp.return_value.__enter__.return_value = MagicMock()
        email_sender.enviar_documento("a@b.example", "", "", "x", "y", "h", 25, "u", "p", "f@b.example", "", pdf_bytes=b"%PDF-1.4")
    smtp.assert_called_once_with("h", 25)


# ── El teléfono ───────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("texto", "esperado"), [
    ("11 5555-1234", "5491155551234"),
    ("(011) 5555-1234", "5491155551234"),
    ("011 15 5555-1234", "5491155551234"),
    ("15 5555-1234 ", ""),  # sin característica no se sabe de dónde es
    ("+54 9 11 5555 1234", "5491155551234"),
    ("+54 11 5555 1234", "5491155551234"),
    ("0054 9 11 5555 1234", "5491155551234"),
    ("2255-123456", "5492255123456"),
    ("+598 99 123 456", "59899123456"),  # otro país: pasa tal cual
    ("5555-1234", ""),  # 8 dígitos
    ("12345", ""),
    ("", ""),
    (None, ""),
    # El 15 del interior no se puede ubicar solo: ante la duda, vacío y no un número inventado.
    ("2342 15 123456", ""),
    # Un número largo sin `+` podría ser un argentino con el 15 puesto: no se adivina.
    ("598 99 123 456", ""),
    ("hola", ""),
])
def test_telefono_para_whatsapp(texto, esperado):
    assert telefono_para_whatsapp(texto) == esperado


@pytest.mark.parametrize(("texto", "ok"), [
    ("5491155551234", True), ("59899123456", True), ("12025550123", True),
    ("1155551234", False),  # nacional sin país: se leería como un número de Estados Unidos
    ("2255123456", False), ("3515551234", False),
    ("0491155551234", False), ("+5491155551234", False), ("549 11", False), ("", False), ("1" * 16, False),
])
def test_es_telefono_normalizado(texto, ok):
    assert es_telefono_normalizado(texto) is ok


def test_lo_que_devuelve_telefono_para_whatsapp_siempre_pasa_el_proveedor():
    for crudo in ("11 5555-1234", "+54 11 5555 1234", "2255-123456", "+598 99 123 456"):
        assert es_telefono_normalizado(telefono_para_whatsapp(crudo)), crudo


# ── Una demo nunca manda mails ────────────────────────────────────────────────────────────────────────────


def test_una_demo_no_manda_mails_aunque_tenga_smtp(monkeypatch):
    """Una demo con SMTP cargado mandaría los recordatorios a los mails ficticios de la semilla."""
    monkeypatch.setenv("DEMO_MODE", "1")
    llamadas = []
    p = ProveedorSmtp(lambda: _Cfg(), enviar_texto=lambda **kw: llamadas.append(kw))
    assert p.disponible(), "sigue disponible si hay SMTP: lo que cambia es que no sale"
    assert p.simulado, "para que su `registrado` cierre el tema, como el de WhatsApp en una demo"

    r = p.enviar(Mensaje("jugador@ejemplo.example", "Turno confirmado", "Mañana 19:00"))

    assert r.estado == REGISTRADO and "demo" in r.detalle
    assert llamadas == []


def test_una_demo_no_toca_smtplib_con_el_camino_real(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    with patch("libracore.email_sender.smtplib.SMTP") as smtp:
        r = ProveedorSmtp(lambda: _Cfg()).enviar(Mensaje("jugador@ejemplo.example", "x", "y"))
    assert r.estado == REGISTRADO
    smtp.assert_not_called()


def test_fuera_de_una_demo_el_smtp_manda_y_no_es_simulado(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    llamadas = []
    p = ProveedorSmtp(lambda: _Cfg(), enviar_texto=lambda **kw: llamadas.append(kw))
    assert not p.simulado
    assert p.enviar(Mensaje("jugador@ejemplo.example", "x", "y")).estado == ENVIADO and len(llamadas) == 1
