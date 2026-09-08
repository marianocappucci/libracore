"""El detalle por ítem: la aclaración corta que va DEBAJO del nombre del ítem.

Es distinto de `observations`, que es del presupuesto entero. Este texto
describe **un** renglón, es opcional renglón por renglón, y se imprime más
chico y más claro que el nombre del ítem.

Dos cosas que estos tests custodian:

- que el campo llegue **entero** desde el payload hasta el JSON guardado, y que
  un detalle vacío **no** escriba la clave (un presupuesto sin detalles queda
  igual que antes de la feature);
- que el PDF lo dibuje **con otro estilo**, no sólo que lo dibuje. Un assert de
  "aparece el texto" se cumpliría igual si saliera en negrita 8pt pegado al
  título, que es justo lo que no se quiere.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore import presupuestos_router as pr
from libracore.db import core
from libracore.db import remitos_presupuestos as rp
from libracore.db.schema import init_core_schema

# ── El camino de datos: payload → JSON guardado ───────────────────────────────


@pytest.fixture
def app_client(tmp_path):
    core.configure(db_path=str(tmp_path / "pres.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    cur = conn.execute(
        "INSERT INTO usuarios (username, nombre, email, password_hash, role, activo) "
        "VALUES ('t','T','','x','admin',1)"
    )
    uid = cur.lastrowid
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(pr.build_presupuestos_router(
        usuario_actual=lambda: {"id": uid},
        generar_pdf=lambda p: f"/tmp/p-{p['id']}.pdf",
        convertir_a_remito=lambda p, valorizado: None,
        smtp_configurado=lambda: True,
        enviar_comprobante=lambda **kw: None,
        moneda=lambda v: f"{v:.2f}",
    ))
    yield TestClient(app)
    core._db_path = None


def _payload(items):
    return {"date": "2026-09-08", "client_name": "Cliente X", "tax_rate": 0.21,
            "items": items}


def test_el_detalle_del_item_se_guarda(app_client):
    resp = app_client.post("/api/presupuestos", json=_payload([
        {"description": "Chapa galvanizada", "qty": 3, "unit_price": 1000,
         "detalle": "espesor 0,5 mm, corte a medida"},
    ]))
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["detalle"] == "espesor 0,5 mm, corte a medida"
    assert item["description"] == "Chapa galvanizada"   # el nombre no lo absorbe


def test_un_item_sin_detalle_no_escribe_la_clave(app_client):
    """El opcional es opcional: sin texto, el ítem queda como antes de la feature."""
    resp = app_client.post("/api/presupuestos", json=_payload([
        {"description": "Con espacios", "qty": 1, "unit_price": 10, "detalle": "  "},
        {"description": "Sin campo", "qty": 1, "unit_price": 10},
    ]))
    assert resp.status_code == 200, resp.text
    con_espacios, sin_campo = resp.json()["items"]
    assert "detalle" not in con_espacios   # sólo espacios == sin detalle
    assert "detalle" not in sin_campo


def test_editar_puede_borrar_el_detalle(app_client):
    """Vaciar el campo lo saca. Si no, un detalle puesto por error sería eterno."""
    creado = app_client.post("/api/presupuestos", json=_payload([
        {"description": "Item", "qty": 1, "unit_price": 10, "detalle": "aclaración"},
    ])).json()
    assert creado["items"][0]["detalle"] == "aclaración"

    editado = app_client.put(f"/api/presupuestos/{creado['id']}", json=_payload([
        {"description": "Item", "qty": 1, "unit_price": 10, "detalle": ""},
    ]))
    assert editado.status_code == 200, editado.text
    assert "detalle" not in editado.json()["items"][0]


def test_el_detalle_viaja_al_remito_en_la_conversion(app_client):
    """La conversión copia los ítems verbatim; el detalle es parte del ítem."""
    creado = app_client.post("/api/presupuestos", json=_payload([
        {"description": "Service de equipo", "qty": 1, "unit_price": 5000,
         "detalle": "incluye repuestos"},
    ])).json()
    remito = rp.convertir_presupuesto_a_remito(rp.get_presupuesto(creado["id"]))
    assert remito["items"][0]["detalle"] == "incluye repuestos"


# ── El PDF: el detalle sale con OTRO estilo, no sólo presente ─────────────────


def _pdf_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib

    from libracore import config_manager as cm
    from libracore import pdf_generator as pg
    importlib.reload(cm)
    importlib.reload(pg)
    return pg


def _espiar_trazos(pg, monkeypatch):
    """Cada texto dibujado en la tabla de ítems, con estilo, tamaño y color.

    Se engancha en la instancia de FPDF que recibe `_draw_items_table`, así se
    mide lo que la tabla realmente le pidió al motor de PDF y no una intención.
    """
    trazos: list[tuple] = []
    original = pg._draw_items_table

    def envoltura(pdf, items, **kw):
        estado = {"estilo": None, "tam": None, "color": None}
        set_font, set_color, cell = pdf.set_font, pdf.set_text_color, pdf.cell

        def _set_font(family, style="", size=0):
            estado["estilo"] = style
            estado["tam"] = size or estado["tam"]
            return set_font(family, style, size)

        def _set_color(r, g=None, b=None):
            estado["color"] = (r, g, b)
            return set_color(r, g, b)

        def _cell(w=None, h=None, text="", *a, **kw2):
            if str(text).strip():
                trazos.append((str(text).strip(), estado["estilo"],
                               estado["tam"], estado["color"]))
            return cell(w, h, text, *a, **kw2)

        pdf.set_font, pdf.set_text_color, pdf.cell = _set_font, _set_color, _cell
        try:
            return original(pdf, items, **kw)
        finally:
            pdf.set_font, pdf.set_text_color, pdf.cell = set_font, set_color, cell

    monkeypatch.setattr(pg, "_draw_items_table", envoltura)
    return trazos


def _presupuesto(items):
    return {
        "number": "PRES-00000001", "date": "2026-09-08", "valid_until": "2026-10-08",
        "client_name": "Cliente Test", "client_cuit": "", "client_address": "",
        "client_email": "", "client_phone": "", "items": items,
        "subtotal": 1000, "tax_amount": 210, "total": 1210, "tax_rate": 0.21,
        "observations": "",
    }


def _buscar(trazos, texto):
    encontrados = [t for t in trazos if t[0] == texto]
    assert len(encontrados) == 1, f"{texto!r} no se dibujó una sola vez: {encontrados}"
    return encontrados[0]


def test_el_pdf_dibuja_el_detalle_mas_chico_y_mas_claro(tmp_path, monkeypatch):
    pg = _pdf_module(tmp_path, monkeypatch)
    trazos = _espiar_trazos(pg, monkeypatch)

    pg.generate_pdf_presupuesto(_presupuesto([
        {"description": "Chapa galvanizada", "qty": 1, "unit_price": 1000,
         "subtotal": 1000, "detalle": "espesor de medio milimetro"},
    ]), output_dir=str(tmp_path))

    _, estilo_t, tam_t, color_t = _buscar(trazos, "Chapa galvanizada")
    _, estilo_d, tam_d, color_d = _buscar(trazos, "espesor de medio milimetro")

    assert tam_d < tam_t                      # letra más chica que la del ítem
    assert color_d == pg._MUTED               # y más clara
    assert color_t == pg._INK
    assert estilo_d == "I" and estilo_t == "B"


def test_sin_detalle_no_se_dibuja_ningun_renglon_secundario(tmp_path, monkeypatch):
    """Control: el estilo del detalle no aparece si el ítem no lo tiene.

    Sin este caso, el test de arriba pasaría igual si la tabla dibujara SIEMPRE
    un renglón en itálica —vacío o no— debajo de cada ítem.
    """
    pg = _pdf_module(tmp_path, monkeypatch)
    trazos = _espiar_trazos(pg, monkeypatch)

    pg.generate_pdf_presupuesto(_presupuesto([
        {"description": "Chapa galvanizada", "qty": 1, "unit_price": 1000, "subtotal": 1000},
    ]), output_dir=str(tmp_path))

    assert [t for t in trazos if t[0] == "Chapa galvanizada"]
    assert [t for t in trazos if t[1] == "I"] == []


def test_el_campo_explicito_le_gana_a_la_convencion_vieja(tmp_path, monkeypatch):
    """La descripción multilínea era el detalle antes de que el campo existiera.

    Sigue andando para los comprobantes ya guardados así, pero cuando los dos
    están, manda el campo: es el que la pantalla muestra y edita.
    """
    pg = _pdf_module(tmp_path, monkeypatch)
    trazos = _espiar_trazos(pg, monkeypatch)

    pg.generate_pdf_presupuesto(_presupuesto([
        {"description": "Item viejo\nlo de abajo era el detalle", "qty": 1,
         "unit_price": 1000, "subtotal": 1000},
        {"description": "Item nuevo\nesto queda tapado", "qty": 1,
         "unit_price": 1000, "subtotal": 1000, "detalle": "el campo manda"},
    ]), output_dir=str(tmp_path))

    assert _buscar(trazos, "lo de abajo era el detalle")[1] == "I"
    assert _buscar(trazos, "el campo manda")[1] == "I"
    assert [t for t in trazos if t[0] == "esto queda tapado"] == []
