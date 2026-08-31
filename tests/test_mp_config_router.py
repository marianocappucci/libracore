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


# ── La clave del interruptor de facturación automática ───────────────────────

@pytest.fixture
def cliente_con_clave_propia(cm):
    """Un producto que guarda el interruptor con OTRO nombre. El caso vivo es
    LibraClub: lo que cobra el QR de su mostrador es un turno de cancha, no una
    venta, y su `servicios/cobro_qr` lee `mp_auto_facturar_reservas`."""
    import libracore.mp_config_router as mcr
    importlib.reload(mcr)

    def gate(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    aplicacion = FastAPI()
    aplicacion.include_router(
        mcr.build_mp_config_router(campo_auto_facturar="mp_auto_facturar_reservas"),
        dependencies=[Depends(gate)],
    )
    return TestClient(aplicacion)


def test_el_interruptor_se_guarda_en_la_clave_que_pide_el_producto(
    cliente_con_clave_propia, cm,
):
    """🔴 Con la clave equivocada el interruptor escribe donde nadie lee: la
    pantalla diría que está prendido y no se emitiría ninguna factura, sin un
    solo error. Es la forma de fallar que este parámetro existe para impedir."""
    cliente_con_clave_propia.put(RUTA, headers=ADMIN, json={
        "mp_user_id": "555", "mp_auto_facturar_ventas": True,
    })
    guardado = cm.load()
    assert guardado["mp_auto_facturar_reservas"] is True
    # Y NO deja además la clave de ventas prendida: dos fuentes de verdad para
    # el mismo interruptor es peor que una equivocada.
    assert guardado.get("mp_auto_facturar_ventas") is not True


def test_el_nombre_en_la_API_no_cambia_aunque_cambie_el_de_la_base(
    cliente_con_clave_propia, cm,
):
    """La pantalla es la misma en los ocho productos: si el JSON cambiara de
    nombre según el producto, el interruptor quedaría muerto en uno de ellos."""
    cm.save({**cm.load(), "mp_auto_facturar_reservas": True})
    datos = cliente_con_clave_propia.get(RUTA, headers=ADMIN).json()
    assert datos["mp_auto_facturar_ventas"] is True
    assert "mp_auto_facturar_reservas" not in datos


def test_el_control_por_defecto_sigue_usando_la_clave_de_ventas(cliente, cm):
    """El control del caso de arriba. Sin esto, un `campo_auto_facturar` que se
    ignorara —o que se escribiera SIEMPRE en `..._reservas`— dejaría los dos
    tests anteriores en verde y rompería los siete productos que no pasan el
    parámetro."""
    cliente.put(RUTA, headers=ADMIN, json={
        "mp_user_id": "555", "mp_auto_facturar_ventas": True,
    })
    guardado = cm.load()
    assert guardado["mp_auto_facturar_ventas"] is True
    assert guardado.get("mp_auto_facturar_reservas") is not True


def test_apagar_el_interruptor_lo_apaga_de_verdad(cliente_con_clave_propia, cm):
    """Un `False` tiene que llegar a la base. Si el guardado sólo escribiera los
    valores "verdaderos" —que es lo que sale natural cuando se filtra por
    "vino en el payload"— el interruptor se podría prender y no apagar."""
    cm.save({**cm.load(), "mp_auto_facturar_reservas": True})
    cliente_con_clave_propia.put(RUTA, headers=ADMIN, json={
        "mp_user_id": "555", "mp_auto_facturar_ventas": False,
    })
    assert cm.load()["mp_auto_facturar_reservas"] is False


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


# ── De qué ambiente es la credencial ─────────────────────────────────────────
#
# 🔴 El defecto que estos tests fijan: hasta que esto existió, la pantalla
# mostraba igual un token de prueba y uno real, y las dos fallas eran mudas —
# producción en una `dev` cobra plata de verdad, prueba en la instancia de un
# cliente no cobra nada, y las dos "funcionan".

TOKEN_TEST = "TEST-1234567890abcdef"
#: El token de un **usuario de prueba**: empieza con `APP_USR-`, igual que uno
#: real. Es el caso que una implementación por prefijo clasifica al revés.
NICK_DE_PRUEBA = "TEST45I5GYIH"


def _mp_responde(cliente, monkeypatch, nickname):
    _mockear_mp(cliente, monkeypatch, httpx.Response(200, json={
        "id": 555, "nickname": nickname, "email": "yo@test",
        "site_id": "MLA", "country_id": "AR",
    }))


def test_sin_credencial_el_ambiente_no_es_ninguno(cliente):
    assert cliente.get(RUTA, headers=ADMIN).json()["mp_ambiente"] == ""


def test_el_prefijo_test_se_reconoce_sin_salir_a_la_red(cliente, cm):
    """Las credenciales de prueba de la aplicación se delatan solas. Este caso
    no necesita `probar`, ni caché que pueda quedar viejo."""
    cm.save({**cm.load(), "mp_access_token": TOKEN_TEST})
    assert cliente.get(RUTA, headers=ADMIN).json()["mp_ambiente"] == "prueba"


def test_un_app_usr_sin_verificar_no_se_da_por_produccion(cliente, cm):
    """🔑 Que no empiece con `TEST-` NO lo hace de producción: el token de un
    usuario de prueba también empieza con `APP_USR-`. Hasta preguntar, no se
    sabe, y decir "producción" acá sería el mismo cartel mentiroso al revés."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    assert cliente.get(RUTA, headers=ADMIN).json()["mp_ambiente"] == "indeterminado"


def test_probar_reconoce_al_usuario_de_prueba_por_el_nickname(cliente, cm, monkeypatch):
    """🔑 **El test que distingue esta implementación de la ingenua.** Token
    `APP_USR-`, cuenta de prueba: la única señal es el nickname."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    _mp_responde(cliente, monkeypatch, NICK_DE_PRUEBA)

    assert cliente.post(f"{RUTA}/probar", headers=ADMIN).json()["ambiente"] == "prueba"
    visible = cliente.get(RUTA, headers=ADMIN).json()
    assert visible["mp_ambiente"] == "prueba"
    assert visible["mp_ambiente_verificado"], "tiene que decir desde cuándo se sabe"


def test_probar_con_una_cuenta_real_dice_produccion(cliente, cm, monkeypatch):
    """El control del de arriba. Sin él, una clasificación que devolviera
    siempre "prueba" dejaría al anterior en verde."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    _mp_responde(cliente, monkeypatch, "MICOMERCIO")

    assert cliente.post(f"{RUTA}/probar", headers=ADMIN).json()["ambiente"] == "produccion"
    assert cliente.get(RUTA, headers=ADMIN).json()["mp_ambiente"] == "produccion"


def test_la_clasificacion_sobrevive_a_una_lectura_posterior(cliente, cm, monkeypatch):
    """El positivo que le da sentido a los dos tests de invalidación de abajo:
    sin esto, una implementación que devolviera SIEMPRE "indeterminado" los
    dejaría a los dos en verde."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    _mp_responde(cliente, monkeypatch, NICK_DE_PRUEBA)
    cliente.post(f"{RUTA}/probar", headers=ADMIN)

    assert cliente.get(RUTA, headers=ADMIN).json()["mp_ambiente"] == "prueba"
    assert cliente.get(RUTA, headers=ADMIN).json()["mp_ambiente"] == "prueba"


def test_la_huella_descarta_una_clasificacion_de_otro_token(cliente, cm):
    """🔴 **La defensa, medida sola.** El `config.json` se escribe a mano —como
    lo haría `panel_admin`, o restaurar un backup— con la clasificación de una
    credencial y el token de otra. Sin el cotejo de huella, la pantalla diría
    "prueba" sobre un token que nunca se verificó.

    Va sin pasar por el `PUT` a propósito: el `PUT` además limpia, así que un
    test que entrara por ahí pasaría igual con el cotejo roto.
    """
    cm.save({**cm.load(),
             "mp_access_token": TOKEN,
             "mp_ambiente": "prueba",
             "mp_ambiente_verificado": "2026-08-30 10:00:00",
             "mp_ambiente_huella": "huelladeotrotoken"})

    visible = cliente.get(RUTA, headers=ADMIN).json()
    assert visible["mp_ambiente"] == "indeterminado"
    assert visible["mp_ambiente_verificado"] == ""


def test_guardar_otro_token_deja_de_decir_prueba(cliente, cm, monkeypatch):
    """El camino que de verdad se recorre: la instancia se verificó contra una
    cuenta de prueba y después le cargan la credencial real del cliente."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    _mp_responde(cliente, monkeypatch, NICK_DE_PRUEBA)
    cliente.post(f"{RUTA}/probar", headers=ADMIN)

    r = cliente.put(RUTA, headers=ADMIN, json={"mp_access_token": "APP_USR-el-real-del-cliente"})

    assert r.json()["mp_ambiente"] != "prueba"
    assert cliente.get(RUTA, headers=ADMIN).json()["mp_ambiente"] == "indeterminado"
    assert cm.load()["mp_ambiente"] == "", "el archivo tampoco puede quedar diciéndolo"


def test_guardar_otra_cosa_no_pierde_la_clasificacion(cliente, cm, monkeypatch):
    """El control del de arriba: el `PUT` olvida cuando **el token** cambia, no
    en cada guardado. Si olvidara siempre, el cartel se apagaría al tocar
    cualquier campo de la pantalla y habría que re-verificar por nada."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    _mp_responde(cliente, monkeypatch, NICK_DE_PRUEBA)
    cliente.post(f"{RUTA}/probar", headers=ADMIN)

    # El token va vacío, que en esta pantalla significa "no lo toqués".
    r = cliente.put(RUTA, headers=ADMIN, json={"mp_user_id": "555"})
    assert r.json()["mp_ambiente"] == "prueba"


def test_borrar_las_credenciales_borra_la_clasificacion(cliente, cm, monkeypatch):
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    _mp_responde(cliente, monkeypatch, NICK_DE_PRUEBA)
    cliente.post(f"{RUTA}/probar", headers=ADMIN)

    assert cliente.delete(f"{RUTA}/credenciales", headers=ADMIN).json()["mp_ambiente"] == ""
    assert cm.load()["mp_ambiente_huella"] == ""


def test_el_ambiente_no_se_puede_poner_a_mano(cliente, cm):
    """Se deriva. Un `PUT` que lo aceptara convertiría el cartel en una
    declaración del usuario, que es exactamente lo que no sirve."""
    cliente.put(RUTA, headers=ADMIN, json={"mp_user_id": "555", "mp_ambiente": "produccion"})
    assert cm.load()["mp_ambiente"] == ""


def test_un_config_json_sin_las_claves_nuevas_se_lee_igual(cliente, cm, tmp_path):
    """Las instancias vivas tienen un `config.json` escrito antes de que estas
    claves existieran. Leerlo no puede romper la pantalla."""
    import json
    (tmp_path / "config.json").write_text(
        json.dumps({"mp_access_token": TOKEN, "empresa_nombre": "Sin las claves"}),
        encoding="utf-8",
    )
    r = cliente.get(RUTA, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["mp_ambiente"] == "indeterminado"


# ── `tags` manda sobre `nickname` ────────────────────────────────────────────
#
# 🔴 El defecto que estos fijan: clasificar por el NOMBRE de la cuenta es una
# heuristica sobre un string, y falla en las dos direcciones. MercadoPago
# declara el dato en `tags: ["test_user", "normal"]` — medido contra una cuenta
# real el 2026-08-30, con un token `APP_USR-` que era de prueba.

TAGS_DE_PRUEBA = ["test_user", "normal"]
TAGS_REALES = ["normal"]


def test_el_tag_reconoce_una_cuenta_de_prueba_con_nickname_cualquiera(cliente, cm, monkeypatch):
    """🔑 Lo que el nickname no puede: la cuenta es de prueba y no se llama
    TEST nada. Con la implementación anterior esto daba "produccion"."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    _mockear_mp(cliente, monkeypatch, httpx.Response(200, json={
        "id": 555, "nickname": "MICOMERCIO", "tags": TAGS_DE_PRUEBA,
    }))

    assert cliente.post(f"{RUTA}/probar", headers=ADMIN).json()["ambiente"] == "prueba"
    assert cliente.get(RUTA, headers=ADMIN).json()["mp_ambiente"] == "prueba"


def test_el_tag_le_gana_a_un_nickname_que_empieza_con_test(cliente, cm, monkeypatch):
    """🔑 La otra dirección, y la razón de que `tags` MANDE en vez de sumar: un
    comercio real que se llame `TESTORE` no es una cuenta de prueba. Si el
    nickname siguiera pudiendo decir "prueba" por su cuenta, la pantalla diría
    que no se cobra plata real en una instancia que sí la cobra."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    _mockear_mp(cliente, monkeypatch, httpx.Response(200, json={
        "id": 555, "nickname": "TESTORE", "tags": TAGS_REALES,
    }))

    assert cliente.post(f"{RUTA}/probar", headers=ADMIN).json()["ambiente"] == "produccion"


def test_sin_tags_el_nickname_sigue_siendo_el_respaldo(cliente, cm, monkeypatch):
    """`tags` puede no venir. Perder el criterio viejo al agregar el nuevo
    dejaría sin reconocer a los usuarios de prueba clásicos."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    _mockear_mp(cliente, monkeypatch, httpx.Response(200, json={
        "id": 555, "nickname": NICK_DE_PRUEBA,
    }))

    assert cliente.post(f"{RUTA}/probar", headers=ADMIN).json()["ambiente"] == "prueba"


def test_el_prefijo_test_no_necesita_ni_tags_ni_red(cliente, cm):
    """El control de que las tres señales conviven: la vieja de todas sigue
    resolviendo sola, sin preguntarle nada a MercadoPago."""
    cm.save({**cm.load(), "mp_access_token": TOKEN_TEST})
    assert cliente.get(RUTA, headers=ADMIN).json()["mp_ambiente"] == "prueba"


def test_una_lista_de_tags_vacia_no_es_una_cuenta_de_prueba(cliente, cm, monkeypatch):
    """Una lista vacía es una respuesta, no una ausencia: `[]` significa que
    MercadoPago contestó y no puso la marca. Tratarla como "no vino" mandaría
    al respaldo del nickname justo cuando hay un dato declarado que dice que
    no."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    _mockear_mp(cliente, monkeypatch, httpx.Response(200, json={
        "id": 555, "nickname": "TESTORE", "tags": [],
    }))

    assert cliente.post(f"{RUTA}/probar", headers=ADMIN).json()["ambiente"] == "produccion"


# -- El QR de la caja --------------------------------------------------------
#
# 🔴 El defecto que esto cierra no es que faltara un botón: es que conseguir el
# cartel del mostrador obligaba a entrar al panel de MercadoPago y encontrar la
# caja entre las de todos los productos. Con diez cajas que se llaman parecido
# —`CONTADEV`, `CONTADEMO`, `RESTODEV`…— bajar la del vecino **no da ningún
# error**: da un cartel que cobra en la caja equivocada.

#: La caja tal como la devuelve MercadoPago. Los tres archivos y el
#: `external_id` en MAYÚSCULAS son los de la respuesta real, medida contra la
#: cuenta el 2026-08-31.
CAJA = {
    "id": 137400058,
    "name": "Caja dev de contalibra",
    "external_id": "CONTADEV",
    "fixed_amount": True,
    "qr": {
        "image": "https://www.mercadopago.com/instore/merchant/qr/137400058/abc.png",
        "template_image": "https://www.mercadopago.com/instore/merchant/qr/137400058/t_abc.png",
        "template_document": "https://www.mercadopago.com/instore/merchant/qr/137400058/t_abc.pdf",
    },
}

PNG = b"\x89PNG\r\n\x1a\nlos-bytes-del-qr"
PDF = b"%PDF-1.4 el-cartel-para-imprimir"


def _mockear_por_url(cliente, monkeypatch, rutas, *, defecto=None):
    """Despacha **por URL**, y guarda todos los pedidos.

    🔑 El `_mockear_mp` de arriba devuelve la MISMA respuesta a cualquier
    llamada, y este flujo hace dos —buscar la caja y bajar el archivo—. Con una
    respuesta única el test del PDF pasaría recibiendo el JSON de la caja, que
    es justo lo que no se quiere afirmar.

    Y despachar por URL es además lo que deja **afirmar a qué URL se llamó**:
    una implementación que le pegue a otro endpoint de MercadoPago no matchea
    ninguna ruta y revienta, en vez de recibir la respuesta de otra cosa.
    """
    cliente.pedidos = []

    class Transporte(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            cliente.pedidos.append(request)
            for fragmento, respuesta in rutas.items():
                if fragmento in str(request.url):
                    return respuesta
            if defecto is not None:
                return defecto
            raise AssertionError("URL no esperada: %s" % request.url)

    def fabricar(*a, **kw):
        kw["transport"] = Transporte()
        return _ASYNC_CLIENT_REAL(*a, **kw)

    monkeypatch.setattr(cliente.modulo.httpx, "AsyncClient", fabricar)


def _con_caja(cm, pos_id="CONTADEV", token=TOKEN):
    cm.save({**cm.load(), "mp_access_token": token,
             "mp_user_id": "3392230021", "mp_pos_id": pos_id})


def _pos(resultados):
    return {"/pos": httpx.Response(200, json={"results": resultados})}


def test_sin_token_no_sale_a_buscar_ningun_qr(cliente, cm):
    cm.save({**cm.load(), "mp_pos_id": "CONTADEV"})
    r = cliente.get(RUTA + "/qr", headers=ADMIN)
    assert r.status_code == 400
    assert "Access Token" in r.json()["detail"]


def test_sin_pos_id_lo_dice_en_vez_de_pedir_la_lista_entera(cliente, cm):
    """🔑 `GET /pos` sin `external_id` devuelve **todas** las cajas de la
    cuenta. Salir a buscar con el campo vacío y quedarse con la primera es
    mostrar el QR de otro producto de la familia."""
    cm.save({**cm.load(), "mp_access_token": TOKEN})
    r = cliente.get(RUTA + "/qr", headers=ADMIN)
    assert r.status_code == 400
    assert "POS ID" in r.json()["detail"]


def test_la_caja_que_no_esta_en_esta_cuenta_nombra_a_la_otra_cuenta(cliente, cm, monkeypatch):
    """El caso vivo: un token de producción con el `pos_id` de la caja de
    prueba —o al revés—, que es lo que queda al migrar una instancia de dev a
    un cliente. Un "no encontrado" pelado manda a buscar el error donde no
    está."""
    _con_caja(cm)
    _mockear_por_url(cliente, monkeypatch, _pos([]))
    r = cliente.get(RUTA + "/qr", headers=ADMIN)
    assert r.status_code == 404
    detalle = r.json()["detail"]
    assert "CONTADEV" in detalle
    assert "prueba" in detalle and "real" in detalle


def test_el_qr_dice_de_que_caja_es_y_de_que_ambiente(cliente, cm, monkeypatch):
    """El ambiente va acá y no sólo en la tarjeta: un QR de una cuenta de
    prueba se ve **idéntico** a uno real y no cobra nada. Sin decirlo, el error
    se descubre con el cartel ya pegado en el mostrador."""
    _con_caja(cm, token=TOKEN_TEST)
    _mockear_por_url(cliente, monkeypatch, _pos([CAJA]))
    r = cliente.get(RUTA + "/qr", headers=ADMIN)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["pos_id"] == "CONTADEV"
    assert d["pos_nombre"] == "Caja dev de contalibra"
    assert d["pos_numero"] == 137400058
    assert d["ambiente"] == "prueba"
    assert sorted(d["formatos"]) == ["cartel", "pdf", "qr"]


def test_el_external_id_que_se_muestra_es_el_de_mercadopago(cliente, cm, monkeypatch):
    """🔑 El filtro de MercadoPago **no distingue mayúsculas** —medido:
    `?external_id=contadev` devuelve `CONTADEV`—. Se acepta la caja, pero lo
    que se muestra es el nombre canónico: devolver el texto tipeado haría que
    una configuración mal escrita se viera idéntica a una bien escrita."""
    _con_caja(cm, pos_id="contadev")
    _mockear_por_url(cliente, monkeypatch, _pos([CAJA]))
    assert cliente.get(RUTA + "/qr", headers=ADMIN).json()["pos_id"] == "CONTADEV"


def test_no_se_queda_con_la_primera_de_la_lista(cliente, cm, monkeypatch):
    """🔑 El riesgo real de esta cuenta: **las diez cajas de la familia viven
    juntas**. Si el filtro de MercadoPago no acota —porque cambió, porque el
    parámetro llegó vacío— la respuesta es la lista entera, y quedarse con la
    primera devuelve el QR de otro producto sin ningún error.

    Por eso la caja se elige **por su `external_id`** y no por su posición. La
    lista de acá abajo arranca con `CONTADEMO` a propósito: una implementación
    que tome `results[0]` pasa el resto de los tests y falla éste.
    """
    demo = {**CAJA, "id": 137400060, "external_id": "CONTADEMO",
            "name": "Caja demo de contalibra"}
    resto = {**CAJA, "id": 137400064, "external_id": "RESTODEV",
             "name": "Caja dev de restolibra"}
    _con_caja(cm)
    _mockear_por_url(cliente, monkeypatch, _pos([demo, resto, CAJA]))
    d = cliente.get(RUTA + "/qr", headers=ADMIN).json()
    assert d["pos_id"] == "CONTADEV"
    assert d["pos_numero"] == 137400058


def test_dos_cajas_con_el_mismo_nombre_no_eligen_una(cliente, cm, monkeypatch):
    """El caso que el filtro **no** puede desempatar. Como MercadoPago no
    distingue mayúsculas, `CONTADEV` y `contadev` son la misma búsqueda y las
    dos matchean: no hay forma de saber cuál quiso el operador, y son cajas
    distintas —cobran en lugares distintos—. Mejor decirlo que elegir al azar.
    """
    gemela = {**CAJA, "id": 999, "external_id": "contadev"}
    _con_caja(cm)
    _mockear_por_url(cliente, monkeypatch, _pos([CAJA, gemela]))
    assert cliente.get(RUTA + "/qr", headers=ADMIN).status_code == 404


def test_solo_se_ofrecen_los_formatos_que_la_caja_tiene(cliente, cm, monkeypatch):
    """Ofrecer un formato que no está publicado es un botón que falla al
    hacerle clic."""
    sin_pdf = {**CAJA, "qr": {k: v for k, v in CAJA["qr"].items()
                              if k != "template_document"}}
    _con_caja(cm)
    _mockear_por_url(cliente, monkeypatch, _pos([sin_pdf]))
    formatos = cliente.get(RUTA + "/qr", headers=ADMIN).json()["formatos"]
    assert sorted(formatos) == ["cartel", "qr"]


def test_un_formato_inventado_no_sale_a_la_red(cliente, cm, monkeypatch):
    _con_caja(cm)
    _mockear_por_url(cliente, monkeypatch, _pos([CAJA]))
    assert cliente.get(RUTA + "/qr/gif", headers=ADMIN).status_code == 404
    assert cliente.pedidos == []


def test_el_qr_baja_como_png_con_un_nombre_que_se_entiende(cliente, cm, monkeypatch):
    """El nombre importa: MercadoPago lo publica como un hash de 64 caracteres,
    y un cartel impreso que hay que volver a encontrar entre diez descargas
    iguales no sirve."""
    _con_caja(cm)
    _mockear_por_url(cliente, monkeypatch,
                     {**_pos([CAJA]), "/137400058/abc.png": httpx.Response(200, content=PNG)})
    r = cliente.get(RUTA + "/qr/qr", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert 'filename="qr-CONTADEV-qr.png"' in r.headers["content-disposition"]


def test_el_pdf_del_cartel_baja_como_pdf(cliente, cm, monkeypatch):
    """El control positivo del de arriba: si los dos formatos devolvieran lo
    mismo, el test del PNG pasaría igual con el mapa de formatos roto."""
    _con_caja(cm)
    _mockear_por_url(cliente, monkeypatch,
                     {**_pos([CAJA]), "t_abc.pdf": httpx.Response(200, content=PDF)})
    r = cliente.get(RUTA + "/qr/pdf", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.content == PDF
    assert r.headers["content-type"] == "application/pdf"
    assert 'filename="qr-CONTADEV-pdf.pdf"' in r.headers["content-disposition"]


def test_la_busqueda_va_firmada_y_la_descarga_no(cliente, cm, monkeypatch):
    """Los dos lados de la misma decisión. La búsqueda de la caja necesita el
    token; el archivo es **público** —medido contra la cuenta real— y mandarle
    el token a un CDN es filtrar la credencial a un servidor que no la pidió."""
    _con_caja(cm)
    _mockear_por_url(cliente, monkeypatch,
                     {**_pos([CAJA]), "abc.png": httpx.Response(200, content=PNG)})
    cliente.get(RUTA + "/qr/qr", headers=ADMIN)

    busqueda, descarga = cliente.pedidos
    assert "/pos" in str(busqueda.url)
    assert busqueda.headers["authorization"] == "Bearer " + TOKEN
    assert "authorization" not in descarga.headers


def test_la_url_de_mercadopago_no_llega_al_navegador(cliente, cm, monkeypatch):
    """🔴 La URL **es** el cartel: se sirve sin autenticación, así que quien la
    tenga puede imprimir el QR que cobra en esa cuenta. Por eso el motor trae
    los bytes en vez de devolver el link."""
    _con_caja(cm)
    _mockear_por_url(cliente, monkeypatch, _pos([CAJA]))
    cuerpo = cliente.get(RUTA + "/qr", headers=ADMIN).text
    assert "mercadopago.com" not in cuerpo
    assert "abc.png" not in cuerpo


def test_el_qr_esta_detras_del_gate_de_admin(cliente, cm):
    """Todo el router va detrás del gate; los endpoints nuevos no son la
    excepción."""
    _con_caja(cm)
    assert cliente.get(RUTA + "/qr").status_code == 403
    assert cliente.get(RUTA + "/qr/qr").status_code == 403
