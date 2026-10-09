"""La capa HTTP de la pre factura, montada como la montaría un producto (ADR-030).

Lo que se fija acá: el gate del producto llega a todas las rutas; el producto fija de quién son las
pre facturas (las de otro origen dan 404 y no se cuelan en el listado ni en los contadores); los códigos
(404, 409, 422, 400, 502); el PDF sale como `application/pdf`; quién aceptó o anuló sale del
`usuario_actual` del producto y no del cuerpo; y los ganchos `al_crear`, `al_editar` y `al_anular`
corren **en la misma transacción** que el cambio: si el gancho falla, no queda nada escrito.
"""
import io
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pypdf import PdfReader

from libracore import config_manager, pre_facturas, pre_facturas_router
from libracore.db import core
from libracore.db.schema import init_core_schema


@pytest.fixture(params=["sqlite", "postgres"])
def motor(request, tmp_path):
    if request.param == "postgres":
        url = os.environ.get("LIBRACORE_POSTGRES_URL")
        if not url:
            pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
        import psycopg

        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True) as c:
            c.execute("DROP SCHEMA IF EXISTS public CASCADE")
            c.execute("CREATE SCHEMA public")
        core.configure(db_path=url)
    else:
        core.configure(db_path=str(tmp_path / "pre_facturas_router.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.commit()
    yield request.param
    core._db_path = None
    core._database_url = None


@pytest.fixture(autouse=True)
def _empresa_ficticia(monkeypatch):
    cfg = {"empresa_nombre": "Transportes del Plata S.R.L.", "empresa_cuit": "30-12345678-1",
           "empresa_iva_condition": "Responsable Inscripto"}
    monkeypatch.setattr(config_manager, "load", lambda *a, **k: dict(cfg))


ADMIN = {"x-rol": "admin", "x-usuario": "mariano"}
FLETE = {"description": "Flete Rosario - Buenos Aires", "qty": 1, "unit_price": 100000.0, "iva_rate": 0.21}


class Producto:
    """Un producto de juguete: arma el router con su gate y sus ganchos, y anota qué le pidieron."""

    def __init__(self, **opciones):
        self.llamadas: list = []
        self.opciones = opciones

    def al_crear(self, conn, pre_factura, datos):
        self.llamadas.append(("crear", pre_factura["numero_interno"]))

    def al_editar(self, conn, pre_factura, datos):
        self.llamadas.append(("editar", pre_factura["numero_interno"], pre_factura["estado"]))

    def al_anular(self, conn, pre_factura, datos):
        self.llamadas.append(("anular", pre_factura["numero_interno"], datos["motivo"]))

    def cliente(self, **extra):
        def gate_admin(x_rol: str = Header(default="")):
            if x_rol != "admin":
                raise HTTPException(403, "solo administradores")

        app = FastAPI()
        opciones = dict(origen_producto="libracargo", origen_instancia="demo",
                        usuario_actual=lambda request: request.headers.get("x-usuario", ""),
                        dependencies=[Depends(gate_admin)], al_crear=self.al_crear,
                        al_editar=self.al_editar, al_anular=self.al_anular)
        opciones.update(self.opciones)
        opciones.update(extra)
        app.include_router(pre_facturas_router.build_pre_facturas_router(**opciones))
        return TestClient(app)


@pytest.fixture
def producto(motor):
    return Producto()


@pytest.fixture
def api(producto):
    return producto.cliente()


def _cuerpo(**kwargs):
    base = dict(cliente_razon="Juan Pérez", cliente_cuit="20-12345678-6", items=[FLETE], tipo_comprobante=1,
                fecha_sugerida="2026-10-06")
    base.update(kwargs)
    return base


def _crear(api, **kwargs):
    r = api.post("/api/pre-facturas", json=_cuerpo(**kwargs), headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _arca():
    with core.get_connection() as c:
        cur = c.execute("INSERT INTO arca_config (empresa, cuit, punto_venta, clave_path, certificado_path) "
                        "VALUES ('Transportes del Plata S.R.L.', '30-12345678-1', 1, 'k', 'c')")
        c.commit()
        return cur.lastrowid


# ── El gate y el alcance ─────────────────────────────────────────────────────


def test_el_gate_del_producto_llega_a_todas_las_rutas(api):
    pf = _crear(api)
    i = pf["id"]
    rutas = [("get", "/api/pre-facturas"), ("post", "/api/pre-facturas"), ("get", f"/api/pre-facturas/{i}"),
             ("put", f"/api/pre-facturas/{i}"), ("get", f"/api/pre-facturas/{i}/pdf"),
             ("post", f"/api/pre-facturas/{i}/enviar-email"), ("post", f"/api/pre-facturas/{i}/aceptar"),
             ("post", f"/api/pre-facturas/{i}/anular")]
    for metodo, ruta in rutas:
        r = getattr(api, metodo)(ruta, **({"json": {}} if metodo in ("post", "put") else {}))
        assert r.status_code == 403, (metodo, ruta)
    assert api.get(f"/api/pre-facturas/{i}", headers=ADMIN).json()["estado"] == "pendiente"


def test_crear_devuelve_201_con_el_numero_y_el_total_y_ignora_lo_que_no_es_suyo(api):
    r = api.post("/api/pre-facturas", headers=ADMIN, json=_cuerpo(
        origen_producto="otro", origen_instancia="x", total=1, numero_interno="PF-0099", estado="facturado"))
    assert r.status_code == 201
    pf = r.json()
    assert (pf["numero_interno"], pf["total"], pf["estado"]) == ("PF-0001", 121000.0, "pendiente")
    assert (pf["origen_producto"], pf["origen_instancia"]) == ("libracargo", "demo")


def test_las_pre_facturas_de_otro_origen_dan_404_y_no_se_ven(api, motor):
    propia = _crear(api)
    ajena = pre_facturas.crear(origen_producto="libradesk", origen_instancia="demo", cliente_razon="Ajeno",
                               items=[FLETE])
    otra_instancia = pre_facturas.crear(origen_producto="libracargo", origen_instancia="otra",
                                        cliente_razon="Otra", items=[FLETE])
    for pf in (ajena, otra_instancia):
        i = pf["id"]
        assert api.get(f"/api/pre-facturas/{i}", headers=ADMIN).status_code == 404
        assert api.get(f"/api/pre-facturas/{i}/pdf", headers=ADMIN).status_code == 404
        assert api.put(f"/api/pre-facturas/{i}", json={"observaciones": "x"}, headers=ADMIN).status_code == 404
        assert api.post(f"/api/pre-facturas/{i}/aceptar", headers=ADMIN).status_code == 404
        assert api.post(f"/api/pre-facturas/{i}/anular", json={}, headers=ADMIN).status_code == 404
        assert api.post(f"/api/pre-facturas/{i}/enviar-email", json={"email": "a@example.com"},
                        headers=ADMIN).status_code == 404
        assert pre_facturas.get(i)["estado"] == "pendiente"
    lista = api.get("/api/pre-facturas", headers=ADMIN).json()
    assert [p["id"] for p in lista["items"]] == [propia["id"]]
    assert lista["counts"]["pendiente"] == 1


def test_listar_filtra_y_cuenta(api):
    a = _crear(api, cliente_razon="Juan Pérez")
    b = _crear(api, cliente_razon="Acopio del Sur S.A.", cliente_cuit="30-87654321-0")
    api.post(f"/api/pre-facturas/{b['id']}/aceptar", headers=ADMIN)
    lista = api.get("/api/pre-facturas", headers=ADMIN).json()
    assert [p["id"] for p in lista["items"]] == [b["id"], a["id"]]
    assert lista["counts"] == {"pendiente": 1, "enviado": 0, "aceptado": 1, "facturado": 0, "descartado": 0}
    assert [p["id"] for p in api.get("/api/pre-facturas?estado=aceptado", headers=ADMIN).json()["items"]] == [b["id"]]
    assert [p["id"] for p in api.get("/api/pre-facturas?cliente=pérez", headers=ADMIN).json()["items"]] == [a["id"]]
    assert api.get("/api/pre-facturas?estado=inventado", headers=ADMIN).status_code == 422


# ── Alta, edición y estados ──────────────────────────────────────────────────


@pytest.mark.parametrize("cuerpo", [
    _cuerpo(items=[]),
    _cuerpo(cliente_razon=" "),
    _cuerpo(tipo_comprobante=3),
    _cuerpo(tipo_comprobante=11),                       # clase C con IVA
    _cuerpo(emisor_id=999),
    _cuerpo(origen_tipo="otro"),
])
def test_un_alta_invalida_da_422_y_no_escribe(api, cuerpo):
    assert api.post("/api/pre-facturas", json=cuerpo, headers=ADMIN).status_code == 422
    assert api.get("/api/pre-facturas", headers=ADMIN).json()["items"] == []


@pytest.mark.parametrize("campo", ["cliente_id", "emisor_id", "tipo_comprobante"])
def test_un_booleano_no_es_un_numero(api, campo):
    assert api.post("/api/pre-facturas", json=_cuerpo(**{campo: True}), headers=ADMIN).status_code == 422
    for item in ({"qty": True}, {"unit_price": False}, {"iva_rate": True}):
        r = api.post("/api/pre-facturas", json=_cuerpo(items=[{**FLETE, **item}]), headers=ADMIN)
        assert r.status_code == 422
    assert api.get("/api/pre-facturas", headers=ADMIN).json()["items"] == []


def test_editar_cambia_lo_que_se_manda_y_recalcula(api):
    pf = _crear(api)
    r = api.put(f"/api/pre-facturas/{pf['id']}", headers=ADMIN, json={
        "items": [{**FLETE, "qty": 3}], "observaciones": "Entrega en planta", "numero_interno": "PF-0099"})
    assert r.status_code == 200
    nuevo = r.json()
    assert (nuevo["total"], nuevo["observaciones"], nuevo["cliente_razon"], nuevo["numero_interno"]) == (
        363000.0, "Entrega en planta", "Juan Pérez", "PF-0001")


def test_editar_una_aceptada_la_devuelve_a_pendiente(api):
    pf = _crear(api)
    api.post(f"/api/pre-facturas/{pf['id']}/aceptar", headers=ADMIN)
    nuevo = api.put(f"/api/pre-facturas/{pf['id']}", json={"cliente_domicilio": "Calle Falsa 123"},
                    headers=ADMIN).json()
    assert nuevo["estado"] == "pendiente" and nuevo["aceptado_por"] is None


def test_editar_con_null_en_un_texto_no_lo_vacia(api):
    pf = _crear(api, observaciones="Importante")
    nuevo = api.put(f"/api/pre-facturas/{pf['id']}", json={"observaciones": None}, headers=ADMIN).json()
    assert nuevo["observaciones"] == "Importante"
    sin_emisor = api.put(f"/api/pre-facturas/{pf['id']}", json={"emisor_id": _arca()}, headers=ADMIN).json()
    assert sin_emisor["emisor_id"] is not None
    assert api.put(f"/api/pre-facturas/{pf['id']}", json={"emisor_id": None}, headers=ADMIN).json()["emisor_id"] is None


def test_aceptar_toma_el_usuario_del_producto_y_no_del_cuerpo(api):
    pf = _crear(api)
    r = api.post(f"/api/pre-facturas/{pf['id']}/aceptar", json={"usuario": "intruso"}, headers=ADMIN)
    assert r.status_code == 200
    assert (r.json()["estado"], r.json()["aceptado_por"]) == ("aceptado", "mariano")


def test_anular_deja_quien_y_el_motivo_y_despues_no_se_toca(api):
    pf = _crear(api)
    r = api.post(f"/api/pre-facturas/{pf['id']}/anular", json={"motivo": "pedido duplicado"}, headers=ADMIN)
    assert r.status_code == 200
    assert (r.json()["estado"], r.json()["resuelto_por"], r.json()["motivo_descarte"]) == (
        "descartado", "mariano", "pedido duplicado")
    i = pf["id"]
    assert api.put(f"/api/pre-facturas/{i}", json={"observaciones": "x"}, headers=ADMIN).status_code == 409
    assert api.post(f"/api/pre-facturas/{i}/aceptar", headers=ADMIN).status_code == 409
    assert api.post(f"/api/pre-facturas/{i}/anular", json={}, headers=ADMIN).status_code == 409
    assert api.post(f"/api/pre-facturas/{i}/enviar-email", json={"email": "a@example.com"},
                    headers=ADMIN).status_code == 409


def test_una_facturada_no_se_edita_ni_se_anula(api):
    pf = _crear(api)
    with core.get_connection() as c:
        c.execute("INSERT INTO facturas (tipo, punto_venta, numero, fecha, items, subtotal, iva_amount, total) "
                  "VALUES (1, 1, 1, '2026-10-07', '[]', 1, 0, 1)")
        c.commit()
    pre_facturas.marcar_facturada(pf["id"], 1)
    assert api.put(f"/api/pre-facturas/{pf['id']}", json={"observaciones": "x"}, headers=ADMIN).status_code == 409
    assert api.post(f"/api/pre-facturas/{pf['id']}/anular", json={}, headers=ADMIN).status_code == 409
    assert api.get(f"/api/pre-facturas/{pf['id']}", headers=ADMIN).json()["estado"] == "facturado"


def test_una_que_no_existe_da_404(api):
    assert api.get("/api/pre-facturas/999", headers=ADMIN).status_code == 404
    assert api.put("/api/pre-facturas/999", json={}, headers=ADMIN).status_code == 404


# ── La cuenta de cobro de una FCE (ADR-040) ─────────────────────────────────

CBU_A, CBU_B, CBU_C = "1" * 22, "2" * 22, "3" * 22
EMPRESA_ARCA = "Transportes del Plata S.R.L."
CUENTA_A = {"cbu": CBU_A, "alias": "", "etiqueta": "Principal"}
CUENTA_B = {"cbu": CBU_B, "alias": "cuenta.segunda", "etiqueta": "Segunda"}


def _arca_con_cuentas(cuentas=(CUENTA_A, CUENTA_B), predeterminado=CBU_A):
    """El emisor con sus CBU cargados. Devuelve el `arca_config.id`."""
    from libracore.db import arca_config as db_arca
    emisor = _arca()
    db_arca.actualizar_arca_config(
        EMPRESA_ARCA, fce_cbu=predeterminado, fce_transmision="SCA", fce_cbus=list(cuentas))
    return emisor


def _fce(api, **kwargs):
    return api.post("/api/pre-facturas", headers=ADMIN, json=_cuerpo(
        tipo_comprobante=201, cliente_cuit="30-70933285-2", cliente_razon="Cliente SA", **kwargs))


def _texto_del_pdf(api, pf_id) -> str:
    r = api.get(f"/api/pre-facturas/{pf_id}/pdf", headers=ADMIN)
    assert r.status_code == 200
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)


def test_crear_una_fce_con_un_cbu_de_la_lista_lo_guarda_y_devuelve_la_cuenta(api):
    emisor = _arca_con_cuentas()
    r = _fce(api, emisor_id=emisor, fce_cbu=CBU_B)
    assert r.status_code == 201, r.text
    pf = r.json()
    assert pf["fce_cbu"] == CBU_B
    assert pf["fce_cuenta"] == CUENTA_B


def test_crear_una_fce_eligiendo_por_alias_guarda_el_cbu(api):
    emisor = _arca_con_cuentas()
    pf = _fce(api, emisor_id=emisor, fce_cbu="Cuenta.Segunda").json()
    assert pf["fce_cbu"] == CBU_B and pf["fce_cuenta"]["alias"] == "cuenta.segunda"


def test_crear_una_fce_sin_elegir_guarda_null_y_la_cuenta_es_la_predeterminada(api):
    emisor = _arca_con_cuentas(predeterminado=CBU_B)
    pf = _fce(api, emisor_id=emisor).json()
    assert pf["fce_cbu"] is None
    assert pf["fce_cuenta"] == CUENTA_B


def test_crear_una_fce_con_un_cbu_fuera_de_la_lista_es_422_y_no_escribe(api):
    emisor = _arca_con_cuentas()
    for elegido in (CBU_C, "alias.inexistente"):
        r = _fce(api, emisor_id=emisor, fce_cbu=elegido)
        assert r.status_code == 422
        assert "no está entre los cargados" in r.json()["detail"]
    assert api.get("/api/pre-facturas", headers=ADMIN).json()["items"] == []


def test_crear_una_pre_factura_que_no_es_fce_ignora_el_cbu(api):
    emisor = _arca_con_cuentas()
    pf = _crear(api, emisor_id=emisor, tipo_comprobante=1, fce_cbu=CBU_C)  # ni siquiera es de la lista
    assert pf["fce_cbu"] is None and pf["fce_cuenta"] is None


def test_una_fce_de_un_emisor_sin_cbu_no_tiene_cuenta(api):
    pf = _fce(api, emisor_id=_arca()).json()
    assert pf["fce_cbu"] is None and pf["fce_cuenta"] is None


def test_la_cuenta_sale_en_el_detalle_y_en_el_listado(api):
    emisor = _arca_con_cuentas()
    pf = _fce(api, emisor_id=emisor, fce_cbu=CBU_B).json()
    assert api.get(f"/api/pre-facturas/{pf['id']}", headers=ADMIN).json()["fce_cuenta"] == CUENTA_B
    items = api.get("/api/pre-facturas", headers=ADMIN).json()["items"]
    assert items[0]["fce_cbu"] == CBU_B and items[0]["fce_cuenta"] == CUENTA_B


def test_editar_la_cuenta_por_cbu_o_por_alias(api):
    emisor = _arca_con_cuentas()
    pf = _fce(api, emisor_id=emisor).json()
    url = f"/api/pre-facturas/{pf['id']}"
    assert api.put(url, headers=ADMIN, json={"fce_cbu": CBU_B}).json()["fce_cbu"] == CBU_B
    assert api.put(url, headers=ADMIN, json={"fce_cbu": "cuenta.segunda"}).json()["fce_cbu"] == CBU_B
    assert api.put(url, headers=ADMIN, json={"fce_cbu": CBU_A}).json()["fce_cbu"] == CBU_A


def test_editar_sin_mandar_la_cuenta_o_con_null_no_la_toca_y_vacio_vuelve_a_la_predeterminada(api):
    emisor = _arca_con_cuentas()
    pf = _fce(api, emisor_id=emisor, fce_cbu=CBU_B).json()
    url = f"/api/pre-facturas/{pf['id']}"
    assert api.put(url, headers=ADMIN, json={"observaciones": "x"}).json()["fce_cbu"] == CBU_B
    assert api.put(url, headers=ADMIN, json={"fce_cbu": None}).json()["fce_cbu"] == CBU_B
    vuelta = api.put(url, headers=ADMIN, json={"fce_cbu": ""}).json()
    assert vuelta["fce_cbu"] is None and vuelta["fce_cuenta"] == CUENTA_A


def test_editar_con_un_cbu_fuera_de_la_lista_es_422_y_no_cambia_nada(api):
    emisor = _arca_con_cuentas()
    pf = _fce(api, emisor_id=emisor, fce_cbu=CBU_B).json()
    r = api.put(f"/api/pre-facturas/{pf['id']}", headers=ADMIN, json={"fce_cbu": CBU_C})
    assert r.status_code == 422
    assert api.get(f"/api/pre-facturas/{pf['id']}", headers=ADMIN).json()["fce_cbu"] == CBU_B


def test_cambiar_la_cuenta_de_una_aceptada_la_devuelve_a_pendiente(api):
    emisor = _arca_con_cuentas()
    pf = _fce(api, emisor_id=emisor).json()
    api.post(f"/api/pre-facturas/{pf['id']}/aceptar", headers=ADMIN)
    nuevo = api.put(f"/api/pre-facturas/{pf['id']}", headers=ADMIN, json={"fce_cbu": CBU_B}).json()
    assert nuevo["estado"] == "pendiente" and nuevo["aceptado_por"] is None


def test_pasar_una_fce_a_un_tipo_que_no_lo_es_borra_la_cuenta(api):
    emisor = _arca_con_cuentas()
    pf = _fce(api, emisor_id=emisor, fce_cbu=CBU_B).json()
    nuevo = api.put(f"/api/pre-facturas/{pf['id']}", headers=ADMIN, json={"tipo_comprobante": 1}).json()
    assert nuevo["fce_cbu"] is None and nuevo["fce_cuenta"] is None
    # y volver a FCE no la resucita
    assert api.put(f"/api/pre-facturas/{pf['id']}", headers=ADMIN,
                   json={"tipo_comprobante": 201}).json()["fce_cbu"] is None


def test_cambiar_de_emisor_con_una_cuenta_que_el_otro_no_tiene_es_422(api):
    from libracore.db import arca_config as db_arca
    emisor = _arca_con_cuentas()
    with core.get_connection() as c:
        otro = c.execute("INSERT INTO arca_config (empresa, cuit, punto_venta, clave_path, certificado_path) "
                         "VALUES ('Otra S.A.', '30-71111111-3', 1, 'k', 'c')").lastrowid
        c.commit()
    db_arca.actualizar_arca_config("Otra S.A.", fce_cbu=CBU_C, fce_transmision="SCA")
    pf = _fce(api, emisor_id=emisor, fce_cbu=CBU_B).json()
    url = f"/api/pre-facturas/{pf['id']}"
    r = api.put(url, headers=ADMIN, json={"emisor_id": otro})
    assert r.status_code == 422 and "nuevo emisor" in r.json()["detail"]
    # con la cuenta vacía (la predeterminada del otro) sí
    ok = api.put(url, headers=ADMIN, json={"emisor_id": otro, "fce_cbu": ""}).json()
    assert ok["emisor_id"] == otro and ok["fce_cuenta"]["cbu"] == CBU_C


def test_el_pdf_de_una_fce_dice_en_que_cuenta_se_cobra(api):
    emisor = _arca_con_cuentas()
    pf = _fce(api, emisor_id=emisor, fce_cbu=CBU_B).json()
    texto = _texto_del_pdf(api, pf["id"])
    assert f"Cobro en: CBU {CBU_B} (alias cuenta.segunda)" in texto


def test_el_pdf_sin_cuenta_elegida_usa_la_predeterminada_y_sin_alias_no_pone_parentesis(api):
    emisor = _arca_con_cuentas()
    pf = _fce(api, emisor_id=emisor).json()
    texto = _texto_del_pdf(api, pf["id"])
    assert f"Cobro en: CBU {CBU_A}" in texto and "alias" not in texto


def test_el_pdf_de_una_pre_factura_que_no_es_fce_no_tiene_cobro(api):
    emisor = _arca_con_cuentas()
    pf = _crear(api, emisor_id=emisor, fce_cbu=CBU_B)
    assert "Cobro en" not in _texto_del_pdf(api, pf["id"])


# ── El PDF y el correo ───────────────────────────────────────────────────────


def test_el_pdf_sale_como_application_pdf_con_el_sello(api):
    pf = _crear(api)
    r = api.get(f"/api/pre-facturas/{pf['id']}/pdf", headers=ADMIN)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'inline; filename="PF-0001.pdf"'
    texto = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
    assert "PRE FACTURA" in texto and "PF-0001" in texto and "CAE" not in texto


def test_el_emisor_del_pdf_lo_pone_el_producto(motor):
    producto = Producto()
    api = producto.cliente(emisor_del_pdf=lambda pf: {"nombre": "Logística Austral S.A.", "cuit": "30-71111111-3"})
    pf = _crear(api)
    r = api.get(f"/api/pre-facturas/{pf['id']}/pdf", headers=ADMIN)
    texto = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
    assert "Logística Austral S.A." in texto and "Transportes del Plata" not in texto


def _smtp_resuelto(configurado=True):
    return lambda: SimpleNamespace(configurado=configurado, host="smtp.example.com", port=587,
                                   user="facturacion@example.com", password="clave-de-prueba",
                                   from_email="facturacion@example.com", from_name="Transportes del Plata")


@patch("libracore.email_sender.smtplib.SMTP")
def test_enviar_email_manda_el_pdf_y_deja_la_pre_factura_enviada(mock_smtp, motor):
    servidor = MagicMock()
    mock_smtp.return_value.__enter__.return_value = servidor
    api = Producto().cliente(smtp_resolver=_smtp_resuelto())
    pf = _crear(api)
    r = api.post(f"/api/pre-facturas/{pf['id']}/enviar-email", json={"email": "cliente@example.com"}, headers=ADMIN)
    assert r.status_code == 200
    assert (r.json()["estado"], r.json()["enviado_a"]) == ("enviado", "cliente@example.com")
    adjunto = next(servidor.send_message.call_args[0][0].iter_attachments())
    assert adjunto.get_filename() == "PF-0001.pdf" and adjunto.get_content().startswith(b"%PDF-")


@patch("libracore.email_sender.smtplib.SMTP")
def test_enviar_email_sin_smtp_da_400_y_con_un_smtp_caido_502(mock_smtp, motor):
    sin_smtp = Producto().cliente(smtp_resolver=_smtp_resuelto(configurado=False),
                                  donde_configurar_smtp="Ajustes → Correo")
    pf = _crear(sin_smtp)
    r = sin_smtp.post(f"/api/pre-facturas/{pf['id']}/enviar-email", json={"email": "a@example.com"}, headers=ADMIN)
    assert r.status_code == 400 and "Ajustes → Correo" in r.json()["detail"]
    mock_smtp.assert_not_called()

    mock_smtp.side_effect = ConnectionRefusedError("sin red")
    caido = Producto().cliente(smtp_resolver=_smtp_resuelto())
    r = caido.post(f"/api/pre-facturas/{pf['id']}/enviar-email", json={"email": "a@example.com"}, headers=ADMIN)
    assert r.status_code == 502 and "sin red" in r.json()["detail"]
    assert pre_facturas.get(pf["id"])["estado"] == "pendiente"
    assert caido.post(f"/api/pre-facturas/{pf['id']}/enviar-email", json={"email": " "},
                      headers=ADMIN).status_code == 422


# ── Los ganchos del producto ─────────────────────────────────────────────────


def test_los_ganchos_reciben_la_pre_factura_ya_escrita_y_el_cuerpo_con_las_claves_del_producto(producto):
    recibido = {}

    def al_crear(conn, pre_factura, datos):
        recibido.update(pre_factura=pre_factura, datos=datos)
        # La pre factura ya está escrita *en esta conexión*, todavía sin confirmar.
        assert pre_facturas.get(pre_factura["id"], conn=conn) is not None
        assert pre_facturas.get(pre_factura["id"]) is None

    api = producto.cliente(al_crear=al_crear)
    pf = _crear(api, orden_ids=[7, 8])
    assert recibido["pre_factura"]["numero_interno"] == pf["numero_interno"] == "PF-0001"
    assert recibido["datos"]["orden_ids"] == [7, 8] and recibido["datos"]["cliente_razon"] == "Juan Pérez"


def test_si_el_gancho_de_crear_falla_no_queda_nada_y_el_numero_no_se_gasta(producto):
    def al_crear(conn, pre_factura, datos):
        raise HTTPException(409, "La orden 7 ya está reservada")

    api = producto.cliente(al_crear=al_crear)
    r = api.post("/api/pre-facturas", json=_cuerpo(orden_ids=[7]), headers=ADMIN)
    assert r.status_code == 409 and "orden 7" in r.json()["detail"]
    assert pre_facturas.listar() == []
    ok = Producto().cliente()
    assert _crear(ok)["numero_interno"] == "PF-0001"


def test_si_el_gancho_de_crear_revienta_tampoco_queda_nada(producto):
    def al_crear(conn, pre_factura, datos):
        raise RuntimeError("se cayó la reserva")

    api = producto.cliente(al_crear=al_crear)
    with pytest.raises(RuntimeError, match="se cayó la reserva"):      # el TestClient relanza el 500
        api.post("/api/pre-facturas", json=_cuerpo(), headers=ADMIN)
    assert pre_facturas.listar() == []


def test_los_ganchos_de_editar_y_anular_corren_con_la_pre_factura_como_quedo(api, producto):
    pf = _crear(api, orden_ids=[7])
    api.post(f"/api/pre-facturas/{pf['id']}/aceptar", headers=ADMIN)
    api.put(f"/api/pre-facturas/{pf['id']}", json={"observaciones": "x"}, headers=ADMIN)
    api.post(f"/api/pre-facturas/{pf['id']}/anular", json={"motivo": "pedido duplicado"}, headers=ADMIN)
    assert producto.llamadas == [("crear", "PF-0001"), ("editar", "PF-0001", "pendiente"),
                                 ("anular", "PF-0001", "pedido duplicado")]


def test_si_el_gancho_de_anular_falla_la_pre_factura_sigue_viva(producto):
    def al_anular(conn, pre_factura, datos):
        raise HTTPException(409, "Hay órdenes ya facturadas")

    api = producto.cliente(al_anular=al_anular)
    pf = _crear(api)
    assert api.post(f"/api/pre-facturas/{pf['id']}/anular", json={"motivo": "x"}, headers=ADMIN).status_code == 409
    assert pre_facturas.get(pf["id"])["estado"] == "pendiente"


def test_si_el_gancho_de_editar_falla_no_se_confirma_el_cambio(producto):
    def al_editar(conn, pre_factura, datos):
        raise HTTPException(409, "Una orden ya no está disponible")

    api = producto.cliente(al_editar=al_editar)
    pf = _crear(api)
    assert api.put(f"/api/pre-facturas/{pf['id']}", json={"observaciones": "cambio"}, headers=ADMIN).status_code == 409
    assert pre_facturas.get(pf["id"])["observaciones"] == ""


def test_sin_ganchos_ni_usuario_el_router_anda(motor):
    app = FastAPI()
    app.include_router(pre_facturas_router.build_pre_facturas_router(origen_producto="libracargo"))
    api = TestClient(app)
    pf = api.post("/api/pre-facturas", json=_cuerpo()).json()
    assert api.post(f"/api/pre-facturas/{pf['id']}/aceptar").json()["aceptado_por"] == ""
    assert api.post(f"/api/pre-facturas/{pf['id']}/anular", json={"motivo": "x"}).json()["estado"] == "descartado"


def test_el_prefijo_se_puede_cambiar(motor):
    app = FastAPI()
    app.include_router(pre_facturas_router.build_pre_facturas_router(origen_producto="libracargo", prefix="/api/v2/pf"))
    assert TestClient(app).get("/api/v2/pf").status_code == 200
