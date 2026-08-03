"""Los fragmentos SQL que deciden si un movimiento es cuenta corriente.

Ese criterio se consultaba desde siete lugares del SQL del motor con el literal
escrito a mano en cada uno. Consolidarlo en un helper es seguro **solo si el
texto que genera es exactamente el que estaba**: estas consultas calculan
saldos de cuenta corriente y cuánto se cobró de cada factura, así que una
diferencia sutil —una grafía de menos, un `LOWER` que se cae— no rompe nada
visible, cambia números.

Por eso el test no se conforma con "anda": compara el fragmento generado contra
el literal historico, carácter por carácter.
"""
import sqlite3

import pytest

from libracore.db.caja import (
    MEDIOS_CUENTA_CORRIENTE,
    sql_es_cuenta_corriente,
    sql_no_es_cuenta_corriente,
)

# Copiados de git antes del refactor. Si alguien cambia el helper, esto se cae.
LITERAL_EXCLUSION_SIN_ALIAS = "LOWER(medio_pago) NOT IN ('cuenta corriente','cuenta_corriente')"
LITERAL_EXCLUSION_CON_ALIAS = "LOWER(cm.medio_pago) NOT IN ('cuenta corriente','cuenta_corriente')"
LITERAL_INCLUSION_CON_ALIAS = "LOWER(cm.medio_pago) IN ('cuenta corriente','cuenta_corriente')"


def test_el_fragmento_de_exclusion_es_identico_al_literal_historico():
    assert sql_no_es_cuenta_corriente() == LITERAL_EXCLUSION_SIN_ALIAS


def test_el_fragmento_de_exclusion_con_alias_tambien():
    assert sql_no_es_cuenta_corriente("cm.medio_pago") == LITERAL_EXCLUSION_CON_ALIAS


def test_el_fragmento_de_inclusion_es_identico_al_literal_historico():
    assert sql_es_cuenta_corriente("cm.medio_pago") == LITERAL_INCLUSION_CON_ALIAS


def test_las_dos_grafias_siguen_estando():
    """Hay movimientos historicos con cada una: perder cualquiera de las dos
    cambiaria saldos ya calculados."""
    assert set(MEDIOS_CUENTA_CORRIENTE) == {"cuenta corriente", "cuenta_corriente"}


@pytest.mark.parametrize("medio,es_cc", [
    ("cuenta_corriente", True),
    ("Cuenta Corriente", True),
    ("CUENTA CORRIENTE", True),
    ("cuenta corriente", True),
    ("efectivo", False),
    ("transferencia", False),
    ("", False),
])
def test_los_fragmentos_clasifican_igual_corriendo_contra_sqlite(medio, es_cc):
    """No alcanza con que el texto coincida: se ejecuta contra SQLite de verdad
    para comprobar que la clasificacion es la esperada y que los dos fragmentos
    son exactamente complementarios."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE m (medio_pago TEXT)")
    conn.execute("INSERT INTO m VALUES (?)", (medio,))

    incluye = conn.execute(
        f"SELECT COUNT(*) FROM m WHERE {sql_es_cuenta_corriente()}").fetchone()[0]
    excluye = conn.execute(
        f"SELECT COUNT(*) FROM m WHERE {sql_no_es_cuenta_corriente()}").fetchone()[0]

    assert bool(incluye) is es_cc
    assert incluye + excluye == 1, "los dos fragmentos tienen que ser complementarios"


def test_un_medio_nulo_no_cuenta_como_cuenta_corriente():
    """`NULL` no matchea ni `IN` ni `NOT IN` en SQL: los dos fragmentos lo
    dejan afuera. Queda fijado porque es lo que ya hacian los literales, y un
    movimiento sin medio no debe convertirse en deuda por un refactor."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE m (medio_pago TEXT)")
    conn.execute("INSERT INTO m VALUES (NULL)")
    for fragmento in (sql_es_cuenta_corriente(), sql_no_es_cuenta_corriente()):
        assert conn.execute(f"SELECT COUNT(*) FROM m WHERE {fragmento}").fetchone()[0] == 0
