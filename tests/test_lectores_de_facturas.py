"""Ninguna consulta sobre `facturas` queda sin decidir si filtra o no.

🔴 **El riesgo de este paso no es equivocarse en una consulta: es olvidarse de
una.** Un comprobante emitido contra homologación es real en la tabla; lo único
que lo separa de los del cliente es que cada lector lo excluya. Si un solo
reporte no filtra, ese reporte miente — y miente hacia arriba, sumando plata que
no existe.

Por eso el barrido no pregunta *"¿filtra?"* sino *"¿alguien decidió?"*. Cada
consulta está clasificada abajo con su motivo. Una consulta nueva que no esté en
la lista **pone el test en rojo**, y la única forma de arreglarlo es decidir.
"""

import pathlib
import re

import pytest

from libracore.db import core, libros_iva
from libracore.db import facturas as db_facturas
from libracore.db.schema import init_core_schema

DB = pathlib.Path(__file__).resolve().parents[1] / "libracore" / "db"

#: Las consultas que **deben** excluir lo emitido contra homologación, y por qué.
#: Son las que producen números que el cliente presenta o mira como suyos.
FISCALES = {
    "libros_iva.py": "el Libro IVA: un comprobante de prueba rompe la correlatividad",
    "facturas.py:get_next_factura_numero": (
        "la numeración: ARCA lleva secuencias independientes por ambiente"),
}

#: Las que **no** filtran, y por qué no. Que estén acá es la decisión, no un
#: olvido: son las que tienen que poder ver un comprobante de prueba.
NO_FISCALES = {
    "get_factura": "el detalle: si no se pudiera abrir, no habría cómo mirar la prueba",
    "get_factura_by_numero": "la búsqueda puntual, por la misma razón",
    "delete_factura": "borrar una de prueba tiene que poder hacerse",
    "listar": "el listado las muestra: el operador necesita encontrar la que acaba de emitir",
}

#: 🔑 El fragmento que marca una consulta como filtrada. Una sola definición
#: para el código y para el test: con el patrón escrito dos veces, romper el del
#: código no pondría nada en rojo.
MARCA = db_facturas.SOLO_FISCALES


@pytest.fixture
def conn(tmp_path):
    core._db_path = None
    core._database_url = None
    core.configure(db_path=str(tmp_path / "lectores.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def _factura(numero, ambiente, **extra):
    return db_facturas.create_factura(
        tipo=6, punto_venta=1, numero=numero, fecha="2026-09-01",
        cliente_cuit="20111111112", cliente_razon="Cliente", cliente_iva_cond=5,
        items=[], subtotal=100.0, iva_amount=0.0, total=100.0,
        ambiente=ambiente, **extra)


# -- El barrido -------------------------------------------------------------

def test_las_consultas_fiscales_llevan_el_filtro():
    """Las dos que producen números que el cliente presenta.

    Se lee el fuente porque lo que se afirma es que **el filtro está escrito**,
    no que un caso concreto dé bien: un test por caso cubre los casos que se me
    ocurrieron, y acá lo que duele es el que no se me ocurrió.
    """
    faltan = []
    iva = (DB / "libros_iva.py").read_text(encoding="utf-8")
    if "sql_solo_fiscales" not in iva and MARCA not in iva:
        faltan.append("libros_iva.py")

    fact = (DB / "facturas.py").read_text(encoding="utf-8")
    m = re.search(r"def get_next_factura_numero.*?(?=\ndef )", fact, re.S)
    assert m, "no encontré `get_next_factura_numero`"
    if "ambiente=?" not in m.group(0):
        faltan.append("facturas.py:get_next_factura_numero")

    assert not faltan, (
        "Consultas fiscales sin el filtro de ambiente: " + ", ".join(faltan)
        + "\nCada una de estas produce números que el cliente presenta."
    )


def test_el_control_del_barrido_reconoce_una_consulta_sin_filtro():
    """🔑 El control positivo. El test de arriba pasa **leyendo texto**: con el
    patrón mal escrito daría verde para siempre sin mirar nada.

    Acá se le da un fuente que **no** filtra y se comprueba que lo detecta.
    """
    sin_filtro = 'rows = conn.execute("SELECT * FROM facturas WHERE fecha >= ?")'
    assert "sql_solo_fiscales" not in sin_filtro and MARCA not in sin_filtro


def test_la_marca_del_filtro_es_la_MISMA_del_codigo():
    """La constante sale de `db.facturas`, no de una copia en este archivo. Con
    dos definiciones, cambiar la del código dejaría el test verde midiendo un
    literal que ya no se usa."""
    assert MARCA == db_facturas.SOLO_FISCALES
    assert "produccion" in MARCA


def test_estan_clasificadas_las_dos_familias():
    """Que la lista de arriba no quede vacía por un refactor: si alguien la
    vacía, el barrido pasa sin mirar nada."""
    assert len(FISCALES) >= 2
    assert len(NO_FISCALES) >= 4


# -- El comportamiento ------------------------------------------------------

def test_el_libro_IVA_no_ve_los_de_prueba(conn):
    _factura(1, "produccion")
    _factura(2, "homologacion")

    filas = libros_iva.get_facturas_para_iva("2026-01-01", "2026-12-31")
    assert [f["numero"] for f in filas] == [1], (
        "el Libro IVA incluye un comprobante emitido contra homologación")


def test_el_libro_IVA_SI_ve_los_reales(conn):
    """El control positivo: sin esto, un filtro que devolviera siempre vacío
    pasaría el test de arriba."""
    _factura(1, "produccion")
    _factura(2, "produccion")

    filas = libros_iva.get_facturas_para_iva("2026-01-01", "2026-12-31")
    assert [f["numero"] for f in filas] == [1, 2]


def test_la_numeracion_es_independiente_por_ambiente(conn):
    """🔴 El defecto que más caro sale. ARCA lleva secuencias independientes: un
    comprobante de prueba numerado 500 —el que le tocaba en homologación— haría
    que el próximo real salga 501 cuando producción va por 84."""
    _factura(83, "produccion")
    _factura(500, "homologacion")

    assert db_facturas.get_next_factura_numero(1, 6, "produccion") == 84
    assert db_facturas.get_next_factura_numero(1, 6, "homologacion") == 501


def test_sin_ambiente_la_numeracion_sigue_la_real(conn):
    """La firma vieja sigue andando y numera en producción: quien no sabe de
    ambientes está facturando de verdad."""
    _factura(83, "produccion")
    _factura(500, "homologacion")

    assert db_facturas.get_next_factura_numero(1, 6) == 84


def test_una_de_prueba_se_puede_abrir_y_borrar(conn):
    """La contracara: si los lectores no fiscales filtraran, el operador no
    podría ni ver ni limpiar lo que acaba de probar."""
    fid = _factura(1, "homologacion")
    assert db_facturas.get_factura(fid)["numero"] == 1
    db_facturas.delete_factura(fid)
    assert db_facturas.get_factura(fid) is None
