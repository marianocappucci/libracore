"""El emisor de cada comprobante (`facturas.emisor_id`): varias razones sociales en una instancia.

El motor suponía un solo emisor —diez lugares tomaban `configs[0]`— y por eso
LibraCargo, que factura con varias razones sociales, tenía su propio modelo de
comprobantes. Lo que se fija acá:

- **Sin emisor no cambia nada.** Es lo que hacen los otros siete productos: el
  comprobante queda con `emisor_id` en `NULL` y se emite con la primera
  configuración activa, como siempre.
- **Con emisor, todo es de ese emisor**: la numeración, el punto de venta, las
  notas que le cuelgan y el par con el que se autoriza.
- Dos emisores con el mismo punto de venta tienen cada uno su Factura C
  0001-00000001, y el detalle de una no muestra las notas de la otra.

`ENV=development`: el motor numera local y simula el CAE, así que la emisión
corre entera sin salir a ARCA.
"""

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore import config_manager
from libracore import facturas_router as fr
from libracore import pdf_generator as pdf_gen
from libracore.db import arca_config as db_arca
from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db.schema import init_core_schema

USUARIO = {"id": 1, "username": "admin", "nombre": "Administrador"}
API = "/api/facturas"
ADMIN = {"x-rol": "admin"}


def _emisor(conn, empresa, cuit, punto_venta, *, activo=1):
    return conn.execute(
        "INSERT INTO arca_config (empresa, cuit, punto_venta, clave_path, certificado_path, activo) "
        "VALUES (?, ?, ?, '', '', ?)",
        (empresa, cuit, punto_venta, activo),
    ).lastrowid


@pytest.fixture
def base(tmp_path, monkeypatch):
    """Una instancia con dos emisores activos (PV 1 los dos) y uno dado de baja."""
    core.configure(db_path=str(tmp_path / "emisores.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?, ?, ?, ?, ?)",
        (USUARIO["id"], USUARIO["username"], USUARIO["nombre"], "x", "admin"),
    )
    ids = {
        "agencia": _emisor(conn, "agencia", "20-11111111-2", 1),
        "transporte": _emisor(conn, "transporte", "30222222223", 1),
        "baja": _emisor(conn, "vieja", "20333333334", 3, activo=0),
    }
    conn.commit()
    conn.close()
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setattr(pdf_gen, "FACTURAS_PDF_DIR", str(tmp_path / "pdf"))
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(tmp_path / "config.json"))
    return ids


@pytest.fixture
def client(base):
    def gate_admin(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    app = FastAPI()
    app.include_router(fr.build_comprobantes_router(usuario_actual=lambda: USUARIO, solo_admin=gate_admin))
    return TestClient(app)


def _emitir(client, **extra) -> dict:
    cuerpo = {
        "tipo": 11, "punto_venta": 1, "fecha": "2026-10-05", "condicion_venta": "Contado",
        "client_name": "Cerealera SA", "client_cuit": "30555555556", "client_iva": "Responsable Inscripto",
        "items": [{"description": "Flete Rosario - Buenos Aires", "qty": 1, "unit_price": 1000.0}],
    }
    cuerpo.update(extra)
    r = client.post(API, json=cuerpo)
    assert r.status_code == 200, r.text
    return r.json()


# ── Elegir la configuración ──────────────────────────────────────────────


def test_sin_emisor_es_la_primera_activa_como_siempre(base):
    assert db_arca.config_del_emisor()["empresa"] == "agencia"


def test_con_emisor_es_esa_y_ninguna_otra(base):
    assert db_arca.config_del_emisor(base["transporte"])["empresa"] == "transporte"


@pytest.mark.parametrize("cual", ["baja", "inexistente"])
def test_un_emisor_que_no_esta_activo_no_cae_a_otro(base, cual):
    """Caer a otra fila sería emitir con el CUIT equivocado; `None` se leería como «no hay ARCA»."""
    emisor_id = base.get(cual, 999)
    with pytest.raises(db_arca.EmisorDesconocido):
        db_arca.config_del_emisor(emisor_id)


def test_por_cuit_ignora_los_guiones(base):
    assert db_arca.config_por_cuit("20111111112")["empresa"] == "agencia"
    assert db_arca.config_por_cuit("30-22222222-3")["empresa"] == "transporte"


def test_por_cuit_sin_fila_es_none_y_una_dada_de_baja_no_cuenta(base):
    assert db_arca.config_por_cuit("20999999990") is None
    assert db_arca.config_por_cuit("20333333334") is None
    assert db_arca.config_por_cuit("") is None


def test_por_cuit_con_dos_filas_no_adivina(base):
    conn = core.get_connection()
    _emisor(conn, "agencia-bis", "20111111112", 2)
    conn.commit()
    conn.close()
    with pytest.raises(db_arca.ArcaAmbiguo):
        db_arca.config_por_cuit("20111111112")


# ── Numeración ───────────────────────────────────────────────────────────


def test_cada_emisor_numera_su_secuencia(base):
    def crear(emisor_id):
        numero = db_facturas.get_next_factura_numero(1, 11, "produccion", emisor_id)
        db_facturas.create_factura(
            11, 1, numero, "2026-10-05", "", "x", 5, [], 1, 0, 1,
            ambiente="produccion", emisor_id=emisor_id)
        return numero

    assert [crear(base["agencia"]), crear(base["agencia"]), crear(base["transporte"]), crear(None)] == [1, 2, 1, 1]


# ── El router ────────────────────────────────────────────────────────────


def test_sin_emisor_el_comprobante_queda_sin_emisor(client):
    assert _emitir(client)["emisor_id"] is None


def test_con_emisor_el_comprobante_es_suyo_y_numera_aparte(client, base):
    a1 = _emitir(client, emisor_id=base["agencia"])
    a2 = _emitir(client, emisor_id=base["agencia"])
    t1 = _emitir(client, emisor_id=base["transporte"])
    assert (a1["emisor_id"], a2["emisor_id"], t1["emisor_id"]) == (base["agencia"], base["agencia"], base["transporte"])
    assert (a1["numero"], a2["numero"], t1["numero"]) == (1, 2, 1)


@pytest.mark.parametrize("cual", ["baja", "inexistente"])
def test_emitir_con_un_emisor_que_no_esta_es_422_y_no_numera(client, base, cual):
    r = client.post(API, json={
        "tipo": 11, "fecha": "2026-10-05", "client_name": "X",
        "items": [{"description": "a", "qty": 1, "unit_price": 1.0}], "emisor_id": base.get(cual, 999),
    })
    assert r.status_code == 422, r.text
    assert client.get(API).json()["total"] == 0


def test_los_tipos_dan_el_punto_de_venta_del_emisor(client, base):
    conn = core.get_connection()
    conn.execute("UPDATE arca_config SET punto_venta = 7 WHERE id = ?", (base["transporte"],))
    conn.commit()
    conn.close()
    assert client.get(f"{API}/tipos", params={"emisor_id": base["transporte"]}).json()["punto_venta"] == 7
    assert client.get(f"{API}/tipos").json()["punto_venta"] == 1


def test_la_nota_es_del_emisor_de_su_factura_y_el_detalle_no_mezcla_emisores(client, base):
    """Las dos razones sociales tienen la Factura C 0001-00000001; la nota de una no cuelga de la otra."""
    de_agencia = _emitir(client, emisor_id=base["agencia"])
    de_transporte = _emitir(client, emisor_id=base["transporte"])
    assert (de_agencia["numero"], de_transporte["numero"]) == (1, 1)

    r = client.post(f"{API}/{de_agencia['id']}/nota-credito", headers=ADMIN)
    assert r.status_code == 200, r.text
    nota = r.json()
    assert nota["emisor_id"] == base["agencia"]

    detalle_agencia = client.get(f"{API}/{de_agencia['id']}").json()
    detalle_transporte = client.get(f"{API}/{de_transporte['id']}").json()
    assert [n["id"] for n in detalle_agencia["notas_credito"]] == [nota["id"]]
    assert detalle_transporte["notas_credito"] == []
    assert client.get(f"{API}/{nota['id']}").json()["factura_original"]["id"] == de_agencia["id"]

    # Y la de la otra razón social todavía admite su propia nota total: la guarda
    # de «ya tiene nota» no mira la nota del vecino.
    r = client.post(f"{API}/{de_transporte['id']}/nota-credito", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["emisor_id"] == base["transporte"]


def test_la_nota_de_debito_tambien_es_del_emisor(client, base):
    factura = _emitir(client, emisor_id=base["transporte"])
    r = client.post(f"{API}/{factura['id']}/nota-debito", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["emisor_id"] == base["transporte"]
