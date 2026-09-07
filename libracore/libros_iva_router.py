"""Libros IVA como factory de router (P9-M4, 2026-09-07): `GET /api/libros-iva`
(los dos libros con sus resúmenes por alícuota) y los cuatro exports REGINFO
fuera de `/api/`, con la sesión por cookie.

`web/api/libros_iva.py` y `web/routers/libros_iva.py` eran iguales en los dos
productos. Los generadores viven en `libracore.libros_iva`.

```python
app.include_router(build_libros_iva_router(),
                   dependencies=[Depends(require_admin_json), Depends(require_module("libros_iva"))])
app.include_router(build_libros_iva_export_router(solo_admin=require_role("admin")))
```
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from libracore import config_manager
from libracore.db import libros_iva as db_iva
from libracore.libros_iva import (
    _compras_alicuotas,
    _compras_cbte,
    _default_periodo,
    _resumen_compras,
    _resumen_ventas,
    _ventas_alicuotas,
    _ventas_cbte,
)


def _periodo_o_default(desde: str, hasta: str):
    if not desde or not hasta:
        return _default_periodo()
    return desde, hasta


def build_libros_iva_router(*, prefix: str = "/api/libros-iva") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["libros_iva"])

    @router.get("")
    def obtener(desde: str = "", hasta: str = ""):
        desde, hasta = _periodo_o_default(desde, hasta)
        cfg = config_manager.load()
        facturas = db_iva.get_facturas_para_iva(desde, hasta)
        egresos = db_iva.get_egresos_para_iva(desde, hasta)
        return {
            "desde": desde,
            "hasta": hasta,
            "empresa_cuit": cfg.get("empresa_cuit", ""),
            "facturas": facturas,
            "egresos": egresos,
            "resumen_v": _resumen_ventas(facturas),
            "resumen_c": _resumen_compras(egresos),
        }

    return router


def _txt(contenido: str, nombre: str) -> Response:
    return Response(
        content=contenido.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


def build_libros_iva_export_router(*, solo_admin: Callable[..., Any], prefix: str = "") -> APIRouter:
    """Los cuatro archivos REGINFO (`/libros-iva/export/*`). Contable-fiscal:
    sólo admin, con la dependencia de sesión del producto."""
    router = APIRouter(prefix=prefix, tags=["libros_iva"], dependencies=[Depends(solo_admin)])

    @router.get("/libros-iva/export/ventas-cbte")
    def export_ventas_cbte(desde: str = "", hasta: str = ""):
        desde, hasta = _periodo_o_default(desde, hasta)
        return _txt(_ventas_cbte(db_iva.get_facturas_para_iva(desde, hasta)),
                    f"REGINFO_CV_VENTAS_CBTE_{desde[:7].replace('-', '')}.txt")

    @router.get("/libros-iva/export/ventas-alicuotas")
    def export_ventas_alicuotas(desde: str = "", hasta: str = ""):
        desde, hasta = _periodo_o_default(desde, hasta)
        return _txt(_ventas_alicuotas(db_iva.get_facturas_para_iva(desde, hasta)),
                    f"REGINFO_CV_VENTAS_ALICUOTAS_{desde[:7].replace('-', '')}.txt")

    @router.get("/libros-iva/export/compras-cbte")
    def export_compras_cbte(desde: str = "", hasta: str = ""):
        desde, hasta = _periodo_o_default(desde, hasta)
        return _txt(_compras_cbte(db_iva.get_egresos_para_iva(desde, hasta)),
                    f"REGINFO_CV_COMPRAS_CBTE_{desde[:7].replace('-', '')}.txt")

    @router.get("/libros-iva/export/compras-alicuotas")
    def export_compras_alicuotas(desde: str = "", hasta: str = ""):
        desde, hasta = _periodo_o_default(desde, hasta)
        return _txt(_compras_alicuotas(db_iva.get_egresos_para_iva(desde, hasta)),
                    f"REGINFO_CV_COMPRAS_ALICUOTAS_{desde[:7].replace('-', '')}.txt")

    return router
