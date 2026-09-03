"""Las lecturas del motor, EJECUTADAS contra PostgreSQL y comparadas con SQLite.

Por qué existe este archivo, que es lo que menos se ve y más importa:

`tests/db/test_schema.py` prueba que el schema **nace** en PostgreSQL y
`tests/db/test_postgres_compat.py` prueba el traductor de SQL **a nivel de
texto**. Con esos dos, el CI de esta rama llegó a 624 tests verdes contra
PostgreSQL 16 — y aun así **5 de las 7 lecturas de `remitos_presupuestos`
fallaban**, que es justo la superficie que consume el piloto LibraDesk. Ninguno
de los dos gates las ejecuta.

Y las dos formas de fallar sólo aparecen **con filas sembradas**:

1. `dict(fila)` moría con *"cannot convert dictionary update sequence element
   #0 to a sequence"* porque el `Row` del adaptador no tenía `keys()`. Es el
   patrón de retorno de toda la capa (95 llamados en 20 módulos). Con las
   tablas vacías la comprensión de lista no itera nada y el test pasa.
2. `valid_until < date('now')` se traducía a `text < date`, que en PostgreSQL
   no tiene operador.

De ahí la regla de este archivo: **sembrar siempre, y comparar contra SQLite en
vez de asertar que "no explota"**. Una lectura que devuelve `[]` en los dos
motores puede estar diciendo que los dos andan o que los dos están vacíos.
"""
import json
import os
import re

import pytest

from libracore.db import core, productos, reportes
from libracore.db import remitos_presupuestos as rp
from libracore.db.schema import init_core_schema

ITEMS = json.dumps([{"desc": "Servicio", "cant": 1, "precio": 1000.0}])

# 4.9996 redondeado a 3 decimales da exactamente el mínimo (5.0), así que NO es
# stock bajo. Es el borde donde el HAVING sin ROUND daba distinto.
CANTIDAD_BORDE = 4.9996
STOCK_MINIMO = 5.0


def _sembrar(conn):
    conn.execute(
        "INSERT INTO remitos (number, date, client_name, items, subtotal, "
        "tax_amount, total) VALUES (?,?,?,?,?,?,?)",
        ("R-0001", "2026-08-01", "Cliente Uno", ITEMS, 1000.0, 210.0, 1210.0),
    )
    for numero, valido in (("P-0001", "2026-01-15"), ("P-0002", "2099-12-31")):
        conn.execute(
            "INSERT INTO presupuestos (number, date, valid_until, status, "
            "client_name, items, subtotal, tax_amount, total) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (numero, "2026-08-01", valido, "pendiente", "Cliente Uno", ITEMS,
             1000.0, 210.0, 1210.0),
        )
    conn.execute(
        "INSERT INTO productos (codigo, nombre, unidad, tipo, activo, stock_minimo) "
        "VALUES (?,?,?,?,?,?)",
        ("BORDE", "Justo en el minimo", "u", "producto", 1, STOCK_MINIMO),
    )
    conn.execute(
        "INSERT INTO productos (codigo, nombre, unidad, tipo, activo, stock_minimo) "
        "VALUES (?,?,?,?,?,?)",
        ("BAJO", "Por debajo del minimo", "u", "producto", 1, STOCK_MINIMO),
    )
    deposito_id = conn.execute("SELECT id FROM depositos").fetchone()[0]
    for codigo, cantidad in (("BORDE", CANTIDAD_BORDE), ("BAJO", 1.0)):
        producto_id = conn.execute(
            "SELECT id FROM productos WHERE codigo=?", (codigo,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO movimientos_stock (producto_id, deposito_id, cantidad, "
            "tipo, fecha) VALUES (?,?,?,?,?)",
            (producto_id, deposito_id, cantidad, "ingreso", "2026-08-01"),
        )
    conn.commit()
    return deposito_id


def _preparar(db_path, limpiar_schema=False):
    core.configure(db_path)
    conn = core.get_connection()
    if limpiar_schema:
        # Slate limpia: este archivo siembra filas y no puede heredar las de
        # otro test del mismo servicio de CI.
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    init_core_schema(conn)
    conn.commit()
    deposito_id = _sembrar(conn)
    conn.close()
    return deposito_id


def _liberar():
    core._db_path = None
    core._database_url = None


LECTURAS = {
    "get_all_remitos": lambda dep: rp.get_all_remitos(),
    "get_all_presupuestos": lambda dep: rp.get_all_presupuestos(),
    "get_remito": lambda dep: rp.get_remito(1),
    "get_presupuesto": lambda dep: rp.get_presupuesto(1),
    "get_next_remito_number": lambda dep: rp.get_next_remito_number(),
    "get_presupuestos_count_by_estado": lambda dep: rp.get_presupuestos_count_by_estado(),
    "get_stock_por_deposito": lambda dep: productos.get_stock_por_deposito(dep),
    "get_reporte_stock_bajo": lambda dep: reportes.get_reporte_stock_bajo(),
}


# Columnas con la hora de escritura. No se comparan entre motores: las dos
# corridas ocurren en momentos distintos y un cruce de segundo las haría
# fallar sin que hubiera ningún problema. Lo que sí importa de ellas —que el
# FORMATO del texto sea el mismo— se verifica aparte, en
# `test_created_at_tiene_el_mismo_formato_en_los_dos_motores`.
VOLATILES = {"created_at", "updated_at"}


def _normalizar(valor):
    """Compara resultados entre motores sin que los tipos los separen.

    PostgreSQL devuelve `Decimal` donde SQLite devuelve `float`, y eso no es
    una diferencia de comportamiento — es el tipo de retorno del driver. Se
    normaliza a float con 6 decimales para que la comparación mire los datos.
    """
    if isinstance(valor, dict):
        return {k: _normalizar(v) for k, v in valor.items() if k not in VOLATILES}
    if isinstance(valor, list):
        return [_normalizar(v) for v in valor]
    try:
        return round(float(valor), 6)
    except (TypeError, ValueError):
        return valor


def _correr_todas(db_path, limpiar_schema=False):
    deposito_id = _preparar(db_path, limpiar_schema=limpiar_schema)
    salida = {}
    for nombre, fn in LECTURAS.items():
        salida[nombre] = _normalizar(fn(deposito_id))
    _liberar()
    return salida


@pytest.fixture
def resultados_sqlite(tmp_path):
    return _correr_todas(str(tmp_path / "lecturas.db"))


@pytest.fixture
def resultados_postgres():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    try:
        return _correr_todas(url, limpiar_schema=True)
    finally:
        _liberar()


def test_las_lecturas_dan_lo_mismo_en_los_dos_motores(resultados_sqlite, resultados_postgres):
    """El gate que faltaba: ejecutar, no sólo crear el schema.

    Se comparan los resultados enteros, no un `len()`: una diferencia de
    contenido entre motores es tan grave como una excepción, y más difícil de
    ver después.
    """
    diferencias = {
        nombre: (resultados_sqlite[nombre], resultados_postgres[nombre])
        for nombre in LECTURAS
        if resultados_sqlite[nombre] != resultados_postgres[nombre]
    }
    assert not diferencias, f"lecturas que difieren entre motores: {diferencias}"


def test_las_lecturas_traen_filas_de_verdad(resultados_sqlite):
    """Contraprueba del test de arriba.

    Sin esto, la comparación pasaría igual con las ocho lecturas devolviendo
    `[]` en los dos motores — que es exactamente cómo estas fallas se
    escondieron hasta ahora.
    """
    assert len(resultados_sqlite["get_all_remitos"]) == 1
    assert len(resultados_sqlite["get_all_presupuestos"]) == 2
    assert resultados_sqlite["get_remito"] is not None
    assert len(resultados_sqlite["get_stock_por_deposito"]) == 2
    assert len(resultados_sqlite["get_reporte_stock_bajo"]) == 1


def test_created_at_tiene_el_mismo_formato_en_los_dos_motores(tmp_path):
    """🔴 El DEFAULT de las 30 columnas `created_at TEXT`.

    En SQLite `datetime('now')` escribe 'YYYY-MM-DD HH:MM:SS' exacto. Traducido
    a `CURRENT_TIMESTAMP` a secas, PostgreSQL escribía
    '2026-08-08 23:45:24.986262+00' — mismo instante, otro string. Rompe los
    `strptime` sobre la columna y las comparaciones lexicográficas de rango,
    que es como este motor filtra por fecha.

    Se comparan los FORMATOS, no los valores: las dos corridas ocurren en
    momentos distintos y un cruce de segundo haría fallar una comparación de
    valores sin que hubiera nada roto.
    """
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")

    formato = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
    leidos = {}
    for etiqueta, destino, limpiar in (
        ("sqlite", str(tmp_path / "created_at.db"), False),
        ("postgres", url, True),
    ):
        _preparar(destino, limpiar_schema=limpiar)
        core.configure(destino)
        with core.get_connection() as conn:
            leidos[etiqueta] = conn.execute(
                "SELECT created_at FROM remitos WHERE number=?", ("R-0001",)
            ).fetchone()[0]
        _liberar()

    assert formato.match(leidos["sqlite"]), f"cambió el formato de SQLite: {leidos['sqlite']!r}"
    assert formato.match(leidos["postgres"]), (
        f"PostgreSQL no escribe el formato de SQLite: {leidos['postgres']!r}"
    )


def test_dict_de_una_fila_funciona_en_postgres(resultados_postgres):
    """🔴 `dict(fila)` es el patrón de retorno de toda la capa.

    El `Row` del adaptador no tenía `keys()`, así que `dict()` no lo trataba
    como mapping y moría. Se exige acá explícitamente, sobre una lectura que
    devuelve filas, porque es la falla que rompía 95 llamados de una.
    """
    remitos = resultados_postgres["get_all_remitos"]
    assert isinstance(remitos[0], dict)
    assert remitos[0]["number"] == "R-0001"


def test_el_producto_que_redondea_justo_al_minimo_no_es_stock_bajo(resultados_postgres):
    """El ROUND del HAVING, en el borde donde se nota.

    `get_reporte_stock_bajo` compara el stock **redondeado a 3 decimales**
    contra el mínimo. Con 4.9996 y mínimo 5.0 el redondeo da 5.0 y el producto
    NO es stock bajo. Sin el ROUND en el HAVING —como quedó un rato en esta
    rama— sí aparecía. Se asierta sobre el que la defensa excluye, no sólo
    sobre el que incluye.
    """
    codigos = {fila["codigo"] for fila in resultados_postgres["get_reporte_stock_bajo"]}
    assert "BAJO" in codigos
    assert "BORDE" not in codigos
