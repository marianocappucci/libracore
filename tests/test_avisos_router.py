"""La configuración de avisos y su API (ADR-090).

Lo que se fija, y casi todo es seguridad:
- 🔴 la `apikey` de Evolution se guarda **sólo en el almacén cifrado**: no queda en `config.json`, no vuelve en `config_manager.load()`
  ni en ninguna respuesta (ni entera ni sus últimos caracteres), ni aparece en los logs;
- sin almacén no se guarda (la alternativa sería escribirla en claro);
- una `apikey` vacía no borra la que está; `quitar_apikey` sí;
- las cuatro rutas son de administrador, y el gate no tiene default;
- «probar» manda un texto fijo, traduce una falla de Evolution a 502 (nunca 401) y en un nodo contesta 409;
- una demo nunca manda;
- guardar la configuración no pisa el resto de `config.json`.

Nada real sale: Evolution va contra un `httpx.MockTransport` y el SMTP contra `smtplib.SMTP` parcheado.
"""
import importlib
import json
import logging
import smtplib
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore.avisos import (
    Mensaje,
    ProveedorApagado,
    ProveedorEvolution,
    ProveedorRegistro,
    despachar,
)
from libracore.db import core
from libracore.db.schema import init_core_schema

APIKEY = "apikey-ficticia-0123456789abcdef"
ADMIN = {"x-rol": "admin"}
OPERADOR = {"x-rol": "operador"}
URL = "https://evolution.example"


class AlmacenFalso:
    def __init__(self, rompe=False):
        self.datos, self.rompe = {}, rompe

    def get(self, clave):
        return self.datos.get(clave, "")

    def set(self, clave, valor):
        if self.rompe:
            raise RuntimeError(f"no hay con qué cifrar {APIKEY}")
        if valor:
            self.datos[clave] = valor
        else:
            self.datos.pop(clave, None)


@pytest.fixture
def cm(tmp_path, monkeypatch):
    """Un `config_manager` recién recargado apuntando a un DATA_DIR propio, sin almacén ni demo."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("LIBRAEDGE_NODE_ID", raising=False)
    import libracore.config_manager as modulo

    importlib.reload(modulo)
    yield modulo
    modulo.usar_almacen_de_secretos(None)


@pytest.fixture
def almacen(cm):
    a = AlmacenFalso()
    cm.usar_almacen_de_secretos(a)
    return a


@pytest.fixture
def conf(cm):
    from libracore.avisos import configuracion

    return configuracion


@pytest.fixture
def base(tmp_path):
    core.configure(db_path=str(tmp_path / "avisos.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.commit()
    yield
    core._db_path = None
    core._database_url = None


def _gate(x_rol: str = Header(default="")):
    if x_rol != "admin":
        raise HTTPException(403, "solo administradores")


def _app(evolution=None, *, smtp_config=None, nodo=None, enviar_texto=None):
    from libracore.avisos_router import build_avisos_router

    transporte = httpx.MockTransport(evolution) if evolution else None
    app = FastAPI()
    app.include_router(build_avisos_router(require_admin=_gate, smtp_config=smtp_config, transporte=transporte,
                                           enviar_texto=enviar_texto, nodo=nodo))
    return TestClient(app)


def _contesta(status, cuerpo=None):
    pedidos = []

    def handler(request):
        pedidos.append(request)
        return httpx.Response(status, json=cuerpo if cuerpo is not None else {"key": {"id": "BAE5"}})

    handler.pedidos = pedidos
    return handler


def _conectar(cliente, **extra):
    r = cliente.put("/api/avisos/config", headers=ADMIN,
                    json={"modo": "evolution", "url": URL, "instancia": "club-demo", "apikey": APIKEY, **extra})
    assert r.status_code == 200, r.text
    return r


# ── 🔴 La apikey no se filtra ─────────────────────────────────────────────────────────────────────────────


def test_la_apikey_va_solo_al_almacen_y_no_queda_en_el_json(cm, almacen, conf):
    conf.guardar(modo="evolution", url=URL, instancia="club-demo", apikey=APIKEY)

    assert almacen.datos == {"avisos_evolution_apikey": APIKEY}
    with open(cm.CONFIG_PATH, encoding="utf-8") as f:
        crudo = f.read()
    assert APIKEY not in crudo, "la apikey no puede quedar en config.json"
    assert json.loads(crudo)["avisos_evolution_url"] == URL


def test_load_no_devuelve_la_apikey(cm, almacen, conf):
    """`config_manager.load()` lo devuelven en claro algunos productos en su `GET /api/config`: la apikey no puede estar ahí."""
    conf.guardar(modo="evolution", url=URL, instancia="club-demo", apikey=APIKEY)
    assert APIKEY not in json.dumps(cm.load())
    assert APIKEY not in json.dumps(cm.load_sin_demo())


def test_sin_almacen_no_se_guarda_la_apikey_ni_en_claro(cm, conf):
    with pytest.raises(conf.ErrorDeConfiguracion, match="almacén de secretos"):
        conf.guardar(modo="registro", apikey=APIKEY)
    assert not cm.os.path.exists(cm.CONFIG_PATH) or APIKEY not in open(cm.CONFIG_PATH, encoding="utf-8").read()
    assert conf.leer().modo == "apagado", "no se guardó nada a medias"


def test_si_el_almacen_falla_el_mensaje_no_lleva_la_apikey(cm, conf):
    cm.usar_almacen_de_secretos(AlmacenFalso(rompe=True))
    with pytest.raises(conf.ErrorDeConfiguracion) as e:
        conf.guardar(apikey=APIKEY)
    assert APIKEY not in str(e.value) and "RuntimeError" in str(e.value)


def test_ninguna_respuesta_de_la_api_lleva_la_apikey(cm, almacen, base):
    cliente = _app(_contesta(201))
    textos = [_conectar(cliente).text, cliente.get("/api/avisos/config", headers=ADMIN).text]
    textos.append(cliente.post("/api/avisos/probar", headers=ADMIN, json={"destino": "11 5555-1234"}).text)
    textos.append(cliente.get("/api/avisos/envios", headers=ADMIN).text)
    # un 422 de validación del cuerpo tampoco repite lo que se mandó en la apikey
    malo = cliente.put("/api/avisos/config", headers=ADMIN, json={"modo": "nada", "apikey": APIKEY})
    assert malo.status_code == 422
    textos.append(malo.text)
    # ni siquiera los últimos caracteres
    for t in textos:
        assert APIKEY not in t and APIKEY[-8:] not in t and APIKEY[:8] not in t


def test_la_apikey_no_aparece_en_los_logs(cm, almacen, base, caplog):
    cliente = _app(_contesta(401, {"message": f"apikey {APIKEY} inválida"}))
    with caplog.at_level(logging.DEBUG):
        _conectar(cliente)
        cliente.post("/api/avisos/probar", headers=ADMIN, json={"destino": "11 5555-1234"})
        from libracore.avisos import configuracion

        despachar(configuracion.proveedor_whatsapp(transporte=httpx.MockTransport(_contesta(401))),
                  Mensaje("5491155551234", cuerpo="hola"), clave="reserva:1:confirmacion")
    assert APIKEY not in caplog.text


def test_una_apikey_vacia_no_borra_la_que_esta_y_quitar_apikey_si(cm, almacen, conf):
    conf.guardar(modo="registro", apikey=APIKEY)
    assert conf.guardar(url=URL, apikey="").apikey_cargada, "vacío = no la toqués"
    assert almacen.datos["avisos_evolution_apikey"] == APIKEY

    assert not conf.guardar(quitar_apikey=True).apikey_cargada
    assert almacen.datos == {}


def test_guardar_la_configuracion_no_pisa_el_resto_del_json(cm, almacen, conf):
    cm.save({**cm.load(), "empresa_nombre": "Club Ficticio SA", "mp_pos_id": "POS-9", "campo_del_producto": "x"})
    conf.guardar(modo="registro", url=URL, instancia="club-demo", apikey=APIKEY)
    cfg = cm.load()
    assert cfg["empresa_nombre"] == "Club Ficticio SA" and cfg["mp_pos_id"] == "POS-9" and cfg["campo_del_producto"] == "x"


# ── Validación ────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", ["ftp://evolution.example", "evolution.example", "https://usuario:clave@evolution.example",
                                 "https://evolution.example/x?apikey=1", "https://evolution.example/#x", "https://evo lution.example",
                                 "https://" + "a" * 300 + ".example"])
def test_la_url_tiene_que_ser_http_sin_credenciales(cm, almacen, conf, url):
    with pytest.raises(conf.ErrorDeConfiguracion):
        conf.guardar(url=url)


def test_la_url_se_guarda_sin_la_barra_final_y_se_puede_borrar(cm, almacen, conf):
    assert conf.guardar(url="http://evolution:8080/").url == "http://evolution:8080"
    assert conf.guardar(url="").url == ""


@pytest.mark.parametrize("instancia", ["con espacio", "a/b", "-empieza", "x" * 65, "ñandú"])
def test_el_nombre_de_la_instancia_es_acotado(cm, almacen, conf, instancia):
    with pytest.raises(conf.ErrorDeConfiguracion):
        conf.guardar(instancia=instancia)


def test_evolution_pide_tener_todo_cargado(cm, almacen, conf):
    with pytest.raises(conf.ErrorDeConfiguracion, match="la URL y el nombre de la instancia y la apikey"):
        conf.guardar(modo="evolution")
    conf.guardar(url=URL, instancia="club-demo", apikey=APIKEY)
    assert conf.guardar(modo="evolution").modo == "evolution"
    conf.guardar(modo="registro", quitar_apikey=True)
    with pytest.raises(conf.ErrorDeConfiguracion, match="la apikey"):
        conf.guardar(modo="evolution")


def test_un_modo_inventado_se_rechaza(cm, almacen, conf):
    with pytest.raises(conf.ErrorDeConfiguracion):
        conf.guardar(modo="meta")


# ── Qué proveedor sale de la configuración ────────────────────────────────────────────────────────────────


def test_el_proveedor_de_whatsapp_sigue_al_modo(cm, almacen, conf):
    assert isinstance(conf.proveedor_whatsapp(), ProveedorApagado) and not conf.proveedor_whatsapp().disponible()

    conf.guardar(modo="registro")
    p = conf.proveedor_whatsapp()
    assert isinstance(p, ProveedorRegistro) and p.simulado and p.disponible()

    conf.guardar(modo="evolution", url=URL, instancia="club-demo", apikey=APIKEY)
    p = conf.proveedor_whatsapp()
    assert isinstance(p, ProveedorEvolution) and p.disponible() and not p.simulado
    assert APIKEY not in repr(p)


def test_proveedores_arma_los_dos_canales(cm, almacen, conf):
    ps = conf.proveedores(lambda: MagicMock(configurado=False))
    assert set(ps) == {"email", "whatsapp"} and not ps["email"].disponible() and not ps["whatsapp"].disponible()


def test_una_demo_nunca_manda_por_whatsapp(cm, almacen, conf, monkeypatch):
    conf.guardar(modo="evolution", url=URL, instancia="club-demo", apikey=APIKEY)
    monkeypatch.setenv("DEMO_MODE", "1")

    assert isinstance(conf.proveedor_whatsapp(), ProveedorRegistro), "diga lo que diga la configuración"
    with pytest.raises(conf.ErrorDeConfiguracion, match="demo"):
        conf.guardar(modo="evolution")
    assert conf.motivo_de_whatsapp() == ""


def test_el_motivo_explica_por_que_no_esta_disponible(cm, conf):
    assert conf.motivo_de_whatsapp() == "WhatsApp está apagado."
    conf.guardar(modo="registro")
    # `evolution` sin almacén (un cron que no lo enchufó): el motivo lo dice
    cfg = cm.load_sin_demo()
    cfg["avisos_whatsapp_modo"] = "evolution"
    cm._escribir_atomico(cm.CONFIG_PATH, {**cfg, cm.MARCA_SECRETOS_EN_ALMACEN: True})
    assert "no abrió el almacén" in conf.motivo_de_whatsapp()
    cm._escribir_atomico(cm.CONFIG_PATH, cfg)
    assert "no tiene un almacén" in conf.motivo_de_whatsapp()
    cm.usar_almacen_de_secretos(AlmacenFalso())
    assert conf.motivo_de_whatsapp() == "Falta cargar la URL y el nombre de la instancia y la apikey."


# ── El gate ───────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("metodo, ruta, cuerpo", [
    ("get", "/api/avisos/config", None), ("put", "/api/avisos/config", {"modo": "registro"}),
    ("post", "/api/avisos/probar", {"destino": "11 5555-1234"}), ("get", "/api/avisos/envios", None),
])
def test_las_cuatro_rutas_son_de_administrador(cm, almacen, base, metodo, ruta, cuerpo):
    cliente = _app(_contesta(201))
    kw = {"json": cuerpo} if cuerpo is not None else {}
    assert getattr(cliente, metodo)(ruta, **kw).status_code == 403
    assert getattr(cliente, metodo)(ruta, headers=OPERADOR, **kw).status_code == 403
    assert getattr(cliente, metodo)(ruta, headers=ADMIN, **kw).status_code in (200, 409)


def test_el_gate_no_tiene_default(cm):
    from libracore.avisos_router import build_avisos_router

    with pytest.raises(TypeError):
        build_avisos_router()


def test_si_el_gate_es_una_dependencia_que_falla_no_se_ejecuta_ninguna_ruta(cm, almacen, base):
    """El router entero cuelga del gate (`dependencies=`): una ruta nueva no puede quedar abierta por olvido."""
    from libracore.avisos_router import build_avisos_router

    router = build_avisos_router(require_admin=_gate)
    assert router.dependencies and all(r.dependant.dependencies for r in router.routes)


# ── GET / PUT config ──────────────────────────────────────────────────────────────────────────────────────


def test_get_config_de_una_instancia_nueva(cm, almacen, base):
    r = _app().get("/api/avisos/config", headers=ADMIN).json()
    assert r["whatsapp"] == {"modo": "apagado", "modos": ["apagado", "registro", "evolution"], "url": "", "instancia": "",
                             "apikey_cargada": False, "disponible": False, "motivo": "WhatsApp está apagado."}
    assert r["email"] == {"disponible": False}


def test_put_config_guarda_y_responde_lo_mismo_que_el_get(cm, almacen, base):
    cliente = _app()
    r = _conectar(cliente).json()
    assert r["whatsapp"]["modo"] == "evolution" and r["whatsapp"]["apikey_cargada"] is True
    assert r["whatsapp"]["disponible"] is True and r["whatsapp"]["url"] == URL
    assert cliente.get("/api/avisos/config", headers=ADMIN).json() == r
    assert "apikey" not in r["whatsapp"]


def test_put_config_con_datos_malos_es_422_con_el_motivo(cm, almacen, base):
    cliente = _app()
    r = cliente.put("/api/avisos/config", headers=ADMIN, json={"modo": "evolution"})
    assert r.status_code == 422 and "falta cargar" in r.json()["detail"]
    r = cliente.put("/api/avisos/config", headers=ADMIN, json={"url": "ftp://x"})
    assert r.status_code == 422 and "http" in r.json()["detail"]


def test_el_email_esta_disponible_si_el_resolver_del_producto_lo_esta(cm, almacen, base):
    cfg = MagicMock(configurado=True, host="smtp.ejemplo.example", port=587, user="avisos@ejemplo.example", password="x",
                    from_email="avisos@ejemplo.example", from_name="")
    assert _app(smtp_config=lambda: cfg).get("/api/avisos/config", headers=ADMIN).json()["email"] == {"disponible": True}


# ── Probar ────────────────────────────────────────────────────────────────────────────────────────────────


def test_probar_manda_un_mensaje_fijo_por_evolution(cm, almacen, base):
    handler = _contesta(201)
    cliente = _app(handler)
    cm.save({**cm.load(), "empresa_nombre": "Club Ficticio"})
    _conectar(cliente)

    r = cliente.post("/api/avisos/probar", headers=ADMIN, json={"destino": "(011) 5555-1234"})

    assert r.status_code == 200 and r.json() == {"ok": True, "estado": "enviado", "simulado": False, "detalle": ""}
    (pedido,) = handler.pedidos
    cuerpo = json.loads(pedido.content)
    assert cuerpo["number"] == "5491155551234"
    assert cuerpo["text"] == "Mensaje de prueba de Club Ficticio. Si lo recibiste, los avisos están conectados."
    assert pedido.headers["apikey"] == APIKEY


def test_probar_no_deja_elegir_el_texto(cm, almacen, base):
    """Un endpoint que manda el texto que le pasan es un relé de mensajes."""
    handler = _contesta(201)
    cliente = _app(handler)
    _conectar(cliente)
    cliente.post("/api/avisos/probar", headers=ADMIN, json={"destino": "11 5555-1234", "texto": "compre ya", "cuerpo": "compre ya"})
    assert "compre ya" not in handler.pedidos[0].content.decode()


def test_probar_no_toca_el_registro_de_avisos(cm, almacen, base):
    cliente = _app(_contesta(201))
    _conectar(cliente)
    cliente.post("/api/avisos/probar", headers=ADMIN, json={"destino": "11 5555-1234"})
    assert cliente.get("/api/avisos/envios", headers=ADMIN).json() == []


def test_probar_en_modo_registro_no_sale_nada_y_lo_dice(cm, almacen, base):
    def no_deberia_llamarse(request):
        raise AssertionError("el modo registro no sale a la red")

    cliente = _app(no_deberia_llamarse)
    cliente.put("/api/avisos/config", headers=ADMIN, json={"modo": "registro"})
    r = cliente.post("/api/avisos/probar", headers=ADMIN, json={"destino": "11 5555-1234"})
    assert r.status_code == 200 and r.json()["simulado"] is True and r.json()["estado"] == "registrado"


def test_probar_con_whatsapp_apagado_es_409(cm, almacen, base):
    r = _app().post("/api/avisos/probar", headers=ADMIN, json={"destino": "11 5555-1234"})
    assert r.status_code == 409 and r.json()["detail"] == {"codigo": "no_disponible", "mensaje": "WhatsApp está apagado."}


@pytest.mark.parametrize("destino", ["", "abc", "5555-1234", "15 5555-1234"])
def test_probar_con_un_telefono_que_no_se_reconoce_es_422(cm, almacen, base, destino):
    handler = _contesta(201)
    cliente = _app(handler)
    _conectar(cliente)
    assert cliente.post("/api/avisos/probar", headers=ADMIN, json={"destino": destino}).status_code == 422
    assert handler.pedidos == []


def test_probar_con_la_apikey_mala_es_502_y_nunca_401(cm, almacen, base):
    """Un 401 hace que el kit mande al usuario al login: el que falló fue Evolution, no su sesión."""
    cliente = _app(_contesta(401, {"message": "Unauthorized"}))
    _conectar(cliente)
    r = cliente.post("/api/avisos/probar", headers=ADMIN, json={"destino": "11 5555-1234"})
    assert r.status_code == 502
    assert r.json()["detail"]["codigo"] == "fallo_del_proveedor" and "apikey" in r.json()["detail"]["mensaje"]


def test_probar_con_timeout_es_502(cm, almacen, base):
    def lento(request):
        raise httpx.ReadTimeout("lento", request=request)

    cliente = _app(lento)
    _conectar(cliente)
    r = cliente.post("/api/avisos/probar", headers=ADMIN, json={"destino": "11 5555-1234"})
    assert r.status_code == 502 and "no respondió" in r.json()["detail"]["mensaje"]


def test_probar_a_un_numero_sin_whatsapp_es_502_con_el_motivo(cm, almacen, base):
    cuerpo = {"response": {"message": [{"exists": False, "number": "5491155551234"}]}}
    cliente = _app(_contesta(400, cuerpo))
    _conectar(cliente)
    r = cliente.post("/api/avisos/probar", headers=ADMIN, json={"destino": "11 5555-1234"})
    assert r.status_code == 502 and "no tiene WhatsApp" in r.json()["detail"]["mensaje"]


def test_probar_el_mail_usa_el_smtp_de_la_instancia(cm, almacen, base):
    cfg = MagicMock(configurado=True, host="smtp.ejemplo.example", port=587, user="avisos@ejemplo.example", password="clave-ficticia",
                    from_email="avisos@ejemplo.example", from_name="Club")
    cliente = _app(smtp_config=lambda: cfg)
    with patch("libracore.email_sender.smtplib.SMTP") as smtp:
        servidor = smtp.return_value.__enter__.return_value = MagicMock()
        r = cliente.post("/api/avisos/probar", headers=ADMIN, json={"canal": "email", "destino": "dueno@ejemplo.example"})
    assert r.status_code == 200 and r.json()["estado"] == "enviado"
    servidor.login.assert_called_once_with("avisos@ejemplo.example", "clave-ficticia")
    (msg,), _ = servidor.send_message.call_args
    assert msg["To"] == "dueno@ejemplo.example" and "Mensaje de prueba" in msg["Subject"]


def test_probar_el_mail_sin_servidor_es_409_y_con_un_error_de_smtp_es_502(cm, almacen, base):
    sin = MagicMock(configurado=False)
    assert _app(smtp_config=lambda: sin).post("/api/avisos/probar", headers=ADMIN,
                                              json={"canal": "email", "destino": "a@b.example"}).status_code == 409
    cfg = MagicMock(configurado=True, host="h", port=587, user="u", password="p", from_email="u@b.example", from_name="")
    with patch("libracore.email_sender.smtplib.SMTP") as smtp:
        smtp.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"mala")
        r = _app(smtp_config=lambda: cfg).post("/api/avisos/probar", headers=ADMIN, json={"canal": "email", "destino": "a@b.example"})
    assert r.status_code == 502 and "contraseña" in r.json()["detail"]["mensaje"]


def test_probar_el_mail_se_prueba_sin_red_inyectando_enviar_texto(cm, almacen, base):
    """Lo que un producto necesita en sus tests: `transporte` para Evolution y `enviar_texto` para el mail, sin parchear `smtplib`."""
    cfg = MagicMock(configurado=True, host="smtp.ejemplo.example", port=587, user="avisos@ejemplo.example", password="clave-ficticia",
                    from_email="avisos@ejemplo.example", from_name="Club")
    llamadas = []
    cliente = _app(smtp_config=lambda: cfg, enviar_texto=lambda **kw: llamadas.append(kw))
    with patch("libracore.email_sender.smtplib.SMTP") as smtp:
        r = cliente.post("/api/avisos/probar", headers=ADMIN, json={"canal": "email", "destino": "dueno@ejemplo.example"})
    assert r.status_code == 200 and r.json()["estado"] == "enviado"
    smtp.assert_not_called()
    (kw,) = llamadas
    assert kw["to_email"] == "dueno@ejemplo.example" and "Mensaje de prueba" in kw["asunto"]


def test_proveedores_deja_inyectar_el_transporte_y_el_envio_de_mail(cm, almacen, conf):
    conf.guardar(modo="evolution", url=URL, instancia="club-demo", apikey=APIKEY)
    handler = _contesta(201)
    llamadas = []
    cfg = MagicMock(configurado=True, host="h", port=587, user="u", password="p", from_email="u@b.example", from_name="")
    ps = conf.proveedores(lambda: cfg, transporte=httpx.MockTransport(handler), enviar_texto=lambda **kw: llamadas.append(kw))
    assert ps["whatsapp"].enviar(Mensaje("5491155551234", cuerpo="hola")).estado == "enviado" and len(handler.pedidos) == 1
    assert ps["email"].enviar(Mensaje("a@b.example", "s", "c")).estado == "enviado" and len(llamadas) == 1


def test_probar_el_mail_en_una_demo_no_manda_nada(cm, almacen, base, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = MagicMock(configurado=True, host="smtp.ejemplo.example", port=587, user="avisos@ejemplo.example", password="clave-ficticia",
                    from_email="avisos@ejemplo.example", from_name="Club")
    llamadas = []
    cliente = _app(smtp_config=lambda: cfg, enviar_texto=lambda **kw: llamadas.append(kw))
    with patch("libracore.email_sender.smtplib.SMTP") as smtp:
        r = cliente.post("/api/avisos/probar", headers=ADMIN, json={"canal": "email", "destino": "dueno@ejemplo.example"})
    assert r.status_code == 200 and r.json()["estado"] == "registrado" and r.json()["simulado"] is True
    assert llamadas == []
    smtp.assert_not_called()


# ── Nodo ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_en_un_nodo_no_se_configura_ni_se_prueba_pero_se_lee(cm, almacen, base):
    cliente = _app(_contesta(201), nodo=lambda: "nodo-del-local")
    for r in (cliente.put("/api/avisos/config", headers=ADMIN, json={"modo": "registro"}),
              cliente.post("/api/avisos/probar", headers=ADMIN, json={"destino": "11 5555-1234"})):
        assert r.status_code == 409 and r.json()["detail"]["codigo"] == "solo_en_el_central"
    assert cliente.get("/api/avisos/config", headers=ADMIN).status_code == 200
    assert cliente.get("/api/avisos/envios", headers=ADMIN).status_code == 200
    assert almacen.datos == {}


# ── El registro ───────────────────────────────────────────────────────────────────────────────────────────


def test_envios_lista_filtra_y_valida(cm, almacen, base):
    from libracore.avisos import configuracion

    configuracion.guardar(modo="registro")
    registro = configuracion.proveedor_whatsapp()
    despachar(registro, Mensaje("5491155551234", cuerpo="uno"), clave="a", tipo="confirmacion")
    despachar(registro, Mensaje("5491155551235", cuerpo="dos"), clave="b", tipo="recordatorio")
    cliente = _app()

    todos = cliente.get("/api/avisos/envios", headers=ADMIN).json()
    assert [f["clave"] for f in todos] == ["b", "a"] and todos[0]["estado"] == "registrado" and todos[0]["variables"] == {}
    assert [f["clave"] for f in cliente.get("/api/avisos/envios?tipo=confirmacion", headers=ADMIN).json()] == ["a"]
    assert cliente.get("/api/avisos/envios?estado=enviado", headers=ADMIN).json() == []
    assert cliente.get("/api/avisos/envios?estado=inventado", headers=ADMIN).status_code == 422
    assert cliente.get("/api/avisos/envios?canal=sms", headers=ADMIN).status_code == 422
    assert cliente.get("/api/avisos/envios?limite=0", headers=ADMIN).status_code == 422
    assert len(cliente.get("/api/avisos/envios?limite=1", headers=ADMIN).json()) == 1
