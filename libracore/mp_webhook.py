"""El webhook de MercadoPago: la notificación que convierte un cobro en una
factura sin que nadie esté mirando.

Es el más delicado de los cuatro caminos porque **es público** —lo llama
MercadoPago desde internet— y porque corre solo. Las cuatro reglas que lo
gobiernan, y que estaban escritas tres veces con tres criterios distintos:

1. 🔴 **La firma es lo único que separa una notificación real de una inventada.**
   Si hay `mp_webhook_secret` configurado, una firma que no valida se rechaza
   con 400. Sin secret no se puede verificar nada — y eso es el estado por
   omisión de una instancia recién dada de alta, así que la mitigación es la
   regla 2.
2. 🔑 **El estado se le pregunta a MercadoPago; el cuerpo de la notificación no
   se cree.** El payload sólo aporta el id del pago. Todo lo demás —importe,
   pagador, estado— sale de consultar la API con el access token.
3. **Contesta 200 casi siempre, y no es descuido.** MercadoPago reintenta ante
   cualquier código que no sea 2xx, así que devolver 500 por un error propio
   convierte un problema en una tormenta de reintentos. Se contesta 200 y el
   error queda en el log. Las dos excepciones son el JSON ilegible y la firma
   inválida: ahí el 400 es correcto porque el reintento tampoco va a servir.
4. **Idempotencia.** Un mismo `payment_id` no puede generar dos facturas. MP
   reintenta la misma notificación, y sin este corte el reintento duplicaría el
   comprobante.

## Las dos costuras

Lo que no es igual en todos los productos entra por parámetro, no por copia:

- `manejadores_de_referencia` — qué hacer cuando el pago trae un
  `external_reference` con cierto prefijo. Es como Contalibra reconoce el cobro
  de una **venta presencial por QR** (`venta-123`) y lo aplica a esa venta en
  vez de tratarlo como una suscripción.
- `debe_auto_facturar` — la regla de negocio de cuándo facturar solo. Por
  omisión es la bandera `auto_facturar` del cliente, que es lo que hacen todos;
  Contalibra le suma su regla de *"Hosting Mensual"*, que es su propio negocio
  y no tiene por qué vivir en el motor.
"""

import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from libracore import config_manager, mp_api, mp_facturacion
from libracore.db import mp as db_mp

logger = logging.getLogger(__name__)


def verificar_firma(x_signature: str, x_request_id: str,
                    payment_id: str, secret: str) -> bool:
    """El HMAC que manda MercadoPago en `x-signature`.

    ⚠️ La plantilla es exactamente `id:...;request-id:...;ts:...` y el orden
    importa: cualquier otro armado da un digest distinto y rechaza pagos reales.

    Se compara con `compare_digest` y no con `==` — la comparación de strings
    corta en el primer byte distinto y filtra el largo del prefijo correcto.
    """
    ts = v1 = ""
    for parte in x_signature.split(","):
        parte = parte.strip()
        if parte.startswith("ts="):
            ts = parte[3:]
        elif parte.startswith("v1="):
            v1 = parte[3:]
    if not ts or not v1:
        return False
    plantilla = f"id:{payment_id};request-id:{x_request_id};ts:{ts}"
    esperado = hmac.new(secret.encode(), plantilla.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, v1)


def _auto_facturar_por_bandera(client: dict, contexto: dict) -> bool:
    """La regla por omisión: la bandera del cliente y nada más."""
    return bool(client.get("auto_facturar"))


def _datos_del_pagador(pago: dict) -> dict:
    payer = pago.get("payer", {}) or {}
    identificacion = payer.get("identification", {}) or {}
    email = payer.get("email", "") or ""
    nombre = f"{payer.get('first_name', '')} {payer.get('last_name', '')}".strip()
    return {
        "payer_email": email,
        "payer_name": nombre or email,
        "payer_id_type": identificacion.get("type", "") or "",
        "payer_id_number": identificacion.get("number", "") or "",
    }


def build_mp_webhook_router(
    *,
    prefix: str = "",
    ruta: str = "/webhooks/mercadopago",
    manejadores_de_referencia: dict[
        str, Callable[[int, dict, dict], Awaitable[int | None]]
    ] | None = None,
    debe_auto_facturar: Callable[[dict, dict], bool] = _auto_facturar_por_bandera,
) -> APIRouter:
    """El router del webhook. **Va sin gate de rol**: lo llama MercadoPago, no
    un usuario logueado. Lo que lo protege es la firma, no una cookie."""
    router = APIRouter(prefix=prefix, tags=["mercadopago"])
    manejadores = manejadores_de_referencia or {}

    @router.post(ruta, include_in_schema=False)
    async def webhook_mercadopago(request: Request):
        body = await request.body()
        cfg = config_manager.load()
        access_token = cfg.get("mp_access_token", "")
        secret = cfg.get("mp_webhook_secret", "")

        if not access_token:
            # 200 a propósito: la instancia no tiene MercadoPago configurado, y
            # que MP reintente no lo va a arreglar.
            logger.warning("Webhook de MercadoPago sin access_token configurado.")
            return JSONResponse({"ok": False, "error": "not configured"}, status_code=200)

        try:
            payload = json.loads(body)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

        if payload.get("type", "") != "payment":
            return JSONResponse({"ok": True, "msg": "ignored"}, status_code=200)

        payment_id = str(payload.get("data", {}).get("id", "") or "")
        if not payment_id:
            return JSONResponse({"ok": False, "error": "no payment id"}, status_code=400)

        if secret:
            firma = request.headers.get("x-signature", "")
            pedido = request.headers.get("x-request-id", "")
            if not firma or not verificar_firma(firma, pedido, payment_id, secret):
                logger.warning("Firma de MercadoPago invalida para %s — rechazado", payment_id)
                return JSONResponse({"ok": False, "error": "invalid signature"}, status_code=400)

        if db_mp.get_mp_pago(payment_id):
            return JSONResponse({"ok": True, "msg": "already processed"}, status_code=200)

        try:
            pago = await mp_api.obtener_pago(payment_id, access_token)
        except Exception as e:
            logger.error("Error obteniendo el pago %s de MercadoPago: %s", payment_id, e)
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

        estado = pago.get("status", "")
        datos = _datos_del_pagador(pago)
        monto = float(pago.get("transaction_amount", 0) or 0)

        # ── Un pago que pertenece a otra cosa del producto ───────────────────
        referencia = pago.get("external_reference", "") or ""
        for prefijo, manejador in manejadores.items():
            if not referencia.startswith(prefijo):
                continue
            try:
                identificador = int(referencia[len(prefijo):])
            except ValueError:
                break
            factura_id = None
            if estado == "approved":
                try:
                    factura_id = await manejador(identificador, pago, cfg)
                except Exception as e:
                    # El cobro ya está hecho: perderlo sería peor que quedarse
                    # sin la factura, que se puede emitir después a mano.
                    logger.error("Error manejando %s%s: %s", prefijo, identificador, e)
            db_mp.create_mp_pago(
                mp_payment_id=payment_id, status=estado, monto=monto,
                payer_email=datos["payer_email"], payer_name="",
                factura_id=factura_id,
                estado_factura="facturado" if factura_id else None,
            )
            return JSONResponse(
                {"ok": True, "msg": f"{prefijo}{identificador} {estado}"}, status_code=200
            )

        # ── El camino normal: un cobro suelto ────────────────────────────────
        descripcion = pago.get("description", "") or ""
        payment_type = pago.get("payment_type_id", "") or ""
        db_mp.create_mp_pago(
            mp_payment_id=payment_id, status=estado, monto=monto,
            factura_id=None,
            # Aprobado entra a la bandeja como pendiente de facturar; el resto
            # se registra nada más, para que quede el rastro del intento.
            estado_factura="pendiente" if estado == "approved" else None,
            payment_type=payment_type,
            payment_method=pago.get("payment_method_id", "") or "",
            descripcion_mp=descripcion,
            **datos,
        )

        if estado != "approved":
            return JSONResponse({"ok": True, "msg": f"status={estado}"}, status_code=200)

        logger.info("Pago de MercadoPago aprobado %s por %.2f", payment_id, monto)

        # 🔑 El cliente sale de `resolver_cliente_pago` —alias primero— y no de
        # un match propio. Es el punto único que comparten los cuatro caminos.
        client = db_mp.resolver_cliente_pago(datos["payer_email"], datos["payer_id_number"])
        contexto = {"descripcion": descripcion, "pago": pago, "monto": monto}

        if client and debe_auto_facturar(client, contexto):
            try:
                factura_id, numero, tipo_lb, _ = await mp_facturacion.generar_factura_mp(
                    monto=monto,
                    payer_email=client.get("email") or datos["payer_email"],
                    payer_name=client["name"],
                    referencia=f"MP#{payment_id}",
                    cfg=cfg,
                    concepto_override=descripcion,
                    cliente_override=client,
                    payment_type=payment_type,
                )
                db_mp.update_mp_pago_estado(
                    db_mp.get_mp_pago(payment_id)["id"], "facturado", factura_id,
                )
                logger.info(
                    "Auto-factura %s %s para el pago %s (%s)",
                    tipo_lb, numero, payment_id, client["name"],
                )
            except Exception as e:
                # Queda en la bandeja como pendiente: alguien la emite a mano.
                logger.error("Error auto-facturando el pago %s: %s", payment_id, e)

        return JSONResponse({"ok": True, "msg": f"status={estado}"}, status_code=200)

    return router
