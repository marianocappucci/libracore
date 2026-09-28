"""Recibos (comprobante de cobro) como factory de router. Extraído de Contalibra
(`app/web/api/recibos.py`, el mismo archivo salvo el auth y el `get_venta` de la
línea de ventas — Restolibra no tiene este endpoint). Sobre `libracore.db.recibos`
y `libracore.recibos`.

```python
app.include_router(
    build_recibos_router(usuario_actual=get_current_user_json, solo_admin=require_admin_json,
                         get_venta=db_ventas.get_venta),
)
```

**`get_venta` es el único gancho que hace falta.** `emitir_recibo_factura`
(`libracore.recibos`) ya resuelve sola la factura, los cobros y el cliente
contra tablas del motor que todo producto comparte (`facturas`, `caja_movimientos`,
`clients`); `emitir_recibo_cobranza` también. Sólo `emitir_recibo_venta` necesita
saber de qué tabla sale la venta -- `ventas` del propio esquema o `sales` de
LibraCommerce, según el producto -- y por eso es el único parámetro de acá.
Sin pasarlo, usa el default de `libracore.recibos.emitir_recibo_venta`
(`libracore.db.ventas.get_venta`).

**Sin `require_module` fijo, a propósito.** Un recibo nace de una factura, de
una venta o de un pago de cuenta corriente: gatearlo por uno de esos tres
módulos dejaría sin reimpresión a los otros dos. El gate real está en el botón
que lo emite, que sí vive dentro de su módulo.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from libracore import recibos as _recibos
from libracore.db import recibos as db_recibos
from libracore.pdf_generator import generate_pdf_recibo_doc
from libracore.recibos import SinCobros

_PAGE_SIZE = 50


class AnularPayload(BaseModel):
    motivo: str = ""


def _numero_visible(recibo: dict) -> str:
    return f"{str(recibo['punto_venta']).zfill(4)}-{str(recibo['numero']).zfill(8)}"


def _resumen(recibo: dict) -> dict:
    """Lo que necesita el listado. Sin el snapshot de pagos, que sólo importa
    en el PDF y hace pesada la grilla."""
    return {
        "id":             recibo["id"],
        "numero_visible": _numero_visible(recibo),
        "fecha":          recibo["fecha"],
        "cliente_id":     recibo["cliente_id"],
        "cliente_razon":  recibo["cliente_razon"],
        "cliente_cuit":   recibo["cliente_cuit"],
        "concepto":       recibo["concepto"],
        "origen_tipo":    recibo["origen_tipo"],
        "origen_id":      recibo["origen_id"],
        "total":          recibo["total"],
        "anulado":        recibo["anulado"],
        "anulado_motivo": recibo["anulado_motivo"],
    }


def build_recibos_router(
    *,
    usuario_actual: Callable[..., Any],
    solo_admin: Callable[..., Any],
    get_venta: Callable[..., Any] | None = None,
    prefix: str = "/api/recibos",
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["recibos"])

    @router.get("")
    def listar(desde: str = "", hasta: str = "", q: str = "", cliente_id: int | None = None,
               incluir_anulados: bool = True, page: int = 1,
               user: dict = Depends(usuario_actual)):  # noqa: ARG001
        page = max(1, page)
        filtros = dict(desde=desde, hasta=hasta, q=q, cliente_id=cliente_id,
                       incluir_anulados=incluir_anulados)
        recibos = db_recibos.get_recibos(**filtros, limit=_PAGE_SIZE, offset=(page - 1) * _PAGE_SIZE)
        total = db_recibos.contar_recibos(**filtros)
        return {
            "recibos":   [_resumen(r) for r in recibos],
            "total":     total,
            "page":      page,
            "page_size": _PAGE_SIZE,
        }

    @router.get("/{recibo_id}")
    def detalle(recibo_id: int, user: dict = Depends(usuario_actual)):  # noqa: ARG001
        recibo = db_recibos.get_recibo(recibo_id)
        if not recibo:
            raise HTTPException(404, "Recibo no encontrado")
        return {**_resumen(recibo), "pagos": recibo["pagos"], "observaciones": recibo["observaciones"]}

    @router.get("/{recibo_id}/pdf")
    def pdf(recibo_id: int, user: dict = Depends(usuario_actual)):  # noqa: ARG001
        recibo = db_recibos.get_recibo(recibo_id)
        if not recibo:
            raise HTTPException(404, "Recibo no encontrado")
        return Response(
            content=generate_pdf_recibo_doc(recibo),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="recibo_{_numero_visible(recibo)}.pdf"'},
        )

    @router.post("/factura/{factura_id}")
    def emitir_de_factura(factura_id: int, user: dict = Depends(usuario_actual)):
        try:
            recibo = _recibos.emitir_recibo_factura(factura_id, usuario_id=user.get("id"))
        except SinCobros as exc:
            raise HTTPException(409, str(exc)) from exc
        return _resumen(recibo)

    @router.post("/venta/{venta_id}")
    def emitir_de_venta(venta_id: int, user: dict = Depends(usuario_actual)):
        try:
            recibo = _recibos.emitir_recibo_venta(venta_id, usuario_id=user.get("id"), get_venta=get_venta)
        except SinCobros as exc:
            raise HTTPException(409, str(exc)) from exc
        return _resumen(recibo)

    @router.post("/cobranza/{cc_pago_id}")
    def emitir_de_cobranza(cc_pago_id: int, user: dict = Depends(usuario_actual)):
        try:
            recibo = _recibos.emitir_recibo_cobranza(cc_pago_id, usuario_id=user.get("id"))
        except SinCobros as exc:
            raise HTTPException(409, str(exc)) from exc
        return _resumen(recibo)

    @router.post("/{recibo_id}/anular", dependencies=[Depends(solo_admin)])
    def anular(recibo_id: int, payload: AnularPayload, user: dict = Depends(usuario_actual)):
        """Anula el recibo. **No toca la caja ni la cuenta corriente**: el
        recibo es el comprobante del cobro, no el cobro. Revertir la plata es
        la baja del pago, que es otra operación y otro botón."""
        recibo = db_recibos.get_recibo(recibo_id)
        if not recibo:
            raise HTTPException(404, "Recibo no encontrado")
        if not db_recibos.anular_recibo(recibo_id, motivo=payload.motivo, usuario_id=user.get("id")):
            raise HTTPException(409, "El recibo ya estaba anulado.")
        return _resumen(db_recibos.get_recibo(recibo_id))

    return router
