"""Tesorería (cuentas bancarias y sus movimientos) como factory de router
(P9-M4, 2026-09-07). `web/api/tesoreria.py` era el mismo archivo en los dos
productos salvo el docstring. Sobre `libracore.db.tesoreria`.

```python
app.include_router(build_tesoreria_router(usuario_actual=get_current_user_json),
                   dependencies=[Depends(require_admin_json), Depends(require_module("tesoreria"))])
```
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from libracore.db import tesoreria as db_tes


class CuentaPayload(BaseModel):
    nombre: str
    tipo: str = "banco"
    banco: str = ""
    numero: str = ""
    descripcion: str = ""
    saldo_inicial: float = 0


class MovimientoTesoreriaPayload(BaseModel):
    tipo: str  # ingreso | egreso
    monto: float
    concepto: str
    fecha: str
    referencia: str = ""


class TransferenciaPayload(BaseModel):
    cuenta_origen_id: int
    cuenta_destino_id: int
    monto: float
    fecha: str
    concepto: str = "Transferencia entre cuentas"
    referencia: str = ""


def build_tesoreria_router(*, usuario_actual: Callable[..., Any], prefix: str = "/api/tesoreria") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["tesoreria"])

    @router.get("")
    def resumen():
        return {
            "cuentas": db_tes.get_all_cuentas_tesoreria(),
            "movimientos": db_tes.get_movimientos_tesoreria(limit=30),
            "resumen": db_tes.get_resumen_tesoreria(),
        }

    @router.post("/cuentas")
    def crear_cuenta(payload: CuentaPayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        cid = db_tes.create_cuenta_tesoreria(
            nombre=nombre, tipo=payload.tipo, banco=payload.banco.strip(),
            numero=payload.numero.strip(), descripcion=payload.descripcion.strip(),
            saldo_inicial=payload.saldo_inicial,
        )
        return db_tes.get_cuenta_tesoreria(cid)

    @router.get("/cuentas/{cid}")
    def detalle_cuenta(cid: int, desde: str = "", hasta: str = ""):
        cuenta = db_tes.get_cuenta_tesoreria(cid)
        if not cuenta:
            raise HTTPException(404, "Cuenta no encontrada")
        return {
            "cuenta": cuenta,
            "movimientos": db_tes.get_movimientos_tesoreria(cuenta_id=cid, desde=desde, hasta=hasta),
        }

    @router.put("/cuentas/{cid}")
    def actualizar_cuenta(cid: int, payload: CuentaPayload):
        if not db_tes.get_cuenta_tesoreria(cid):
            raise HTTPException(404, "Cuenta no encontrada")
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        db_tes.update_cuenta_tesoreria(
            cid, nombre=nombre, tipo=payload.tipo, banco=payload.banco.strip(),
            numero=payload.numero.strip(), descripcion=payload.descripcion.strip(),
            saldo_inicial=payload.saldo_inicial,
        )
        return db_tes.get_cuenta_tesoreria(cid)

    @router.delete("/cuentas/{cid}")
    def eliminar_cuenta(cid: int):
        if not db_tes.get_cuenta_tesoreria(cid):
            raise HTTPException(404, "Cuenta no encontrada")
        db_tes.delete_cuenta_tesoreria(cid)
        return {"ok": True}

    @router.post("/cuentas/{cid}/movimiento")
    def crear_movimiento(cid: int, payload: MovimientoTesoreriaPayload, user: dict = Depends(usuario_actual)):
        if not db_tes.get_cuenta_tesoreria(cid):
            raise HTTPException(404, "Cuenta no encontrada")
        if payload.monto <= 0:
            raise HTTPException(422, "El monto debe ser mayor a cero.")
        concepto = payload.concepto.strip()
        if not concepto:
            raise HTTPException(422, "El concepto es obligatorio.")
        db_tes.create_movimiento_tesoreria(
            fecha=payload.fecha, cuenta_id=cid, tipo=payload.tipo, monto=payload.monto,
            concepto=concepto, referencia=payload.referencia.strip(), usuario_id=user.get("id"),
        )
        return db_tes.get_cuenta_tesoreria(cid)

    @router.post("/transferencia")
    def transferir(payload: TransferenciaPayload, user: dict = Depends(usuario_actual)):
        if payload.cuenta_origen_id == payload.cuenta_destino_id:
            raise HTTPException(422, "La cuenta origen y destino deben ser distintas.")
        if payload.monto <= 0:
            raise HTTPException(422, "El monto debe ser mayor a cero.")
        if not db_tes.get_cuenta_tesoreria(payload.cuenta_origen_id) or not db_tes.get_cuenta_tesoreria(payload.cuenta_destino_id):
            raise HTTPException(404, "Cuenta no encontrada")
        db_tes.create_transferencia_tesoreria(
            fecha=payload.fecha, cuenta_origen_id=payload.cuenta_origen_id,
            cuenta_destino_id=payload.cuenta_destino_id, monto=payload.monto,
            concepto=payload.concepto.strip(), referencia=payload.referencia.strip(), usuario_id=user.get("id"),
        )
        return {"ok": True}

    @router.delete("/movimientos/{mid}")
    def eliminar_movimiento(mid: int):
        db_tes.delete_movimiento_tesoreria(mid)
        return {"ok": True}

    return router
