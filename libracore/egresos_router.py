"""Proveedores y egresos como factories de router (P9-M4, 2026-09-07).

`web/api/proveedores.py` y `web/api/egresos.py` eran el mismo archivo en
Contalibra y Restolibra salvo docstrings. Todo sobre `libracore.db.egresos` (los
proveedores viven ahí) y `libracore.db.caja` para el movimiento del pago.

```python
app.include_router(build_proveedores_router(),
                   dependencies=[_auth_json, Depends(require_module("proveedores"))])
app.include_router(build_egresos_router(usuario_actual=get_current_user_json),
                   dependencies=[_auth_json, Depends(require_module("egresos"))])
```
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from libracore.db import caja as db_caja
from libracore.db import egresos as db_egresos

TIPOS_COMPROBANTE = [
    {"id": "factura", "label": "Factura"},
    {"id": "ticket", "label": "Ticket / Recibo"},
    {"id": "recibo", "label": "Recibo oficial"},
    {"id": "otro", "label": "Otro"},
]

_TIPO_LABEL_CAJA = {"factura": "Factura", "ticket": "Ticket", "recibo": "Recibo", "otro": "Comprobante"}


def _hoy() -> str:
    return datetime.date.today().isoformat()


class ProveedorPayload(BaseModel):
    nombre: str
    cuit_dni: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    iva_condition: str = ""


class EgresoPayload(BaseModel):
    fecha: str = ""
    concepto: str
    categoria: str = ""
    tipo_comprobante: str = "otro"
    numero: str = ""
    observaciones: str = ""
    proveedor_id: int | None = None
    monto_neto: float = 0
    iva_pct: float = 0


class PagoEgresoPayload(BaseModel):
    monto: float | None = None
    medio_pago: str = ""
    caja_id: int | None = None
    fecha: str = ""
    referencia: str = ""


class CategoriaPayload(BaseModel):
    nombre: str


def build_proveedores_router(*, prefix: str = "/api/proveedores") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["proveedores"])

    @router.get("")
    def listar(q: str = ""):
        return db_egresos.search_proveedores(q) if q else db_egresos.get_all_proveedores()

    @router.post("")
    def crear(payload: ProveedorPayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        pid = db_egresos.create_proveedor(
            nombre=nombre, cuit_dni=payload.cuit_dni.strip(), email=payload.email.strip(),
            phone=payload.phone.strip(), address=payload.address.strip(),
            iva_condition=payload.iva_condition.strip(),
        )
        return db_egresos.get_proveedor(pid)

    @router.put("/{pid}")
    def actualizar(pid: int, payload: ProveedorPayload):
        if not db_egresos.get_proveedor(pid):
            raise HTTPException(404, "Proveedor no encontrado")
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        db_egresos.update_proveedor(
            pid, nombre=nombre, cuit_dni=payload.cuit_dni.strip(), email=payload.email.strip(),
            phone=payload.phone.strip(), address=payload.address.strip(),
            iva_condition=payload.iva_condition.strip(),
        )
        return db_egresos.get_proveedor(pid)

    @router.delete("/{pid}")
    def eliminar(pid: int):
        if not db_egresos.get_proveedor(pid):
            raise HTTPException(404, "Proveedor no encontrado")
        try:
            db_egresos.delete_proveedor(pid)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return {"ok": True}

    return router


def build_egresos_router(*, usuario_actual: Callable[..., Any], prefix: str = "/api/egresos") -> APIRouter:
    """`pagar` registra el pago (`egresos_pagos`) y el movimiento de caja
    correspondiente; `GET /cajas` es sólo lectura, para el selector de destino
    del pago, sin exponer un CRUD de cajas acá."""
    router = APIRouter(prefix=prefix, tags=["egresos"])

    @router.get("")
    def listar(desde: str = "", hasta: str = "", categoria: str = "", estado: str = "", proveedor_id: int = 0):
        if not desde and not hasta:
            hoy = datetime.date.today()
            desde, hasta = hoy.replace(day=1).isoformat(), hoy.isoformat()
        return {
            "items": db_egresos.get_all_egresos(desde=desde, hasta=hasta, categoria=categoria, estado=estado, proveedor_id=proveedor_id),
            "resumen": db_egresos.get_resumen_egresos(desde=desde, hasta=hasta),
        }

    @router.get("/tipos-comprobante")
    def tipos_comprobante():
        return TIPOS_COMPROBANTE

    @router.get("/categorias")
    def listar_categorias():
        return db_egresos.get_categorias_egreso()

    @router.post("/categorias")
    def crear_categoria(payload: CategoriaPayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        db_egresos.create_categoria_egreso(nombre)
        return db_egresos.get_categorias_egreso()

    @router.delete("/categorias/{cid}")
    def eliminar_categoria(cid: int):
        db_egresos.delete_categoria_egreso(cid)
        return db_egresos.get_categorias_egreso()

    @router.get("/cajas")
    def listar_cajas():
        return db_caja.get_all_cajas()

    @router.post("")
    def crear(payload: EgresoPayload, user: dict = Depends(usuario_actual)):
        concepto = payload.concepto.strip()
        if not concepto:
            raise HTTPException(422, "El concepto es obligatorio.")

        proveedor_nombre = ""
        if payload.proveedor_id:
            p = db_egresos.get_proveedor(payload.proveedor_id)
            if p:
                proveedor_nombre = p["nombre"]

        iva_monto = round(payload.monto_neto * payload.iva_pct, 2)
        total = round(payload.monto_neto + iva_monto, 2)
        if total <= 0:
            raise HTTPException(422, "El monto debe ser mayor a cero.")

        eid = db_egresos.create_egreso(
            fecha=payload.fecha or _hoy(), concepto=concepto,
            total=total, proveedor_id=payload.proveedor_id, proveedor_nombre=proveedor_nombre,
            tipo_comprobante=payload.tipo_comprobante, numero=payload.numero.strip(),
            categoria=payload.categoria.strip(), monto_neto=payload.monto_neto,
            iva_pct=payload.iva_pct, iva_monto=iva_monto,
            observaciones=payload.observaciones.strip(), usuario_id=user.get("id"),
        )
        return db_egresos.get_egreso(eid)

    @router.get("/{eid}/pagos")
    def listar_pagos(eid: int):
        if not db_egresos.get_egreso(eid):
            raise HTTPException(404, "Egreso no encontrado")
        return db_egresos.get_pagos_egreso(eid)

    @router.post("/{eid}/pagar")
    def pagar(eid: int, payload: PagoEgresoPayload, user: dict = Depends(usuario_actual)):
        egreso = db_egresos.get_egreso(eid)
        if not egreso:
            raise HTTPException(404, "Egreso no encontrado")

        monto = payload.monto if payload.monto is not None else float(egreso["total"])
        fecha = payload.fecha or _hoy()

        db_egresos.create_pago_egreso(
            egreso_id=eid, fecha=fecha, monto=monto, caja_id=payload.caja_id,
            medio_pago=payload.medio_pago, referencia=payload.referencia, usuario_id=user.get("id"),
        )

        tipo_lbl = _TIPO_LABEL_CAJA.get(egreso["tipo_comprobante"], "Comprobante")
        concepto_caja = f"Pago {tipo_lbl} {egreso['numero'] or ''} — {egreso['concepto'][:60]}".strip(" —")
        db_caja.create_caja_movimiento(
            fecha=fecha, tipo="egreso", concepto=concepto_caja, monto=monto,
            referencia=payload.referencia, caja_id=payload.caja_id,
            medio_pago=payload.medio_pago, usuario_id=user.get("id"),
        )
        return db_egresos.get_egreso(eid)

    @router.delete("/{eid}")
    def eliminar(eid: int):
        if not db_egresos.get_egreso(eid):
            raise HTTPException(404, "Egreso no encontrado")
        db_egresos.delete_egreso(eid)
        return {"ok": True}

    return router
