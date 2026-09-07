"""El tablero como factory de router (P9-M4, 2026-09-07).

`web/api/dashboard.py` era el mismo en los dos productos salvo que Restolibra
suma al JSON el estado del salón, los pedidos activos, las reservas del día y
el reporte gastronómico. Eso es `extra`: un callable que recibe el día de hoy y
devuelve las claves que se agregan a la respuesta.

```python
app.include_router(build_dashboard_router(usuario_actual=get_current_user_json))
# Restolibra:
app.include_router(build_dashboard_router(
    usuario_actual=get_current_user_json,
    extra=lambda hoy: {"resumen_salon": db.resumen_salon_ahora(), ...},
))
```
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends

from libracore.db import dashboard as db_dashboard

_TIPO_LETRA = {1: "A", 6: "B", 11: "C"}


def build_dashboard_router(
    *,
    usuario_actual: Callable[..., Any],
    extra: Callable[[str], dict] | None = None,
    prefix: str = "/api",
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["dashboard"])

    @router.get("/dashboard")
    def dashboard(user: dict = Depends(usuario_actual)):
        hoy = datetime.date.today()
        hoy_iso = hoy.isoformat()
        mes_desde = hoy.replace(day=1).isoformat()

        data = db_dashboard.get_dashboard_data(mes_desde, hoy_iso)

        for f in data["facturas_sin_cobrar"]:
            f["letra"] = _TIPO_LETRA.get(f["tipo"], "")
            pv = str(f["punto_venta"]).zfill(4)
            num = str(f["numero"]).zfill(8)
            f["label_numero"] = f"{pv}-{num}"

        respuesta = {**data, "mes_desde": mes_desde, "mes_hasta": hoy_iso}
        if extra is not None:
            respuesta.update(extra(hoy_iso))
        return respuesta

    return router
