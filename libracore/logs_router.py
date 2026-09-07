"""El log de actividad como factory de router (P9-M4, 2026-09-07): la
pantalla (`GET /api/logs`) y el export CSV (`GET /admin/logs/export`).

`web/api/logs.py` y `web/routers/logs.py` eran iguales en los dos productos
salvo el nombre del archivo exportado. Lo único que varía por producto es **de
dónde sale la línea de tiempo**: los que tienen las ventas en LibraCommerce
pasan `actividad`/`actividad_count` de `libracommerce.erp.actividad`; el default
es `libracore.db.logs`, que lee las tablas de este motor.

```python
app.include_router(build_logs_router(usuarios=db.get_all_usuarios, actividad=db.get_actividad_log,
                                     actividad_count=db.get_actividad_count),
                   dependencies=[Depends(require_admin_json)])
app.include_router(build_logs_export_router(solo_admin=require_admin, nombre_archivo="logs_contalibra.csv",
                                            actividad=db.get_actividad_log))
```
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from libracore.db import logs as db_logs

PAGE_SIZE = 100

TIPO_META = {
    "venta": {"label": "Venta", "color": "#0d6efd"},
    "caja": {"label": "Caja", "color": "#198754"},
    "stock": {"label": "Stock", "color": "#6f42c1"},
    "factura": {"label": "Factura", "color": "#0dcaf0"},
    "turno": {"label": "Turno", "color": "#fd7e14"},
    "remito": {"label": "Remito", "color": "#6c757d"},
    "presupuesto": {"label": "Presupuesto", "color": "#20c997"},
}


def _tipos(tipo: str) -> list[str]:
    return [t.strip() for t in tipo.split(",") if t.strip()] if tipo else []


def build_logs_router(
    *,
    usuarios: Callable[[], list[dict]],
    actividad: Callable[..., list[dict]] = db_logs.get_actividad_log,
    actividad_count: Callable[..., int] = db_logs.get_actividad_count,
    prefix: str = "/api/logs",
) -> APIRouter:
    """`usuarios` es el listado del producto (`db.get_all_usuarios`): la tabla la
    declara libraauth y su lectura vive en cada producto."""
    router = APIRouter(prefix=prefix, tags=["logs"])

    @router.get("")
    def listar(tipo: str = "", usuario_id: int = 0, turno_id: int = 0, desde: str = "", hasta: str = "", page: int = 1):
        tipos_sel = _tipos(tipo)
        offset = (page - 1) * PAGE_SIZE
        filas = actividad(
            tipos=tipos_sel or None, usuario_id=usuario_id or None, turno_id=turno_id or None,
            desde=desde, hasta=hasta, limit=PAGE_SIZE, offset=offset,
        )
        total = actividad_count(
            tipos=tipos_sel or None, usuario_id=usuario_id or None, turno_id=turno_id or None,
            desde=desde, hasta=hasta,
        )
        return {
            "actividad": filas,
            "tipo_meta": TIPO_META,
            "total": total,
            "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "page": page,
            "usuarios": usuarios(),
            "auth_log": db_logs.get_auth_log(limit=100),
        }

    return router


def build_logs_export_router(
    *,
    solo_admin: Callable[..., Any],
    nombre_archivo: str = "logs.csv",
    actividad: Callable[..., list[dict]] = db_logs.get_actividad_log,
    prefix: str = "",
) -> APIRouter:
    """`GET /admin/logs/export`, con la sesión por cookie de la SPA (`solo_admin`
    es la dependencia del producto que la exige), fuera de `/api/`."""
    router = APIRouter(prefix=prefix, tags=["logs"], dependencies=[Depends(solo_admin)])

    @router.get("/admin/logs/export")
    def logs_export(tipo: str = "", usuario_id: int = 0, turno_id: int = 0, desde: str = "", hasta: str = ""):
        filas = actividad(
            tipos=_tipos(tipo) or None, usuario_id=usuario_id or None, turno_id=turno_id or None,
            desde=desde, hasta=hasta, limit=5000, offset=0,
        )
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Fecha", "Tipo", "Descripción", "Monto", "Usuario", "Turno ID"])
        for r in filas:
            w.writerow([r["fecha"], r["tipo"], r["descripcion"], r["monto"], r["usuario"], r["turno_id"] or ""])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={nombre_archivo}"},
        )

    return router
