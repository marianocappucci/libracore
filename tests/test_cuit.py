"""`libracore.cuit.solo_digitos`: la regla única de «sólo los dígitos del CUIT»."""

import re
from pathlib import Path

import pytest

from libracore import arca_wsfe
from libracore.cuit import solo_digitos


@pytest.mark.parametrize("crudo, esperado", [
    (None, ""),
    ("", ""),
    ("   ", ""),
    ("20-12345678-6", "20123456786"),
    ("20 12345678 6", "20123456786"),
    ("30.70933285.2", "30709332852"),
    ("20123456786", "20123456786"),
    ("sin cuit", ""),
    ("20-1234x5678-6", "20123456786"),
    (" \t20\n12345678/6 ", "20123456786"),
    ("²", ""),
])
def test_quita_todo_lo_que_no_es_digito(crudo, esperado):
    assert solo_digitos(crudo) == esperado


@pytest.mark.parametrize("valor, esperado", [(20123456786, "20123456786"), (0, ""), (b"20", "20")])
def test_lo_que_no_es_texto_no_lanza(valor, esperado):
    assert solo_digitos(valor) == esperado


@pytest.mark.parametrize("crudo", [None, "", "30-70933285-2", " 30.70933285.2 ", "abc", "20/12345678/6"])
def test_el_cuit_del_receptor_es_solo_digitos_del_cliente_cuit(crudo):
    """La guarda del receptor se apoya en la misma función: no pueden discrepar."""
    assert arca_wsfe.cuit_del_receptor({"cliente_cuit": crudo}) == solo_digitos(crudo)


#: La copia: un `join` de caracteres filtrados por `isdigit()` (en una línea).
_COPIA = re.compile(r"join\(\s*\w+\s+for\s+\w+\s+in\b[^\n]*\bif\s+\w+\.isdigit\(\)")


def test_ningun_modulo_del_motor_filtra_los_digitos_por_su_cuenta():
    """La regla es una sola: `libracore.cuit.solo_digitos`. Si vuelve una copia, el motor tiene dos."""
    raiz = Path(arca_wsfe.__file__).resolve().parent
    copias = [
        f"{ruta.relative_to(raiz.parent)}:{n}"
        for ruta in sorted(raiz.rglob("*.py"))
        for n, linea in enumerate(ruta.read_text(encoding="utf-8").splitlines(), start=1)
        if _COPIA.search(linea)
    ]
    assert not copias, f"copias de «sólo los dígitos»: {copias}; usar libracore.cuit.solo_digitos"
