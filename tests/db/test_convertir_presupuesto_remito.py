"""La conversión presupuesto→remito, ejecutada contra PostgreSQL.

Es la lógica que estaba reimplementada por producto (Contalibra/Restolibra
byte-idénticas, LibraDesk con su wrapper). Ahora vive en el motor
(`convertir_presupuesto_a_remito`); esto fija su contrato: copia los importes
verbatim, deja el presupuesto linkeado, y opcionalmente es idempotente y genera
el PDF por callback (el template es arista del producto).
"""
import os

import pytest

from libracore.db import core
from libracore.db import remitos_presupuestos as rp


def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    return url


@pytest.fixture
def db(crear_schema):
    """Base PostgreSQL limpia con el schema REAL (init_core_schema + alembic)."""
    core.configure(db_path=_url())
    with core.get_connection() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
    with core.get_connection() as c:
        crear_schema(c)
    yield
    core._db_path = None


def _crear_presupuesto():
    return rp.create_presupuesto(
        number="P-0001", date="2026-09-03", valid_until="2026-09-30",
        client_id=None, client_name="Distribuidora Test", client_address="Calle 1",
        client_cuit="20-11111111-2", client_email="d@test.com", client_phone="011",
        items=[{"description": "Producto A", "qty": 3, "unit_price": 100.0, "subtotal": 300.0}],
        subtotal=300.0, tax_rate=21.0, tax_amount=63.0, total=363.0,
        observations="entrega lunes",
    )


def test_convierte_copiando_los_importes_y_linkea(db):
    pres = rp.get_presupuesto(_crear_presupuesto())
    remito = rp.convertir_presupuesto_a_remito(pres)

    assert remito["items"] == pres["items"]
    assert remito["subtotal"] == 300.0
    assert remito["tax_rate"] == 21.0
    assert remito["tax_amount"] == 63.0
    assert remito["total"] == 363.0
    assert remito["client_name"] == "Distribuidora Test"
    assert remito["client_cuit"] == "20-11111111-2"
    assert remito["observations"] == "entrega lunes"
    # el presupuesto quedó linkeado al remito recién creado
    assert rp.get_presupuesto(pres["id"])["remito_id"] == remito["id"]


def test_sin_idempotente_crea_siempre_un_remito_nuevo(db):
    pres = rp.get_presupuesto(_crear_presupuesto())
    r1 = rp.convertir_presupuesto_a_remito(pres)
    # `pres` es la lectura vieja (sin remito_id); volver a convertir crea otro,
    # que es el comportamiento actual de Contalibra/Restolibra ("aceptar y
    # convertir" no chequea si ya había uno).
    r2 = rp.convertir_presupuesto_a_remito(pres)
    assert r1["id"] != r2["id"]
    assert len(rp.get_all_remitos()) == 2


def test_idempotente_no_crea_un_segundo_remito(db):
    pres = rp.get_presupuesto(_crear_presupuesto())
    r1 = rp.convertir_presupuesto_a_remito(pres)
    # releer para traer el remito_id ya linkeado, y convertir idempotente
    pres2 = rp.get_presupuesto(pres["id"])
    r2 = rp.convertir_presupuesto_a_remito(pres2, idempotente=True)
    assert r2["id"] == r1["id"]
    assert len(rp.get_all_remitos()) == 1


def test_generar_pdf_recibe_el_remito_y_guarda_la_ruta(db):
    pres = rp.get_presupuesto(_crear_presupuesto())
    recibido = {}

    def fake_pdf(remito):
        recibido["remito"] = remito
        return "/tmp/remito-fake.pdf"

    remito = rp.convertir_presupuesto_a_remito(pres, generar_pdf=fake_pdf)
    # el callback recibió el remito ya creado (dict con ítems parseados) ...
    assert recibido["remito"]["id"] == remito["id"]
    assert recibido["remito"]["items"] == pres["items"]
    # ... y la ruta del PDF quedó guardada en el remito
    assert rp.get_remito(remito["id"])["pdf_path"] == "/tmp/remito-fake.pdf"
