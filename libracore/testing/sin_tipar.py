"""Guardia hermana de `campos_numericos_que_aceptan_booleano` (ADR-013): los cuerpos que no tienen forma, para quien monta routers de este motor (ADR-015). Se importa desde la suite de un producto,
no desde la app.

Una sola función: `cuerpos_sin_tipar(app)`. La guardia de booleanos sólo mide lo que el tipo declara: un `dict`, un `list[dict]`, un `Any`, un modelo con `extra="allow"` o un endpoint que lee
`await request.json()` a mano reciben **cualquier** cosa, con números adentro que nadie valida (así quedó `CobroPayload.pagos`: `{"pagos": [{"monto": true}]}` registraba un cobro de 1 peso, y se
encontró a mano). Esta función no valida nada: **lista dónde pasa eso**, para que una persona mire cada lugar y decida si lo tipa o lo acepta a sabiendas.
"""

from __future__ import annotations

import collections.abc
import types
import typing

from pydantic import JsonValue

from libracore.testing._recorrido import (
    _dependants,
    _es_modelo,
    _ignorados,
    _nombre_del_campo,
    _parametros,
    _pelar,
    _rutas,
)

_METODOS_CON_CUERPO = {"POST", "PUT", "PATCH", "DELETE"}
#: Lo que marca un endpoint que declara `Request` y no tiene ningún cuerpo tipado: es el candidato a leer `request.json()` a mano.
REQUEST_SIN_CUERPO_TIPADO = "request-sin-cuerpo-tipado"
#: Los contenedores que, sin tipo de elemento (`list`, `dict`), dejan entrar cualquier cosa. Se compara por identidad: `str` y `bytes` también son `Sequence`, y no cuentan.
_SECUENCIAS = (list, set, frozenset, tuple, collections.abc.Sequence, collections.abc.MutableSequence, collections.abc.Set, collections.abc.MutableSet, collections.abc.Collection,
               collections.abc.Iterable)
_UNIONES = (typing.Union, types.UnionType)


def _es_mapa(ann) -> bool:
    return isinstance(ann, type) and issubclass(ann, collections.abc.Mapping) and not hasattr(ann, "__required_keys__")  # un `TypedDict` tiene forma


def _es_json_value(ann) -> bool:
    """`JsonValue` (JSON de cualquier forma). Como campo de un modelo llega tal cual; como parámetro de una ruta FastAPI lo expande a un `Annotated[Union[...], ..., _AllowAnyJson]`, y esa marca
    de pydantic es lo que lo delata."""
    if ann is JsonValue:
        return True
    return typing.get_origin(ann) is typing.Annotated and any(getattr(m, "__name__", None) == "_AllowAnyJson" for m in typing.get_args(ann)[1:])


def _admite_cualquier_cosa(ann) -> bool:
    """`Any`, `object` y `JsonValue`: lo que no restringe nada."""
    return ann is typing.Any or ann is object or _es_json_value(ann)


def _extra_permitido(ann) -> bool:
    return _es_modelo(ann) and ann.model_config.get("extra") == "allow"


def _hay_libre(ann) -> bool:
    """¿El tipo `ann` tiene, sin entrar a ningún modelo, un lugar que acepta cualquier cosa? Un `dict`/`Mapping` o una lista sin tipo de elemento, `Any`/`object`/`JsonValue`, un
    `dict[str, Any]`, un `list[dict]` (el dict de adentro) o un modelo con `extra="allow"` (que acepta claves de más). `dict[str, int]` no: sus valores tienen tipo y los mide la guardia de
    booleanos. Los modelos de adentro (sin `extra="allow"`) no se recorren acá: se recorren sus campos, uno por uno, en `_campos`."""
    if _es_json_value(ann):
        return True
    ann = _pelar(ann)
    if _admite_cualquier_cosa(ann) or _extra_permitido(ann):
        return True
    origen, args = typing.get_origin(ann), typing.get_args(ann)
    if origen in _UNIONES:
        return any(_hay_libre(a) for a in args if a is not type(None))
    contenedor = origen or ann
    if _es_mapa(contenedor):
        return len(args) != 2 or _hay_libre(args[1])
    if contenedor in _SECUENCIAS:
        if not args:
            return True
        return any(_hay_libre(a) for a in args if a is not Ellipsis) if contenedor is tuple else _hay_libre(args[0])
    return False


def _modelos(ann, marca: str = ""):
    """`(marca, modelo)` de los modelos que cuelgan de `ann` por uniones, listas y valores de dict: `marca` es cómo se llega (`[]`, `{valor}`), como en la guardia de booleanos."""
    ann = _pelar(ann)
    origen, args = typing.get_origin(ann), typing.get_args(ann)
    if origen in _UNIONES:
        for a in args:
            yield from _modelos(a, marca)
    elif _es_modelo(ann):
        yield marca, ann
    elif _es_mapa(origen or ann) and len(args) == 2:
        yield from _modelos(args[1], f"{marca}{{valor}}")
    elif (origen or ann) in _SECUENCIAS and args:
        for a in args:
            if a is not Ellipsis:
                yield from _modelos(a, f"{marca}[]")


def _nombre(ann) -> str:
    """El tipo como se lee en el informe: `dict[str, Any]`, `list[dict]`, `Any`, `FacturaPayload(extra="allow")`. El `extra="allow"` se escribe en el nombre del modelo, así se ve cuál es."""
    if _es_json_value(ann):
        return "JsonValue"
    ann = _pelar(ann)
    if ann is typing.Any:
        return "Any"
    if ann is type(None):
        return "None"
    if ann is Ellipsis:
        return "..."
    origen, args = typing.get_origin(ann), typing.get_args(ann)
    if origen in _UNIONES:
        return " | ".join(_nombre(a) for a in args)
    if _es_modelo(ann):
        return f'{ann.__name__}(extra="allow")' if _extra_permitido(ann) else ann.__name__
    if origen is typing.Literal:
        return str(ann).replace("typing.", "")
    if origen is not None and args:
        return f"{getattr(origen, '__name__', str(origen))}[{', '.join(_nombre(a) for a in args)}]"
    if isinstance(ann, type):
        return ann.__name__
    return str(ann).replace("typing.", "")


def _campos(ann, ruta: str, vistos: tuple = ()):
    """`(ruta, tipo)` de cada campo de `ann` que acepta cualquier cosa. La `ruta` es la del **campo** (`pagos`, `items[].extra`) y el `tipo` el que declara ese campo entero (`list[dict]`), no sólo
    la parte libre. Baja por los modelos de adentro, que también tienen campos."""
    if _hay_libre(ann):
        yield ruta, _nombre(ann)
    for marca, modelo in _modelos(ann):
        if modelo in vistos:
            continue
        for nombre, campo in modelo.model_fields.items():
            yield from _campos(campo.annotation, f"{ruta}{marca}.{nombre}" if ruta or marca else nombre, (*vistos, modelo))


def cuerpos_sin_tipar(app, *, ignorar=frozenset()) -> list[tuple[str, str, str]]:
    """Los campos de entrada de `app` que **aceptan cualquier cosa** y los endpoints que leen el cuerpo a mano (ADR-015). Es una guardia **informativa, para que una persona la revise**: no se
    afirma `== []` (un `dict` es a veces lo correcto: el cuerpo que un proveedor externo manda, un JSON que se guarda tal cual) y puede dar falsos positivos. Lo que hace útil es fijar el
    conjunto conocido en un test, con un comentario por entrada que diga por qué es aceptable: un cuerpo sin tipar **nuevo** rompe ese test y obliga a mirarlo antes de que llegue a producción.

    `app` es una `FastAPI` ya armada (la `create_app()` completa del producto, con sus routers y los del motor) o un `APIRouter`. Recorre las mismas rutas reales que
    `campos_numericos_que_aceptan_booleano` (con el prefijo del `include_router`, dentro de un `Mount` y con los parámetros de sus `Depends`) y devuelve una lista ordenada y sin repetidos de
    `(metodo y ruta, campo, tipo)`:

    - un campo de entrada (cuerpo, query, formulario, path, header o cookie) cuyo tipo es `dict`/`Mapping`, `list`/`set`/`tuple` sin tipo de elemento, `Any`, `object` o `JsonValue`, o que los
      contiene (`list[dict]`, `dict[str, Any]`, `dict[str, list[dict]]`, `X | None`): `("POST /api/facturas/{factura_id}/cobrar", "pagos", "list[dict]")`. `dict[str, int]` **no**: sus valores
      tienen tipo y los mide la guardia de booleanos.
    - un modelo con `extra="allow"`, que acepta claves de más sin tipo: el tipo informado lleva el nombre del modelo con la marca, `("POST /api/facturas", "payload", 'FacturaPayload(extra="allow")')`.
      Baja por los modelos anidados: el campo de adentro se informa con su ruta (`items[].extra`).
    - un endpoint `POST`/`PUT`/`PATCH`/`DELETE` que declara `Request` **él mismo** y **no tiene ningún cuerpo tipado** (ni en la ruta ni en sus `Depends`): el candidato a leer
      `await request.json()` a mano. Va como `(ruta, nombre del parámetro, "request-sin-cuerpo-tipado")`. Una dependencia que recibe `Request` (la autenticación, casi siempre) no cuenta.

    Cómo usarla: correrla sobre la app completa, mirar cada línea y, para cada una, o **tipar el cuerpo** (un modelo de pydantic con `sin_booleanos` en los campos numéricos, ADR-013) o aceptarla
    a sabiendas. Lo que se acepta se escribe en `ignorar`, un conjunto de `(ruta, campo)` con la `ruta` como la devuelve la función (`"POST /api/x"`) o sin el método (`"/api/x"`, vale para
    todos), **con un comentario al lado que justifique cada excepción** (por ejemplo: «el cuerpo lo manda MercadoPago; el importe se pide a su API y no se lee de acá»). Una entrada que no coincide
    con nada no avisa. Un test que fije el resultado entero de la app (`assert cuerpos_sin_tipar(app) == [...]`) cumple lo mismo desde el otro lado y es lo que hace la suite de libracore.

    Lo que **no** ve, a propósito o porque no puede:

    - qué hace el endpoint con el cuerpo: sólo mira los tipos. Un `request.json()` en un endpoint que además tiene un cuerpo tipado no se detecta, ni tampoco `request.form()`, `request.stream()`
      o `request.body()` leídos a mano (el último es el del webhook de MercadoPago, que sí aparece por no tener cuerpo tipado, pero un endpoint con un modelo y un `body()` extra no).
    - un `Request` declarado en un `GET` (el cuerpo de un `GET` no se usa) y las dependencias que leen el cuerpo: sólo se mira lo que declara la ruta.
    - un texto que después se parsea como JSON (`str` con un JSON adentro), un `bytes`, un `UploadFile`, una `dataclass` o un `TypedDict` (tienen forma, aunque no la de un modelo).
    - un campo con `dict` dentro de un tipo que ya traduce un validador (`BeforeValidator`, un `field_validator(mode="before")` que acepta cualquier cosa): se ve el tipo declarado, no el validador.
    - los falsos positivos: un `dict[str, Any]` que es el contrato real (un JSON que se guarda tal cual) también se informa, y es para eso que existe `ignorar`.
    - los endpoints que no son de FastAPI (una `Route` suelta de Starlette, un `WebSocket`).
    """
    ignorados = _ignorados(ignorar)
    encontrados = set()
    for metodo, ruta, dependant in _rutas(app.routes):
        sitio = f"{metodo} {ruta}"
        hallazgos = []
        parametros = list(_parametros(dependant))
        varios_cuerpos = sum(1 for texto, _ in parametros if not texto) > 1
        for texto, p in parametros:
            for campo_ruta, tipo in _campos(p.field_info.annotation, ""):
                hallazgos.append((_nombre_del_campo(p.name, campo_ruta, varios_cuerpos and not texto), tipo))
        nombre_del_request = getattr(dependant, "request_param_name", None)
        if metodo in _METODOS_CON_CUERPO and nombre_del_request and not any(dep.body_params for dep in _dependants(dependant)):
            hallazgos.append((nombre_del_request, REQUEST_SIN_CUERPO_TIPADO))
        for campo, tipo in hallazgos:
            if (sitio, campo) not in ignorados and (ruta, campo) not in ignorados:
                encontrados.add((sitio, campo, tipo))
    return sorted(encontrados)
