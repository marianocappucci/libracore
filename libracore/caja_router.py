"""Caja, cajas y turnos de caja como factories de router (P9-M3).

Tres routers que Contalibra y Restolibra tenían escritos dos veces
(`web/api/caja.py`, `web/api/cajas.py`, `web/api/turnos.py`), byte a byte
salvo una cosa: Restolibra le había agregado a las cajas el **punto de venta
de ARCA por mostrador** (`punto_venta`, con el 409 cuando ya lo tiene otra
caja). Queda para los dos: es el modelo *usuario → turno abierto → caja → punto
de venta* que ya resuelve `db.caja.resolver_punto_venta`, y en una instancia
de un solo POS el campo queda vacío y no hay que tocarlo.

```python
app.include_router(build_caja_router(usuario_actual=get_current_user_json),
                   dependencies=[_auth_json, Depends(require_module("caja"))])
app.include_router(build_cajas_router(),
                   dependencies=[_auth_json, Depends(require_module("cajas"))])
app.include_router(build_turnos_router(usuario_actual=get_current_user_json,
                                       resumen_turno=db.get_resumen_turno,
                                       cerrar_turno=db.cerrar_turno),
                   dependencies=[_auth_json])
```

El de turnos recibe `resumen_turno` y `cerrar_turno` porque dependen de **dónde
viven las ventas**: los de `libracore.db.turnos` leen la tabla `ventas` de este
motor; un producto cuyas ventas están en LibraCommerce pasa los de
`libracommerce.erp.ventas`, y VentaLibra los que arquean sobre la caja.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from libracore import medios_pago
from libracore.db import caja as db_caja
from libracore.db import turnos as db_turnos
from libracore.db.caja import PuntoDeVentaRepetido

# ── Caja: los movimientos ────────────────────────────────────────────────


class MovimientoPayload(BaseModel):
    fecha: str
    tipo: str  # ingreso | egreso
    concepto: str
    monto: float
    referencia: str = ""
    factura_id: int | None = None
    caja_id: int | None = None
    medio_pago: str = ""


def _periodo_actual():
    hoy = datetime.date.today()
    return hoy.replace(day=1).isoformat(), hoy.isoformat()


def build_caja_router(*, usuario_actual: Callable[..., Any], prefix: str = "/api/caja") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["caja"])

    @router.get("")
    def listar(desde: str = "", hasta: str = "", caja_id: int = 0):
        if not desde or not hasta:
            desde, hasta = _periodo_actual()
        return {
            "movimientos": db_caja.get_caja_movimientos(desde, hasta, caja_id=caja_id or None),
            "resumen": db_caja.get_caja_resumen(desde, hasta, caja_id=caja_id or None),
        }

    @router.post("")
    def crear(payload: MovimientoPayload, user: dict = Depends(usuario_actual)):
        concepto = payload.concepto.strip()
        if not concepto:
            raise HTTPException(422, "El concepto es obligatorio.")
        if payload.tipo not in ("ingreso", "egreso"):
            raise HTTPException(422, "Tipo inválido.")
        if payload.monto <= 0:
            raise HTTPException(422, "El monto debe ser mayor a cero.")
        mov_id = db_caja.create_caja_movimiento(
            payload.fecha, payload.tipo, concepto, payload.monto, payload.referencia.strip(),
            payload.factura_id, user.get("id"), caja_id=payload.caja_id,
            medio_pago=payload.medio_pago,
        )
        return {"id": mov_id}

    @router.delete("/{mov_id}")
    def eliminar(mov_id: int):
        """Anula el movimiento. **La fila queda.** La ruta sigue siendo `DELETE`
        para no romper al frontend, pero lo que hace es marcar `anulado=1`:
        sale de los totales del arqueo y la lista lo sigue mostrando."""
        db_caja.anular_caja_movimiento(mov_id)
        return {"ok": True}

    return router


# ── Cajas: los puntos de cobro ───────────────────────────────────────────


class CajaPayload(BaseModel):
    nombre: str
    descripcion: str = ""
    medios_pago: list[str] = []
    #: El punto de venta de ARCA de este mostrador. `None` deja la caja usando
    #: el de la empresa, que es como funcionan las instancias de un solo POS.
    punto_venta: int | None = None


class CajaUpdatePayload(CajaPayload):
    activo: bool = True


def build_cajas_router(*, prefix: str = "/api/cajas") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["cajas"])

    @router.get("")
    def listar():
        return db_caja.get_all_cajas()

    @router.get("/medios-disponibles")
    def medios_disponibles():
        return medios_pago.para_selector()

    @router.post("")
    def crear(payload: CajaPayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        try:
            cid = db_caja.create_caja_config(
                nombre, payload.descripcion.strip(), payload.medios_pago,
                punto_venta=payload.punto_venta,
            )
        except PuntoDeVentaRepetido as choque:
            # 409 y no 422: el dato no está mal escrito, ya lo tiene otra caja.
            raise HTTPException(409, str(choque)) from choque
        return db_caja.get_caja_config(cid)

    @router.put("/{cid}")
    def actualizar(cid: int, payload: CajaUpdatePayload):
        if not db_caja.get_caja_config(cid):
            raise HTTPException(404, "Caja no encontrada")
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        try:
            db_caja.update_caja_config(
                cid, nombre, payload.descripcion.strip(), payload.medios_pago,
                1 if payload.activo else 0, punto_venta=payload.punto_venta,
            )
        except PuntoDeVentaRepetido as choque:
            raise HTTPException(409, str(choque)) from choque
        return db_caja.get_caja_config(cid)

    @router.post("/{cid}/set-default")
    def set_default(cid: int):
        if not db_caja.get_caja_config(cid):
            raise HTTPException(404, "Caja no encontrada")
        db_caja.set_default_caja(cid)
        return db_caja.get_caja_config(cid)

    @router.delete("/{cid}")
    def eliminar(cid: int):
        if not db_caja.get_caja_config(cid):
            raise HTTPException(404, "Caja no encontrada")
        try:
            db_caja.delete_caja_config(cid)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return {"ok": True}

    return router


# ── Turnos de caja ───────────────────────────────────────────────────────


class AbrirPayload(BaseModel):
    monto_inicial: float = 0
    notas: str = ""
    #: Sobre qué mostrador se abre. Opcional: sin cajas múltiples el turno se
    #: abre suelto, como siempre.
    caja_id: int | None = None


class CerrarPayload(BaseModel):
    monto_declarado: float = 0
    notas: str = ""


def _puede_ver(turno: dict, user: dict) -> bool:
    return user.get("role") == "admin" or turno["usuario_id"] == user.get("id")


def build_turnos_router(
    *,
    usuario_actual: Callable[..., Any],
    resumen_turno: Callable[[int], dict] = db_turnos.get_resumen_turno,
    cerrar_turno: Callable[[int, float, str], Any] = db_turnos.cerrar_turno,
    prefix: str = "/api/turnos",
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["turnos"])

    @router.get("")
    def listar(user: dict = Depends(usuario_actual)):
        es_admin = user.get("role") == "admin"
        return {
            "turnos": db_turnos.get_all_turnos(usuario_id=None if es_admin else user["id"]),
            "turno_activo": db_turnos.get_turno_activo(user["id"]),
        }

    @router.post("/abrir")
    def abrir(payload: AbrirPayload, user: dict = Depends(usuario_actual)):
        turno_activo = db_turnos.get_turno_activo(user["id"])
        if turno_activo:
            return turno_activo
        tid = db_turnos.create_turno(user["id"], payload.monto_inicial, payload.notas.strip(),
                                     caja_id=payload.caja_id)
        return db_turnos.get_turno(tid)

    @router.get("/{tid}")
    def detalle(tid: int, user: dict = Depends(usuario_actual)):
        turno = db_turnos.get_turno(tid)
        if not turno:
            raise HTTPException(404, "Turno no encontrado")
        if not _puede_ver(turno, user):
            raise HTTPException(403, "No autorizado")
        return {"turno": turno, "resumen": resumen_turno(tid)}

    @router.post("/{tid}/cerrar")
    def cerrar(tid: int, payload: CerrarPayload, user: dict = Depends(usuario_actual)):
        turno = db_turnos.get_turno(tid)
        if not turno:
            raise HTTPException(404, "Turno no encontrado")
        if not _puede_ver(turno, user):
            raise HTTPException(403, "No autorizado")
        if turno["estado"] != "abierto":
            raise HTTPException(422, "El turno ya está cerrado.")
        cerrar_turno(tid, payload.monto_declarado, payload.notas.strip())
        return db_turnos.get_turno(tid)

    return router
