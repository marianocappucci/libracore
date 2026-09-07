"""Clientes como factory de router (P9-M4, 2026-09-07).

`web/api/clientes.py` de Contalibra y Restolibra era el mismo archivo salvo el
docstring: alta, edición, detalle con facturas/presupuestos/remitos, el toggle
de auto-factura de MercadoPago, los alias de facturación (payer CUIT/email →
cliente) y activar/desactivar. Todo sobre `libracore.db.clients` y
`libracore.db.mp`, que ya eran del motor.

```python
app.include_router(build_clientes_router(),
                   dependencies=[_auth_json, Depends(require_module("clientes"))])
```

El gate lo pone el producto al montar; el add-on mayorista de Contalibra
comparte el prefijo con su propio router y su propio gate.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from libracore.db import clients as db_clients
from libracore.db import mp as db_mp
from libracore.db import remitos_presupuestos as db_rp

IVA_CONDITIONS = [
    "Responsable Inscripto",
    "Monotributista",
    "IVA Exento",
    "Consumidor Final",
    "No Alcanzado",
    "IVA No Responsable",
]


class ClientePayload(BaseModel):
    name: str
    address: str = ""
    cuit_dni: str = ""
    email: str = ""
    phone: str = ""
    iva_condition: str = ""
    auto_facturar: bool = False


class AliasPayload(BaseModel):
    tipo: str
    valor: str


def build_clientes_router(*, prefix: str = "/api/clientes") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["clientes"])

    def _o_404(cliente_id: int) -> dict:
        cliente = db_clients.get_client(cliente_id)
        if not cliente:
            raise HTTPException(404, "Cliente no encontrado")
        return cliente

    @router.get("")
    def listar():
        return db_clients.get_all_clients_including_inactive()

    @router.post("")
    def crear(payload: ClientePayload):
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, "El nombre es obligatorio.")
        try:
            cliente_id = db_clients.create_client(
                name, payload.address.strip(), payload.cuit_dni.strip(),
                payload.email.strip(), payload.phone.strip(), payload.iva_condition.strip(),
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return db_clients.get_client(cliente_id)

    @router.get("/{cliente_id}")
    def detalle(cliente_id: int):
        """Incluye facturas, presupuestos, remitos y alias de facturación."""
        cliente = _o_404(cliente_id)
        return {
            **cliente,
            "alias_facturacion": db_mp.get_alias_facturacion_by_cliente(cliente_id),
            "facturas": db_clients.get_facturas_by_client(cliente.get("cuit_dni") or "", cliente.get("name") or ""),
            "presupuestos": db_rp.get_presupuestos_by_client(cliente_id),
            "remitos": db_rp.get_remitos_by_client(cliente_id),
        }

    @router.put("/{cliente_id}")
    def actualizar(cliente_id: int, payload: ClientePayload):
        _o_404(cliente_id)
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, "El nombre es obligatorio.")
        db_clients.update_client(
            cliente_id, name=name, address=payload.address.strip(),
            cuit_dni=payload.cuit_dni.strip(), email=payload.email.strip(),
            phone=payload.phone.strip(), iva_condition=payload.iva_condition.strip(),
            auto_facturar=1 if payload.auto_facturar else 0,
        )
        return db_clients.get_client(cliente_id)

    @router.post("/{cliente_id}/toggle-auto-facturar")
    def toggle_auto_facturar(cliente_id: int):
        _o_404(cliente_id)
        db_clients.toggle_auto_facturar(cliente_id)
        return db_clients.get_client(cliente_id)

    @router.post("/{cliente_id}/alias-facturacion")
    def crear_alias(cliente_id: int, payload: AliasPayload):
        _o_404(cliente_id)
        try:
            db_mp.crear_alias_facturacion(payload.tipo.strip(), payload.valor.strip(), cliente_id)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return db_mp.get_alias_facturacion_by_cliente(cliente_id)

    @router.delete("/{cliente_id}/alias-facturacion/{alias_id}")
    def eliminar_alias(cliente_id: int, alias_id: int):
        _o_404(cliente_id)
        db_mp.eliminar_alias_facturacion(alias_id)
        return db_mp.get_alias_facturacion_by_cliente(cliente_id)

    @router.post("/{cliente_id}/desactivar")
    def desactivar(cliente_id: int):
        cliente = _o_404(cliente_id)
        if db_clients.tiene_presupuestos_aprobados(cliente_id):
            raise HTTPException(
                422,
                f"No se puede desactivar {cliente['name']} porque tiene presupuestos aprobados. "
                "Primero hay que cancelar o rechazar esos presupuestos.",
            )
        db_clients.desactivar_cliente(cliente_id)
        return db_clients.get_client(cliente_id)

    @router.post("/{cliente_id}/activar")
    def activar(cliente_id: int):
        _o_404(cliente_id)
        db_clients.activar_cliente(cliente_id)
        return db_clients.get_client(cliente_id)

    return router
