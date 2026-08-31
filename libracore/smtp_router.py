"""*Probar conexión* del correo saliente, para los ocho productos.

Hasta hoy este botón existía **sólo en Contalibra y Restolibra**, cada uno con
su `GET /api/email/probar` escrito en el producto. Los otros seis configuraban
el SMTP en la pantalla compartida y no tenían forma de saber si andaba: el
primer indicio era un comprobante que no llegaba, o un mail de recuperación de
contraseña que nadie recibía.

## 🔑 Prueba EXACTAMENTE lo que después manda

La resolución no se escribe acá: sale de `smtp_efectivo`, la misma función que
usan el envío de comprobantes, el de presupuestos y la recuperación de
contraseña. Es la condición de que el botón signifique algo — antes de que esa
función existiera, el endpoint de Contalibra leía `config.json` mientras la
pantalla escribía en la base de libraauth, así que decía *Conectado* contra un
servidor y los mails salían por otro.

Por eso `smtp_config` se **inyecta** y no se importa: LibraCore no depende de
libraauth, igual que en `build_comprobantes_router`. El producto pasa su
resolver —normalmente `lambda: resolver_smtp_config(fabrica_de_sesiones)`— y
este router usa el mismo que el envío.

## Por qué el prefijo por defecto es `/admin/smtp`

Es donde **seis de los ocho** montan el router de SMTP de `libraauth` —es el
default de ese router— y es el `basePath` por defecto de `ConfiguracionSmtp` en
`libra-ui`. Colgando `/probar` de ahí, la pantalla compartida arma la URL sola y
esos seis no tienen que configurar nada.

Los otros dos lo montaron en otro lado y lo pasan: Contalibra y Restolibra usan
`/api/config/smtp`, que es lo que ya publicaron y cambiarlo rompería su frontend
desplegado sin ganar nada.

## El gate lo pone el producto

Como en el resto de los routers del motor. Acá no alcanza con "logueado": una
prueba de conexión abre una sesión SMTP con las credenciales del cliente, así
que va detrás del gate de admin del producto.
"""

from __future__ import annotations

import smtplib

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from libracore.facturas_router import smtp_efectivo

#: Segundos de espera. Diez alcanzan para un servidor que anda y no dejan la
#: pantalla colgada dos minutos contra uno que no contesta.
TIMEOUT = 10


class PruebaSmtpOut(BaseModel):
    """Lo que la pantalla necesita para decir contra qué se conectó.

    🔑 **No lleva la contraseña**, obviamente, pero tampoco hace falta que la
    lleve el `from_email`: lo que el cliente quiere confirmar es el servidor y
    la casilla con la que se autenticó.
    """

    ok: bool
    host: str
    port: int
    user: str


def build_smtp_probe_router(smtp_config, *, prefix: str = "/admin/smtp") -> APIRouter:
    """`POST {prefix}/probar` — abre la conexión, negocia TLS y hace login.

    `smtp_config` es el resolver del producto, el mismo que se le pasa a
    `build_comprobantes_router`. Se llama **en cada request** y no una vez al
    arrancar: si se resolviera al construir el router, guardar el SMTP por
    pantalla no tendría efecto hasta recrear el contenedor.
    """
    router = APIRouter(prefix=prefix, tags=["smtp"])

    @router.post("/probar", response_model=PruebaSmtpOut)
    def probar():
        cfg = smtp_efectivo(smtp_config)
        host = (cfg.get("host") or "").strip()
        usuario = (cfg.get("user") or "").strip()
        clave = (cfg.get("password") or "").strip()
        puerto = int(cfg.get("port") or 587)

        # 🔑 Se corta acá y no se sale a la red: sin las tres el error de
        # `smtplib` hablaría de la conexión o del login, y la causa es que falta
        # completar la pantalla. Es el mismo criterio que la guarda de pareja de
        # ARCA antes de ir a WSAA.
        if not host or not usuario or not clave:
            raise HTTPException(
                400, "Completá servidor, usuario y contraseña antes de probar.")

        try:
            with smtplib.SMTP(host, puerto, timeout=TIMEOUT) as servidor:
                servidor.ehlo()
                servidor.starttls()
                servidor.login(usuario, clave)
        except smtplib.SMTPAuthenticationError:
            # 🔴 Separado del resto a propósito: es el error que se ve siempre, y
            # el que tiene una causa concreta que el cliente puede arreglar solo
            # —Gmail no acepta la contraseña de la cuenta, hay que generar una
            # contraseña de aplicación—. Un 502 genérico no lo dice.
            raise HTTPException(
                401,
                "Autenticación fallida. Si es Gmail, revisá que sea una "
                "contraseña de aplicación y no la de la cuenta.",
            ) from None
        except (OSError, smtplib.SMTPException) as e:
            # El texto del servidor va tal cual: distingue "el host no existe"
            # de "el puerto está cerrado" de "no soporta STARTTLS", y las tres
            # se arreglan en lugares distintos.
            raise HTTPException(502, f"No se pudo conectar: {e}") from None

        return PruebaSmtpOut(ok=True, host=host, port=puerto, user=usuario)

    return router
