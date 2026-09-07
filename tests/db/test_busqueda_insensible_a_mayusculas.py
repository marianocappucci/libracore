"""La búsqueda de texto, EJECUTADA en los dos motores con la misma consulta.

Por qué existe, que es lo que menos se ve:

`db/core.py` traduce el SQL entre motores, pero la traducción cubre los
**nombres** y la sintaxis, no la semántica de cada operador. `LIKE` es el caso
donde eso se nota: en SQLite es **insensible a mayúsculas para ASCII** y en
PostgreSQL **no lo es**. Toda la capa de búsqueda de este motor se escribió
sobre SQLite, así que el corte a PostgreSQL del 2026-08-12 la dejó
silenciosamente más estricta: buscar `juan` deja de encontrar a `Juan Perez`.

No lo iba a señalar ninguna suite verde. El schema nace igual en los dos
motores, las consultas no explotan, y el resultado *"no hay coincidencias"* es
una respuesta legítima: lo único que lo delata es sembrar una fila con
mayúsculas y buscarla en minúscula.

Regla de este archivo, heredada de `test_postgres_lecturas.py`: **sembrar
siempre y comparar los dos motores entre sí**, más un control positivo. Una
búsqueda que devuelve `[]` en los dos motores puede estar diciendo que los dos
andan igual o que los dos están vacíos.
"""
import json
import os

import pytest

from libracore.db import core, egresos, facturas, productos, recibos, ventas
from libracore.db import remitos_presupuestos as rp
from libracore.db.schema import init_core_schema

ITEMS = json.dumps([{"desc": "Servicio", "cant": 1, "precio": 1000.0}])

# Todo lo sembrado lleva mayúscula inicial y se busca en minúscula. Es la forma
# real del dato: los nombres propios se cargan capitalizados y nadie escribe
# así en un buscador.
RAZON = "Juan Perez"
QUERY = "juan"


def _sembrar(conn):
    conn.execute(
        "INSERT INTO proveedores (nombre, cuit_dni) VALUES (?,?)",
        (RAZON, "20-11111111-1"),
    )
    conn.execute(
        "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_razon, "
        "cliente_cuit, items, subtotal, iva_amount, total, ambiente) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (11, 1, 1, "2026-08-01", RAZON, "20-11111111-1", ITEMS,
         1000.0, 0.0, 1000.0, "produccion"),
    )
    conn.execute(
        "INSERT INTO productos (codigo, nombre, unidad, tipo, activo) "
        "VALUES (?,?,?,?,?)",
        ("PRD-0001", "Teclado Mecanico", "u", "producto", 1),
    )
    conn.execute(
        "INSERT INTO recibos (numero, fecha, cliente_razon, cliente_cuit, "
        "origen_tipo, concepto, total) VALUES (?,?,?,?,?,?,?)",
        (1, "2026-08-01", RAZON, "20-11111111-1", "manual", "Pago de agosto", 1000.0),
    )
    conn.execute(
        "INSERT INTO remitos (number, date, client_name, items, subtotal, "
        "tax_amount, total) VALUES (?,?,?,?,?,?,?)",
        ("R-0001", "2026-08-01", RAZON, ITEMS, 1000.0, 210.0, 1210.0),
    )
    conn.commit()


#: Cada entrada es una búsqueda de usuario que hoy consume algún producto de la
#: familia. La clave es el nombre que se lee en el fallo.
BUSQUEDAS = {
    "proveedores": lambda: egresos.search_proveedores(QUERY),
    "facturas_filtradas": lambda: facturas.get_facturas_filtradas(q=QUERY)["items"],
    "productos": lambda: productos.get_all_productos(q="teclado"),
    "recibos": lambda: recibos.get_recibos(q=QUERY),
    "remitos": lambda: rp.search_remitos(QUERY),
}


def _correr_todas(db_path, limpiar_schema=False):
    core.configure(db_path)
    conn = core.get_connection()
    if limpiar_schema:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    init_core_schema(conn)
    conn.commit()
    _sembrar(conn)
    conn.close()
    salida = {nombre: len(fn()) for nombre, fn in BUSQUEDAS.items()}
    core._db_path = None
    core._database_url = None
    return salida


@pytest.fixture
def encontrados_sqlite(tmp_path):
    return _correr_todas(str(tmp_path / "busqueda.db"))


@pytest.fixture
def encontrados_postgres():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    try:
        return _correr_todas(url, limpiar_schema=True)
    finally:
        core._db_path = None
        core._database_url = None


def test_la_busqueda_encuentra_lo_mismo_en_los_dos_motores(
    encontrados_sqlite, encontrados_postgres
):
    """El gate que faltaba: `LIKE` no significa lo mismo en los dos motores."""
    diferencias = {
        nombre: (encontrados_sqlite[nombre], encontrados_postgres[nombre])
        for nombre in BUSQUEDAS
        if encontrados_sqlite[nombre] != encontrados_postgres[nombre]
    }
    assert not diferencias, (
        "búsquedas que encuentran distinto según el motor "
        f"(sqlite, postgres): {diferencias}"
    )


def test_la_busqueda_encuentra_de_verdad(encontrados_sqlite):
    """Contraprueba del test de arriba.

    Sin esto, la comparación pasaría con las cinco búsquedas devolviendo `[]`
    en los dos motores — que es exactamente la forma que tiene este defecto de
    esconderse.
    """
    assert encontrados_sqlite == {k: 1 for k in BUSQUEDAS}, encontrados_sqlite


def test_la_busqueda_es_insensible_en_postgres(encontrados_postgres):
    """El defecto en sí, dicho sin depender de la comparación entre motores.

    Si mañana SQLite deja de estar en la suite, esta aserción sigue diciendo
    qué se espera: una fila cargada como «Juan Perez» se encuentra escribiendo
    «juan», en el motor donde la familia corre de verdad.
    """
    assert encontrados_postgres == {k: 1 for k in BUSQUEDAS}, encontrados_postgres
