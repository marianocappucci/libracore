"""La pestaña de MercadoPago de la pantalla de Configuración.

Es la última pieza que faltaba para que los productos que cobran por
MercadoPago tengan la misma pantalla: las credenciales, el QR de caja, y el
botón que dice si el token sirve.

## Dos cosas que este router hace distinto de lo que había

1. 🔴 **El token no vuelve en claro.** Hoy `GET /api/config` de Contalibra
   devuelve `config_manager.load()` **entero**, o sea el `mp_access_token` y la
   contraseña de SMTP en el JSON de una pantalla. Acá el token sale enmascarado
   (`APP_USR-…3f2a`) y sólo se manda entero hacia adentro.

2. 🔴 **El `PUT` guarda SOBRE la config existente, nunca un dict armado de
   cero.** `config_manager.save()` mergea contra los **DEFAULTS**, no contra el
   archivo: toda clave que no venga en `data` vuelve a su valor por defecto. Ese
   detalle ya borró el token de MercadoPago una vez —guardar la razón social
   reseteaba `servicio_estado` y vaciaba el token—, así que acá se carga, se
   actualiza lo que vino, y se guarda.

Y una del mismo tipo: **un token vacío no borra el que estaba.** La pantalla
muestra el valor enmascarado, así que mandar el campo tal como se ve borraría la
credencial. Vacío significa "no lo toqués", igual que la contraseña de SMTP.

## De qué ambiente es la credencial

MercadoPago **no tiene un ambiente de homologación** como ARCA: no hay host de
sandbox, es el mismo `api.mercadopago.com` para los dos y **lo que define el
ambiente es el token**. Hasta que esto existió, la pantalla no lo decía en
ningún lado, y las dos fallas eran mudas: un token de producción en una `dev`
cobra plata de verdad, y uno de prueba en la instancia de un cliente no cobra
nada. Las dos "funcionan": el QR se genera y la orden se crea igual.

Ver `clasificar_ambiente` para por qué mirar el prefijo del token no alcanza.
"""

import hashlib

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from libracore import config_manager
from libracore.db.core import _ar_now

#: Los campos de MercadoPago que la pantalla edita. El resto de `config.json`
#: no se toca desde acá.
#:
#: `mp_auto_facturar_ventas` NO está en esta tupla: es el único cuyo nombre en
#: `config.json` cambia de producto en producto. Ver `CAMPO_AUTO_FACTURAR`.
CAMPOS = (
    "mp_access_token",
    "mp_webhook_secret",
    "mp_concepto_descripcion",
    "mp_iva_rate",
    "mp_user_id",
    "mp_pos_id",
)

#: Con qué clave de `config.json` se guarda el interruptor de facturación
#: automática, por defecto.
#:
#: 🔴 **El nombre en la API es siempre `mp_auto_facturar_ventas`; el que cambia
#: es el de la base.** LibraClub guarda `mp_auto_facturar_reservas` —lo que
#: cobra el QR de su mostrador es un turno de cancha, no una venta— y su
#: `servicios/cobro_qr` lee esa clave para decidir si emite. Montar este router
#: ahí con la clave de ventas dejaría el interruptor escribiendo en un lugar
#: que nadie lee: la pantalla diría que está prendido y no se emitiría ninguna
#: factura, sin ningún error.
#:
#: Se parametriza la clave y no se renombra la de LibraClub porque el valor ya
#: está guardado en las instancias vivas: renombrarla apagaría la facturación
#: automática de todo complejo que la tuviera prendida, en el deploy.
CAMPO_AUTO_FACTURAR = "mp_auto_facturar_ventas"

#: Los que no pueden salir en claro por la API.
SECRETOS = ("mp_access_token", "mp_webhook_secret")

URL_USERS_ME = "https://api.mercadopago.com/users/me"

#: Los tres valores que puede tomar el ambiente, más el vacío de "no hay
#: credencial cargada". `INDETERMINADO` no es un error: es "hay un token y
#: todavía nadie le preguntó a MercadoPago de quién es".
PRUEBA        = "prueba"
PRODUCCION    = "produccion"
INDETERMINADO = "indeterminado"

#: Las claves derivadas que este router escribe en `config.json`. **No están en
#: `CAMPOS`** a propósito: el ambiente no se elige desde la pantalla, se deriva.
CAMPOS_AMBIENTE = ("mp_ambiente", "mp_ambiente_verificado", "mp_ambiente_huella")


def huella(token: str) -> str:
    """Identifica al token sin guardarlo de nuevo. Es lo que hace que la
    clasificación se descarte sola cuando la credencial cambia."""
    token = (token or "").strip()
    if not token:
        return ""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


#: La marca con la que MercadoPago declara que una cuenta es de prueba, en el
#: `tags` de `/users/me`. Es un dato **declarado**, no una inferencia sobre un
#: nombre: por eso manda sobre el `nickname`.
TAG_DE_PRUEBA = "test_user"


def clasificar_ambiente(
    token: str,
    nickname: str | None = None,
    tags: list | None = None,
) -> str:
    """De qué ambiente es una credencial de MercadoPago.

    🔴 **Mirar el prefijo del token NO alcanza**, y esa es toda la dificultad de
    esta función. Un token de prueba puede empezar con `APP_USR-`, igual que uno
    real, en los dos casos que hay:

    1. Un **usuario de prueba** — la cuenta ficticia completa. Sus credenciales
       salen de una aplicación creada adentro de esa cuenta y llevan `APP_USR-`.
    2. Las **credenciales de prueba automáticas de la aplicación**. Hasta 2025
       empezaban con `TEST-`; desde el cambio de MercadoPago de noviembre de
       2025 —la app las recibe sola al crearse— también vienen con `APP_USR-`.
       Medido contra una cuenta real el 2026-08-30: token `APP_USR-…` y cuenta
       de prueba.

    O sea que el prefijo `TEST-` sólo sirve para reconocer las viejas, y hay que
    preguntarle a `/users/me` de quién es el token.

    ## Qué se mira de la respuesta, y en qué orden

    🔑 **`tags` manda sobre `nickname`.** MercadoPago devuelve
    `tags: ["test_user", "normal"]` en las cuentas de prueba: es una marca
    **declarada** por ellos. El `nickname` es una heurística sobre un string, y
    falla en las dos direcciones — un comercio real llamado `TESTORE` quedaría
    marcado como prueba, y basta con que cambien el formato del nickname (cosa
    que acaban de hacer con los tokens) para dejar de reconocer las de prueba.

    Por eso, **si `tags` vino, decide él y el nickname no se mira**. El nickname
    queda como respaldo para el caso de que `tags` no venga.

    `nickname` y `tags` son parámetros y no algo que esta función salga a
    buscar: pintar una pantalla no puede depender de que MercadoPago conteste.
    Sin ninguno de los dos, responde `INDETERMINADO` en vez de arriesgar.
    """
    token = (token or "").strip()
    if not token:
        return ""
    if token.upper().startswith("TEST-"):
        return PRUEBA
    if tags is not None:
        return PRUEBA if TAG_DE_PRUEBA in tags else PRODUCCION
    if nickname is None:
        return INDETERMINADO
    return PRUEBA if str(nickname).upper().startswith("TEST") else PRODUCCION


def ambiente_de(cfg: dict) -> tuple[str, str]:
    """El ambiente que hay que mostrar, y desde cuándo se sabe.

    🔴 **La huella es la que impide que esto mienta.** La clasificación guardada
    vale para el token sobre el que se determinó y para ningún otro; si el
    `mp_access_token` de al lado cambió —por la pantalla, por `panel_admin`
    escribiendo `config.json`, por restaurar un backup— deja de coincidir y acá
    se responde `INDETERMINADO`. Sin ese cotejo, pasar una instancia de prueba a
    producción dejaría el cartel diciendo "prueba" sobre una credencial real:
    justo la falla que el cartel viene a evitar, ahora confirmada por escrito.
    """
    token = (cfg.get("mp_access_token") or "").strip()
    if not token:
        return "", ""
    sin_red = clasificar_ambiente(token)
    if sin_red == PRUEBA:
        # El prefijo `TEST-` es concluyente por sí solo: no hay nada que
        # verificar contra MercadoPago, ni caché que pueda quedar viejo.
        return PRUEBA, ""
    if cfg.get("mp_ambiente") and cfg.get("mp_ambiente_huella") == huella(token):
        return cfg["mp_ambiente"], cfg.get("mp_ambiente_verificado", "")
    return INDETERMINADO, ""


class MpPayload(BaseModel):
    """⚠️ Los secretos son `str` con default vacío **a propósito**: vacío quiere
    decir "dejá el que está", no "borralo". Ver el docstring del módulo."""

    mp_access_token: str = ""
    mp_webhook_secret: str = ""
    mp_concepto_descripcion: str = ""
    mp_iva_rate: str = "0"
    mp_user_id: str = ""
    mp_pos_id: str = ""
    mp_auto_facturar_ventas: bool = False


def enmascarar(valor: str) -> str:
    """`APP_USR-1234…9f2a`. Sirve para que la pantalla muestre **cuál** de dos
    credenciales está cargada sin exponerla."""
    valor = (valor or "").strip()
    if not valor:
        return ""
    if len(valor) <= 8:
        return "…" * 4
    return f"{valor[:4]}…{valor[-4:]}"


def _visible(cfg: dict, campo_auto: str = CAMPO_AUTO_FACTURAR) -> dict:
    salida = {k: cfg.get(k, "") for k in CAMPOS}
    for k in SECRETOS:
        salida[k] = enmascarar(cfg.get(k, ""))
        salida[f"{k}_cargado"] = bool((cfg.get(k) or "").strip())
    # Sale SIEMPRE con el nombre de la API, venga de la clave que venga.
    salida["mp_auto_facturar_ventas"] = bool(cfg.get(campo_auto))
    salida["mp_ambiente"], salida["mp_ambiente_verificado"] = ambiente_de(cfg)
    return salida


def _olvidar_ambiente(cfg: dict) -> None:
    """Deja el `config.json` sin clasificación. Muta el dict que se le pasa,
    para poder llamarla en el medio del cargar-actualizar-guardar."""
    for campo in CAMPOS_AMBIENTE:
        cfg[campo] = ""


def build_mp_config_router(
    *,
    prefix: str = "/api/config/mercadopago",
    campo_auto_facturar: str = CAMPO_AUTO_FACTURAR,
) -> APIRouter:
    """Va detrás del gate de admin del producto. **Todo el router**, incluida la
    lectura: aunque el token salga enmascarado, saber si hay credenciales
    cargadas y con qué CUIT cobra el negocio no es información de cualquier
    usuario logueado.

    `campo_auto_facturar` es la clave de `config.json` donde vive el
    interruptor de facturación automática. El nombre en la API no cambia — ver
    `CAMPO_AUTO_FACTURAR`.
    """
    router = APIRouter(prefix=prefix, tags=["mercadopago"])

    @router.get("")
    def obtener():
        return _visible(config_manager.load(), campo_auto_facturar)

    @router.put("")
    def guardar(payload: MpPayload):
        cfg = config_manager.load()
        token_anterior = cfg.get("mp_access_token", "")
        for campo in CAMPOS:
            valor = getattr(payload, campo)
            if campo in SECRETOS and not str(valor).strip():
                # Vacío = no lo toqués. La pantalla muestra el enmascarado.
                continue
            cfg[campo] = valor
        cfg[campo_auto_facturar] = bool(payload.mp_auto_facturar_ventas)
        if cfg.get("mp_access_token", "") != token_anterior:
            # Que el `config.json` no quede con la clasificación de la
            # credencial anterior al lado de la nueva. Quien impide que eso se
            # muestre es el cotejo de huella de `ambiente_de()` —esto es
            # limpieza, no la defensa—, pero un archivo que se lee a mano no
            # tiene por qué decir algo que ya no es cierto.
            _olvidar_ambiente(cfg)
        config_manager.save(cfg)
        return _visible(config_manager.load(), campo_auto_facturar)

    @router.delete("/credenciales")
    def borrar_credenciales():
        """Sacar las credenciales tiene que ser posible desde la pantalla:
        con "vacío = no lo toqués", no hay otra forma de desconectar la cuenta."""
        cfg = config_manager.load()
        for campo in SECRETOS:
            cfg[campo] = ""
        _olvidar_ambiente(cfg)
        config_manager.save(cfg)
        return _visible(config_manager.load(), campo_auto_facturar)

    @router.post("/probar")
    async def probar():
        """Le pregunta a MercadoPago quién es el dueño del token.

        Además de decir si sirve, devuelve el `user_id` — que es justo lo que
        hay que copiar en el campo de al lado para armar el QR de caja.
        """
        token = (config_manager.load().get("mp_access_token") or "").strip()
        if not token:
            raise HTTPException(400, "No hay Access Token configurado.")
        try:
            async with httpx.AsyncClient(timeout=10) as cliente:
                r = await cliente.get(
                    URL_USERS_ME, headers={"Authorization": f"Bearer {token}"}
                )
        except httpx.RequestError as e:
            raise HTTPException(502, f"Sin conexión con MercadoPago: {e}") from None

        if r.status_code != 200:
            # El texto de MercadoPago va tal cual, recortado: distingue un token
            # vencido de uno de otra aplicación.
            raise HTTPException(502, f"MercadoPago respondió {r.status_code}: {r.text[:200]}")

        datos = r.json()

        # Este es el único momento en que se sabe de quién es el token, así que
        # es acá donde se clasifica el ambiente y se anota. Pintar la pantalla
        # no puede depender de que MercadoPago conteste.
        cfg = config_manager.load()
        ambiente = clasificar_ambiente(
            token, datos.get("nickname"), datos.get("tags"))
        cfg["mp_ambiente"] = ambiente
        cfg["mp_ambiente_verificado"] = _ar_now()
        cfg["mp_ambiente_huella"] = huella(token)
        config_manager.save(cfg)

        return {
            "ok": True,
            "user_id": datos.get("id"),
            "nickname": datos.get("nickname"),
            "email": datos.get("email"),
            "site_id": datos.get("site_id"),
            "pais": datos.get("country_id"),
            "ambiente": ambiente,
        }

    return router
