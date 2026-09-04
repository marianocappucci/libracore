"""Factory del router JSON de Presupuestos, compartido por los productos.

Los siete endpoints (listar / crear / detalle / actualizar / estado / enviar-email
/ eliminar) eran **byte-idénticos** entre Contalibra y Restolibra: el único punto
que diverge es la **conversión a remito** (Contalibra elige valorizado/pelado;
Restolibra convierte plano), y eso entra por el callback `convertir_a_remito`.

Mismo criterio que `libracore.facturas_router.build_comprobantes_router` y
`libracore.remitos_router.build_remitos_router`: el dominio y el cálculo de
totales son de libracore; lo del producto (auth, PDF, conversión, envío de email,
formato de moneda, dónde se configura el SMTP) se inyecta.

> El PDF por descarga (`GET /presupuestos/{id}/pdf`) NO entra acá: sigue en el
> router propio de cada producto (descarga por cookie).

LibraDesk **no** usa este factory: su presupuesto es service-based (arista).
"""
import os
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from libracore import config_manager
from libracore.db import clients as _clients
from libracore.db import remitos_presupuestos as _rp
from libracore.facturas_router import calcular_totales

_ESTADOS_VALIDOS = {"borrador", "enviado", "aceptado", "rechazado", "vencido", "facturado"}


class _ItemPayload(BaseModel):
    description: str
    qty: float
    unit_price: float


class _PresupuestoPayload(BaseModel):
    date: str
    valid_until: str = ""
    client_id: int | None = None
    client_name: str = ""
    tax_rate: float = 0.21
    observations: str = ""
    items: list[_ItemPayload]


class _EstadoPayload(BaseModel):
    estado: str
    convertir_remito: bool = False
    # Se elige al convertir: el remito sale valorizado (con los precios del
    # presupuesto) o pelado. Restolibra ignora este flag (convierte plano).
    valorizado: bool = False


class _EmailPayload(BaseModel):
    email: str


def _resolver_cliente(client_id: int | None, client_name: str) -> dict:
    if client_id:
        c = _clients.get_client(client_id)
        if c:
            return {
                "client_name": c["name"], "client_address": c.get("address", ""),
                "client_cuit": c.get("cuit_dni", ""), "client_email": c.get("email", ""),
                "client_phone": c.get("phone", ""),
            }
    return {"client_name": client_name.strip(), "client_address": "", "client_cuit": "",
            "client_email": "", "client_phone": ""}


def _armar_items(items: list[_ItemPayload]) -> list[dict]:
    return [
        {"description": i.description.strip(), "qty": i.qty, "unit_price": i.unit_price,
         "subtotal": round(i.qty * i.unit_price, 2)}
        for i in items if i.description.strip()
    ]


def build_presupuestos_router(
    *,
    usuario_actual: Callable[..., Any],
    generar_pdf: Callable[[dict], str],
    convertir_a_remito: Callable[[dict, bool], None],
    smtp_configurado: Callable[[], bool],
    enviar_comprobante: Callable[..., None],
    moneda: Callable[[Any], str],
    donde_configurar_smtp: str = "Configuración → Email",
    prefix: str = "/api/presupuestos",
) -> APIRouter:
    """Los siete endpoints de presupuestos, con lo del producto inyectado.

    `convertir_a_remito(presupuesto, valorizado)` es la conversión del producto
    (Contalibra respeta `valorizado`; Restolibra lo ignora). `generar_pdf` arma el
    PDF del presupuesto. `enviar_comprobante` es el envío por SMTP del producto.
    `moneda` formatea importes para el cuerpo del mail.
    """
    router = APIRouter(prefix=prefix, tags=["presupuestos"])

    @router.get("")
    def listar(q: str = "", estado: str = ""):
        estado_f = estado if estado in _ESTADOS_VALIDOS else None
        items = _rp.search_presupuestos(q, estado_f) if q else _rp.get_all_presupuestos(200, estado_f)
        return {"items": items, "counts": _rp.get_presupuestos_count_by_estado()}

    @router.post("")
    def crear(payload: _PresupuestoPayload, user: dict = Depends(usuario_actual)):
        cliente = _resolver_cliente(payload.client_id, payload.client_name)
        if not cliente["client_name"]:
            raise HTTPException(422, "El nombre del cliente es requerido.")
        items = _armar_items(payload.items)
        if not items:
            raise HTTPException(422, "Debe agregar al menos un ítem válido.")
        totals = calcular_totales(items, payload.tax_rate)
        number = _rp.get_next_presupuesto_number()
        pres_id = _rp.create_presupuesto(
            number=number, date=payload.date, valid_until=payload.valid_until,
            client_id=payload.client_id, client_name=cliente["client_name"],
            client_address=cliente["client_address"], client_cuit=cliente["client_cuit"],
            client_email=cliente["client_email"], client_phone=cliente["client_phone"],
            items=items, subtotal=totals["subtotal"], tax_rate=payload.tax_rate,
            tax_amount=totals["iva_amount"], total=totals["total"],
            observations=payload.observations.strip(), usuario_id=user["id"],
        )
        pdf_path = generar_pdf(_rp.get_presupuesto(pres_id))
        _rp.update_presupuesto_pdf_path(pres_id, pdf_path)
        return _rp.get_presupuesto(pres_id)

    @router.get("/{pres_id}")
    def detalle(pres_id: int):
        pres = _rp.get_presupuesto(pres_id)
        if not pres:
            raise HTTPException(404, "Presupuesto no encontrado")
        return pres

    @router.put("/{pres_id}")
    def actualizar(pres_id: int, payload: _PresupuestoPayload):
        pres = _rp.get_presupuesto(pres_id)
        if not pres:
            raise HTTPException(404, "Presupuesto no encontrado")
        if pres["status"] != "borrador":
            raise HTTPException(400, "Solo se pueden editar presupuestos en estado borrador.")
        cliente = _resolver_cliente(payload.client_id, payload.client_name)
        if not cliente["client_name"]:
            raise HTTPException(422, "El nombre del cliente es requerido.")
        items = _armar_items(payload.items)
        if not items:
            raise HTTPException(422, "Debe agregar al menos un ítem válido.")
        totals = calcular_totales(items, payload.tax_rate)
        _rp.update_presupuesto(
            pres_id, date=payload.date, valid_until=payload.valid_until, status="borrador",
            client_id=payload.client_id, client_name=cliente["client_name"],
            client_address=cliente["client_address"], client_cuit=cliente["client_cuit"],
            client_email=cliente["client_email"], client_phone=cliente["client_phone"],
            items=items, subtotal=totals["subtotal"], tax_rate=payload.tax_rate,
            tax_amount=totals["iva_amount"], total=totals["total"],
            observations=payload.observations.strip(),
        )
        pdf_path = generar_pdf(_rp.get_presupuesto(pres_id))
        _rp.update_presupuesto_pdf_path(pres_id, pdf_path)
        return _rp.get_presupuesto(pres_id)

    @router.post("/{pres_id}/estado")
    def cambiar_estado(pres_id: int, payload: _EstadoPayload):
        pres = _rp.get_presupuesto(pres_id)
        if not pres:
            raise HTTPException(404, "Presupuesto no encontrado")
        if payload.estado not in _ESTADOS_VALIDOS:
            raise HTTPException(422, "Estado inválido.")
        _rp.update_presupuesto_status(pres_id, payload.estado)
        if payload.estado == "aceptado" and payload.convertir_remito:
            convertir_a_remito(pres, payload.valorizado)
        return _rp.get_presupuesto(pres_id)

    @router.post("/{pres_id}/enviar-email")
    def enviar_email(pres_id: int, payload: _EmailPayload):
        pres = _rp.get_presupuesto(pres_id)
        if not pres:
            raise HTTPException(404, "Presupuesto no encontrado")
        if not smtp_configurado():
            raise HTTPException(400, f"Configurá el servidor SMTP en {donde_configurar_smtp}.")
        if not payload.email.strip():
            raise HTTPException(422, "Ingresá una dirección de email.")
        cfg = config_manager.load()
        pdf_path = pres.get("pdf_path")
        if not pdf_path or not os.path.exists(pdf_path):
            pdf_path = generar_pdf(pres)
        doc_label = f"Presupuesto {pres['number']}"
        try:
            enviar_comprobante(
                to_email=payload.email.strip(), to_name=pres["client_name"], pdf_path=pdf_path,
                factura_label=doc_label, total=pres["total"],
                asunto=f"{doc_label} — {cfg.get('empresa_nombre', '')}",
                cuerpo=(
                    f"Estimado/a {pres['client_name']},\n\nAdjuntamos el presupuesto solicitado.\n\n"
                    f"Número: {pres['number']}\nTotal: $ {moneda(pres['total'])}\n"
                    f"Válido hasta: {pres['valid_until']}\n\nMuchas gracias.\n{cfg.get('empresa_nombre', '')}"
                ),
            )
        except Exception as e:
            raise HTTPException(502, f"Error al enviar: {e}")
        return {"ok": True}

    @router.delete("/{pres_id}")
    def eliminar(pres_id: int):
        try:
            _rp.delete_presupuesto(pres_id)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True}

    return router
