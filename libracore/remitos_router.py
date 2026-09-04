"""Factory del router JSON de Remitos, compartido por los productos.

Los cuatro endpoints (listar / crear / detalle / eliminar) eran **byte-idénticos**
entre Contalibra y Restolibra —sólo difería el docstring—. Ahora viven acá; cada
producto inyecta lo suyo: la dependencia de auth (`usuario_actual`) y el generador
de PDF (`generar_pdf`, la arista de presentación). El dominio es de
`libracore.db.remitos_presupuestos`; el cliente, de `libracore.db.clients`.

Mismo criterio que `libracore.facturas_router.build_comprobantes_router`.

> El PDF por descarga (`GET /remitos/{id}/pdf`) NO entra acá: es una descarga
> autenticada por cookie que la SPA linkea directo, y sigue en el router propio
> de cada producto (`web/routers/remitos.py`).

LibraDesk **no** usa este factory: su router de remitos es service-based
(`RemitoService`, deriva el cliente de su propia tabla) — otra estructura, arista.
"""
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from libracore.db import clients as _clients
from libracore.db import remitos_presupuestos as _rp


class _ItemPayload(BaseModel):
    description: str
    qty: float


class _RemitoPayload(BaseModel):
    date: str
    client_id: int | None = None
    client_name: str = ""
    observations: str = ""
    items: list[_ItemPayload]


def build_remitos_router(
    *,
    usuario_actual: Callable[..., Any],
    generar_pdf: Callable[[dict], str],
    prefix: str = "/api/remitos",
) -> APIRouter:
    """Los cuatro endpoints de remitos, con lo del producto inyectado.

    `usuario_actual` es la dependencia que devuelve el usuario de la sesión (hace
    falta para el `usuario_id` que queda en el remito). `generar_pdf(remito) ->
    pdf_path` es el generador de PDF del producto; se llama tras crear el remito.
    """
    router = APIRouter(prefix=prefix, tags=["remitos"])

    @router.get("")
    def listar(q: str = ""):
        return _rp.search_remitos(q) if q else _rp.get_all_remitos(200)

    @router.post("")
    def crear(payload: _RemitoPayload, user: dict = Depends(usuario_actual)):
        client_name = payload.client_name.strip()
        client_address = client_cuit = client_email = client_phone = ""
        if payload.client_id:
            c = _clients.get_client(payload.client_id)
            if c:
                client_name = c["name"]
                client_address = c.get("address", "")
                client_cuit = c.get("cuit_dni", "")
                client_email = c.get("email", "")
                client_phone = c.get("phone", "")
        if not client_name:
            raise HTTPException(422, "El nombre del cliente es requerido.")

        items = [
            {"description": i.description.strip(), "qty": i.qty}
            for i in payload.items if i.description.strip()
        ]
        if not items:
            raise HTTPException(422, "Debe agregar al menos un ítem válido.")

        number = _rp.get_next_remito_number()
        remito_id = _rp.create_remito(
            number=number, date=payload.date, client_id=payload.client_id,
            client_name=client_name, client_address=client_address, client_cuit=client_cuit,
            client_email=client_email, client_phone=client_phone, items=items,
            subtotal=0, tax_rate=0, tax_amount=0, total=0,
            observations=payload.observations.strip(), usuario_id=user["id"],
        )
        pdf_path = generar_pdf(_rp.get_remito(remito_id))
        _rp.update_remito_pdf_path(remito_id, pdf_path)
        return _rp.get_remito(remito_id)

    @router.get("/{remito_id}")
    def detalle(remito_id: int):
        remito = _rp.get_remito(remito_id)
        if not remito:
            raise HTTPException(404, "Remito no encontrado")
        return remito

    @router.delete("/{remito_id}")
    def eliminar(remito_id: int):
        if not _rp.get_remito(remito_id):
            raise HTTPException(404, "Remito no encontrado")
        _rp.delete_remito(remito_id)
        return {"ok": True}

    return router
