"""Contra qué ambiente de ARCA se emitió cada comprobante.

🔴 **El defecto que esto cierra.** Un comprobante emitido contra homologación
trae CAE y numeración del WSFE de homologación. Hasta hoy caía en la misma tabla
que los reales y era **indistinguible**: entraba al libro IVA y rompía la
correlatividad de los libros del cliente.

Es la pieza que permite que una instancia de producción pruebe con el cliente
antes del corte a facturación real —el pendiente que el humano planteó el
2026-08-30—. El segundo par de credenciales viene después: **sin esto primero**,
probar contra homologación desde una instancia viva ensucia los libros.
"""

import ast
import pathlib

import pytest

from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db.schema import init_core_schema

RAIZ = pathlib.Path(__file__).resolve().parents[1] / "libracore"


@pytest.fixture
def conn(tmp_path):
    """Una base propia por test, con el schema del motor recién creado."""
    core.configure(db_path=str(tmp_path / "ambiente_factura.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def _factura(**extra):
    base = dict(
        tipo=6, punto_venta=1, numero=1, fecha="2026-09-01",
        cliente_cuit="20111111112", cliente_razon="Cliente",
        cliente_iva_cond=5, items=[{"description": "x", "qty": 1, "unit_price": 100.0}],
        subtotal=100.0, iva_amount=0.0, total=100.0,
    )
    base.update(extra)
    return db_facturas.create_factura(**base)


# -- El barrido: nadie puede omitir el ambiente -----------------------------

def test_toda_llamada_a_create_factura_declara_el_ambiente():
    """🔑 El hueco que este paso cierra es el **default de la base**: la columna
    tiene `DEFAULT 'produccion'` —lo necesita el backfill de las filas viejas—
    así que un `INSERT` que la omita declara real un comprobante que puede no
    serlo, y entra al libro IVA del cliente.

    Se parsea el **AST** y no se grepea: las llamadas son multilínea y el
    argumento cae varias líneas después del nombre de la función.
    """
    faltan, total = [], 0
    for f in RAIZ.rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Call):
                nombre = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                if nombre == "create_factura":
                    total += 1
                    if not any(k.arg == "ambiente" for k in n.keywords):
                        faltan.append(f"{f.relative_to(RAIZ)}:{n.lineno}")
    assert total >= 3, f"el barrido solo encontro {total} llamadas: cambio el nombre?"
    assert not faltan, "Llamadas sin declarar el ambiente:\n  " + "\n  ".join(faltan)


# -- El comportamiento ------------------------------------------------------

def test_una_factura_sin_ambiente_no_se_escribe(conn):
    """Los dos defaults posibles mienten en direcciones opuestas y las dos
    duelen: marcar de producción un comprobante de prueba ensucia los libros;
    marcar de prueba uno real lo **saca** del libro IVA en silencio, que es
    peor. Por eso no hay default."""
    with pytest.raises(TypeError):
        _factura()


def test_un_ambiente_inventado_rebota(conn):
    """El `CHECK` de la base lo rechaza igual, pero el error de psycopg no dice
    qué se intentó poner. Este valida antes y lo dice."""
    with pytest.raises(ValueError) as e:
        _factura(ambiente="testing")
    assert "testing" in str(e.value)


@pytest.mark.parametrize("ambiente", ["homologacion", "produccion"])
def test_el_ambiente_declarado_queda_guardado(conn, ambiente):
    fid = _factura(ambiente=ambiente)
    assert db_facturas.get_factura(fid)["ambiente"] == ambiente


def test_las_dos_facturas_se_distinguen(conn):
    """🔑 El control que hace que todo esto valga: dos comprobantes idénticos
    salvo el ambiente **no** son la misma cosa. Sin la columna, ésta era la
    consulta que no se podía hacer."""
    real = _factura(numero=1, ambiente="produccion")
    prueba = _factura(numero=2, ambiente="homologacion")

    assert db_facturas.get_factura(real)["ambiente"] == "produccion"
    assert db_facturas.get_factura(prueba)["ambiente"] == "homologacion"


def test_el_ambiente_se_normaliza(conn):
    """`Produccion`, ` produccion ` y `PRODUCCION` son lo mismo. Sin normalizar,
    el `CHECK` los rechaza y el error aparece recién al emitir, con el CAE ya
    pedido a ARCA."""
    fid = _factura(ambiente="  Produccion ")
    assert db_facturas.get_factura(fid)["ambiente"] == "produccion"


# -- `ambiente_de()`: la traduccion, en un solo lugar ------------------------

def test_ambiente_de_lee_el_dict_de_arca():
    """El caso normal: ARCA configurado, y el ambiente sale de ahi."""
    from libracore import arca_facturacion
    assert arca_facturacion.ambiente_de({"ambiente": "homologacion"}) == "homologacion"
    assert arca_facturacion.ambiente_de({"ambiente": "produccion"}) == "produccion"


def test_ambiente_de_sobrevive_al_string_de_dev():
    """🔴 **El tercer valor de `get_next_numero_with_arca` NO es siempre un
    dict**: en dev devuelve el string `"_dev_mock_"`. Un `.get()` derecho
    revienta con `AttributeError: 'str' object has no attribute 'get'` — pasó al
    escribir esto, y lo delataron 64 tests. El nombre de la variable no dice de
    que tipo es."""
    from libracore import arca_facturacion
    assert arca_facturacion.ambiente_de("_dev_mock_") == "produccion"


def test_ambiente_de_sin_arca_configurado():
    """Sin ARCA no hay CAE y el numero es el de la propia instancia: ese
    comprobante **es** el real del cliente, asi que va como `produccion` y entra
    al libro IVA, que es donde tiene que estar."""
    from libracore import arca_facturacion
    assert arca_facturacion.ambiente_de(None) == "produccion"
    assert arca_facturacion.ambiente_de({}) == "produccion"


def test_ambiente_de_no_deja_pasar_cualquier_cosa():
    """🔑 Un valor raro en la config **no** se propaga a la factura: el `CHECK`
    de la base lo rechazaria, pero recien al escribir, con el CAE ya pedido a
    ARCA. Ademas `create_factura` lo validaria de nuevo -- esto es que la
    traduccion no genere basura, no que la escritura la atrape."""
    from libracore import arca_facturacion
    assert arca_facturacion.ambiente_de({"ambiente": "testing"}) == "produccion"
    assert arca_facturacion.ambiente_de({"ambiente": ""}) == "produccion"
    assert arca_facturacion.ambiente_de({"ambiente": None}) == "produccion"


def test_ambiente_de_normaliza_igual_que_la_escritura():
    """Las dos puntas normalizan lo mismo. Si solo lo hiciera una, un
    `Homologacion` de la config llegaria como `produccion` a la factura."""
    from libracore import arca_facturacion
    assert arca_facturacion.ambiente_de({"ambiente": " Homologacion "}) == "homologacion"
