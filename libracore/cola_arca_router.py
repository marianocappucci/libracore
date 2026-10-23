"""La API de la cola de comprobantes del nodo (ADR-079): lo que la pantalla del nodo necesita para avisar «sin CPE válida»
y para que una persona resuelva lo que quedó para revisar.

Se monta junto al router del nodo (`build_facturacion_del_nodo_router`, ADR-074) con el mismo `usuario_actual`. Fuera de un
nodo contesta igual (`es_nodo: false`, sin pedidos) y no hace nada.

| Ruta | Qué hace |
|---|---|
| `GET  {prefix}` | `{"es_nodo", "en_linea", "node_id", "cuentas", "a_revisar", "pedidos"}`. `?estado=` filtra (se repite), `?tipo=`, `?limite=` (200) |
| `POST {prefix}/emitir` | Emite ya lo que está `esperando` (lo que hace sola la tanda). 409 `sin_conexion` sin conexión |
| `POST {prefix}/{id}/reintentar` | `{"datos"?, "confirmo_que_no_salio"?}`: `a_revisar` o `incierto` → `esperando`. Un `incierto` pide la confirmación (422) |
| `POST {prefix}/{id}/descartar` | `{"motivo"?}` |
| `POST {prefix}/{id}/emitido` | `{"resultado"}`: una persona comprobó en ARCA que sí salió |

Las tres últimas deciden sobre dinero y sobre un documento fiscal: `resolver_con` es una dependencia más (el producto pone
acá su «sólo administradores») que se suma a `usuario_actual` en esas rutas y no en las de lectura.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, JsonValue

from libracore import cola_arca as cola_mod
from libracore.cola_arca import ColaDeArca
from libracore.db import cola_arca as db
from libracore.nodo_offline import SinConexion


class _Reintento(BaseModel):
    datos: dict[str, JsonValue] | None = None
    confirmo_que_no_salio: bool = False


class _Descarte(BaseModel):
    motivo: str = Field(default="", max_length=500)


class _Emitido(BaseModel):
    resultado: dict[str, JsonValue]


def build_cola_del_nodo_router(*, cola: ColaDeArca, usuario_actual: Callable[..., Any],
                               resolver_con: Callable[..., Any] | None = None,
                               prefix: str = "/api/nodo/cola") -> APIRouter:
    """Ver el docstring del módulo."""
    router = APIRouter(prefix=prefix, tags=["nodo"])
    gate = [Depends(resolver_con)] if resolver_con else []

    def _o_error(operacion, *args, **kwargs):
        try:
            return operacion(cola, *args, **kwargs)
        except cola_mod.PedidoNoEncontrado as e:
            raise HTTPException(404, str(e)) from None
        except cola_mod.ConfirmacionRequerida as e:
            raise HTTPException(422, {"codigo": "confirmacion_requerida", "mensaje": str(e)}) from None
        except ValueError as e:
            raise HTTPException(409, str(e)) from None

    @router.get("")
    def listar(estado: list[str] | None = Query(default=None), tipo: str | None = None,
               limite: int = Query(default=200, ge=1, le=1000), user: dict = Depends(usuario_actual)):
        r = cola_mod.resumen(cola)
        pedidos = []
        if r["es_nodo"]:
            try:
                pedidos = db.listar(estados=tuple(estado) if estado else None, origen_producto=cola.producto,
                                    origen_instancia=cola.instancia(), origen_tipo=tipo, limite=limite,
                                    mas_nuevos_primero=True)
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
        return {**r, "pedidos": pedidos}

    @router.post("/emitir")
    def emitir(user: dict = Depends(usuario_actual)):
        c = cola.conexion
        if c.es_nodo() and c.sin_conexion():
            raise HTTPException(409, SinConexion(
                "Sin conexión: los comprobantes se emiten cuando vuelva la conexión.").detalle())
        return asyncio.run(cola_mod.procesar_cola(cola, usuario_id=user.get("id")))

    @router.post("/{pedido_id}/reintentar", dependencies=gate)
    def reintentar(pedido_id: int, cuerpo: _Reintento | None = None, user: dict = Depends(usuario_actual)):
        cuerpo = cuerpo or _Reintento()
        return _o_error(cola_mod.reintentar, pedido_id, datos=cuerpo.datos,
                        confirmo_que_no_salio=cuerpo.confirmo_que_no_salio)

    @router.post("/{pedido_id}/descartar", dependencies=gate)
    def descartar(pedido_id: int, cuerpo: _Descarte | None = None, user: dict = Depends(usuario_actual)):
        return _o_error(cola_mod.descartar, pedido_id, (cuerpo or _Descarte()).motivo)

    @router.post("/{pedido_id}/emitido", dependencies=gate)
    def emitido(pedido_id: int, cuerpo: _Emitido, user: dict = Depends(usuario_actual)):
        return _o_error(cola_mod.marcar_emitido, pedido_id, cuerpo.resultado)

    return router
