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

`facturacion_habilitada` es `() -> bool`, default siempre `True` (lo de hoy).
Existe porque VentaLibra tiene un módulo `facturacion` que se puede apagar
(`ModuleRepository`, concepto del producto) independiente de `mp_auto_facturar_ventas`
de `config_manager`: con la automática prendida y el módulo apagado, `mp-status`
facturaba igual. En `False`, ni `mp-status` ni el manual (`POST /{vid}/facturar`)
emiten un comprobante.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from libracore import config_manager
from libracore import pagos as acreditacion
from libracore.db import caja as db_caja
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


def _siempre_facturable() -> bool:
    return True


#: Cuando el pago entró en MercadoPago pero la venta ya no existe (se anuló
#: entre que se generó el QR y que se acreditó). No hay UPDATE que revierta
#: eso automáticamente — alguien tiene que devolver la plata a mano.
MENSAJE_PAGO_ANULADA = "El pago entró en MercadoPago pero la venta está anulada: hay que devolverlo a mano."


def _venta_anulada(venta: dict) -> bool:
    """Mismo criterio que `venta_facturacion.facturar_venta`: `status` es el
    crudo y `estado` puede venir pisado por `status_detail`."""
    return venta.get("status") == "cancelled" or venta.get("estado") == "anulada"


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
    facturacion_habilitada: Callable[[], bool] = _siempre_facturable,
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["ventas"])
    mp = mercadopago or ClienteMercadoPago()

    def _venta_o_404(vid: int) -> dict:
        venta = ventas.obtener(vid)
        if not venta:
            raise HTTPException(404, "Venta no encontrada")
        return venta

    # 🔴 Las tres rutas son `def` y no `async def`, a propósito: uvicorn corre
    # con UN solo proceso. Como corrutinas, el puerto de ventas —la base del
    # producto—, `config.json` y, al facturar, la firma del TRA con `openssl`
    # por subproceso y el PDF frenaban el loop entero mientras duraban; y
    # `mp-status` se consulta cada pocos segundos mientras el cliente escanea.
    # Como `def` corren en el threadpool. Lo asincrónico de verdad
    # —MercadoPago, y `facturar_venta`, que es async sólo en los bordes de
    # red— va con `asyncio.run` en un loop propio de ese hilo: lo sincrónico
    # de adentro bloquea a ese hilo y a nadie más.

    @router.post("/{vid}/facturar")
    def facturar(vid: int, user: dict = Depends(usuario_actual)):
        """Emite la factura de la venta y las vincula. Es el mismo camino que
        usa el webhook con la automática prendida, así que sirve también para
        reintentar una venta cuyo CAE falló. Idempotente.

        403 si `facturacion_habilitada` dice que el módulo está apagado —
        mismo código que usa `modules_gate.require_module` para "módulo no
        incluido": es la misma clase de rechazo, funcionalidad deshabilitada
        por configuración de la instancia, no un conflicto sobre el estado de
        la venta."""
        if not facturacion_habilitada():
            raise HTTPException(403, "El módulo de facturación está deshabilitado para esta instancia.")
        try:
            factura = asyncio.run(facturar_venta(ventas, vid, usuario_id=user.get("id")))
        except VentaNoFacturable as e:
            raise HTTPException(422, str(e)) from None
        return {"venta": ventas.obtener(vid), "factura": factura}

    @router.post("/{vid}/mp-qr")
    def venta_mp_qr(vid: int, user: dict = Depends(usuario_actual)):
        """Pone el monto de esta venta a cobrar en el QR de la caja.

        🔑 **No devuelve ninguna imagen, y no es un olvido.** Es el modelo de
        QR fijo por punto de venta: el cartel impreso del mostrador, que no
        cambia nunca. Lo que esta llamada cambia es *cuánto cobra* cuando
        alguien lo escanea.

        409 sin llamar a MercadoPago si la venta está anulada (pedir plata por
        algo que ya no existe) o si ya fue cobrada por QR —`mp_payment_id` ya
        seteado— (pedir de nuevo una plata que ya entró). Una venta con un
        pago electrónico declarado `aprobado` pero sin `mp_payment_id` TODAVÍA
        puede pedir el QR: es el flujo de "Cobrar con QR" del detalle
        (`libra-ui/src/comercio/VentaDetalle.tsx`, `puedeCobrarConQr`), que
        necesita esa fila de pago para poder sellar la referencia después.
        """
        venta = _venta_o_404(vid)
        cfg = config_manager.load()
        access_token = cfg.get("mp_access_token", "")
        user_id = cfg.get("mp_user_id", "")
        if not access_token or not user_id:
            raise HTTPException(
                400,
                "Configurá el Access Token y el User ID de MercadoPago "
                "en Configuración → Integraciones.",
            )

        pos_id = db_caja.mp_pos_id_con_fallback(
            user.get("id"), cfg.get("mp_pos_id")
        )
        if not pos_id:
            raise HTTPException(
                422,
                "La caja activa no tiene un POS de MercadoPago configurado. "
                "Configurá el POS ID en la caja antes de cobrar con QR.",
            )

        if _venta_anulada(venta):
            raise HTTPException(409, f"La venta {vid} está anulada: no se puede generar un cobro por QR.")
        if venta.get("mp_payment_id"):
            raise HTTPException(409, f"La venta {vid} ya fue cobrada por QR.")

        referencia = f"venta-{vid}"
        try:
            resultado = asyncio.run(mp.crear_orden_qr(
                user_id=user_id, pos_id=pos_id, access_token=access_token,
                external_reference=referencia, titulo=f"Venta {venta['numero']}",
                items=venta["items"], total=venta["total"],
            ))
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
    def venta_mp_status(vid: int, user: dict = Depends(usuario_actual)):
        """¿Ya entró la plata del QR de esta venta?

        Es un GET **con efectos**: cuando MercadoPago dice `approved`, acredita
        el pago —escribe el movimiento de caja y recalcula el estado— y factura
        si la automática está prendida (y el módulo de facturación no está
        apagado). Las dos cosas son idempotentes, así que el poll pegándole
        cada pocos segundos no duplica nada, y da igual si el webhook llegó
        primero.

        Si la venta está anulada, `"status": "anulada"` en vez de `"approved"`
        y no factura — la plata pudo entrar en MercadoPago después de anularse
        (`libracommerce.erp.ventas.acreditar_pago_qr` no toca nada en ese
        caso, y avisa por log que hay que devolverla a mano).
        """
        venta = _venta_o_404(vid)
        usuario_id = user.get("id")
        anulada = _venta_anulada(venta)

        if venta.get("mp_payment_id"):
            if anulada:
                return {"status": "anulada", "payment_id": venta["mp_payment_id"],
                        "message": MENSAJE_PAGO_ANULADA}
            # Ya estaba acreditada. Se llama igual: cubre a las que se
            # acreditaron antes de que esto existiera, y a las que fallaron el
            # CAE la primera vez.
            ventas.acreditar(vid, venta["mp_payment_id"], usuario_id)
            factura_id = venta.get("factura_id")
            if not factura_id and facturacion_habilitada():
                factura_id = asyncio.run(facturar_si_esta_prendida(ventas, vid))
            return {"status": "approved", "payment_id": venta["mp_payment_id"],
                    "factura_id": factura_id}

        cfg = config_manager.load()
        access_token = cfg.get("mp_access_token", "")
        if not access_token:
            raise HTTPException(400, "Access Token de MercadoPago no configurado.")

        try:
            pago = asyncio.run(mp.buscar_pago_por_referencia(f"venta-{vid}", access_token))
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
        if anulada:
            return {"status": "anulada", "payment_id": payment_id, "message": MENSAJE_PAGO_ANULADA}
        # 🔴 Acá entra la plata a la caja, y no antes.
        ventas.acreditar(vid, payment_id, usuario_id)
        ventas.sellar_referencia_mp(vid, payment_id)
        factura_id = (asyncio.run(facturar_si_esta_prendida(ventas, vid, cfg))
                      if facturacion_habilitada() else None)
        return {"status": "approved", "payment_id": payment_id, "factura_id": factura_id}

    return router
