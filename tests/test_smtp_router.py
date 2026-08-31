"""*Probar conexión* del correo saliente.

El test que manda es el que fija por qué este router vive en el motor y no en
cada producto: **prueba lo mismo que después manda**. Contalibra ya tuvo la
falla contraria —el endpoint leía `config.json` mientras la pantalla escribía en
la base de libraauth, así que decía *Conectado* contra un servidor y los mails
salían por otro— y acá se cubre cargando **los dos stores con datos distintos**.
"""

import importlib
import smtplib

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

ADMIN = {"x-rol": "admin"}


class _SmtpFalso:
    """Reemplaza a `smtplib.SMTP`. Anota lo que le pidieron y falla si se le
    indica: no sale a la red en ningún caso."""

    def __init__(self, registro, falla=None):
        self.registro = registro
        self.falla = falla

    def __call__(self, host, port, timeout=None):
        self.registro.append({"host": host, "port": port, "timeout": timeout})
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def ehlo(self):
        self.registro.append("ehlo")

    def starttls(self):
        self.registro.append("starttls")

    def login(self, usuario, clave):
        self.registro.append({"login": usuario, "clave": clave})
        if self.falla is not None:
            raise self.falla


class _Cfg:
    """Lo que devuelve el resolver del producto, con la forma de `SmtpConfig`."""

    def __init__(self, **campos):
        self.configurado = True
        self.host = campos.get("host", "smtp.dominio-propio.com.ar")
        self.port = campos.get("port", 587)
        self.user = campos.get("user", "de-la-base@dominio-propio.com.ar")
        self.password = campos.get("password", "clave-de-la-base")
        self.from_email = campos.get("from_email", "")
        self.from_name = campos.get("from_name", "")


class _SinConfigurar:
    configurado = False


@pytest.fixture
def cm(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as modulo
    importlib.reload(modulo)
    return modulo


@pytest.fixture
def armar(cm, monkeypatch):
    """Devuelve `(cliente, registro, configurar, llamadas_al_resolver)`."""
    import libracore.facturas_router as fr
    importlib.reload(fr)
    import libracore.smtp_router as sr
    importlib.reload(sr)

    registro = []
    llamadas = []
    estado = {"resolver": None}

    monkeypatch.setattr(sr.smtplib, "SMTP", _SmtpFalso(registro))

    def gate(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    def resolver():
        llamadas.append(1)
        if estado["resolver"] is None:
            raise AssertionError("el test no configuró resolver")
        return estado["resolver"]()

    app = FastAPI()
    app.include_router(
        sr.build_smtp_probe_router(resolver), dependencies=[Depends(gate)])

    def configurar(*, devuelve, falla=None):
        estado["resolver"] = devuelve
        monkeypatch.setattr(sr.smtplib, "SMTP", _SmtpFalso(registro, falla))

    return TestClient(app), registro, configurar, llamadas


def test_prueba_el_servidor_que_resuelve_el_producto(armar):
    cliente, registro, configurar, _ = armar
    configurar(devuelve=lambda: _Cfg())

    r = cliente.post("/admin/smtp/probar", headers=ADMIN)

    assert r.status_code == 200, r.text
    assert r.json() == {
        "ok": True, "host": "smtp.dominio-propio.com.ar", "port": 587,
        "user": "de-la-base@dominio-propio.com.ar",
    }
    # Y de verdad abrió la conexión, negoció TLS y se autenticó.
    assert registro[0] == {"host": "smtp.dominio-propio.com.ar", "port": 587, "timeout": 10}
    assert "ehlo" in registro and "starttls" in registro
    assert {"login": "de-la-base@dominio-propio.com.ar",
            "clave": "clave-de-la-base"} in registro


def test_prueba_el_mismo_smtp_que_manda_los_comprobantes(armar, cm):
    """🔑 El test que justifica que esto viva en el motor.

    Los dos stores cargados **con datos distintos**: si el router resolviera por
    su cuenta —leyendo `config.json`, que es lo que hacía el endpoint viejo de
    Contalibra— probaría el servidor equivocado y diría *Conectado* igual.
    """
    cm.save({
        **cm.load(),
        "email_smtp_host": "smtp.el-store-viejo.com",
        "email_smtp_user": "viejo@el-store-viejo.com",
        "email_smtp_password": "clave-vieja",
    })
    cliente, registro, configurar, _ = armar
    configurar(devuelve=lambda: _Cfg())

    r = cliente.post("/admin/smtp/probar", headers=ADMIN)

    assert r.status_code == 200, r.text
    assert r.json()["host"] == "smtp.dominio-propio.com.ar"
    assert not any("el-store-viejo" in str(paso) for paso in registro), registro


def test_si_el_producto_no_tiene_nada_guardado_cae_al_config_json(armar, cm):
    """La red de seguridad de `smtp_efectivo`, que este router hereda entera.

    Es el **control positivo** del test de arriba: sin él, "no probó el store
    viejo" se cumpliría igual con un router que no probara nada.
    """
    cm.save({
        **cm.load(),
        "email_smtp_host": "smtp.el-store-viejo.com",
        "email_smtp_user": "viejo@el-store-viejo.com",
        "email_smtp_password": "clave-vieja",
    })
    cliente, _, configurar, _ = armar
    configurar(devuelve=lambda: _SinConfigurar())

    r = cliente.post("/admin/smtp/probar", headers=ADMIN)

    assert r.status_code == 200, r.text
    assert r.json()["host"] == "smtp.el-store-viejo.com"


def test_sin_credenciales_lo_dice_y_no_sale_a_la_red(armar):
    cliente, registro, configurar, _ = armar
    configurar(devuelve=lambda: _Cfg(host="", user="", password=""))

    r = cliente.post("/admin/smtp/probar", headers=ADMIN)

    assert r.status_code == 400
    assert "Completá" in r.json()["detail"]
    # 🔑 Con el status solo no alcanza: un 400 que igual hubiera abierto la
    # conexión pasaría el test.
    assert registro == []


def test_sin_la_contrasena_tampoco_sale_a_la_red(armar):
    """El estado real entre cargar el servidor y cargar la clave."""
    cliente, registro, configurar, _ = armar
    configurar(devuelve=lambda: _Cfg(password=""))

    r = cliente.post("/admin/smtp/probar", headers=ADMIN)

    assert r.status_code == 400
    assert registro == []


def test_la_autenticacion_fallida_nombra_la_contrasena_de_aplicacion(armar):
    """Es el error que se ve siempre, y el único que el cliente arregla solo."""
    cliente, _, configurar, _ = armar
    configurar(
        devuelve=lambda: _Cfg(),
        falla=smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted"),
    )

    r = cliente.post("/admin/smtp/probar", headers=ADMIN)

    assert r.status_code == 401
    assert "contraseña de aplicación" in r.json()["detail"]


def test_el_error_del_servidor_llega_a_la_pantalla(armar):
    cliente, _, configurar, _ = armar
    configurar(
        devuelve=lambda: _Cfg(),
        falla=smtplib.SMTPException("STARTTLS extension not supported by server"),
    )

    r = cliente.post("/admin/smtp/probar", headers=ADMIN)

    assert r.status_code == 502
    # El texto tal cual: distingue "no soporta STARTTLS" de "el host no existe".
    assert "STARTTLS extension not supported" in r.json()["detail"]


def test_un_host_que_no_resuelve_no_revienta_la_pantalla(armar):
    cliente, _, configurar, _ = armar
    configurar(devuelve=lambda: _Cfg(), falla=OSError("[Errno -2] Name or service not known"))

    r = cliente.post("/admin/smtp/probar", headers=ADMIN)

    assert r.status_code == 502
    assert "Name or service not known" in r.json()["detail"]


def test_la_contrasena_no_sale_por_la_api(armar):
    cliente, _, configurar, _ = armar
    configurar(devuelve=lambda: _Cfg(password="clave-secretisima"))

    r = cliente.post("/admin/smtp/probar", headers=ADMIN)

    assert r.status_code == 200
    assert "clave-secretisima" not in r.text


def test_el_resolver_se_llama_en_cada_request(armar):
    """Si se resolviera al construir el router, guardar el SMTP por pantalla no
    tendría efecto hasta recrear el contenedor."""
    cliente, _, configurar, llamadas = armar
    configurar(devuelve=lambda: _Cfg())

    assert llamadas == []
    cliente.post("/admin/smtp/probar", headers=ADMIN)
    cliente.post("/admin/smtp/probar", headers=ADMIN)

    assert len(llamadas) == 2


def test_el_gate_del_producto_manda(armar):
    """El router no trae gate propio: lo pone el producto, como el resto."""
    cliente, registro, configurar, _ = armar
    configurar(devuelve=lambda: _Cfg())

    r = cliente.post("/admin/smtp/probar")

    assert r.status_code == 403
    assert registro == []


def test_el_prefijo_lo_puede_cambiar_el_producto(cm, monkeypatch):
    """El default es el de la pantalla compartida, pero no está clavado."""
    import libracore.smtp_router as sr
    importlib.reload(sr)
    registro = []
    monkeypatch.setattr(sr.smtplib, "SMTP", _SmtpFalso(registro))
    app = FastAPI()
    app.include_router(sr.build_smtp_probe_router(lambda: _Cfg(), prefix="/api/correo"))
    cliente = TestClient(app)

    assert cliente.post("/api/correo/probar").status_code == 200
    # Control: en el prefijo de siempre ya no está.
    assert cliente.post("/admin/smtp/probar").status_code == 404
