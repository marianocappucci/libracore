"""Qué se sirve del build del frontend y qué cae en la SPA (ADR-020).

Vive aparte de quien monta las rutas **para poder probarlo sin construir la
aplicación**: es una función y una tabla, sin nada que ocurra al importar.

Era `app/spa.py`, byte a byte igual en seis productos (VentaLibra, Contalibra,
Restolibra, Libradesk, GestioLibra y MedLibra) hasta el 2026-10-05; ahora es del
motor, con una guarda nueva: las rutas de la API que no existen dan 404 y no el
`index.html` (`PREFIJOS_API`).
"""
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# `mimetypes` no conoce `.webmanifest`, y sin un tipo que el navegador acepte no
# hay aplicación instalable.
TIPOS_PROPIOS = {".webmanifest": "application/manifest+json"}


def archivo_publico(dist, ruta: str):
    """El archivo real de `dist` que corresponde a `ruta`, o None para caer al index.

    🔴 Existe por el catch-all de la SPA, que **devuelve `index.html` con 200
    para cualquier cosa**. Sin esto, `/manifest.webmanifest` y `/sw.js`
    contestan HTML: no fallan, contestan 200 con el cuerpo equivocado, así que
    el navegador descarta el manifest en silencio y la aplicación no aparece
    como instalable. No queda nada que mirar para darse cuenta.

    Sólo se sirve lo que existe de verdad adentro de `dist`. El resto sigue
    cayendo en la SPA, que es lo que hace andar el ruteo del lado del cliente —
    e `index.html` también, para no tener dos caminos hacia el mismo archivo.
    """
    if not ruta or ruta.endswith("/"):
        return None
    raiz = Path(dist).resolve()
    candidato = (raiz / ruta).resolve()
    if raiz not in candidato.parents:
        return None  # un `..` que se escapa de dist
    if candidato.name == "index.html" or not candidato.is_file():
        return None
    return candidato


# ── El cableado de la SPA, y sus cabeceras de caché ────────────────────────

#: 🔴 **`index.html` no se cachea, y esto no es una optimización: es lo que
#: hace que un deploy se vea.**
#:
#: Vite le pone un hash en el nombre a cada bundle, así que el archivo nuevo
#: nunca pisa al viejo — pero `index.html` **conserva el nombre** y es el único
#: que dice cuál es el bundle de ahora. Sin `Cache-Control`, el navegador aplica
#: caché heurística (una fracción de la antigüedad del `Last-Modified`) y puede
#: servir el `index.html` guardado sin preguntar. El usuario recarga, no ve el
#: cambio, y del lado del servidor está todo bien: el contenedor tiene el código
#: nuevo, el bundle nuevo está publicado, y el navegador sigue pidiendo el viejo
#: — que además existe, porque el nombre lleva hash.
#:
#: Le pasó a LibraCargo el 2026-08-19 con la pantalla de Backup, y las seis
#: instancias de la familia servían el `index.html` sin la cabecera hasta el
#: 2026-08-20 — medido contra los dominios, no leído del compose.
#:
#: `no-cache` **no** es "no guardes": es "guardá, pero revalidá siempre".
SIN_CACHE = "no-cache, must-revalidate"

#: Los assets, al revés: el nombre lleva el hash del contenido, así que **el
#: mismo nombre nunca cambia de contenido** y se pueden cachear para siempre. Un
#: `index.html` que revalida siempre es lo que hace seguro esto: cuando el
#: contenido cambia, el nombre cambia, y el `index.html` fresco pide el nuevo.
PARA_SIEMPRE = "public, max-age=31536000, immutable"

#: 🔴 **Una ruta de la API que no existe es un 404, no la SPA.** El catch-all
#: contestaba `index.html` con 200 a cualquier cosa, también a `/api/loquesea`:
#: un cliente que pide un endpoint mal escrito, o que todavía no existe en esa
#: versión, recibía HTML con 200 y lo tomaba por un éxito. Pasó en el wiki del
#: ecosistema: los deploys de VentaLibra del 2026-10-04/05 reportaban
#: «`/api/health` 200» y era el `index.html` — el chequeo real es `/health`.
#: Las rutas que sí existen se resuelven antes (se montan antes que el
#: catch-all), así que esto sólo toca lo que iba a caer en la SPA.
PREFIJOS_API = ("api",)


class AssetsInmutables(StaticFiles):
    """`StaticFiles` con la cabecera de caché larga."""

    def file_response(self, *args, **kwargs):
        respuesta = super().file_response(*args, **kwargs)
        respuesta.headers["Cache-Control"] = PARA_SIEMPRE
        return respuesta


def es_de_la_api(ruta: str, prefijos=PREFIJOS_API) -> bool:
    """Si `ruta` (sin la barra inicial) es de la API: `api` o `api/...`."""
    primero = ruta.split("/", 1)[0]
    return primero in prefijos


def montar_spa(app, dist, *, prefijos_api=PREFIJOS_API) -> None:
    """Monta `/assets` y el catch-all del frontend, con sus cabeceras de caché.

    🔴 Vive acá, y no en el módulo que construye la app, por la misma razón que
    `archivo_publico`: **para poder probarlo sin construir la aplicación**.

    No es una preferencia de estilo. Mientras esto vivía junto a la app, la
    única forma de probar las cabeceras era importar ese módulo con un `dist`
    presente — y eso, en medio de la suite, reconstruye la app entera: vuelve a
    sembrar el usuario admin y deja en `sys.modules` una instancia distinta de
    la que usan los demás tests. Medido el 2026-08-20: **106 errores ajenos en
    un producto y 176 en otro**, todos `invalid credentials`, por un archivo de
    test que aislado pasaba en verde.

    Ahora la función que llama producción es la misma que llama el test, sobre
    una `FastAPI` limpia y un `dist` de mentira.
    """
    raiz = Path(dist)
    app.mount(
        "/assets", AssetsInmutables(directory=raiz / "assets"), name="frontend-assets"
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        if es_de_la_api(full_path, prefijos_api):
            # La misma forma que el 404 de FastAPI, para que el cliente lo lea igual.
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        archivo = archivo_publico(raiz, full_path)
        if archivo is not None:
            # Los archivos sueltos del dist (favicon, manifest, sw.js) tampoco
            # llevan hash en el nombre: mismo criterio que el index.
            return FileResponse(
                archivo,
                media_type=TIPOS_PROPIOS.get(archivo.suffix),
                headers={"Cache-Control": SIN_CACHE},
            )
        return FileResponse(raiz / "index.html", headers={"Cache-Control": SIN_CACHE})
