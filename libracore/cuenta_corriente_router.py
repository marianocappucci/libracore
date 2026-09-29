"""Cuenta corriente por cliente como factory de router (P9-M4, 2026-09-07).

`web/api/cuenta_corriente.py` de Contalibra y Restolibra difería en una cosa
real: Contalibra emite el **recibo de cobranza** al registrar el pago y lo
anula al borrarlo. Eso es `con_recibos=True`. Lo demás era igual: el listado con
la deuda total, el detalle, el pago (con su movimiento de caja si eligió caja)
y la baja del pago, que gatea `solo_admin`.

`origen` dice de qué tabla salen los débitos por venta (`OrigenVentas` de
`libracore.db.cuenta_corriente`): los productos con LibraCommerce pasan
`VENTAS_LIBRACOMMERCE`.

```python
app.include_router(
    build_cuenta_corriente_router(
        usuario_actual=get_current_user_json, solo_admin=require_admin_json,
        origen=VENTAS_LIBRACOMMERCE, con_recibos=True,
    ),
    dependencies=[_auth_json, Depends(require_module("cuenta_corriente"))],
)
```

## Variantes de producto: `OpcionesCuentaCorriente`

Un producto cuyo cobro tiene reglas propias las declara con `opciones=` y **no reescribe el router**
(VentaLibra, ADR-027: el arqueo es por turno). Todos los ganchos son opcionales y, sin ellos, el
comportamiento es el de siempre (el de Contalibra y Restolibra):

- `validar_pago(payload, user) -> CobroAprobado`: se llama antes de registrar el pago; levanta
  `HTTPException` para rechazarlo (409 sin turno, 422 con una caja ajena, medio no cobrable...) y decide
  la caja, el turno y la referencia del movimiento de caja.
- `cajas(user) -> list[dict]`: qué cajas ofrece el selector de `GET /cajas` (por defecto, todas).
- `al_eliminar_pago(pago_id, user)`: se llama al dar de baja un pago, antes de anular sus recibos y de
  borrarlo; es donde un producto anula el movimiento de caja que el pago generó (puede levantar
  `HTTPException` y entonces no se toca nada).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from libracore.db import caja as db_caja
from libracore.db import clients as db_clients
from libracore.db import cuenta_corriente as db_cc
from libracore.db import recibos as db_recibos
from libracore.db.cuenta_corriente import VENTAS_LIBRACORE, OrigenVentas

logger = logging.getLogger(__name__)


class PagoCCPayload(BaseModel):
    monto: float
    fecha: str
    concepto: str = "Pago a cuenta"
    referencia: str = ""
    medio_pago: str = "efectivo"
    caja_id: int | None = None
    #: Facturas a las que se aplica el pago. Un pago sin facturas baja el saldo
    #: pero deja cada factura en "Sin cobrar": sólo el cobro de una factura
    #: escribe el movimiento de caja que la marca "Cobrada".
    facturas: list[int] = []


@dataclass(frozen=True)
class CobroAprobado:
    """Lo que `validar_pago` decide de un pago que aprueba."""

    #: La caja donde queda el pago y su movimiento de caja (`None`: sin caja, como Contalibra sin selector).
    caja_id: int | None
    #: El turno al que pertenece el movimiento de caja (para el arqueo por turno).
    turno_id: int | None = None
    #: Plantilla de la referencia del movimiento de caja, con `{pago_id}` (p. ej. `"cc-pago-{pago_id}"`).
    #: `None`: la referencia que escribió el usuario, como Contalibra.
    referencia_movimiento: str | None = None


@dataclass(frozen=True)
class OpcionesCuentaCorriente:
    """Las variantes de un producto sobre el router. Todo opcional: ver el docstring del módulo."""

    validar_pago: Callable[[PagoCCPayload, dict], CobroAprobado] | None = None
    cajas: Callable[[dict], list[dict]] | None = None
    al_eliminar_pago: Callable[[int, dict], None] | None = None


def _repartir(payload: PagoCCPayload, pendientes: list[dict]) -> list[tuple[dict, float]]:
    """Reparte el pago entre las facturas elegidas, la más vieja primero.

    Cada factura recibe como mucho su pendiente; lo que sobra queda como pago a
    cuenta. Levanta 422 si se pide una factura que no está pendiente de este
    cliente (ya cobrada, anulada, de otro cliente): aplicarle plata sería
    inventar un cobro.
    """
    if not payload.facturas:
        return []
    por_id = {f["id"]: f for f in pendientes}
    invalidas = [i for i in payload.facturas if i not in por_id]
    if invalidas:
        raise HTTPException(
            422, f"Factura sin cobro pendiente en esta cuenta: {', '.join(map(str, invalidas))}"
        )
    restante = round(payload.monto, 2)
    aplicaciones = []
    for factura in sorted((por_id[i] for i in set(payload.facturas)),
                          key=lambda f: (f["fecha"], f["id"])):
        if restante <= 0:
            break
        monto = min(restante, factura["pendiente"])
        aplicaciones.append((factura, monto))
        restante = round(restante - monto, 2)
    return aplicaciones


def build_cuenta_corriente_router(
    *,
    usuario_actual: Callable[..., Any],
    solo_admin: Callable[..., Any],
    origen: OrigenVentas = VENTAS_LIBRACORE,
    con_recibos: bool = False,
    prefix: str = "/api/cuenta-corriente",
    opciones: OpcionesCuentaCorriente | None = None,
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["cuenta_corriente"])
    opciones = opciones or OpcionesCuentaCorriente()

    @router.get("")
    def listar():
        clientes = db_cc.get_clientes_con_saldo_cc(origen=origen)
        total_deuda = sum(c["saldo"] for c in clientes if c["saldo"] > 0)
        return {"clientes": clientes, "total_deuda": total_deuda}

    @router.get("/cajas")
    def listar_cajas(user: dict = Depends(usuario_actual)):
        """Sólo lectura, para el selector de caja del pago (o las que el producto decida ofrecer)."""
        if opciones.cajas is not None:
            return opciones.cajas(user)
        return db_caja.get_all_cajas()

    @router.get("/{cliente_id}")
    def detalle(cliente_id: int):
        cliente = db_clients.get_client(cliente_id)
        if not cliente:
            raise HTTPException(404, "Cliente no encontrado")
        return {
            "cliente": cliente,
            "movimientos": db_cc.get_cc_movimientos(cliente_id, origen=origen),
            "saldo": db_cc.get_cc_saldo(cliente_id, origen=origen),
            "facturas_pendientes": db_cc.get_facturas_pendientes_cc(cliente_id),
        }

    @router.post("/{cliente_id}/pagar")
    def pagar(cliente_id: int, payload: PagoCCPayload, user: dict = Depends(usuario_actual)):
        cliente = db_clients.get_client(cliente_id)
        if not cliente:
            raise HTTPException(404, "Cliente no encontrado")

        # Se decide a qué facturas va el pago ANTES de escribir nada: un id que
        # no está pendiente es un error del pedido y no puede dejar un pago a medias.
        aplicaciones = _repartir(payload, db_cc.get_facturas_pendientes_cc(cliente_id))

        # El producto puede rechazar el pago (HTTPException) y decidir la caja, el turno y la referencia.
        cobro = (opciones.validar_pago(payload, user) if opciones.validar_pago is not None
                 else CobroAprobado(caja_id=payload.caja_id))

        pago_id = db_cc.create_cc_pago(
            cliente_id=cliente_id, monto=payload.monto, fecha=payload.fecha,
            concepto=payload.concepto, referencia=payload.referencia,
            medio_pago=payload.medio_pago, caja_id=cobro.caja_id, usuario_id=user.get("id"),
        )

        referencia = (cobro.referencia_movimiento.format(pago_id=pago_id)
                      if cobro.referencia_movimiento else payload.referencia)
        # Una fila de caja por factura, con el mismo monto y medio que el pago:
        # es el "Registrar cobro" de cada comprobante, pero sin un segundo abono
        # (el pago a cuenta ya es el abono). La plata entra a caja una sola vez.
        for factura, monto in aplicaciones:
            db_caja.create_caja_movimiento(
                fecha=payload.fecha, tipo="ingreso",
                concepto=f"Cobro {factura['concepto']} — {cliente['name']}",
                monto=monto, referencia=referencia, factura_id=factura["id"],
                caja_id=cobro.caja_id, medio_pago=payload.medio_pago,
                usuario_id=user.get("id"), turno_id=cobro.turno_id,
                cc_pago_id=pago_id,
            )
        # Lo que no cubre ninguna factura sigue siendo un pago a cuenta suelto.
        resto = round(payload.monto - sum(m for _, m in aplicaciones), 2)
        if cobro.caja_id and resto > 0:
            db_caja.create_caja_movimiento(
                fecha=payload.fecha, tipo="ingreso", concepto=f"Pago CC - {cliente['name']}",
                monto=resto, referencia=referencia,
                caja_id=cobro.caja_id, medio_pago=payload.medio_pago, usuario_id=user.get("id"),
                turno_id=cobro.turno_id, cc_pago_id=pago_id,
            )

        respuesta = {
            "movimientos": db_cc.get_cc_movimientos(cliente_id, origen=origen),
            "saldo": db_cc.get_cc_saldo(cliente_id, origen=origen),
            "facturas_pendientes": db_cc.get_facturas_pendientes_cc(cliente_id),
        }
        if con_recibos:
            # El recibo sale con el cobro, no cuando alguien se acuerda: quien
            # acaba de cobrar tiene al cliente enfrente esperando el papel. Si
            # fallara la emisión, el cobro **ya está registrado** y no se
            # revierte: perder el comprobante es molesto, perder el pago es un
            # problema de plata. El botón por movimiento lo vuelve a intentar.
            from libracore.recibos import emitir_recibo_cobranza

            recibo_id = None
            try:
                recibo_id = emitir_recibo_cobranza(pago_id, usuario_id=user.get("id"))["id"]
            except Exception:
                logger.exception("No se pudo emitir el recibo del cc_pago %s", pago_id)
            respuesta["recibo_id"] = recibo_id
        return respuesta

    @router.delete("/pagos/{pago_id}", dependencies=[Depends(solo_admin)])
    def eliminar_pago(pago_id: int, user: dict = Depends(usuario_actual)):
        if opciones.al_eliminar_pago is not None:
            # Antes de tocar nada: si el producto no puede deshacer el movimiento de caja, rechaza.
            opciones.al_eliminar_pago(pago_id, user)
        if con_recibos:
            # Anular el recibo ANTES de borrar el pago: si el pago se va primero,
            # su recibo queda apuntando a una fila que no existe y sigue
            # figurando como vigente. Se anula en vez de borrarse porque el
            # papel pudo haber salido impreso, y el número queda consumido.
            for recibo in db_recibos.get_recibos_de_origen(db_recibos.ORIGEN_CC_PAGO, pago_id):
                db_recibos.anular_recibo(recibo["id"], motivo="Se elimino el pago que lo origino",
                                         usuario_id=user.get("id"))
        # Los movimientos de caja del pago (los cobros por factura y el resto suelto)
        # se anulan en el motor, igual para todos los productos: el gancho de arriba
        # sólo decide si la baja procede. Sin esto, borrar un pago dejaba las facturas
        # "Cobradas" y la plata en el arqueo.
        db_caja.anular_movimientos_de_cc_pago(pago_id)
        db_cc.delete_cc_pago(pago_id)
        return {"ok": True}

    return router
