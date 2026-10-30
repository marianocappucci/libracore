"""La marca de ESTA instancia: nombre, logo y color, para que un revendedor la venda con la suya (ADR-092, marca blanca).

Lo que se guarda vive en la clave `marca` del `config.json` de la instancia (el logo, normalizado a PNG, en `config_manager.MARCA_DIR`):
una instancia, una marca, y la instancia sigue igual si el backoffice no responde. Es el hermano de `tema_router` (ADR-012) y sigue su
contrato: la lectura es **pública** (el login también lleva la marca), la escritura va detrás del gate de admin del producto, que tiene
que aceptar TAMBIÉN el token de servicio del backoffice (`requiere_o_servicio("config")` en VentaLibra).

## Qué expone

| Ruta | Quién | Qué |
|---|---|---|
| `GET /api/marca` | público | `{propia, nombre, nombre_corto, color, logo}`; `propia` es `False` si no hay nada guardado |
| `GET /api/marca/logo` | público | el logo, PNG |
| `GET /api/marca/icono/{192,512,maskable,apple}.png` | público | los íconos de la app instalable, generados del logo |
| `GET /manifest.webmanifest` | público | el manifest **de la marca**; sin marca, el que trae el build del producto |
| `PUT /api/marca` | admin / servicio | nombre, nombre corto y color (el logo va aparte) |
| `POST /api/marca/logo`, `DELETE /api/marca/logo` | admin / servicio | sube o saca el logo |
| `DELETE /api/marca` | admin / servicio | borra toda la marca y los términos propios: la instancia vuelve a la de su producto |
| `GET`/`PUT`/`DELETE /api/marca/terminos` | admin / servicio | los Términos y Condiciones propios del revendedor (`terminos_propios`, ADR-093) |

Los productos también la leen desde Python: `es_marca_propia()` (el gancho `exigir_propio` de `libraauth`) y `nombre_de_marca(por_defecto)`
(el `product_name` del mail de recuperación).

## 🔑 Decisiones que no se ven en el código

- **El logo no comparte directorio con el del membrete.** `config_router.subir_logo` borra todo lo que empiece con `logo` en `LOGO_DIR`;
  la marca tiene su `MARCA_DIR` para que subir una no pise la otra.
- **El logo se re-codifica a PNG** (`RGBA`, lado mayor 1024 px): quita metadatos, corta las bombas de descompresión y deja un solo formato
  para servir. **No se acepta SVG**: puede llevar scripts y se sirve desde el mismo origen que la app.
- **Los íconos se hacen con Pillow solo.** `iconos_marca.py` (wiki) usa numpy porque limpia imágenes generadas con IA (sombras, recuadros
  torcidos); acá el revendedor sube un logo limpio y alcanza con encajarlo en un cuadrado.
- **El manifest tiene que ganarle al catch-all de la SPA** (`libracore.spa`): el producto monta este router **antes** de ese catch-all, y
  le pasa `dist` para que, sin marca, se siga sirviendo el manifest del build. Sin `dist` y sin marca contesta 404.
- **Los íconos viven bajo `/api/marca/`** y no pisan `/icons/*` del build: así el catch-all no los intercepta y un producto sin marca
  conserva los suyos. El favicon y el `apple-touch-icon` los cambia la SPA (`libra-ui`).
- **La URL de cada ícono lleva `?v=<hash del logo>-<color>`**: con la versión correcta se cachea para siempre; sin ella, `no-cache`.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import unicodedata
from html import escape
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, field_validator

from libracore import config_manager, terminos_propios
from libracore.spa import TIPOS_PROPIOS, archivo_publico
from libracore.tema_router import normalizar_color

#: La clave del `config.json` donde vive la marca.
CLAVE_CONFIG = "marca"

MAX_NOMBRE = 40
#: `short_name` del manifest: los lanzadores lo cortan cerca de los 12 caracteres.
MAX_NOMBRE_CORTO = 12

#: Tope del archivo subido (1 MB): un logo no necesita más y acota lo que se lee en memoria.
MAX_BYTES_LOGO = 1_000_000
#: Tope del lado de la imagen subida; más que eso es una bomba o un error.
MAX_LADO_ORIGEN = 4096
#: Debajo de esto el ícono de 192 px sale pixelado.
MIN_LADO = 192
#: Lado mayor del logo guardado.
LADO_LOGO = 1024
FORMATOS = {"PNG", "JPEG", "WEBP"}

#: Cuánto del lienzo ocupa el logo en el ícono maskable: el círculo que ningún lanzador recorta mide 0,80 (mismo valor que `iconos_marca.py`).
ESCALA_MASKABLE = 0.74
#: Cuánto ocupa en el de iOS, que redondea las esquinas solo y no tiene zona segura circular.
ESCALA_APPLE = 0.80
FONDO_POR_DEFECTO = "#ffffff"

#: tipo -> (lado en px, con fondo opaco, escala del logo)
ICONOS = {
    "192": (192, False, 1.0),
    "512": (512, False, 1.0),
    "maskable": (512, True, ESCALA_MASKABLE),
    "apple": (180, True, ESCALA_APPLE),
}

_HASH = re.compile(r"[0-9a-f]{12}")
_ARCHIVO_LOGO = "logo.png"

_INMUTABLE = "public, max-age=31536000, immutable"
_SIN_CACHE = "no-cache"


# ── Lo guardado ────────────────────────────────────────────────────────────

def _ruta_logo() -> str:
    return os.path.join(config_manager.MARCA_DIR, _ARCHIVO_LOGO)


def _leer() -> dict:
    """La marca guardada, filtrada a lo válido. Nunca lanza: un `config.json` editado a mano o de otra versión no puede romper la
    lectura que hace la SPA en cada arranque. Un logo cuyo archivo no está en disco no cuenta."""
    crudo = config_manager.leer_json_crudo().get(CLAVE_CONFIG)
    if not isinstance(crudo, dict):
        return {}
    marca: dict = {}
    nombre = crudo.get("nombre")
    if isinstance(nombre, str) and nombre.strip():
        marca["nombre"] = nombre.strip()[:MAX_NOMBRE]
    corto = crudo.get("nombre_corto")
    if isinstance(corto, str) and corto.strip():
        marca["nombre_corto"] = corto.strip()[:MAX_NOMBRE_CORTO]
    color = normalizar_color(crudo.get("color"))
    if color:
        marca["color"] = color
    logo = crudo.get("logo")
    if isinstance(logo, str) and _HASH.fullmatch(logo) and os.path.isfile(_ruta_logo()):
        marca["logo"] = logo
    return marca


def _guardar(marca: dict) -> None:
    cfg = config_manager.load()
    if marca:
        cfg[CLAVE_CONFIG] = marca
    else:
        cfg.pop(CLAVE_CONFIG, None)
    config_manager.save(cfg)


def es_marca_propia() -> bool:
    """`True` si esta instancia tiene una marca propia. Es el gancho `exigir_propio` de `libraauth.terminos.TerminosRepository`: una
    instancia con marca propia **tiene que** tener sus Términos propios y no cae a los de la familia. Barata (una lectura de
    `config.json`): se llama en cada pedido gateado."""
    return bool(_leer())


def nombre_de_marca(por_defecto: str) -> str:
    """El nombre de la marca, o `por_defecto` (el del producto) si la instancia no tiene. Es el `product_name` del mail de recuperación
    (`libraauth.password_reset.PasswordResetService`, que acepta una función)."""
    return _leer().get("nombre") or por_defecto


_TITULO = re.compile(r"<title\b[^>]*>.*?</title>", re.IGNORECASE | re.DOTALL)
_ICONO = re.compile(r"""<link\b[^>]*\brel=["'](?:shortcut\s+)?icon["'][^>]*>""", re.IGNORECASE)
_ICONO_APPLE = re.compile(r"""<link\b[^>]*\brel=["']apple-touch-icon["'][^>]*>""", re.IGNORECASE)
_COLOR_DEL_TEMA = re.compile(r"""<meta\b[^>]*\bname=["']theme-color["'][^>]*>""", re.IGNORECASE)


def _reemplazar_o_agregar(html: str, patron: re.Pattern, etiqueta: str) -> str:
    """Cambia la etiqueta que ya está (todas las que coinciden se reemplazan por una) o, si no había, la agrega al final del `<head>`.
    `re.sub` con una función: el texto nuevo no se interpreta como plantilla (`\\1`, `\\g<...>`)."""
    if patron.search(html):
        return patron.sub(lambda _m, e=etiqueta: e, html, count=1)
    return re.sub(r"</head>", lambda _m: etiqueta + "\n  </head>", html, count=1, flags=re.IGNORECASE)


def index_con_marca(html: str) -> str:
    """El `index.html` del build con la marca de la instancia: el `<title>`, el favicon, el `apple-touch-icon` y el `theme-color` (PR 15).

    Sin esto, la pestaña dice el nombre del producto hasta que corre el JavaScript, y un enlace compartido (WhatsApp, redes) —que lee este
    HTML sin ejecutarlo— muestra el del producto. Sin marca propia devuelve `html` tal cual (el mismo objeto). Lo que la marca no define
    (sin logo, sin color) se deja como estaba. Todo valor sale de `_leer()`, que ya lo validó, y el nombre se escapa."""
    marca = _leer()
    if not marca:
        return html
    if marca.get("nombre"):
        titulo = f"<title>{escape(marca['nombre'])}</title>"
        html = _TITULO.sub(lambda _m: titulo, html, count=1)
    if marca.get("logo"):
        v = _version(marca)
        html = _reemplazar_o_agregar(
            html, _ICONO, f'<link rel="icon" type="image/png" href="/api/marca/icono/192.png?v={v}" />'
        )
        html = _reemplazar_o_agregar(
            html, _ICONO_APPLE, f'<link rel="apple-touch-icon" sizes="180x180" href="/api/marca/icono/apple.png?v={v}" />'
        )
    if marca.get("color"):
        html = _reemplazar_o_agregar(html, _COLOR_DEL_TEMA, f'<meta name="theme-color" content="{marca["color"]}" />')
    return html


def _version(marca: dict) -> str:
    """Cambia cuando cambia el logo o el color: es el `?v=` de las URLs que se pueden cachear para siempre."""
    return f"{marca.get('logo', 'x')}-{marca.get('color', '').lstrip('#') or 'x'}"


def _nombre_corto(marca: dict) -> str:
    return marca.get("nombre_corto") or marca.get("nombre", "")[:MAX_NOMBRE_CORTO].rstrip()


class MarcaOut(BaseModel):
    #: `False` si la instancia no tiene marca propia y usa la de su producto. El backoffice lo mira para no pisarla con el tema de la suite.
    propia: bool
    nombre: str = ""
    nombre_corto: str = ""
    color: str = ""
    #: Ruta del logo (con su `?v=`), o vacío.
    logo: str = ""
    #: Si la instancia tiene Términos propios cargados (ADR-093). El backoffice lo mira para avisar «marca propia sin términos».
    terminos_propios: bool = False


def _vista(marca: dict) -> MarcaOut:
    return MarcaOut(
        propia=bool(marca),
        nombre=marca.get("nombre", ""),
        nombre_corto=_nombre_corto(marca),
        color=marca.get("color", ""),
        logo=f"/api/marca/logo?v={marca['logo']}" if marca.get("logo") else "",
        terminos_propios=terminos_propios.leer_terminos_propios() is not None,
    )


# ── Validación de lo que llega ─────────────────────────────────────────────

def _texto_limpio(valor: str, maximo: int, campo: str) -> str:
    limpio = valor.strip()
    if len(limpio) > maximo:
        raise ValueError(f"{campo}: máximo {maximo} caracteres")
    # Cc (control), Cf (formato: ceros de ancho, inversores bidireccionales), Cs, Co, Cn: nada de eso se ve y todo sirve para engañar.
    if any(unicodedata.category(c).startswith("C") for c in limpio):
        raise ValueError(f"{campo}: tiene caracteres de control")
    return limpio


class MarcaIn(BaseModel):
    """El cuerpo del `PUT`: el texto y el color de la marca COMPLETOS (el logo se sube aparte y no se toca). Para borrarla, `DELETE`."""

    nombre: str = ""
    nombre_corto: str = ""
    color: str = ""

    @field_validator("nombre")
    @classmethod
    def _nombre(cls, v: str) -> str:
        return _texto_limpio(v, MAX_NOMBRE, "nombre")

    @field_validator("nombre_corto")
    @classmethod
    def _corto(cls, v: str) -> str:
        return _texto_limpio(v, MAX_NOMBRE_CORTO, "nombre_corto")

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not v.strip():
            return ""
        hex_ = normalizar_color(v)
        if hex_ is None:
            raise ValueError("color: se espera #rrggbb")
        return hex_


def _normalizar_logo(datos: bytes) -> bytes:
    """El PNG (RGBA, lado mayor `LADO_LOGO`) que sale de la imagen subida, o `ValueError` con una frase para mostrar en pantalla."""
    try:
        imagen = Image.open(io.BytesIO(datos))
        if imagen.format not in FORMATOS:
            raise ValueError("El logo debe ser PNG, JPG o WEBP.")
        ancho, alto = imagen.size
        if max(ancho, alto) > MAX_LADO_ORIGEN:
            raise ValueError(f"El logo es demasiado grande (máximo {MAX_LADO_ORIGEN} px de lado).")
        if min(ancho, alto) < MIN_LADO:
            raise ValueError(f"El logo es demasiado chico (mínimo {MIN_LADO} px de lado).")
        imagen = imagen.convert("RGBA")
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("El archivo no es una imagen válida.") from None
    imagen.thumbnail((LADO_LOGO, LADO_LOGO), Image.LANCZOS)
    salida = io.BytesIO()
    imagen.save(salida, format="PNG", optimize=True)
    return salida.getvalue()


def _borrar_logo() -> None:
    try:
        os.unlink(_ruta_logo())
    except FileNotFoundError:
        pass


# ── Íconos y manifest ──────────────────────────────────────────────────────

def _icono(logo_png: bytes, color: str, tipo: str) -> bytes:
    """El ícono de ese `tipo` a partir del logo: encajado (sin deformar) y centrado en un cuadrado. Los dos con fondo (maskable y apple) van
    sobre el color de la marca, sin transparencia; el de iOS además sin canal alfa."""
    lado, con_fondo, escala = ICONOS[tipo]
    logo = Image.open(io.BytesIO(logo_png)).convert("RGBA")
    caja = lado * escala
    factor = min(caja / logo.width, caja / logo.height)
    logo = logo.resize((max(1, round(logo.width * factor)), max(1, round(logo.height * factor))), Image.LANCZOS)
    x, y = (lado - logo.width) // 2, (lado - logo.height) // 2
    if con_fondo:
        lienzo = Image.new("RGBA", (lado, lado), (*_rgb(color or FONDO_POR_DEFECTO), 255))
        lienzo.alpha_composite(logo, (x, y))
        lienzo = lienzo.convert("RGB")
    else:
        lienzo = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
        lienzo.alpha_composite(logo, (x, y))
    salida = io.BytesIO()
    lienzo.save(salida, format="PNG", optimize=True)
    return salida.getvalue()


def _rgb(hex_: str) -> tuple[int, int, int]:
    return int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)


def _base_del_manifest(dist) -> dict:
    """El manifest del build del producto, o uno mínimo. La marca sólo pisa lo suyo: lo demás (`start_url`, `display`, `lang`...) sigue siendo del producto."""
    if dist is not None:
        ruta = archivo_publico(dist, "manifest.webmanifest")
        if ruta is not None:
            try:
                base = json.loads(ruta.read_text(encoding="utf-8"))
                if isinstance(base, dict):
                    return base
            except (OSError, ValueError):
                pass
    return {"start_url": "/", "scope": "/", "display": "standalone"}


def _manifest(marca: dict, dist) -> dict:
    manifest = _base_del_manifest(dist)
    if marca.get("nombre"):
        manifest["name"] = marca["nombre"]
        manifest["short_name"] = _nombre_corto(marca)
    if marca.get("color"):
        manifest["theme_color"] = marca["color"]
    if marca.get("logo"):
        v = f"?v={_version(marca)}"
        manifest["icons"] = [
            {"src": f"/api/marca/icono/192.png{v}", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": f"/api/marca/icono/512.png{v}", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": f"/api/marca/icono/maskable.png{v}", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ]
    return manifest


# ── Routers ────────────────────────────────────────────────────────────────

def build_marca_router(
    *, prefix: str = "/api/marca", dist: Path | str | None = None, ruta_manifiesto: str = "/manifest.webmanifest"
) -> APIRouter:
    """Lectura de la marca, del logo, de los íconos y del manifest. **Sin gate, a propósito**: el login también va con la marca.

    `dist` es el build del frontend del producto: sin marca propia, el manifest que se sirve es el suyo. Se monta **antes** del catch-all de la SPA."""
    router = APIRouter(tags=["marca"])

    @router.get(prefix, response_model=MarcaOut)
    def obtener_marca(response: Response) -> MarcaOut:
        # `no-cache`: un cambio hecho en el backoffice tiene que verse en el próximo arranque, no cuando venza una caché.
        response.headers["Cache-Control"] = _SIN_CACHE
        return _vista(_leer())

    @router.get(f"{prefix}/logo", include_in_schema=False)
    def obtener_logo(v: str = ""):
        marca = _leer()
        if not marca.get("logo"):
            raise HTTPException(404, "No hay logo de marca cargado.")
        cache = _INMUTABLE if v == marca["logo"] else _SIN_CACHE
        return FileResponse(_ruta_logo(), media_type="image/png", headers={"Cache-Control": cache})

    @router.get(prefix + "/icono/{tipo}.png", include_in_schema=False)
    def obtener_icono(tipo: str, v: str = ""):
        marca = _leer()
        if tipo not in ICONOS or not marca.get("logo"):
            raise HTTPException(404, "No hay ícono de marca.")
        with open(_ruta_logo(), "rb") as f:
            png = _icono(f.read(), marca.get("color", ""), tipo)
        cache = _INMUTABLE if v == _version(marca) else _SIN_CACHE
        return Response(png, media_type="image/png", headers={"Cache-Control": cache})

    @router.get(ruta_manifiesto, include_in_schema=False)
    def obtener_manifest():
        marca = _leer()
        if marca:
            return JSONResponse(
                _manifest(marca, dist), media_type=TIPOS_PROPIOS[".webmanifest"], headers={"Cache-Control": _SIN_CACHE}
            )
        if dist is not None:
            ruta = archivo_publico(dist, ruta_manifiesto.lstrip("/"))
            if ruta is not None:
                return FileResponse(ruta, media_type=TIPOS_PROPIOS[".webmanifest"], headers={"Cache-Control": _SIN_CACHE})
        raise HTTPException(404, "No hay manifest.")

    return router


def build_marca_admin_router(*, prefix: str = "/api/marca") -> APIRouter:
    """Escritura de la marca. Va montado con el gate de admin del producto, que tiene que aceptar también el token de servicio del backoffice."""
    router = APIRouter(prefix=prefix, tags=["marca"])
    terminos_propios.agregar_rutas(router, prefix="/terminos")

    @router.put("", response_model=MarcaOut)
    def guardar_marca(cuerpo: MarcaIn) -> MarcaOut:
        marca = {k: v for k, v in cuerpo.model_dump().items() if v}
        actual = _leer()
        if actual.get("logo"):
            marca["logo"] = actual["logo"]
        _guardar(marca)
        return _vista(_leer())

    # `def` y no `async def`: todo lo de acá es disco y Pillow, y con un solo proceso de uvicorn frenaría el loop mientras dura.
    @router.post("/logo", response_model=MarcaOut)
    def subir_logo(logo: UploadFile = File(...)) -> MarcaOut:
        datos = logo.file.read(MAX_BYTES_LOGO + 1)
        if len(datos) > MAX_BYTES_LOGO:
            raise HTTPException(413, f"El logo pesa más de {MAX_BYTES_LOGO // 1_000_000} MB.")
        try:
            png = _normalizar_logo(datos)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        config_manager.escribir_atomico(_ruta_logo(), png)
        marca = _leer()
        marca["logo"] = hashlib.sha256(png).hexdigest()[:12]
        _guardar(marca)
        return _vista(_leer())

    @router.delete("/logo", response_model=MarcaOut)
    def borrar_logo() -> MarcaOut:
        marca = _leer()
        marca.pop("logo", None)
        _guardar(marca)
        _borrar_logo()
        return _vista(_leer())

    @router.delete("", response_model=MarcaOut)
    def borrar_marca() -> MarcaOut:
        """La instancia vuelve a la marca de su producto: sin nombre, logo ni color propios, **y sin sus Términos propios**. Dejarlos
        haría que una instancia que ya no es del revendedor siguiera pidiendo aceptar un contrato que no es el de su producto."""
        _guardar({})
        _borrar_logo()
        terminos_propios.quitar_terminos_propios()
        return _vista({})

    return router
