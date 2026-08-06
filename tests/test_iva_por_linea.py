"""El presupuesto que mezcla alícuotas (2026-08-05).

Hasta ahora un comprobante tenía **una** alícuota y el total decía «IVA 21%».
Desde que cada línea puede traer la suya (LibraDesk, ítem 2 de los pendientes
transversales), ese rótulo puede ser falso: si hay líneas al 21% y otras
exentas, «IVA 21%» declara mal las segundas.

Lo que fijan estos tests:

1. 🔴 Que un presupuesto de **una sola alícuota salga exactamente igual que
   antes**. Es lo que protege a Contalibra y Restolibra, que facturan con
   clientes reales y no mezclan alícuotas.
2. Que al mezclar, el total **no invente un porcentaje** y el detalle por línea
   aparezca en la tabla.
"""
import importlib

from io import BytesIO

import pytest
from pypdf import PdfReader

from libracore import pdf_generator as pg


@pytest.fixture
def generador(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from libracore import config_manager as cm
    importlib.reload(cm)
    importlib.reload(pg)
    return pg


def _presupuesto(items, **extra):
    base = {
        "number": "0001-00000001",
        "date": "2026-08-05",
        "valid_until": "2026-08-20",
        "client_name": "Cliente Test",
        "client_cuit": "20111111112",
        "client_address": "", "client_email": "", "client_phone": "",
        "items": items,
        "subtotal": 1000, "tax_amount": 105, "total": 1105, "tax_rate": 0.21,
        "observations": "",
    }
    base.update(extra)
    return base


def _texto(path) -> str:
    with open(path, "rb") as f:
        lector = PdfReader(BytesIO(f.read()))
    return "\n".join(p.extract_text() or "" for p in lector.pages)


def _item(desc, precio, tax_rate=None, iva_pct=None):
    it = {"description": desc, "qty": 1, "unit_price": precio, "subtotal": precio}
    if tax_rate is not None:
        it["tax_rate"] = tax_rate
    if iva_pct is not None:
        it["iva_pct"] = iva_pct
    return it


# ── 🔴 Lo que no puede cambiar ────────────────────────────────────────────

def test_una_sola_alicuota_sigue_diciendo_el_porcentaje(generador, tmp_path):
    """El caso de siempre, y el único de Contalibra y Restolibra hoy."""
    p = _presupuesto([_item("Servicio", 1000, tax_rate=0.21)])
    texto = _texto(generador.generate_pdf_presupuesto(p, output_dir=str(tmp_path)))

    assert "IVA 21%" in texto


def test_un_presupuesto_sin_alicuota_por_linea_no_cambia(generador, tmp_path):
    """Los comprobantes ya guardados no tienen `tax_rate` por ítem."""
    p = _presupuesto([_item("Servicio", 1000), _item("Otro", 500)])
    texto = _texto(generador.generate_pdf_presupuesto(p, output_dir=str(tmp_path)))

    assert "IVA 21%" in texto


def test_un_presupuesto_a_medio_migrar_no_se_ve_como_mezcla(generador, tmp_path):
    """🔴 El caso que de verdad ejercita el fallback al `tax_rate` del
    documento: **algunos** ítems traen la alícuota y otros no.

    Pasa de verdad en la transición: un presupuesto guardado antes del cambio
    que se edita después, y al que se le agrega una línea nueva. Sin el
    fallback la línea vieja cuenta como `0`, el documento parece mezclar
    alícuotas y sale con la columna de IVA y sin porcentaje — cuando en
    realidad es 21% de punta a punta.

    ⚠️ La primera versión de este test usaba ítems **sin ninguna** alícuota, y
    pasaba con el fallback sacado: ahí todos caen a `0`, el conjunto sigue
    teniendo un solo elemento y no hay mezcla. Lo delató el arnés de falla
    forzada.
    """
    p = _presupuesto([
        _item("Línea nueva", 1000, tax_rate=0.21),
        _item("Línea vieja, sin el campo", 500),
    ])
    texto = _texto(generador.generate_pdf_presupuesto(p, output_dir=str(tmp_path)))

    assert "IVA 21%" in texto


def test_todas_las_lineas_con_la_misma_alicuota_tampoco_mezcla(generador, tmp_path):
    p = _presupuesto([
        _item("Uno", 500, tax_rate=0.21),
        _item("Dos", 500, tax_rate=0.21),
    ])
    texto = _texto(generador.generate_pdf_presupuesto(p, output_dir=str(tmp_path)))

    assert "IVA 21%" in texto


# ── Cuando sí mezcla ──────────────────────────────────────────────────────

def test_mezclando_alicuotas_el_total_no_inventa_un_porcentaje(generador, tmp_path):
    """🔴 Con una línea al 21% y otra exenta, «IVA 21%» declararía mal la
    segunda. Se muestra el monto sin porcentaje."""
    p = _presupuesto([
        _item("Servicio gravado", 1000, tax_rate=0.21, iva_pct=21),
        _item("Servicio exento", 500, tax_rate=0, iva_pct=0),
    ])
    texto = _texto(generador.generate_pdf_presupuesto(p, output_dir=str(tmp_path)))

    assert "IVA 21%" not in texto
    assert "IVA" in texto


def test_mezclando_aparece_la_columna_de_iva_por_linea(generador, tmp_path):
    """Sacar el porcentaje del total sin poner el detalle por línea dejaría un
    comprobante donde no se puede saber qué pagó IVA."""
    p = _presupuesto([
        _item("Servicio gravado", 1000, tax_rate=0.21, iva_pct=21),
        _item("Servicio exento", 500, tax_rate=0, iva_pct=0),
    ])
    texto = _texto(generador.generate_pdf_presupuesto(p, output_dir=str(tmp_path)))

    assert "IVA" in texto
    # La cabecera de la columna y los dos porcentajes de las filas.
    assert "21%" in texto
    assert "0%" in texto


def test_tres_alicuotas_distintas_tambien_mezclan(generador, tmp_path):
    p = _presupuesto([
        _item("Gravado", 1000, tax_rate=0.21, iva_pct=21),
        _item("Reducido", 500, tax_rate=0.105, iva_pct=10.5),
        _item("Exento", 300, tax_rate=0, iva_pct=0),
    ])
    texto = _texto(generador.generate_pdf_presupuesto(p, output_dir=str(tmp_path)))

    assert "IVA 21%" not in texto


def test_el_comprobante_sigue_siendo_un_pdf_valido_al_mezclar(generador, tmp_path):
    """La columna extra achica el ancho de la descripción: conviene fijar que
    el documento sigue generándose y no se rompe el layout."""
    p = _presupuesto([
        _item("Una descripción larga que ocupa varias líneas del comprobante "
              "y obliga a la tabla a envolver el texto", 1000, tax_rate=0.21, iva_pct=21),
        _item("Exento", 500, tax_rate=0, iva_pct=0),
    ])
    path = generador.generate_pdf_presupuesto(p, output_dir=str(tmp_path))

    with open(path, "rb") as f:
        assert f.read(5) == b"%PDF-"
