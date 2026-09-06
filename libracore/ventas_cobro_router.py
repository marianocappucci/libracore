"""El cobro por QR de MercadoPago y la factura desde la venta, como factory
(P9-M3): `POST /{vid}/facturar`, `POST /{vid}/mp-qr`, `GET /{vid}/mp-status`.

Se monta con el **mismo prefijo** que el router de ventas de LibraCommerce
(`/api/ventas`): son las tres rutas de ahí que hablan de dinero y de
comprobantes, y por eso viven en este motor y no en aquél.

```python
app.include_router(
    build_cobro_de_ventas_router(ventas=PUERTO, usuario_actual=get_current_user_json),
    dependencies=[_auth_json, Depends(require_module("ventas"))],
)
```

Lo que difería entre las dos copias de los productos, y cómo quedó:

- Contalibra las tenía en el router legado (`/ventas/{id}/mp-qr`, con la
  sesión por cookie); Restolibra en `/api/ventas`. Queda `/api/ventas`, y el
  producto que necesite la ruta vieja monta la factory dos veces.
- `mp-qr` respondía `{"ok", "order_id"}` en uno y `{"ok", "total", "pos_id"}`
  en el otro. Queda el segundo —la SPA no usa `order_id`, y la referencia
  externa es lo que vuelve en el webhook—; la orden igual se guarda en la venta.
- Restolibra ya decidía el estado del pago con `pagos.estado_desde_mercadopago`
  (un `authorized` no acredita); Contalibra comparaba `== "approved"`. Queda el
  motor decidiendo.

`mercadopago` es el cliente HTTP: por default el de `libracore.mp_api`, resuelto
**en cada llamada** y no al montar, para que un `monkeypatch` sobre ese módulo
intercepte. Un producto cuya suite parchea su propio shim pasa el suyo.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from libracore import config_manager
from libracore import pagos as acreditacion
from libracore.venta_facturacion import (
    PuertoDeVentas,
    VentaNoFacturable,
    facturar_si_esta_prendida,
    facturar_venta,
)

logger = logging.getLogger(__name__)


async def _crear_orden_qr(**kw) -> dict:
    from libracore import mp_api

    return await mp_api.crear_orden_qr(**kw)


async def _buscar_pago_por_referencia(referencia: str, access_token: str) -> dict | None:
    from libracore import mp_api

    return await mp_api.buscar_pago_por_referencia(referencia, access_token)


@dataclass(frozen=True)
class ClienteMercadoPago:
    """Las dos llamadas a MercadoPago que hace el cobro por QR."""

    crear_orden_qr: Callable[..., Awaitable[dict]] = field(default=_crear_orden_qr)
    buscar_pago_por_referencia: Callable[[str, str], Awaitable[dict | None]] = field(
        default=_buscar_pago_por_referencia
    )


def build_cobro_de_ventas_router(
    *,
    ventas: PuertoDeVentas,
    usuario_actual: Callable[..., Any],
    prefix: str = "/api/ventas",
    mercadopago: ClienteMercadoPago | None = None,
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["ventas"])
    mp = mercadopago or ClienteMercadoPago()

    def _venta_o_404(vid: int) -> dict:
        venta = ventas.obtener(vid)
        if not venta:
            raise HTTPException(404, "Venta no encontrada")
        return venta

    @router.post("/{vid}/facturar")
    async def facturar(vid: int, user: dict = Depends(usuario_actual)):
        """Emite la factura de la venta y las vincula. Es el mismo camino que
        usa el webhook con la automática prendida, así que sirve también para
        reintentar una venta cuyo CAE falló. Idempotente."""
        try:
            factura = await facturar_venta(ventas, vid, usuario_id=user.get("id"))
        except VentaNoFacturable as e:
            raise HTTPException(422, str(e)) from None
        return {"venta": ventas.obtener(vid), "factura": factura}

    @router.post("/{vid}/mp-qr")
    async def venta_mp_qr(vid: int, user: dict = Depends(usuario_actual)):
        """Pone el monto de esta venta a cobrar en el QR de la caja.

        🔑 **No devuelve ninguna imagen, y no es un olvido.** Es el modelo de
        QR fijo por punto de venta: el cartel impreso del mostrador, que no
        cambia nunca. Lo que esta llamada cambia es *cuánto cobra* cuando
        alguien lo escanea.
        """
        venta = _venta_o_404(vid)
        cfg = config_manager.load()
        access_token = cfg.get("mp_access_token", "")
        pos_id = cfg.get("mp_pos_id", "")
        user_id = cfg.get("mp_user_id", "")
        if not access_token or not pos_id or not user_id:
            raise HTTPException(
                400,
                "Configurá el Access Token, el User ID y el POS ID de MercadoPago "
                "en Configuración → Integraciones.",
            )

        referencia = f"venta-{vid}"
        try:
            resultado = await mp.crear_orden_qr(
                user_id=user_id, pos_id=pos_id, access_token=access_token,
                external_reference=referencia, titulo=f"Venta {venta['numero']}",
                items=venta["items"], total=venta["total"],
            )
        except Exception as e:
            raise HTTPException(502, f"MercadoPago rechazó la orden: {e}") from None

        # MercadoPago contesta 204 sin cuerpo: la referencia externa es el único
        # identificador que queda de la orden, y alcanza porque es la misma con
        # la que vuelve el pago.
        order_id = referencia
        if isinstance(resultado, dict):
            order_id = resultado.get("in_store_order_id", referencia)
        ventas.set_orden_mp(vid, order_id)
        return {"ok": True, "total": venta["total"], "pos_id": pos_id}

    @router.get("/{vid}/mp-status")
    async def venta_mp_status(vid: int, user: dict = Depends(usuario_actual)):
        """¿Ya entró la plata del QR de esta venta?

        Es un GET **con efectos**: cuando MercadoPago dice `approved`, acredita
        el pago —escribe el movimiento de caja y recalcula el estado— y factura
        si la automática está prendida. Las dos cosas son idempotentes, así que
        el poll pegándole cada pocos segundos no duplica nada, y da igual si el
        webhook llegó primero.
        """
        venta = _venta_o_404(vid)
        usuario_id = user.get("id")

        if venta.get("mp_payment_id"):
            # Ya estaba acreditada. Se llama igual: cubre a las que se
            # acreditaron antes de que esto existiera, y a las que fallaron el
            # CAE la primera vez.
            ventas.acreditar(vid, venta["mp_payment_id"], usuario_id)
            factura_id = (venta.get("factura_id")
                          or await facturar_si_esta_prendida(ventas, vid))
            return {"status": "approved", "payment_id": venta["mp_payment_id"],
                    "factura_id": factura_id}

        cfg = config_manager.load()
        access_token = cfg.get("mp_access_token", "")
        if not access_token:
            raise HTTPException(400, "Access Token de MercadoPago no configurado.")

        try:
            pago = await mp.buscar_pago_por_referencia(f"venta-{vid}", access_token)
        except Exception as e:
            raise HTTPException(502, f"Sin respuesta de MercadoPago: {e}") from None

        if not pago:
            return {"status": "pending"}

        estado = acreditacion.estado_desde_mercadopago(pago.get("status"))
        if estado is not acreditacion.EstadoAcreditacion.APROBADO:
            # `authorized` y cualquier estado desconocido cuentan como
            # pendiente: lo decide el motor, no un `if` escrito acá.
            return {"status": pago.get("status") or "pending"}

        payment_id = str(pago["id"])
        ventas.set_pago_mp(vid, payment_id)
        # 🔴 Acá entra la plata a la caja, y no antes.
        ventas.acreditar(vid, payment_id, usuario_id)
        ventas.sellar_referencia_mp(vid, payment_id)
        factura_id = await facturar_si_esta_prendida(ventas, vid, cfg)
        return {"status": "approved", "payment_id": payment_id, "factura_id": factura_id}

    return router
