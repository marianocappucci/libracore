"""La alícuota que se declara en el Libro de IVA Compras.

Viene de `tests/test_libros_iva_alicuotas.py` de Contalibra y Restolibra, que
lo tenian escrito dos veces --idéntico salvo el docstring-- y lo corrian contra
estas mismas funciones a través de un shim de re-exports
(`app/web/routers/libros_iva.py`). El código vive acá desde P9-M4, así que el
test también. `test_generadores_reginfo` de `test_logs_reportes_libros.py`
cubre el caso feliz de 10,5%; esto fija los bordes.

🔴 **El defecto que fijan estos tests.** El export leía `iva_pct` de la fila de
egresos y lo buscaba en `_ALIC_CODE` / `_ALIC_PCT`, que están indexados en
**puntos** (21.0). Pero en egresos ese campo guarda una **fracción**: el alta
hace `monto_neto * iva_pct` sin dividir por 100 y el detalle lo muestra con
`iva_pct * 100`. O sea que el `.get()` no matcheaba nunca y **todas** las
compras caían al default `"5"` / `"21.00"`.

Con todo al 21% no se nota. Se nota con una compra al 10,5%: el archivo sale
declarando 21% al lado del neto y el IVA reales, que no cierran entre sí —
un REGINFO que ARCA rechaza.

Se encontró midiendo el export de la demo de Contalibra, que salía vacío por
otro motivo (sólo entran los egresos `tipo_comprobante = 'factura'`).
"""
from libracore.libros_iva import (
    _alicuota_de_egreso,
    _compras_alicuotas,
    _resumen_compras,
)


def _egreso(neto, iva_monto, iva_pct, numero="0003-00001842"):
    return {
        "tipo_comprobante": "factura", "numero": numero,
        "monto_neto": neto, "iva_monto": iva_monto, "iva_pct": iva_pct,
    }


# ── La alícuota, derivada del importe ─────────────────────────────────────

def test_veintiuno():
    assert _alicuota_de_egreso(100000, 21000) == 21.0


def test_diez_y_medio():
    """🔴 El caso que rompía: con el campo crudo salía 21%."""
    assert _alicuota_de_egreso(100000, 10500) == 10.5


def test_veintisiete():
    assert _alicuota_de_egreso(100000, 27000) == 27.0


def test_redondea_a_la_alicuota_de_arca():
    """Un centavo de diferencia por redondeo no debe inventar una alícuota que
    ARCA no tiene."""
    assert _alicuota_de_egreso(100000, 10499.62) == 10.5


def test_sin_iva_es_cero():
    assert _alicuota_de_egreso(100000, 0) == 0.0


def test_sin_neto_no_divide_por_cero():
    assert _alicuota_de_egreso(0, 5000) == 0.0


# ── El archivo que se baja ────────────────────────────────────────────────

def test_el_archivo_declara_la_alicuota_real():
    """La verificación que importa: se mira el campo 6 de la línea, que es lo
    que lee ARCA, no el helper."""
    linea = _compras_alicuotas([_egreso(100000, 10500, 0.105)]).split("|")

    assert linea[3] == "4", "código de alícuota de 10,5%"
    assert linea[5] == "10.50"


def test_el_veintiuno_sigue_saliendo_bien():
    """La mitad que protege lo que ya funcionaba: el caso mayoritario no se
    movió."""
    linea = _compras_alicuotas([_egreso(100000, 21000, 0.21)]).split("|")

    assert linea[3] == "5"
    assert linea[5] == "21.00"


def test_una_fila_vieja_en_puntos_tambien_sale_bien():
    """Derivar del importe hace que no importe en qué unidad quedó guardado el
    campo: las filas cargadas antes de esto salen igual de bien."""
    linea = _compras_alicuotas([_egreso(100000, 10500, 10.5)]).split("|")

    assert linea[5] == "10.50"


def test_los_egresos_sin_iva_no_entran():
    assert _compras_alicuotas([_egreso(100000, 0, 0)]) == ""


# ── La tarjeta de resumen ─────────────────────────────────────────────────

def test_el_resumen_agrupa_en_puntos():
    """Tercer lugar con la misma confusión: el resumen agrupaba por el campo
    crudo, así que la tarjeta encabezaba la columna con "0.21%"."""
    r = _resumen_compras([_egreso(100000, 21000, 0.21)])

    assert list(r["por_tasa"]) == [21.0]


def test_el_resumen_junta_las_filas_viejas_con_las_nuevas():
    """🔴 Lo que agrupar por el campo crudo rompía de verdad: dos compras al
    21% guardadas en unidades distintas salían como **dos tasas separadas**, y
    ninguna de las dos sumaba bien."""
    r = _resumen_compras([_egreso(100000, 21000, 0.21), _egreso(50000, 10500, 21.0)])

    assert list(r["por_tasa"]) == [21.0]
    assert r["por_tasa"][21.0]["cbtes"] == 2
    assert r["por_tasa"][21.0]["neto"] == 150000
