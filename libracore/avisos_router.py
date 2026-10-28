"""La API de la configuración de avisos (ADR-090): cómo está WhatsApp, cambiarlo, probarlo y ver qué salió.

El producto lo monta una vez y le pone **su** gate de administrador; la pantalla la arma el kit o el producto después:

    app.include_router(build_avisos_router(require_admin=require_admin, smtp_config=lambda: resolver_smtp_config(fabrica)))

| Ruta | Qué hace |
|---|---|
| `GET  {prefix}/config` | `{"whatsapp": {modo, modos, url, instancia, apikey_cargada, disponible, motivo}, "email": {disponible}}` |
| `PUT  {prefix}/config` | `{modo?, url?, instancia?, apikey?, quitar_apikey?}`: guarda y devuelve lo mismo que el `GET` |
| `POST {prefix}/probar` | `{canal, destino}`: manda **un mensaje fijo de prueba** por el canal configurado. 200 `{ok, estado, simulado, detalle}` |
| `GET  {prefix}/envios` | el registro (`?canal=&estado=&tipo=&limite=`), el más nuevo primero |

- 🔴 **El gate es obligatorio** (`require_admin` no tiene default) y cubre las cuatro rutas: la configuración dice con qué cuenta de
  WhatsApp habla el negocio y el registro tiene los teléfonos y los textos de los clientes.
- 🔴 **La `apikey` es sólo de escritura.** Nunca vuelve: ni entera, ni los últimos caracteres. `apikey_cargada` dice si hay una. Una
  `apikey` vacía en el `PUT` quiere decir «no la toqués»; para sacarla, `quitar_apikey: true`.
- **`probar` manda un texto fijo**, no uno que elija quien llama: un endpoint que manda el texto que le pasan es un relé de mensajes.
  Y no pasa por el registro: una prueba no es un aviso de nadie.
- Un error de Evolution se devuelve como **502** (con el motivo), nunca como 401/403: un 401 hace que el kit mande al usuario al
  login, y el que falló fue Evolution, no su sesión.
- `PUT` y `probar` contestan **409 `solo_en_el_central`** en un nodo de LibraEdge: la apikey vive en el central y un nodo no manda.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from libracore import config_manager
from libracore.avisos import configuracion
from libracore.avisos.mensaje import CANAL_EMAIL, CANAL_WHATSAPP, FALLIDO, RECHAZADO, REGISTRADO, Mensaje
from libracore.avisos.proveedores import ProveedorSmtp
from libracore.avisos.telefono import telefono_para_whatsapp
from libracore.db import avisos_envios as db
from libracore.nodo_offline import guarda_solo_en_el_central


class ConfigIn(BaseModel):
    """Todo opcional: lo que no viene no se toca. `apikey` es `str` con default vacío **a propósito**: vacío = «dejá la que está»."""

    modo: Literal["apagado", "registro", "evolution"] | None = None
    url: str | None = None
    instancia: str | None = None
    apikey: str = Field(default="", repr=False)
    quitar_apikey: bool = False


class PruebaIn(BaseModel):
    canal: Literal["whatsapp", "email"] = "whatsapp"
    destino: str


def _texto_de_prueba() -> str:
    empresa = (config_manager.load().get("empresa_nombre") or "").strip()
    firma = f" de {empresa}" if empresa else ""
    return f"Mensaje de prueba{firma}. Si lo recibiste, los avisos están conectados."


def build_avisos_router(*, require_admin: Callable[..., Any], smtp_config: Callable | None = None,
                        prefix: str = "/api/avisos", transporte: httpx.BaseTransport | None = None,
                        enviar_texto: Callable | None = None, nodo: Callable[[], str | None] | None = None) -> APIRouter:
    """Ver el docstring del módulo. `smtp_config` es el resolver de SMTP del producto (el mismo que el del envío de comprobantes);
    `nodo` reemplaza la lectura de la identidad del nodo (default `LIBRAEDGE_NODE_ID`).

    `transporte` (un `httpx.MockTransport` para Evolution) y `enviar_texto` (reemplaza a `email_sender.enviar_texto`) son para los
    tests de un producto: con ellos `POST /probar` se prueba sin red, por WhatsApp y por mail. Sin ellos, todo sale como siempre."""
    router = APIRouter(prefix=prefix, tags=["avisos"], dependencies=[Depends(require_admin)])
    solo_central = [Depends(guarda_solo_en_el_central(nodo=nodo))]

    def _estado() -> dict:
        cfg = configuracion.leer()
        motivo = configuracion.motivo_de_whatsapp()
        return {
            "whatsapp": {
                "modo": cfg.modo, "modos": list(configuracion.MODOS), "url": cfg.url, "instancia": cfg.instancia,
                "apikey_cargada": cfg.apikey_cargada, "disponible": not motivo, "motivo": motivo,
            },
            "email": {"disponible": ProveedorSmtp(smtp_config).disponible()},
        }

    @router.get("/config")
    def obtener_config():
        return _estado()

    @router.put("/config", dependencies=solo_central)
    def guardar_config(cuerpo: ConfigIn):
        try:
            configuracion.guardar(modo=cuerpo.modo, url=cuerpo.url, instancia=cuerpo.instancia,
                                  apikey=cuerpo.apikey, quitar_apikey=cuerpo.quitar_apikey)
        except configuracion.ErrorDeConfiguracion as e:
            raise HTTPException(422, str(e)) from None
        return _estado()

    @router.post("/probar", dependencies=solo_central)
    def probar(cuerpo: PruebaIn):
        if cuerpo.canal == CANAL_EMAIL:
            proveedor = configuracion.proveedor_email(smtp_config, enviar_texto=enviar_texto)
            destino = cuerpo.destino.strip()
        else:
            proveedor = configuracion.proveedor_whatsapp(transporte=transporte)
            destino = telefono_para_whatsapp(cuerpo.destino)
            if not destino:
                raise HTTPException(422, "El teléfono no se reconoce: escribilo con característica, por ejemplo 11 5555-1234.")
        if not proveedor.disponible():
            motivo = configuracion.motivo_de_whatsapp() if cuerpo.canal == CANAL_WHATSAPP else \
                "Todavía no hay un servidor de correo configurado."
            raise HTTPException(409, {"codigo": "no_disponible", "mensaje": motivo})
        resultado = proveedor.enviar(Mensaje(destino, asunto="Mensaje de prueba", cuerpo=_texto_de_prueba()))
        if resultado.estado in (FALLIDO, RECHAZADO):
            # 502 y no el status de Evolution: ver el docstring del módulo.
            raise HTTPException(502, {"codigo": "fallo_del_proveedor", "mensaje": resultado.detalle})
        return {"ok": True, "estado": resultado.estado, "simulado": resultado.estado == REGISTRADO,
                "detalle": resultado.detalle}

    @router.get("/envios")
    def listar_envios(canal: Literal["whatsapp", "email"] | None = None, estado: str | None = None,
                      tipo: str | None = None, limite: int = Query(default=100, ge=1, le=500)):
        if estado is not None and estado not in db.ESTADOS:
            raise HTTPException(422, f"El estado tiene que ser uno de: {', '.join(db.ESTADOS)}.")
        return db.listar(canal=canal, estado=estado, tipo=tipo, limite=limite)

    return router


__all__ = ["CANAL_EMAIL", "CANAL_WHATSAPP", "ConfigIn", "PruebaIn", "build_avisos_router"]
