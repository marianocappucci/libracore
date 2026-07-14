"""
Cliente delgado sobre la API REST de MercadoPago, compartido por la app
principal de cada producto (Contalibra, Restolibra) — busqueda de pagos
para reconciliacion (bandeja MP), consulta de pago puntual (webhooks) y
QR Dinamico (crear/cancelar orden en el POS).
"""
import httpx
import datetime

MP_API_BASE = "https://api.mercadopago.com"


async def obtener_movimientos(access_token: str, begin_date: str, end_date: str) -> list:
    """
    Busca pagos aprobados en el rango de fechas via /v1/payments/search.
    Se usa para detectar cobros que no llegaron (o se perdieron) por webhook.
    begin_date y end_date en formato YYYY-MM-DD (horario Argentina UTC-3).
    """
    url = f"{MP_API_BASE}/v1/payments/search"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "sort":       "date_created",
        "criteria":   "desc",
        "begin_date": f"{begin_date}T00:00:00.000-03:00",
        "end_date":   f"{end_date}T23:59:59.999-03:00",
        "status":     "approved",
        "limit":      50,
        "offset":     0,
    }
    all_results = []
    max_pages   = 20
    async with httpx.AsyncClient(timeout=20) as client:
        for _ in range(max_pages):
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
            data    = r.json()
            results = data.get("results", [])
            all_results.extend(results)
            total = data.get("paging", {}).get("total", 0)
            if not results or len(all_results) >= total:
                break
            params["offset"] += len(results)
    return all_results


async def obtener_usuario_info(access_token: str) -> dict:
    """Devuelve el dict de /users/me (incluye id, email, nickname, etc.)."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{MP_API_BASE}/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        r.raise_for_status()
        return r.json()


async def obtener_pago(payment_id: str, access_token: str) -> dict:
    url = f"{MP_API_BASE}/v1/payments/{payment_id}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code == 404:
            raise ValueError(f"Pago {payment_id} no encontrado en MercadoPago.")
        r.raise_for_status()
        return r.json()


async def crear_orden_qr(
    user_id: str,
    pos_id: str,
    access_token: str,
    external_reference: str,
    titulo: str,
    items: list[dict],
    total: float,
) -> dict:
    """
    Crea una orden en el POS QR Dinámico de MP.
    Devuelve el dict de respuesta de MP (contiene 'in_store_order_id').
    Los ítems deben tener: title, quantity, unit_price.
    """
    url = f"{MP_API_BASE}/instore/qrs/merchant/stores/{{}}/pos/{pos_id}/orders"
    # MP usa: PUT /instore/qrs/merchant/stores/{store_id}/pos/{pos_id}/orders
    # pero con user_id como header o en la URL según versión de API.
    # La URL correcta para QR Dinámico V2:
    url = f"{MP_API_BASE}/instore/qrs/merchant/stores/default/pos/{pos_id}/orders"

    payload = {
        "external_reference": external_reference,
        "title": titulo,
        "description": titulo,
        "total_amount": round(total, 2),
        "items": [
            {
                "sku_number": str(it.get("producto_id") or idx),
                "category": "marketplace",
                "title": it["nombre"],
                "description": it.get("nombre", ""),
                "quantity": it["qty"],
                "unit_measure": "unit",
                "unit_price": round(it["precio"], 2),
                "total_amount": round(it["subtotal"], 2),
            }
            for idx, it in enumerate(items)
        ],
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.put(url, json=payload, headers=headers)
        if not r.is_success:
            raise RuntimeError(
                f"MP QR error {r.status_code}: {r.text[:300]}"
            )
        return r.json()


async def eliminar_orden_qr(pos_id: str, access_token: str) -> None:
    """Cancela la orden pendiente en el POS (deja el QR sin orden asignada)."""
    url = f"{MP_API_BASE}/instore/qrs/merchant/stores/default/pos/{pos_id}/orders"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=10) as client:
        await client.delete(url, headers=headers)


async def buscar_pago_por_referencia(
    external_reference: str, access_token: str
) -> dict | None:
    """
    Busca el último pago aprobado para una external_reference.
    Devuelve el dict del pago o None.
    """
    url = f"{MP_API_BASE}/v1/payments/search"
    params = {
        "external_reference": external_reference,
        "sort": "date_created",
        "criteria": "desc",
        "limit": 1,
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[0] if results else None
