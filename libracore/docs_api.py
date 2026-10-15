"""El Swagger de la API (`/docs`, `/redoc`, `/openapi.json`) sólo se sirve en desarrollo (ADR-054).

FastAPI publica por defecto tres rutas que describen **toda** la API: `/openapi.json` (cada ruta, cada
parámetro, cada esquema), `/docs` (Swagger UI) y `/redoc`. Sin sesión. Medido el 2026-10-15 en clientes
reales, demos y backoffices: las tres daban 200. No es una fuga de datos, es un mapa: le ahorra a quien
busca una falla el trabajo de descubrir qué rutas existen, y el esquema de una demo es **el mismo** que el
de un cliente real.

Una línea por producto, justo después de crear la app::

    app = FastAPI(title="Contalibra")
    cerrar_docs_de_la_api(app)

**Quién la ve.** Sólo `ENV=development` (la misma marca que ya usa el resto de la familia para «esto es un
entorno de desarrollo»: CAE simulado en `arca_facturacion`, admin por defecto en libraauth; ninguna instancia
real ni demo la lleva) o, a mano y por instancia, `LIBRA_DOCS_API=1`. Todo lo demás —cliente, demo, la
variable ausente— es **cerrado**: ante la duda se cierra, no se abre.

**Por qué no por `LIBRA_ENTORNO`.** Esa variable la escribe el panel al dar de alta una instancia y vale
`cliente` o `demo`; una instancia vieja, o una corrida local, no la tiene (`desconocido`). Un criterio que
se abre ante «no sé» deja abierto justo donde nadie miró. `ENV=development` hay que ponerlo a propósito.

**Por qué se decide en cada pedido y no al crear la app.** Los productos arman la app al importar el módulo;
decidirlo ahí hace que la prueba de un producto dependa del orden de import y de lo que el shell tuviera
puesto (el mismo mal que dejó 106 errores ajenos en la suite del 2026-08-20, ver `libracore.spa`). Acá la
ruta existe siempre y contesta 404 si el entorno no es de desarrollo: la prueba cambia la variable y pide.

**Por qué rutas propias y no `docs_url=None`.** Con `docs_url=None` la ruta desaparece y lo que la atiende es
el catch-all de la SPA, que contesta `index.html` **con 200** a cualquier cosa (ver `libracore.spa`): un
escáner seguiría viendo 200 en `/docs`. Una ruta que existe y contesta 404 JSON —la misma forma que el 404
de FastAPI— le gana al catch-all, que se monta después.

`app.openapi()` no cambia: sigue devolviendo el esquema para quien lo pide desde Python (los tests de rutas
de los productos y `libracore.testing`). Lo que se cierra es sólo su publicación por HTTP.
"""
from __future__ import annotations

import inspect
import os
from collections.abc import Mapping

from starlette.responses import JSONResponse
from starlette.routing import Route

#: Para abrir el Swagger de **una** instancia a propósito (un dev en el VPS, un diagnóstico puntual). El panel
#: no la escribe nunca: hay que ponerla a mano en el compose, y se nota.
VARIABLE_DOCS = "LIBRA_DOCS_API"

_VALORES_QUE_ABREN = ("1", "true")

#: Marca de una ruta ya guardada, para que llamar dos veces a `cerrar_docs_de_la_api` no la envuelva dos veces.
_MARCA = "_libra_docs_guardado"


def docs_abiertos(entorno: Mapping[str, str] | None = None) -> bool:
    """¿Este proceso puede servir el Swagger? Sólo en `ENV=development` o con `LIBRA_DOCS_API=1`."""
    env = os.environ if entorno is None else entorno
    if (env.get("ENV") or "") == "development":
        return True
    return (env.get(VARIABLE_DOCS) or "").strip().lower() in _VALORES_QUE_ABREN


def _guardado(endpoint):
    """`endpoint` detrás de la guarda: 404 si el entorno no es de desarrollo."""

    async def guardado(request):
        if not docs_abiertos():
            # La misma forma que el 404 de FastAPI y que el de `libracore.spa`.
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        respuesta = endpoint(request)
        return await respuesta if inspect.isawaitable(respuesta) else respuesta

    setattr(guardado, _MARCA, True)
    return guardado


def cerrar_docs_de_la_api(app) -> None:
    """Pone `/docs`, `/redoc`, `/openapi.json` y `/docs/oauth2-redirect` de `app` detrás de `docs_abiertos()`.

    Reemplaza, en el mismo lugar de la tabla de rutas, las que FastAPI creó al armar la app; si la app se
    armó con `docs_url=None`, `redoc_url=None` u `openapi_url=None`, esa ruta no existe y no hay nada que
    guardar. Llamarla dos veces es inofensivo. Va **antes** de montar la SPA: la ruta tiene que estar
    antes del catch-all.
    """
    rutas = {
        app.openapi_url,
        app.docs_url,
        app.redoc_url,
        getattr(app, "swagger_ui_oauth2_redirect_url", None),
    } - {None}
    tabla = app.router.routes
    for i, ruta in enumerate(tabla):
        if not isinstance(ruta, Route) or ruta.path not in rutas:
            continue
        if getattr(ruta.endpoint, _MARCA, False):
            continue
        tabla[i] = Route(ruta.path, _guardado(ruta.endpoint), methods=["GET"], include_in_schema=False)
