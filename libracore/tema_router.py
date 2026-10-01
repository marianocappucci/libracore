"""El tema de la suite de ESTA instancia: los colores que el backoffice de la suite le empuja (ADR-012, fase 2 de 4).

Lo que se guarda es la forma `{clave: "#rrggbb"}` en la clave `tema` del `config.json` de la instancia: una instancia, un tema, y la
instancia sigue funcionando igual si el backoffice no responde (por eso la SPA lee de ACÁ y no del control plane).

## 🔑 Acá sólo se valida la FORMA, no el catálogo

La lista de colores editables y el contraste mínimo viven en un solo lugar, `libra-ui/tema` (`COLORES_DE_TEMA`, `validarTema`). Copiar
esa lista a Python sería la segunda copia que se desactualiza sola, así que este router sólo garantiza lo que no depende de ella: la
clave es un identificador corto, el valor es un `#rrggbb` (se acepta `#rgb` y se normaliza) y no hay más de `MAXIMO_DE_COLORES`.
Una clave que el kit no conoce se guarda igual y la SPA la ignora (`aplicarTema` la descarta); la validación de contraste la hace
el formulario del backoffice y, de nuevo, `aplicarTema` antes de pintar.

## El gate lo pone el producto

Como en el resto de los routers del motor. La lectura es **pública a propósito** (el login también va con los colores de la suite) y
no expone nada sensible. La escritura va detrás del gate de admin del producto. 🔴 Para que el backoffice pueda empujar el tema (pantalla «Apariencia»), esa guarda
tiene que aceptar TAMBIÉN el token de servicio (`X-Internal-Auth`): en VentaLibra es `requiere_o_servicio("config")`, y `requiere("config")` a
secas NO lo acepta (lo descubrió el test de la adopción).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Response
from pydantic import BaseModel, field_validator

from libracore import config_manager

#: La clave del `config.json` donde vive el tema.
CLAVE_CONFIG = "tema"

#: Tope de colores: una cota para que un PUT no pueda inflar el `config.json`.
MAXIMO_DE_COLORES = 32

_CLAVE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,39}$")
_HEX6 = re.compile(r"^#[0-9a-f]{6}$")
_HEX3 = re.compile(r"^#[0-9a-f]{3}$")


def normalizar_color(valor: object) -> str | None:
    """`#rgb` o `#rrggbb` (cualquier mayúscula) -> `#rrggbb` en minúsculas, o `None` si no es un color."""
    if not isinstance(valor, str):
        return None
    v = valor.strip().lower()
    if _HEX3.match(v):
        return "#" + "".join(c * 2 for c in v[1:])
    return v if _HEX6.match(v) else None


def _limpiar(crudo: object) -> dict[str, str]:
    """Lo que hay guardado, filtrado a pares `clave -> #rrggbb` válidos. Nunca lanza: un `config.json` editado a mano o de otra versión
    no puede romper la lectura que hace la SPA en cada arranque."""
    if not isinstance(crudo, dict):
        return {}
    limpio: dict[str, str] = {}
    for clave, valor in crudo.items():
        hex_ = normalizar_color(valor)
        if isinstance(clave, str) and _CLAVE.match(clave) and hex_:
            limpio[clave] = hex_
        if len(limpio) >= MAXIMO_DE_COLORES:
            break
    return limpio


class TemaIn(BaseModel):
    """El cuerpo del `PUT`: el tema COMPLETO. `{}` borra todo y la instancia vuelve a los colores de siempre."""

    tema: dict[str, str]

    @field_validator("tema")
    @classmethod
    def _validar(cls, tema: dict[str, str]) -> dict[str, str]:
        if len(tema) > MAXIMO_DE_COLORES:
            raise ValueError(f"demasiados colores (máximo {MAXIMO_DE_COLORES})")
        limpio: dict[str, str] = {}
        for clave, valor in tema.items():
            if not _CLAVE.match(clave):
                raise ValueError(f"clave inválida: {clave!r}")
            hex_ = normalizar_color(valor)
            if hex_ is None:
                raise ValueError(f"{clave}: no es un color (se espera #rrggbb)")
            limpio[clave] = hex_
        return limpio


class TemaOut(BaseModel):
    tema: dict[str, str]


def build_tema_router(*, prefix: str = "/api/tema") -> APIRouter:
    """Lectura del tema. **Sin gate, a propósito**: el login también va con los colores de la suite."""
    router = APIRouter(prefix=prefix, tags=["tema"])

    @router.get("", response_model=TemaOut)
    def obtener_tema(response: Response) -> TemaOut:
        # `no-cache`: un cambio hecho en el backoffice tiene que verse en el próximo arranque, no cuando venza una caché.
        response.headers["Cache-Control"] = "no-cache"
        return TemaOut(tema=_limpiar(config_manager.load().get(CLAVE_CONFIG)))

    return router


def build_tema_admin_router(*, prefix: str = "/api/tema") -> APIRouter:
    """Escritura del tema. Va montado con el gate de admin del producto, que tiene que aceptar también el token de servicio del backoffice."""
    router = APIRouter(prefix=prefix, tags=["tema"])

    @router.put("", response_model=TemaOut)
    def guardar_tema(cuerpo: TemaIn) -> TemaOut:
        cfg = config_manager.load()
        if cuerpo.tema:
            cfg[CLAVE_CONFIG] = cuerpo.tema
        else:
            cfg.pop(CLAVE_CONFIG, None)
        config_manager.save(cfg)
        return TemaOut(tema=cuerpo.tema)

    return router
