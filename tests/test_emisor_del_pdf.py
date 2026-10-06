"""Quién emite lo que dice el PDF: la configuración de la instancia, el `emisor_id` del comprobante y el resolvedor del producto (ADR-031).

Lo que se fija:

- sin resolvedor ni `emisor_id` el PDF **no cambia** (los mismos bytes que con un resolvedor que no aporta nada);
- con `emisor_id`, el nombre y el CUIT son los de ese `arca_config`, y no los de la instancia;
- con resolvedor —registrado o por parámetro— el domicilio, la condición de IVA y el logo son los del producto, en
  **todos** los PDF del motor, no sólo en la factura;
- el router de sólo-PDF y el envío por mail muestran y mandan el mismo documento, con el emisor correcto.

Datos ficticios. Las pruebas de base corren en SQLite y en PostgreSQL (`LIBRACORE_POSTGRES_URL`).
"""
import email
import io
import os
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader

from libracore import config_manager, emisor_del_pdf
from libracore import facturas_router as fr
from libracore import pdf_generator as pg
from libracore.db import arca_config as db_arca
from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db.schema import init_core_schema

GLOBAL = {"empresa_nombre": "Instancia Global SA", "empresa_cuit": "30-00000000-0",
          "empresa_direccion": "Calle Global 1", "empresa_iva_condition": "Monotributista"}

DEMO = {"nombre": "Transportes Demo SRL", "cuit": "30-12345678-1"}
OTRA = {"nombre": "Fletes del Sur SA", "cuit": "30-98765432-1"}
DEL_PRODUCTO = {"direccion": "Av. Siempre Viva 742", "iva_condition": "Responsable Inscripto",
                "iibb": "901-123456-7", "inicio_actividades": "2020-01-15"}

ITEM = {"description": "Flete Rosario - Buenos Aires", "qty": 1, "unit_price": 1000.0, "subtotal": 1000.0}


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
        core.configure(db_path=str(tmp_path / "emisor_del_pdf.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.commit()
    yield request.param
    core._db_path = None
    core._database_url = None


@pytest.fixture(autouse=True)
def _entorno(tmp_path, monkeypatch):
    monkeypatch.setattr(config_manager, "load", lambda *a, **k: dict(GLOBAL))
    monkeypatch.setattr(pg, "FACTURAS_PDF_DIR", str(tmp_path / "facturas"))
    emisor_del_pdf.registrar_resolvedor(None)
    yield
    emisor_del_pdf.registrar_resolvedor(None)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 120), (0, 90, 160)).save(buf, format="PNG")
    return buf.getvalue()


def _bytes(documento) -> bytes:
    """Los generadores devuelven o bytes o la ruta del archivo."""
    return documento if isinstance(documento, bytes) else pathlib.Path(documento).read_bytes()


def _texto(documento) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(_bytes(documento))).pages)


def _imagenes(documento) -> int:
    return _bytes(documento).count(b"/Subtype /Image")


def _comprobante(**extra) -> dict:
    base = {"tipo": 1, "punto_venta": 1, "numero": 1, "fecha": "2026-10-06", "cliente_razon": "Juan Pérez",
            "cliente_cuit": "20-12345678-6", "cliente_iva_cond": 5, "cliente_domicilio": "",
            "condicion_venta": "Contado", "items": [ITEM], "subtotal": 1000.0, "iva_amount": 210.0,
            "total": 1210.0, "concepto": 1, "cae": "", "cae_vto": "", "observaciones": ""}
    return base | extra


def _arca(datos: dict) -> int:
    """Una razón social: la fila de `arca_config` y su id."""
    db_arca.crear_arca_config(datos["nombre"], datos["cuit"], 1, "clave.key", "cert.crt", ambiente="produccion")
    return db_arca.obtener_arca_config(datos["nombre"])["id"]


def _guardar(emisor_id=None, *, tipo=11, numero=1, pdf_path="", **extra) -> dict:
    """Un comprobante en la tabla `facturas`, como lo deja la emisión."""
    fid = db_facturas.create_factura(
        tipo=tipo, punto_venta=1, numero=numero, fecha="2026-10-06", cliente_cuit="20-12345678-6",
        cliente_razon="Juan Pérez", cliente_iva_cond=5, items=[ITEM], subtotal=1000.0, iva_amount=0.0,
        total=1000.0, cae="70123456789012", cae_vto="2026-10-16", condicion_venta="Contado",
        pdf_path=pdf_path, ambiente="produccion", emisor_id=emisor_id, **extra)
    return db_facturas.get_factura(fid)


# ═══════════════════════════════════════════════ Sin resolvedor ni emisor_id: nada cambia


def test_sin_nada_el_emisor_es_el_de_la_instancia():
    emp = emisor_del_pdf.emisor_para(_comprobante())
    assert emp == pg._empresa()
    assert emp["nombre"] == "Instancia Global SA"


def test_sin_nada_el_pdf_tiene_los_datos_de_la_instancia(tmp_path):
    texto = _texto(pg.generate_pdf_factura(_comprobante(), output_dir=str(tmp_path)))
    assert "Instancia Global SA" in texto
    assert "30-00000000-0" in texto
    assert "Calle Global 1" in texto


def test_un_resolvedor_que_no_aporta_nada_no_cambia_ni_un_byte(tmp_path):
    f = _comprobante()
    a = _bytes(pg.generate_pdf_factura(f, output_dir=str(tmp_path / "a")))
    for resolvedor in (lambda d: None, lambda d: {}, lambda d: {"nombre": None, "logo_bytes": None}):
        assert _bytes(pg.generate_pdf_factura(f, output_dir=str(tmp_path / "b"), resolvedor=resolvedor)) == a


# ═══════════════════════════════════════════════ El emisor_id: nombre y CUIT de su arca_config


def test_con_emisor_id_el_nombre_y_el_cuit_son_los_de_esa_razon_social(motor, tmp_path):
    demo, otra = _arca(DEMO), _arca(OTRA)
    de_demo = _texto(pg.generate_pdf_factura(_guardar(demo, numero=1), output_dir=str(tmp_path)))
    de_otra = _texto(pg.generate_pdf_factura(_guardar(otra, numero=2), output_dir=str(tmp_path)))

    assert "Transportes Demo SRL" in de_demo and "30-12345678-1" in de_demo
    assert "Fletes del Sur SA" not in de_demo
    assert "Fletes del Sur SA" in de_otra and "30-98765432-1" in de_otra
    assert "Transportes Demo SRL" not in de_otra
    # Lo que `arca_config` no guarda sigue saliendo de la instancia.
    assert "Calle Global 1" in de_demo
    assert "Instancia Global SA" not in de_demo and "30-00000000-0" not in de_demo


def test_una_razon_social_dada_de_baja_sigue_firmando_lo_que_emitio(motor):
    emisor = _arca(DEMO)
    db_arca.eliminar_arca_config(DEMO["nombre"])
    assert emisor_del_pdf.emisor_para({"emisor_id": emisor})["nombre"] == "Transportes Demo SRL"


def test_un_emisor_que_no_existe_no_cae_a_otro(motor):
    _arca(DEMO)
    with pytest.raises(db_arca.EmisorDesconocido):
        emisor_del_pdf.emisor_para({"emisor_id": 9999})


def test_dos_comprobantes_con_el_mismo_numero_no_comparten_archivo(motor, tmp_path):
    """Una nota de crédito 0001-00000001 pisaba a la factura 0001-00000001, y lo mismo dos razones sociales."""
    demo, otra = _arca(DEMO), _arca(OTRA)
    de_demo = pg.generate_pdf_factura(_guardar(demo, tipo=11, numero=1))
    de_otra = pg.generate_pdf_factura(_guardar(otra, tipo=11, numero=1))
    nota = pg.generate_pdf_factura(_guardar(demo, tipo=13, numero=1))
    assert len({de_demo, de_otra, nota}) == 3
    assert "Transportes Demo SRL" in _texto(de_demo)
    assert "Fletes del Sur SA" in _texto(de_otra)


# ═══════════════════════════════════════════════ El resolvedor del producto


def test_el_resolvedor_por_parametro_pone_domicilio_iva_y_logo(tmp_path):
    sin = pg.generate_pdf_factura(_comprobante(), output_dir=str(tmp_path / "sin"))
    con = pg.generate_pdf_factura(
        _comprobante(), output_dir=str(tmp_path / "con"),
        resolvedor=lambda d: DEL_PRODUCTO | {"logo_bytes": _png()})
    texto = _texto(con)
    assert "Av. Siempre Viva 742" in texto
    assert "IVA Responsable Inscripto" in texto
    assert "901-123456-7" in texto
    assert "Calle Global 1" not in texto
    assert _imagenes(sin) == 0 and _imagenes(con) == 1


def test_el_resolvedor_registrado_vale_para_todos_los_pdf_del_proceso(tmp_path):
    emisor_del_pdf.registrar_resolvedor(lambda d: DEL_PRODUCTO | {"logo_bytes": _png()})
    texto_logo = pg.generate_pdf_factura(_comprobante(), output_dir=str(tmp_path))
    assert "Av. Siempre Viva 742" in _texto(texto_logo)
    assert _imagenes(texto_logo) == 1

    # Y un parámetro lo reemplaza para esa llamada.
    otro = pg.generate_pdf_factura(_comprobante(), output_dir=str(tmp_path),
                                   resolvedor=lambda d: {"direccion": "Otra calle 5"})
    assert "Otra calle 5" in _texto(otro) and "Av. Siempre Viva 742" not in _texto(otro)

    emisor_del_pdf.registrar_resolvedor(None)
    assert "Calle Global 1" in _texto(pg.generate_pdf_factura(_comprobante(), output_dir=str(tmp_path)))


def test_el_resolvedor_pisa_al_emisor_id_y_un_none_no_pisa_nada(motor):
    emisor = _arca(DEMO)
    emp = emisor_del_pdf.emisor_para({"emisor_id": emisor}, resolvedor=lambda d: {"nombre": "Nombre de Fantasía", "cuit": None})
    assert emp["nombre"] == "Nombre de Fantasía"
    assert emp["cuit"] == "30-12345678-1"
    # Un texto vacío sí pisa: es una respuesta («este emisor no tiene Ingresos Brutos»).
    assert emisor_del_pdf.emisor_para({}, resolvedor=lambda d: {"iibb": ""})["iibb"] == ""


def test_el_resolvedor_recibe_el_documento_y_lo_que_levanta_sube():
    visto = []
    emisor_del_pdf.emisor_para({"numero": 7}, resolvedor=lambda d: visto.append(d))
    assert visto == [{"numero": 7}]

    def roto(documento):
        raise RuntimeError("no encuentro la razón social")

    with pytest.raises(RuntimeError, match="razón social"):
        pg.generate_pdf_factura(_comprobante(), resolvedor=roto)


def test_el_parametro_empresa_de_la_pre_factura_gana_sobre_todo(tmp_path):
    pf = {"id": 1, "numero_interno": "PF-0001", "fecha_sugerida": "2026-10-06", "cliente_razon": "Juan Pérez",
          "cliente_cuit": "20-12345678-6", "tipo_comprobante": 1, "items": [ITEM], "total": 1210}
    texto = _texto(pg.generate_pdf_pre_factura(
        pf, empresa={"nombre": "Pisa Todo SA"}, resolvedor=lambda d: DEL_PRODUCTO | {"nombre": "Producto SA"}))
    assert "Pisa Todo SA" in texto and "Producto SA" not in texto
    assert "Av. Siempre Viva 742" in texto


def test_la_pre_factura_con_emisor_id_usa_su_arca_config(motor):
    emisor = _arca(OTRA)
    pf = {"id": 1, "numero_interno": "PF-0001", "fecha_sugerida": "2026-10-06", "cliente_razon": "Juan Pérez",
          "cliente_cuit": "20-12345678-6", "tipo_comprobante": 1, "items": [ITEM], "total": 1210,
          "emisor_id": emisor}
    texto = _texto(pg.generate_pdf_pre_factura(pf))
    assert "Fletes del Sur SA" in texto and "30-98765432-1" in texto and "Instancia Global SA" not in texto


# ═══════════════════════════════════════════════ Todos los documentos pasan por el mismo punto

REMITO = {"number": "0001-00000001", "date": "2026-10-06", "client_name": "Juan Pérez", "client_cuit": "20-12345678-6",
          "client_address": "", "client_email": "", "client_phone": "", "items": [ITEM], "observations": ""}
PRESUPUESTO = REMITO | {"valid_until": "2026-10-13", "subtotal": 1000.0, "tax_amount": 210.0, "total": 1210.0,
                        "tax_rate": 0.21}
RECIBO = {"punto_venta": 1, "numero": 1, "fecha": "2026-10-06", "cliente_razon": "Juan Pérez",
          "cliente_cuit": "20-12345678-6", "concepto": "Cancelación de Factura A 0001-00000001", "total": 1210.0,
          "pagos": [{"fecha": "2026-10-06", "medio_pago": "efectivo", "referencia": "", "monto": 1210.0}]}
PRE_FACTURA = {"id": 1, "numero_interno": "PF-0001", "fecha_sugerida": "2026-10-06", "cliente_razon": "Juan Pérez",
               "cliente_cuit": "20-12345678-6", "tipo_comprobante": 1, "items": [ITEM], "total": 1210}
CLIENTE = {"id": 3, "name": "Juan Pérez", "cuit_dni": "20-12345678-6", "address": "", "email": "", "phone": ""}
PERIODO = {"desde": "2026-10-01", "hasta": "2026-10-31", "saldo_anterior": 0.0, "total_debitos": 0.0,
           "total_creditos": 0.0, "saldo_final": 0.0, "movimientos": [], "emitido": "2026-10-31"}

GENERADORES = {
    "factura": lambda r, d: pg.generate_pdf_factura(_comprobante(), output_dir=d, resolvedor=r),
    "recibo emitido": lambda r, d: pg.generate_pdf_recibo_doc(RECIBO, resolvedor=r),
    "recibo de una factura": lambda r, d: pg.generate_pdf_recibo(
        _comprobante(total=1210.0), RECIBO["pagos"], resolvedor=r),
    "presupuesto": lambda r, d: pg.generate_pdf_presupuesto(PRESUPUESTO, output_dir=d, resolvedor=r),
    "remito": lambda r, d: pg.generate_pdf(REMITO, output_dir=d, resolvedor=r),
    "pre factura": lambda r, d: pg.generate_pdf_pre_factura(PRE_FACTURA, resolvedor=r),
    "resumen de cuenta": lambda r, d: pg.generate_pdf_resumen_cc(CLIENTE, PERIODO, output_dir=d, resolvedor=r),
}


@pytest.mark.parametrize("nombre", GENERADORES)
def test_todos_los_pdf_toman_el_emisor_del_mismo_punto(nombre, tmp_path):
    generar = GENERADORES[nombre]
    resolvedor = lambda d: DEL_PRODUCTO | DEMO | {"logo_bytes": _png()}  # noqa: E731

    sin = generar(None, str(tmp_path / "sin"))
    con = generar(resolvedor, str(tmp_path / "con"))
    assert "Instancia Global SA" in _texto(sin) and _imagenes(sin) == 0
    texto = _texto(con)
    assert "Transportes Demo SRL" in texto and "30-12345678-1" in texto
    assert "Instancia Global SA" not in texto and "30-00000000-0" not in texto
    assert _imagenes(con) == 1
    if "recibo" not in nombre:   # el recibo no dibuja la tarjeta del emisor: sólo membrete y pie
        assert "Av. Siempre Viva 742" in texto

    # Registrado una vez, sin pasar nada, da lo mismo que por parámetro.
    emisor_del_pdf.registrar_resolvedor(resolvedor)
    assert _texto(generar(None, str(tmp_path / "registrado"))) == texto


# ═══════════════════════════════════════════════ El router de sólo-PDF y el mail

SMTP = SimpleNamespace(configurado=True, host="smtp.example.com", port=587, user="facturacion@example.com",
                       password="secreto", from_email="facturacion@example.com", from_name="Transportes Demo")


def _app(**opciones) -> TestClient:
    def sesion():
        return {"id": 1}

    app = FastAPI()
    app.include_router(fr.build_comprobantes_pdf_router(usuario_actual=sesion, smtp_config=lambda: SMTP, **opciones))
    return TestClient(app)


def test_el_router_chico_solo_tiene_el_pdf_y_el_mail():
    rutas = sorted((m, r.path) for r in fr.build_comprobantes_pdf_router(usuario_actual=lambda: None).routes
                   for m in r.methods)
    assert rutas == [("GET", "/api/facturas/{factura_id}/pdf"),
                     ("POST", "/api/facturas/{factura_id}/enviar-email")]


def test_el_router_chico_devuelve_el_pdf_con_el_emisor_correcto(motor):
    demo, otra = _arca(DEMO), _arca(OTRA)
    de_demo, de_otra = _guardar(demo, numero=1), _guardar(otra, numero=2)
    client = _app(emisor_del_pdf=lambda d: DEL_PRODUCTO | {"logo_bytes": _png()})

    r = client.get(f"/api/facturas/{de_demo['id']}/pdf")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    texto = _texto(r.content)
    assert "Transportes Demo SRL" in texto and "30-12345678-1" in texto
    assert "Av. Siempre Viva 742" in texto and "IVA Responsable Inscripto" in texto
    assert _imagenes(r.content) == 1
    assert "Instancia Global SA" not in texto

    assert "Fletes del Sur SA" in _texto(client.get(f"/api/facturas/{de_otra['id']}/pdf").content)


def test_un_comprobante_inexistente_o_que_el_producto_no_deja_ver_es_404(motor):
    emisor = _arca(DEMO)
    visible, oculta = _guardar(emisor, numero=1), _guardar(emisor, numero=2)
    client = _app(puede_ver=lambda f: f["id"] == visible["id"])

    assert client.get(f"/api/facturas/{visible['id']}/pdf").status_code == 200
    assert client.get(f"/api/facturas/{oculta['id']}/pdf").status_code == 404
    assert client.get("/api/facturas/9999/pdf").status_code == 404
    r = client.post(f"/api/facturas/{oculta['id']}/enviar-email", json={"email": "cliente@example.com"})
    assert r.status_code == 404


def test_sin_sesion_no_hay_ni_pdf_ni_mail(motor):
    emisor = _arca(DEMO)
    factura = _guardar(emisor)

    def sin_sesion():
        raise HTTPException(401, "Sin sesión")

    app = FastAPI()
    app.include_router(fr.build_comprobantes_pdf_router(usuario_actual=sin_sesion, smtp_config=lambda: SMTP))
    client = TestClient(app)
    assert client.get(f"/api/facturas/{factura['id']}/pdf").status_code == 401
    assert client.post(f"/api/facturas/{factura['id']}/enviar-email", json={"email": "a@b.com"}).status_code == 401


def _enviar(client, factura_id: int):
    """Manda el comprobante con un SMTP falso y devuelve (mensaje, PDF adjunto)."""
    with patch("libracore.email_sender.smtplib.SMTP") as smtp:
        servidor = MagicMock()
        smtp.return_value.__enter__.return_value = servidor
        r = client.post(f"/api/facturas/{factura_id}/enviar-email", json={"email": "cliente@example.com"})
    assert r.status_code == 200, r.text
    mensaje = servidor.send_message.call_args[0][0]
    adjunto = next(mensaje.iter_attachments()).get_content()
    return mensaje, adjunto


def test_el_mail_manda_el_mismo_pdf_que_se_ve_y_lo_firma_el_emisor(motor):
    emisor = _arca(OTRA)
    factura = _guardar(emisor)
    client = _app(emisor_del_pdf=lambda d: DEL_PRODUCTO | {"logo_bytes": _png()})

    visto = client.get(f"/api/facturas/{factura['id']}/pdf").content
    mensaje, adjunto = _enviar(client, factura["id"])

    assert adjunto == visto
    assert "Fletes del Sur SA" in _texto(adjunto)
    # El asunto y el cuerpo los firma el emisor del comprobante, no la instancia.
    assert "Fletes del Sur SA" in mensaje["Subject"] and "Instancia Global SA" not in mensaje["Subject"]
    assert "Fletes del Sur SA" in mensaje.get_body(preferencelist=("plain",)).get_content()


def test_el_pdf_guardado_no_se_regenera_aunque_cambie_el_emisor(motor, tmp_path):
    """Lo que se emitió es lo que se le mandó al cliente: sólo se rearma si el archivo se perdió."""
    emisor = _arca(DEMO)
    guardado = pg.generate_pdf_factura(
        _guardar(emisor, numero=1), output_dir=str(tmp_path / "emitidos"),
        resolvedor=lambda d: {"direccion": "Calle Vieja 1"})
    factura = _guardar(emisor, numero=2, pdf_path=guardado)
    client = _app(emisor_del_pdf=lambda d: {"direccion": "Calle Nueva 2"})

    assert "Calle Vieja 1" in _texto(client.get(f"/api/facturas/{factura['id']}/pdf").content)
    _, adjunto = _enviar(client, factura["id"])
    assert "Calle Vieja 1" in _texto(adjunto)

    pathlib.Path(guardado).unlink()
    assert "Calle Nueva 2" in _texto(client.get(f"/api/facturas/{factura['id']}/pdf").content)


# ═══════════════════════════════════════════════ El router grande acepta el mismo parámetro


def test_el_router_grande_arma_el_pdf_al_emitir_con_el_emisor_del_comprobante(motor, tmp_path, monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(tmp_path / "config.json"))
    with core.get_connection() as conn:
        conn.execute("INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?,?,?,?,?)",
                     (1, "admin", "Administrador", "x", "admin"))
        conn.commit()
    emisor = _arca(DEMO)

    app = FastAPI()
    app.include_router(fr.build_comprobantes_router(
        usuario_actual=lambda: {"id": 1}, solo_admin=lambda: None, smtp_config=lambda: SMTP,
        emisor_del_pdf=lambda d: DEL_PRODUCTO | {"logo_bytes": _png()}))
    client = TestClient(app)

    r = client.post("/api/facturas", json={
        "tipo": 11, "punto_venta": 1, "fecha": "2026-10-06", "condicion_venta": "Contado",
        "client_name": "Juan Pérez", "client_cuit": "20123456786", "client_iva": "Consumidor Final",
        "items": [{"description": "Flete", "qty": 1, "unit_price": 1000.0}], "emisor_id": emisor})
    assert r.status_code == 200, r.text
    guardado = db_facturas.get_factura(r.json()["id"])["pdf_path"]
    texto = _texto(guardado)
    assert "Transportes Demo SRL" in texto and "Av. Siempre Viva 742" in texto and _imagenes(guardado) == 1

    # El borrador también muestra al emisor que se eligió.
    borrador = client.post("/api/facturas/borrador-pdf", json={
        "tipo": 11, "punto_venta": 1, "fecha": "2026-10-06", "client_name": "Juan Pérez",
        "items": [{"description": "Flete", "qty": 1, "unit_price": 1000.0}], "emisor_id": emisor})
    assert borrador.status_code == 200, borrador.text
    assert "Transportes Demo SRL" in _texto(borrador.content)

    # Y el mail, con el SMTP falso, manda ese mismo archivo.
    mensaje, adjunto = _enviar(client, r.json()["id"])
    assert adjunto == pathlib.Path(guardado).read_bytes()
    assert "Transportes Demo SRL" in mensaje["Subject"]


def test_un_borrador_con_un_emisor_que_no_existe_es_422_y_no_500(motor, tmp_path, monkeypatch):
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(tmp_path / "config.json"))
    app = FastAPI()
    app.include_router(fr.build_comprobantes_router(usuario_actual=lambda: {"id": 1}, solo_admin=lambda: None))
    r = TestClient(app).post("/api/facturas/borrador-pdf", json={
        "tipo": 11, "punto_venta": 1, "fecha": "2026-10-06", "client_name": "Juan Pérez",
        "items": [{"description": "Flete", "qty": 1, "unit_price": 1000.0}], "emisor_id": 9999})
    assert r.status_code == 422, r.text
