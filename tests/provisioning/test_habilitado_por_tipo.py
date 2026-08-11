"""El tipo con el que se escribe `modulos.habilitado` depende de QUIEN declaro
la tabla, y en PostgreSQL tiene que coincidir."""
import pytest

from libracore.provisioning import _como_se_escribe_habilitado


class _ConFalso:
    def __init__(self, tipo=None, revienta=False):
        self._tipo = tipo
        self._revienta = revienta

    def execute(self, *a, **k):
        if self._revienta:
            raise RuntimeError("no such table: information_schema.columns")
        return self

    def fetchone(self):
        return (self._tipo,) if self._tipo else None


def test_columna_boolean_se_escribe_con_bool():
    """La `modulos` de LibraCore. Pasarle un entero da *'column habilitado is of
    type boolean but expression is of type smallint'*."""
    assert _como_se_escribe_habilitado(_ConFalso("boolean")) is bool


def test_columna_integer_se_escribe_con_int():
    """La `modulos` propia de VentaLibra. Pasarle un bool da el error simetrico,
    y es el que freno un alta real el 2026-08-11."""
    assert _como_se_escribe_habilitado(_ConFalso("integer")) is int
    assert _como_se_escribe_habilitado(_ConFalso("smallint")) is int
    assert _como_se_escribe_habilitado(_ConFalso("numeric")) is int


def test_en_sqlite_no_hay_information_schema_y_se_cae_al_booleano():
    """SQLite tipa dinamico, asi que el booleano de siempre sigue andando."""
    assert _como_se_escribe_habilitado(_ConFalso(revienta=True)) is bool


def test_sin_la_columna_tampoco_se_rompe():
    assert _como_se_escribe_habilitado(_ConFalso(None)) is bool


def test_los_dos_valores_son_los_que_espera_cada_motor():
    assert _como_se_escribe_habilitado(_ConFalso("integer"))(True) == 1
    assert _como_se_escribe_habilitado(_ConFalso("integer"))(False) == 0
    assert _como_se_escribe_habilitado(_ConFalso("boolean"))(True) is True
