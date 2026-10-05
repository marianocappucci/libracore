"""La anulación con rastro de un comprobante sin CAE (ADR-022): `facturas.anulada_en`.

Hasta acá un comprobante sin CAE sólo se podía borrar, y su número desaparecía.
Anularlo lo deja en la base con su número, quién, cuándo y por qué. Lo que se
fija acá:

- **Sólo sin CAE y sin cobros.** Con CAE corresponde una nota de crédito; con
  cobros, esa plata entró y primero se anulan los cobros.
- **Sale de todo lo que suma**: libro IVA, resumen, tablero, reportes, cuenta
  corriente y notas previas. Y su número no se reusa.
- **No se opera sobre un anulado**: no se autoriza, no se cobra, no admite notas.
- El `DELETE` sigue como estaba.

Sin `ENV=development` y sin ARCA configurado: el comprobante sale **sin CAE**, que
es lo único que se puede anular. Los que necesitan CAE prenden el modo dev.
"""

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore import config_manager
from libracore import facturas_router as fr
from libracore import pdf_generator as pdf_gen
from libracore.db import core
from libracore.db import cuenta_corriente as db_cc
from libracore.db import dashboard as db_dashboard
from libracore.db import facturas as db_facturas
from libracore.db import libros_iva as db_libros_iva
from libracore.db import reportes as db_reportes
from libracore.db import resumen as db_resumen
from libracore.db.schema import init_core_schema

USUARIO = {"id": 1, "username": "admin", "nombre": "Administrador"}
API = "/api/facturas"
ADMIN = {"x-rol": "admin"}
FECHA = "2026-10-05"


@pytest.fixture
def client(tmp_path, monkeypatch):
    core.configure(db_path=str(tmp_path / "anulacion.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?, ?, ?, ?, ?)",
        (USUARIO["id"], USUARIO["username"], USUARIO["nombre"], "x", "admin"),
    )
    conn.execute("INSERT INTO clients (name, cuit_dni) VALUES ('Cerealera SA', '30555555556')")
    conn.commit()
    conn.close()
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setattr(pdf_gen, "FACTURAS_PDF_DIR", str(tmp_path / "pdf"))
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(tmp_path / "config.json"))

    def gate_admin(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    app = FastAPI()
    app.include_router(fr.build_comprobantes_router(usuario_actual=lambda: USUARIO, solo_admin=gate_admin))
    return TestClient(app)


def _emitir(client, **extra) -> dict:
    cuerpo = {
        "tipo": 11, "punto_venta": 1, "fecha": FECHA, "condicion_venta": "Contado",
        "client_name": "Cerealera SA", "client_cuit": "30555555556", "client_iva": "Responsable Inscripto",
        "items": [{"description": "Flete", "qty": 1, "unit_price": 1000.0}],
    }
    cuerpo.update(extra)
    r = client.post(API, json=cuerpo)
    assert r.status_code == 200, r.text
    return r.json()


def _anular(client, factura_id, motivo="Se cargó dos veces"):
    return client.post(f"{API}/{factura_id}/anular", json={"motivo": motivo}, headers=ADMIN)


# ── Anular ───────────────────────────────────────────────────────────────


def test_anular_deja_el_rastro_y_el_comprobante_en_la_base(client):
    factura = _emitir(client)
    assert not factura["cae"]
    r = _anular(client, factura["id"])
    assert r.status_code == 200, r.text
    anulada = r.json()["factura"]
    assert anulada["anulada_en"]
    assert anulada["anulada_por"] == USUARIO["id"]
    assert anulada["anulacion_motivo"] == "Se cargó dos veces"
    assert r.json()["pendiente"] == 0
    assert [f["id"] for f in client.get(API).json()["items"]] == [factura["id"]]


def test_el_numero_de_un_anulado_no_se_reusa(client):
    primera = _emitir(client)
    assert _anular(client, primera["id"]).status_code == 200
    assert _emitir(client)["numero"] == primera["numero"] + 1


def test_sin_cuerpo_se_anula_igual(client):
    factura = _emitir(client)
    r = client.post(f"{API}/{factura['id']}/anular", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["factura"]["anulacion_motivo"] == ""


def test_con_CAE_no_se_anula(client, monkeypatch):
    monkeypatch.setenv("ENV", "development")
    factura = _emitir(client)
    assert factura["cae"]
    r = _anular(client, factura["id"])
    assert r.status_code == 409
    assert "nota de crédito" in r.json()["detail"]


def test_dos_veces_no(client):
    factura = _emitir(client)
    assert _anular(client, factura["id"]).status_code == 200
    assert _anular(client, factura["id"]).status_code == 409


def test_con_cobros_no_se_anula(client):
    factura = _emitir(client)
    conn = core.get_connection()
    conn.execute(
        "INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, factura_id, medio_pago) "
        "VALUES (?, 'ingreso', 'cobro', 1000, ?, 'Efectivo')", (FECHA, factura["id"]))
    conn.commit()
    conn.close()
    r = _anular(client, factura["id"])
    assert r.status_code == 409
    assert "cobros" in r.json()["detail"]
    assert not db_facturas.get_factura(factura["id"])["anulada_en"]


def test_uno_que_no_existe_es_404(client):
    assert _anular(client, 999).status_code == 404


def test_anular_es_de_admin(client):
    factura = _emitir(client)
    assert client.post(f"{API}/{factura['id']}/anular").status_code == 403


def test_borrar_sigue_como_estaba(client):
    factura = _emitir(client)
    assert client.delete(f"{API}/{factura['id']}", headers=ADMIN).status_code == 200
    assert db_facturas.get_factura(factura["id"]) is None


# ── Sale de lo que suma ──────────────────────────────────────────────────


def test_sale_del_libro_iva_y_de_los_totales(client):
    vigente = _emitir(client)
    anulada = _emitir(client, items=[{"description": "Flete", "qty": 1, "unit_price": 5000.0}])
    assert _anular(client, anulada["id"]).status_code == 200

    assert [f["id"] for f in db_libros_iva.get_facturas_para_iva(FECHA, FECHA)] == [vigente["id"]]
    resumen = db_resumen.get_resumen_core(FECHA, FECHA)
    assert (resumen["facturado"], resumen["comprobantes"]) == (vigente["total"], 1)
    tablero = db_dashboard.get_dashboard_data(FECHA, FECHA)
    assert [f["id"] for f in tablero["facturas_sin_cobrar"]] == [vigente["id"]]
    assert db_reportes.get_reporte_resumen(FECHA, FECHA)["facturas_cantidad"] == 1
    assert db_reportes.get_reporte_resumen()["facturas_cantidad"] == 1


def test_el_debito_de_cuenta_corriente_se_anula_con_el_comprobante(client):
    cliente_id = 1
    factura = _emitir(client, condicion_venta="Cuenta Corriente")
    assert db_cc.get_cc_saldo(cliente_id) == factura["total"]
    assert _anular(client, factura["id"]).status_code == 200
    assert db_cc.get_cc_saldo(cliente_id) == 0


def test_una_nota_anulada_no_cuenta_como_nota_previa(client, monkeypatch):
    """La guarda de notas frena una nueva si hay una previa sin CAE: anulada, ya no frena."""
    monkeypatch.setenv("ENV", "development")
    factura = _emitir(client)
    nota_id = db_facturas.create_factura(
        13, 1, 1, FECHA, factura["cliente_cuit"], "Cerealera SA", 1, [], 1, 0, 1,
        cbte_asoc_tipo=factura["tipo"], cbte_asoc_pv=1, cbte_asoc_nro=factura["numero"],
        ambiente="produccion")
    assert [n["id"] for n in db_facturas.get_nc_de_factura(factura["tipo"], 1, factura["numero"])] == [nota_id]
    assert _anular(client, nota_id).status_code == 200
    assert db_facturas.get_nc_de_factura(factura["tipo"], 1, factura["numero"]) == []
    assert client.post(f"{API}/{factura['id']}/nota-credito", headers=ADMIN).status_code == 200


# ── No se opera sobre un anulado ─────────────────────────────────────────


@pytest.mark.parametrize("ruta, cuerpo", [
    ("autorizar", None),
    ("cobrar", {"pagos": [{"medio_id": 1, "monto": 1000}]}),
    ("nota-credito", None),
    ("nota-debito", None),
])
def test_un_anulado_no_se_autoriza_ni_se_cobra_ni_admite_notas(client, ruta, cuerpo):
    factura = _emitir(client)
    assert _anular(client, factura["id"]).status_code == 200
    r = client.post(f"{API}/{factura['id']}/{ruta}", json=cuerpo, headers=ADMIN)
    assert r.status_code == 409, r.text
    assert "anulado" in r.json()["detail"]
