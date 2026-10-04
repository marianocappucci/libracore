"""El recorrido de las rutas de una app que comparten las guardias de `libracore.testing` (`campos_numericos_que_aceptan_booleano`, `cuerpos_sin_tipar`): qué rutas hay, qué parámetros
tiene cada una y cómo se ignora una excepción. Es interno del paquete: no se importa desde un producto."""

from __future__ import annotations

import typing

from fastapi import params as _params
from fastapi import routing as _routing
from pydantic import BaseModel

_METODOS_QUE_NO_CUENTAN = {"HEAD", "OPTIONS"}


def _pelar(ann):
    """El tipo de adentro de un `Annotated` (las restricciones las conserva el modelo real, que es el que se mide)."""
    while typing.get_origin(ann) is typing.Annotated:
        ann = typing.get_args(ann)[0]
    return ann


def _es_modelo(ann) -> bool:
    return isinstance(ann, type) and issubclass(ann, BaseModel)


def _rutas(rutas, prefijo: str = ""):
    """`(metodo, ruta, dependant)` de cada ruta con sus métodos, con el prefijo de su `include_router` y también dentro de un `Mount` (una sub-aplicación). FastAPI nuevo (0.141 o más) ya
    no aplana `app.routes` al incluir un router: deja un `_IncludedRouter` perezoso, y las rutas con su prefijo y sus dependencias salen de `fastapi.routing.iter_route_contexts` (lo mismo que
    usa para armar el OpenAPI). En una versión sin esa función, `app.routes` ya es plano."""
    iterar = getattr(_routing, "iter_route_contexts", None)
    for r in (iterar(rutas) if iterar else rutas):
        original = getattr(r, "original_route", r)
        ruta = f"{prefijo}{getattr(r, 'path', None) or getattr(original, 'path', '') or ''}"
        if hasattr(original, "dependant"):
            for metodo in sorted((getattr(r, "methods", None) or set()) - _METODOS_QUE_NO_CUENTAN):
                yield metodo, ruta, r.dependant
            continue
        interna = getattr(original, "routes", None) or getattr(getattr(original, "app", None), "routes", None)
        if interna:
            yield from _rutas(interna, ruta)


def _dependants(dependant):
    """El `dependant` de la ruta y los de sus `Depends`: un parámetro de una dependencia (paginación, filtros) llega a la ruta igual."""
    yield dependant
    for sub in getattr(dependant, "dependencies", ()):
        yield from _dependants(sub)


def _parametros(dependant):
    """`(texto, parametro)`: `texto` es True para lo que llega como texto (query, path, header, cookie, formulario) y False para el cuerpo JSON."""
    for dep in _dependants(dependant):
        for p in (*dep.query_params, *dep.path_params, *dep.header_params, *dep.cookie_params):
            yield True, p
        for p in dep.body_params:
            yield isinstance(p.field_info, _params.Form), p


def _ignorados(ignorar) -> set[tuple[str, str]]:
    resultado = set()
    for par in ignorar:
        if not (isinstance(par, tuple) and len(par) == 2 and all(isinstance(x, str) for x in par)):
            raise ValueError(f"`ignorar` es un conjunto de (ruta, campo), las dos de texto; llegó {par!r}")
        resultado.add(par)
    return resultado


def _nombre_del_campo(parametro: str, ruta: str, con_prefijo: bool) -> str:
    """`ruta` es relativa al modelo del parámetro; si no dice nada (un número suelto) o empieza por `[`/`{`, el nombre del parámetro va delante, y siempre con más de un cuerpo."""
    if not (con_prefijo or not ruta or ruta[0] in "[{"):
        return ruta
    return f"{parametro}{ruta}" if not ruta or ruta[0] in "[{" else f"{parametro}.{ruta}"
