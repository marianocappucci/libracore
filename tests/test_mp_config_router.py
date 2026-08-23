"""La pestaña de MercadoPago de Configuración.

Los tres tests que mandan son los que fijan defectos que ya pasaron: el token
que sale en claro por la API, el `save()` que mergea contra los DEFAULTS y borra
lo que no vino, y el campo vacío que borraría la credencial que la pantalla
muestra enmascarada.
"""

import importlib

import httpx
import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

ADMIN = {"x-rol": "admin"}
TOKEN = "APP_USR-1234567890abcdef"


@pytest.fixture
def cm(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as modulo
    importlib.reload(modulo)
    return modulo


@pytest.fixture
def cliente(cm):
    import libracore.mp_config_router as mcr
    importlib.reload(mcr)

    def gate(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    aplicacion = FastAPI()
    aplicacion.include_router(
        mcr.build_mp_config_router(), dependencies=[Depends(gate)]
    )
    c = TestClient(aplicacion)
    c.modulo = mcr
    return c


RUTA = "/api/config/mercadopago"


# ── El gate ──────────────────────────────────────────────────────────────────

def test_hasta_la_lectura_es_de_admin(cliente):
    """Aunque el token salga enmascarado, con qué cuenta cobra el negocio no es
    información de cualquier usuario logueado."""
    assert cliente.get(RUTA).status_code == 403
    assert cliente.get(RUTA, headers=ADMIN).status_code == 200


# ── El token no sale en claro ────────────────────────────────────────────────

def test_el_token_nunca_vuelve_entero(cliente, cm):
    """🔴 Hoy `GET /api/config` devuelve `config_manager.load()` **entero**: el
    access token y la contraseña de SMTP en el JSON de una pantalla."""
    cm.save({**cm.load(), "mp_access_token": TOKEN, "mp_webhook_secret": "secreto-largo"})
    datos = cliente.get(RUTA, headers=ADMIN).json()

    assert TOKEN not in str(datos), "el token entero no puede estar en ninguna parte"
    assert datos["mp_access_token"] == "APP_…cdef"
    assert datos["mp_access_token_cargado"] is True
    assert datos["mp_webhook_secret"] != "secreto-largo"
    assert datos["mp_webhook_secret_cargado"] is True


def test_sin_credenciales_lo_dice_y_no_inventa_mascara(cliente):
    datos = cliente.get(RUTA, headers=ADMIN).json()
    assert datos["mp_access_token"] == ""
    assert datos["mp_access_token_cargado"] is False


def test_un_secreto_corto_no_se_filtra_por_la_mascara(cliente, cm):
    """Con un valor corto la máscara no puede mostrar las puntas: mostrar
    `abc…abc` de un secreto de 7 caracteres lo entrega entero."""
    cm.save({**cm.load(), "mp_access_token": "corto12"})
    devuelto = cliente.get(RUTA, headers=ADMIN).json()["mp_access_token"]
    assert "corto" not in devuelto
    assert devuelto == "…" * 4


# ── El guardado ──────────────────────────────────────────────────────────────

def test_guardar_mercadopago_no_pisa_el_resto_de_la_config(cliente, cm):
    """🔴 `config_manager.save()` mergea contra los **DEFAULTS**, no contra el
    archivo: toda clave que no venga vuelve a su valor por defecto.

    Ese detalle ya reactivó un cliente suspendido y borró un token. Acá se
    verifica con los dos vecinos que más duelen: el estado del servicio y el
    SMTP.
    """
    cm.save({**cm.load(), "servicio_estado": "suspendido",
             "email_smtp_password": "la-de-smtp", "empresa_nombre": "Mi Empresa"})

    r = cliente.put(RUTA, headers=ADMIN, json={"mp_access_token": TOKEN})
    assert r.status_code == 200, r.text

    quedo = cm.load()
    assert quedo["servicio_estado"] == "suspendido", "no se puede despausar solo"
    assert quedo["email_smtp_password"] == "la-de-smtp"
    assert quedo["empresa_nombre"] == "Mi Empresa"
    assert quedo["mp_access_token"] == TOKEN


def test_un_campo_vacio_no_borra_el_secreto_que_estaba(cliente, cm):
    """🔑 La pantalla muestra el enmascarado. Si mandar el campo tal como se ve
    borrara la credencial, guardar cualquier otro campo desconectaría la cuenta.

    Las dos mitades: vacío no toca, y un valor nuevo sí reemplaza.
    """
    cm.save({**cm.load(), "mp_access_token": TOKEN})

    cliente.put(RUTA, headers=ADMIN, json={"mp_concepto_descripcion": "Abono"})
    assert cm.load()["mp_access_token"] == TOKEN, "vacío significa 'no lo toqués'"

    cliente.put(RUTA, headers=ADMIN, json={"mp_access_token": "APP_USR-otro-token-9999"})
    assert cm.load()["mp_access_token"] == "APP_USR-otro-token-9999"


def test_para_desconectar_la_cuenta_hay_una_puerta_propia(cliente, cm):
    """Con "vacío = no lo toqués" no habría otra forma de sacar el token."""
    cm.save({**cm.load(), "mp_access_token": TOKEN, "mp_webhook_secret": "s"})
    r = cliente.delete(f"{RUTA}/credenciales", headers=ADMIN)
    assert r.status_code == 200
    assert cm.load()["mp_access_token"] == ""
    assert cm.load()["mp_webhook_secret"] == ""
    assert r.json()["mp_access_token_cargado"] is False


def test_los_campos_que_no_son_secretos_se_guardan_como_vienen(cliente, cm):
    cliente.put(RUTA, headers=ADMIN, json={
        "mp_concepto_descripcion": "Cuota mensual", "mp_iva_rate": "0.21",
        "mp_user_id": "555", "mp_pos_id": "CAJA1",
        "mp_auto_facturar_ventas": True,
    })
    datos = cliente.get(RUTA, headers=ADMIN).json()
    assert datos["mp_concepto_descripcion"] == "Cuota mensual"
    assert datos["mp_iva_rate"] == "0.21"
    assert datos["mp_user_id"] == "555"
    assert datos["mp_pos_id"] == "CAJA1"
    assert datos["mp_auto_facturar_ventas"] is True


def test_una_clave_de_mas_no_entra_en_config_json(cliente, cm):
    """El payload es declarado: un `PUT` con una clave extra no puede escribir
    cualquier cosa en `config.json`, donde también viven los secretos."""
    cliente.put(RUTA, headers=ADMIN, json={
        "mp_user_id": "555", "email_smtp_password": "intento-de-escritura",
    })
    assert cm.load().get("email_smtp_password", "") == ""


# ── Probar el token ──────────────────────────────────────────────────────────

#: El `AsyncClient` de verdad, capturado UNA vez al importar el modulo de test.
#:
#: 🔴 Sin esto el arnes se rompe solo: `_mockear_mp` parchea el `httpx` global,
#: asi que el segundo test que lo llame tomaria como "original" el mock del
#: primero y devolveria SU respuesta. Pasó: el test del token rechazado daba 200
#: porque heredaba el 200 del test anterior. El fallo era del instrumento.
_ASYNC_CLIENT_REAL = httpx.AsyncClient


def _mockear_mp(cliente, monkeypatch, respuesta):
    class Transporte(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            cliente.pedido = request
            return respuesta

    def fabricar(*a, **kw):
        kw["transport"] = Transporte()
        return _ASYNC_CLIENT_REAL(*a, **kw)

    # `monkeypatch` y no una asignacion pelada: restaura al terminar el test.
    monkeypatch.setattr(cliente.modulo.httpx, "AsyncClient", fabricar)


def test_probar_sin_token_lo_dice_antes_de_salir_a_la_red(cliente):
    assert cliente.post(f"{RUTA}/probar", headers=ADMIN).status_code == 400


def test_probar_devuelve_el_user_id_que_hace_falta_para_el_qr(cliente, cm, monkeypatch):
    """El `user_id` es justo lo que hay que copiar en el campo de al lado para
    armar el QR de caja: devolverlo evita ir a buscarlo al panel de MP."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    _mockear_mp(cliente, monkeypatch, httpx.Response(200, json={
        "id": 555, "nickname": "MICOMERCIO", "email": "yo@test",
        "site_id": "MLA", "country_id": "AR",
    }))
    r = cliente.post(f"{RUTA}/probar", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == 555
    assert r.json()["pais"] == "AR"
    assert cliente.pedido.headers["authorization"] == f"Bearer {TOKEN}"


def test_un_token_que_mp_rechaza_devuelve_lo_que_dijo_mp(cliente, cm, monkeypatch):
    """El texto de MercadoPago distingue un token vencido de uno de otra
    aplicación. Reemplazarlo por "error de credenciales" pierde eso."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    _mockear_mp(cliente, monkeypatch, httpx.Response(401, text="invalid_token"))
    r = cliente.post(f"{RUTA}/probar", headers=ADMIN)
    assert r.status_code == 502
    assert "invalid_token" in r.json()["detail"]
