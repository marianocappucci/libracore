"""Validaciones de entrada que comparten los routers del motor (ADR-013).

Hoy dos: `sin_booleanos` (campos tipados) y `rechazar_booleanos` (dicts sin tipar). Los campos `int`/`float` de pydantic convierten `true` en `1` y `false` en `0` (en modo laxo, que es el de FastAPI) antes de que el servicio, que en varios
lugares rechaza el booleano a propósito (`isinstance(valor, bool)`), llegue a verlo: el cuerpo `{"monto": true}` entraba como un pago de 1 peso y `{"caja_id": true}` como la caja 1. Los
campos `Decimal` ya rechazan el booleano solos, y los `bool` de verdad y los `StrictInt` no lo necesitan: **sólo** hay que aplicarlo a un campo `int`/`float` (o a una lista o un
diccionario de ellos) donde un `1` o un `0` cambian algo del negocio. Lo que quede sin aplicar lo encuentra
`libracore.testing.campos_numericos_que_aceptan_booleano(app)`, que cada producto corre sobre su app completa.

Es la versión canónica: `libracommerce.web._validacion.sin_booleanos` (ADR-026 de ese motor) tiene el mismo código y el mismo mensaje y pasará a reexportar ésta.
"""

from __future__ import annotations

from pydantic import field_validator


def sin_booleanos(*campos: str):
    """Un `field_validator(..., mode="before")` que rechaza `true`/`false` en los `campos` numéricos. Sin esto, pydantic convierte el booleano en `1`/`0` (o
    `1.0`/`0.0`) antes de que el servicio, que sí los rechaza, llegue a verlo. Mira también dentro de una lista y de un diccionario (claves y valores). Un `0` numérico,
    un entero, un texto numérico, `None` o un `Decimal` pasan igual que antes: la conversión que sigue es la de siempre. El mensaje (422): «<campo> tiene que ser un número, no un booleano».

    Uso, dentro del modelo: `_no_son_booleanos = sin_booleanos("monto", "caja_id")`. Un `true` en un campo que lo admite (un `bool` de verdad) no pasa por acá."""
    def _validar(cls, valor, info):
        if isinstance(valor, dict):
            adentro = [*valor, *valor.values()]
        elif isinstance(valor, (list, tuple)):
            adentro = valor
        else:
            adentro = [valor]
        if any(isinstance(v, bool) for v in adentro):
            raise ValueError(f"{info.field_name} tiene que ser un número, no un booleano")
        return valor
    return field_validator(*campos, mode="before")(classmethod(_validar))


def rechazar_booleanos(valor, campos, donde: str = "", *, esperado: str = "un número") -> None:
    """Levanta `ValueError` si algún `campos` de `valor` (un dict, o una lista de dicts) es un `bool`. Es lo que `sin_booleanos` no alcanza: un cuerpo sin tipar (`list[dict]`, un `dict` leído a mano
    con `request.json()`) donde el servicio hace `float(d["monto"])` y `float(True)` es `1.0`. Lo que no es un dict se salta (de eso se ocupa el tipo de pydantic). El mensaje (422 si se llama desde un
    `field_validator`): «<donde><campo> tiene que ser un número, no un booleano», con `esperado="un texto"` para un campo que es un identificador de texto.

    Uso, en un validador: `rechazar_booleanos(pagos, ("monto",), "pagos[].")`. Y en una función del motor que recibe el dict directo de otro llamador, antes del `float(...)`."""
    filas = valor if isinstance(valor, (list, tuple)) else [valor]
    for fila in filas:
        if not isinstance(fila, dict):
            continue
        for campo in campos:
            if isinstance(fila.get(campo), bool):
                raise ValueError(f"{donde}{campo} tiene que ser {esperado}, no un booleano")
