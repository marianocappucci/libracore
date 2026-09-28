"""Consulta de CUIT contra el padrón de ARCA (WSPadron), como factory de
router. Extraído de Contalibra (`consultar_cuit` en `app/web/app.py`, ~70
líneas idénticas salvo el import de auth) -- el cliente WSAA/WSPadron
(`libracore.arca_wsaa`/`libracore.arca_wspadron`) y las credenciales
(`libracore.arca_credenciales`) ya vivían en el motor; lo que faltaba
extraer era el endpoint.

```python
app.include_router(build_consultar_cuit_router(usuario_actual=require_auth))
```

Usa la config de ARCA que el producto ya tenga cargada (`libracore.db.arca_config`,
la misma que llena `build_arca_router`) -- si no hay ninguna, o le falta el par de
certificados del ambiente activo, contesta 503 en vez de fallar.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from libracore import arca_credenciales, arca_wsaa, arca_wspadron
from libracore.db.arca_config import obtener_todas_arca_configs


def build_consultar_cuit_router(
    *, usuario_actual: Callable[..., Any], prefix: str = "/api/consultar-cuit",
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["consultar-cuit"], include_in_schema=False)

    @router.get("/{cuit}")
    def consultar_cuit(cuit: str, user: dict = Depends(usuario_actual)):  # noqa: ARG001
        # `def` y no `async def`: lee la base (la config de ARCA) y resuelve el
        # par en disco, y autenticar contra el WSAA firma con `openssl` por
        # subproceso. Todo sincrónico, y con un solo proceso de uvicorn frenaría
        # a la instancia entera mientras dura. Como `def` corre en el
        # threadpool; lo que sí es asincrónico de verdad -las dos llamadas
        # SOAP- va abajo en un loop propio de este hilo.
        cuit_limpio = re.sub(r"[^0-9]", "", cuit)
        if len(cuit_limpio) != 11:
            return JSONResponse({"error": "CUIT inválido. Debe tener 11 dígitos."}, status_code=400)

        arca_cfg = obtener_todas_arca_configs()
        arca = arca_cfg[0] if arca_cfg else None

        cert_path, clave_path = arca_credenciales.paths_en_disco(arca)
        if not arca or not cert_path or not clave_path:
            return JSONResponse(
                {"error": "Configurá los certificados ARCA en Configuración para habilitar la consulta de CUIT."},
                status_code=503,
            )

        async def _padron():
            ta = await arca_wsaa.autenticar(
                cert_path, clave_path, arca["ambiente"], servicio="ws_sr_padron_a13",
            )
            return await arca_wspadron.consultar_persona(
                arca["cuit"], cuit_limpio, ta["token"], ta["sign"], arca["ambiente"],
            )

        try:
            # En un loop propio de este hilo: `autenticar` es `async` pero
            # firma el TRA con `openssl` por subproceso, que es sincrónico --
            # con `await` desde el loop de uvicorn eso frenaría a toda la
            # instancia. Las excepciones salen de `asyncio.run` tal cual, así
            # que los `except` de abajo ven lo mismo que antes.
            datos = asyncio.run(_padron())
            return JSONResponse(datos)
        except RuntimeError as e:
            msg = str(e)
            if "no encontrado" in msg.lower() or "inexistente" in msg.lower():
                return JSONResponse({"error": msg}, status_code=404)
            # Error de autorización del servicio en WSAA.
            if ("coe" in msg.lower() or "no autorizado" in msg.lower()
                    or "constraints" in msg.lower() or "sin acceso" in msg.lower()):
                return JSONResponse({
                    "error": (
                        "El certificado no tiene acceso al servicio de Padrón (ws_sr_padron_a13). "
                        "Ingresá a ARCA → Administración de Relaciones → delegá el servicio "
                        "'Consulta a Padrón Alcance 13' para tu CUIT y volvé a intentarlo."
                    ),
                }, status_code=403)
            return JSONResponse({"error": msg}, status_code=502)
        except Exception as e:
            return JSONResponse({"error": f"Error al consultar ARCA: {e}"}, status_code=500)

    return router
