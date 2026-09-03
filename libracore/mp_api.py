"""
Cliente delgado sobre la API REST de MercadoPago, compartido por la app
principal de cada producto (Contalibra, Restolibra) — busqueda de pagos
para reconciliacion (bandeja MP), consulta de pago puntual (webhooks) y
QR Dinamico (crear/cancelar orden en el POS).
"""
import datetime

import httpx

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


def _url_orden_qr(user_id: str, pos_id: str) -> str:
    """La URL de la orden de una caja. Una sola definición para el PUT y el
    DELETE: cuando estaban escritas por separado, las dos estaban mal igual."""
    return f"{MP_API_BASE}/instore/qr/seller/collectors/{user_id}/pos/{pos_id}/orders"


async def crear_orden_qr(
    user_id: str,
    pos_id: str,
    access_token: str,
    external_reference: str,
    titulo: str,
    items: list[dict],
    total: float,
) -> dict:
    """Pone una orden a cobrar en el QR de una caja de MercadoPago.

    Es el modelo de **QR fijo por punto de venta**: el QR es el cartel impreso
    de la caja y no cambia nunca. Lo que esta llamada cambia es *cuánto cobra*
    ese QR cuando alguien lo escanea. No devuelve ninguna imagen de QR — no hay
    ninguna que mostrar.

    `pos_id` es el **`external_id`** de la caja, no su nombre ni su id numérico:
    una caja sin `external_id` cargado en MercadoPago no es direccionable por
    esta API. `user_id` es el **collector id** de la cuenta, el que devuelve
    `GET /users/me`.

    Los ítems deben tener: nombre, qty, precio, subtotal.

    Devuelve el cuerpo de la respuesta, que en la práctica viene **vacío**
    (MercadoPago contesta 204): se devuelve `{}` y el caller no debe esperar
    campos adentro.
    """
    # 🔴 Hasta el 2026-08-19 esto pegaba a
    # `/instore/qrs/merchant/stores/default/pos/{pos_id}/orders`, que **no
    # existe**: contra una cuenta real da 404. El código llegaba a esa URL
    # asignándola dos veces seguidas, con un comentario que dudaba de si el
    # user_id iba en la URL o en un header — se escribió de memoria y nunca se
    # ejercitó contra MercadoPago.
    #
    # La forma de acá abajo se determinó **probándola** contra la cuenta real
    # (respondió 204, y el DELETE de la misma URL también). Y explica el
    # parámetro que sobraba: el collector id va en el path, así que `user_id`
    # dejó de estar sin uso.
    url = _url_orden_qr(user_id, pos_id)

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
        # MercadoPago contesta 204 sin cuerpo. `r.json()` sobre eso levanta, y
        # era la línea siguiente al 404: arreglar sólo la URL habría cambiado un
        # error por otro.
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}


async def eliminar_orden_qr(user_id: str, pos_id: str, access_token: str) -> None:
    """Saca la orden pendiente de la caja: el QR impreso queda sin nada que cobrar.

    Cambió la firma el 2026-08-19 — ahora pide `user_id`, porque el collector id
    va en la URL. No rompe a nadie: ningún producto la llamaba, sólo la
    re-exportaban los shims de `app/mp_api.py`.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=10) as client:
        await client.delete(_url_orden_qr(user_id, pos_id), headers=headers)


#: Los tres archivos que MercadoPago publica por cada caja, y con qué nombre los
#: expone esta familia.
#:
#: 🔑 Las tres URLs son **públicas**: se sirven sin `Authorization` (medido
#: contra la cuenta real el 2026-08-31). Eso es coherente con lo que son —un
#: cartel para pegar en el mostrador—, pero significa que la URL **es** el
#: cartel: quien la tenga puede imprimir el QR que cobra en esa cuenta. Por eso
#: el router no la devuelve al navegador, sino que trae los bytes él.
FORMATOS_QR: dict[str, tuple[str, str, str]] = {
    # nombre nuestro   clave en MP           content-type       extensión
    "qr":     ("image",             "image/png",       "png"),
    "cartel": ("template_image",    "image/png",       "png"),
    "pdf":    ("template_document", "application/pdf", "pdf"),
}


async def obtener_pos(external_id: str, access_token: str) -> dict | None:
    """La caja de MercadoPago, buscada por su `external_id`.

    `external_id` es lo único que guarda la configuración del producto
    (`mp_pos_id`), y es además lo que va en la URL de la orden — ver
    `crear_orden_qr`. El **id numérico** de la caja no se guarda en ningún lado,
    así que para llegar al QR hay que pasar por acá.

    Devuelve el dict de la caja, o `None` si esa cuenta no tiene ninguna con ese
    `external_id`. **`None` no es un error de red**: es el caso vivo de un token
    de producción con el `pos_id` de la caja de prueba (o al revés), y quien
    llama tiene que poder decirlo con esas palabras.

    🔴 **El filtro de MercadoPago es exacto pero NO distingue mayúsculas**
    —medido: `?external_id=contadev` devuelve `CONTADEV`—. Se acepta, porque es
    la caja que el operador quiso nombrar, pero **se devuelve la de MercadoPago
    con su `external_id` canónico**: si la pantalla mostrara el texto tipeado,
    una configuración con la caja escrita distinto se vería idéntica a una bien
    escrita.

    Y se exige **un solo resultado**: hoy el filtro devuelve 0 ó 1, pero un
    `external_id` vacío devuelve la lista entera, y "tomar el primero" ahí es
    mostrar el QR de una caja cualquiera de la cuenta —que en esta familia
    significa el de otro producto—.
    """
    external_id = (external_id or "").strip()
    if not external_id:
        return None
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{MP_API_BASE}/pos",
            params={"external_id": external_id},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        r.raise_for_status()
        cajas = [
            c for c in (r.json().get("results") or [])
            if str(c.get("external_id") or "").casefold() == external_id.casefold()
        ]
    return cajas[0] if len(cajas) == 1 else None


async def descargar_archivo_qr(url: str) -> bytes:
    """Trae los bytes de uno de los archivos del QR de la caja.

    Va sin `Authorization`: son públicas. Existe para que **el motor** sea quien
    salga a buscarlas y no el navegador — así la URL no viaja a la pantalla, el
    archivo baja con un nombre que se entiende, y una instancia detrás de una
    red que no llega a `mercadopago.com` sigue pudiendo imprimir el cartel.
    """
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


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
