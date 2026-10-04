"""`libracore.validacion.sin_booleanos` (ADR-013): pydantic, en modo laxo, convierte `true` en `1` y `false` en `0` en un campo `int`/`float`.

Qué fija este archivo: el helper rechaza el booleano (suelto, dentro de una lista y dentro de un diccionario, en las claves y en los valores) con el mensaje «<campo> tiene que ser un número, no un
booleano», y deja pasar exactamente lo que pasaba antes (un `0` numérico, un entero, un texto numérico, `None`, un `Decimal`). Es el mismo contrato que `libracommerce.web._validacion.sin_booleanos`
(ADR-026 de ese motor), que pasará a reexportar éste.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from libracore.validacion import rechazar_booleanos, sin_booleanos


class Modelo(BaseModel):
    n: int = 0
    f: float = 0
    opcional: int | None = None
    ids: list[int] = []
    precios: dict[str, float] = {}
    por_id: dict[int, float] = {}
    monto: Decimal = Decimal(0)
    activo: bool = False

    _no_son_booleanos = sin_booleanos("n", "f", "opcional", "ids", "precios", "por_id")


def _mensaje(**campos) -> str:
    with pytest.raises(ValidationError) as e:
        Modelo(**campos)
    return e.value.errors()[0]["msg"]


@pytest.mark.parametrize("campo", ["n", "f", "opcional"])
@pytest.mark.parametrize("valor", [True, False])
def test_un_booleano_suelto_se_rechaza_con_el_mensaje_de_siempre(campo, valor):
    assert _mensaje(**{campo: valor}) == f"Value error, {campo} tiene que ser un número, no un booleano"


@pytest.mark.parametrize("valor", [True, False])
def test_un_booleano_dentro_de_una_lista_o_de_un_diccionario_se_rechaza(valor):
    assert "ids tiene que ser un número, no un booleano" in _mensaje(ids=[1, valor])
    assert "precios tiene que ser un número, no un booleano" in _mensaje(precios={"1": valor})
    # La clave también: un `{true: 5}` de Python (o de un cliente que no es JSON) se convertiría en el id 1.
    assert "por_id tiene que ser un número, no un booleano" in _mensaje(por_id={valor: 5})


def test_lo_que_no_es_un_booleano_pasa_igual_que_antes():
    m = Modelo(n=0, f=0.0, opcional=None, ids=[0, 1, 2], precios={"1": 0, "2": 2.5}, por_id={1: 2.0}, monto=Decimal("1.50"))
    assert (m.n, m.f, m.opcional, m.ids, m.precios, m.por_id, m.monto) == (0, 0.0, None, [0, 1, 2], {"1": 0.0, "2": 2.5}, {1: 2.0}, Decimal("1.50"))
    # Un texto numérico lo convierte pydantic como siempre.
    t = Modelo(n="3", f="2.5", opcional="7", ids=["1", "2"], precios={"1": "9.5"})
    assert (t.n, t.f, t.opcional, t.ids, t.precios) == (3, 2.5, 7, [1, 2], {"1": 9.5})
    # Un valor que no es número sigue dando el error de pydantic, no el del booleano.
    assert "booleano" not in _mensaje(n="abc")


def test_un_bool_de_verdad_y_un_campo_sin_el_validador_no_se_tocan():
    assert Modelo(activo=True).activo is True
    # `monto` es Decimal y no está en la lista: pydantic ya rechaza el booleano solo, con su propio mensaje.
    with pytest.raises(ValidationError) as e:
        Modelo(monto=True)
    assert "booleano" not in e.value.errors()[0]["msg"]


def test_un_modelo_que_hereda_conserva_el_validador():
    class Hijo(Modelo):
        extra: int = 0

    with pytest.raises(ValidationError):
        Hijo(n=True)
    assert Hijo(n=2, extra=True).extra == 1       # lo nuevo, sin el validador, es justo lo que la guardia marca


def test_rechazar_booleanos_mira_un_dict_o_una_lista_de_dicts_sin_tipar():
    for valor in ({"monto": True}, [{"monto": 1}, {"monto": False}], ({"monto": True},)):
        with pytest.raises(ValueError, match=r"^pagos\[\]\.monto tiene que ser un número, no un booleano$"):
            rechazar_booleanos(valor, ("monto",), "pagos[].")
    with pytest.raises(ValueError, match="medio_id tiene que ser un texto, no un booleano"):
        rechazar_booleanos([{"medio_id": True}], ("medio_id",), esperado="un texto")
    # Lo que pasa igual que antes: números, textos, `None`, claves ausentes, lo que no es un dict y un `True` en otra clave.
    rechazar_booleanos([{"monto": 0}, {"monto": "5"}, {"monto": None}, {}, 3, "x", {"activo": True}], ("monto",), "pagos[].")
    rechazar_booleanos(None, ("monto",))
