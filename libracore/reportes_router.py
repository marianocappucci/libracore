"""Reportes como factory de router (P9-M4, 2026-09-07): `GET /api/reportes`,
`GET /api/reportes/caja-medios` y los exports CSV fuera de `/api/`.

`web/api/reportes.py` y `web/routers/reportes.py` eran iguales en los dos
productos. `reportes` es el `PuertoDeReportes` de `libracore.reportes`: el
producto dice de dónde salen ventas, medios, productos, stock bajo y resumen.

```python
app.include_router(build_reportes_router(reportes=REPORTES),
                   dependencies=[_auth_json, Depends(require_module("reportes"))])
app.include_router(build_reportes_export_router(sesion=require_auth, reportes=REPORTES))
```
"""

from __future__ import annotations

import csv
import datetime
import io
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from libracore.db import caja as db_caja
from libracore.db import reportes as db_reportes
from libracore.reportes import (
    MEDIO_LABEL,
    REPORTES_LIBRACORE,
    PuertoDeReportes,
    medio_label,
    pivot_caja_medios,
    totales_por_medio,
)


def _fechas_default(desde: str, hasta: str):
    if not desde:
        desde = datetime.date.today().replace(day=1).isoformat()
    if not hasta:
        hasta = datetime.date.today().isoformat()
    return desde, hasta


def _csv(filas, campos: list[str], nombre: str) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=campos)
    w.writeheader()
    w.writerows(filas)
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


def build_reportes_router(*, reportes: PuertoDeReportes = REPORTES_LIBRACORE,
                          prefix: str = "/api/reportes") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["reportes"])

    @router.get("")
    def obtener(desde: str = "", hasta: str = "", agrupacion: str = "dia"):
        desde, hasta = _fechas_default(desde, hasta)
        return {
            "desde": desde,
            "hasta": hasta,
            "agrupacion": agrupacion,
            "resumen": reportes.resumen(desde, hasta),
            "ventas_ts": reportes.ventas(desde, hasta, agrupacion),
            "medios": reportes.medios_pago(desde, hasta),
            "productos": reportes.productos_top(desde, hasta),
            "caja": db_reportes.get_reporte_caja(desde, hasta),
            "stock_bajo": reportes.stock_bajo(),
            # Las etiquetas viajan con el reporte: sin esto la pantalla las
            # declaraba por su cuenta y ya divergía. Con los históricos.
            "medio_label": MEDIO_LABEL,
        }

    @router.get("/caja-medios")
    def caja_medios(desde: str = "", hasta: str = "", caja_id: int = 0):
        desde, hasta = _fechas_default(desde, hasta)
        rows = db_reportes.get_reporte_caja_medios(desde, hasta, caja_id)
        cajas_pivot = pivot_caja_medios(rows)
        return {
            "desde": desde,
            "hasta": hasta,
            "cajas_config": db_caja.get_all_cajas(),
            "cajas": cajas_pivot,
            "totales": totales_por_medio(cajas_pivot),
            "medio_label": MEDIO_LABEL,
        }

    return router


def build_reportes_export_router(*, sesion: Callable[..., Any],
                                 reportes: PuertoDeReportes = REPORTES_LIBRACORE,
                                 prefix: str = "") -> APIRouter:
    """Los exports CSV (`/reportes/export/*`, `/reportes/caja-medios/export`),
    con la sesión por cookie de la SPA (`sesion` es `require_auth` del producto)."""
    router = APIRouter(prefix=prefix, tags=["reportes"], dependencies=[Depends(sesion)])

    @router.get("/reportes/export/ventas")
    def export_ventas(desde: str = "", hasta: str = "", agrupacion: str = "dia"):
        desde, hasta = _fechas_default(desde, hasta)
        return _csv(reportes.ventas(desde, hasta, agrupacion), ["periodo", "cantidad", "total"], f"ventas_{desde}_{hasta}.csv")

    @router.get("/reportes/export/medios")
    def export_medios(desde: str = "", hasta: str = ""):
        desde, hasta = _fechas_default(desde, hasta)
        return _csv(reportes.medios_pago(desde, hasta), ["medio", "operaciones", "total"], f"medios_pago_{desde}_{hasta}.csv")

    @router.get("/reportes/export/productos")
    def export_productos(desde: str = "", hasta: str = ""):
        desde, hasta = _fechas_default(desde, hasta)
        return _csv(reportes.productos_top(desde, hasta, limit=500), ["nombre", "cantidad", "total"], f"productos_top_{desde}_{hasta}.csv")

    @router.get("/reportes/caja-medios/export")
    def export_caja_medios(desde: str = "", hasta: str = "", caja_id: int = 0):
        desde, hasta = _fechas_default(desde, hasta)
        rows = db_reportes.get_reporte_caja_medios(desde, hasta, caja_id)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Caja", "Medio de cobro", "Tipo", "Operaciones", "Total"])
        for r in rows:
            w.writerow([r["caja_nombre"], medio_label(r["medio"]), r["tipo"], r["operaciones"], r["total"]])
        buf.seek(0)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="caja_medios_{desde}_{hasta}.csv"'})

    return router
