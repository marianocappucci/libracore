"""La pre factura (`libracore.pre_facturas`, ADR-030): la bandeja de comprobantes por facturar con
número interno, emisor, tipo y el ciclo enviada / aceptada, contra los dos motores.

Lo que se fija acá:
- el número interno `PF-0001` es correlativo e independiente por producto e instancia, no se reusa
  y aguanta dos altas que calculan el mismo (reintenta en un `SAVEPOINT`);
- el total sale de los ítems; el cliente va como foto; los datos inválidos se rechazan;
- el ciclo: `pendiente → enviado → aceptado → facturado`, con aceptar desde `pendiente`,
  `facturado` y `descartado` finales, y **editar una enviada o aceptada la devuelve a `pendiente`**;
- `conn=` (ADR-025): con la conexión de quien llama no se confirma nada;
- la bandeja de siempre (Contalibra, LibraDesk) sigue igual: una fila sin número no es una pre factura;
- el PDF lleva el sello y el número interno y **no** lleva CAE, punto de venta ni QR;
- el correo sale por el SMTP resuelto como en el resto del motor, adjunta el PDF y marca enviada.
"""
import io
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pypdf import PdfReader

from libracore import config_manager, pre_facturas
from libracore.db import comprobantes_pendientes as bandeja
from libracore.db import core
from libracore.db.arca_config import EmisorDesconocido
from libracore.db.schema import init_core_schema
from libracore.pdf_generator import LEYENDA_PRE_FACTURA


@pytest.fixture(params=["sqlite", "postgres"])
def base(request, tmp_path):
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
        core.configure(db_path=str(tmp_path / "pre_facturas.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.commit()
    yield request.param
    core._db_path = None
    core._database_url = None


@pytest.fixture(autouse=True)
def _empresa_ficticia(monkeypatch):
    """La configuración de la instancia, fija y ficticia: el PDF no depende de un `config.json`."""
    cfg = {
        "empresa_nombre": "Transportes del Plata S.R.L.", "empresa_cuit": "30-12345678-1",
        "empresa_direccion": "Av. Siempreviva 742, Rosario", "empresa_iva_condition": "Responsable Inscripto",
        "empresa_email": "", "email_smtp_host": "", "email_smtp_user": "",
    }
    monkeypatch.setattr(config_manager, "load", lambda *a, **k: dict(cfg))


FLETE = {"description": "Flete Rosario - Buenos Aires", "qty": 1, "unit_price": 100000.0, "iva_rate": 0.21}


def _crear(**kwargs):
    base_ = dict(origen_producto="libracargo", origen_instancia="demo", cliente_razon="Juan Pérez",
                 cliente_cuit="20-12345678-6", items=[FLETE], tipo_comprobante=1,
                 fecha_sugerida="2026-10-06")
    base_.update(kwargs)
    return pre_facturas.crear(**base_)


def _arca(empresa="Transportes del Plata S.R.L.", cuit="30-12345678-1"):
    with core.get_connection() as c:
        cur = c.execute("INSERT INTO arca_config (empresa, cuit, punto_venta, clave_path, certificado_path) "
                        "VALUES (?,?,1,'k','c')", (empresa, cuit))
        c.commit()
        return cur.lastrowid


def _factura():
    """Una factura real a la que apuntar (`factura_id` es una FK): cada una con su número."""
    with core.get_connection() as c:
        numero = c.execute("SELECT COALESCE(MAX(numero), 0) + 1 FROM facturas").fetchone()[0]
        cur = c.execute("INSERT INTO facturas (tipo, punto_venta, numero, fecha, items, subtotal, iva_amount, "
                        "total) VALUES (1, 1, ?, '2026-10-07', '[]', 100000, 21000, 121000)", (numero,))
        c.commit()
        return cur.lastrowid


# ── Alta y numeración ────────────────────────────────────────────────────────


def test_crear_asigna_el_numero_interno_y_deriva_el_total(base):
    pf = _crear(items=[FLETE, {"description": "Peaje", "qty": 2, "unit_price": 500.0, "iva_rate": 0}])
    assert pf["numero_interno"] == "PF-0001"
    assert pf["estado"] == "pendiente"
    assert pf["total"] == 122000.0       # 100000 * 1.21 + 2 * 500
    assert (pf["cliente_razon"], pf["cliente_cuit"]) == ("Juan Pérez", "20-12345678-6")
    assert pf["tipo_comprobante"] == 1 and pf["emisor_id"] is None and pf["factura_id"] is None
    assert pf["origen_tipo"] == "pre_factura" and pf["origen_id"] == "PF-0001"
    assert pf["enviado_at"] is None and pf["aceptado_at"] is None
    assert pre_facturas.get(pf["id"]) == pf


def test_la_numeracion_es_correlativa_e_independiente_por_producto_e_instancia(base):
    assert [_crear()["numero_interno"] for _ in range(3)] == ["PF-0001", "PF-0002", "PF-0003"]
    assert _crear(origen_instancia="otra")["numero_interno"] == "PF-0001"
    assert _crear(origen_producto="libradesk")["numero_interno"] == "PF-0001"
    assert _crear()["numero_interno"] == "PF-0004"


def test_un_anulado_conserva_su_numero_y_no_se_reusa(base):
    a, b = _crear(), _crear()
    pre_facturas.anular(b["id"], "mariano", "duplicada")
    assert _crear()["numero_interno"] == "PF-0003"
    assert pre_facturas.get(a["id"])["numero_interno"] == "PF-0001"


def test_el_numero_pasa_de_cuatro_digitos_sin_tope(base):
    assert pre_facturas.numero_interno(9999) == "PF-9999"
    assert pre_facturas.numero_interno(10000) == "PF-10000"
    _crear()
    with core.get_connection() as c:
        c.execute("UPDATE comprobantes_pendientes SET numero_interno='PF-9999'")
        c.commit()
    assert _crear()["numero_interno"] == "PF-10000"


def test_dos_altas_que_calculan_el_mismo_numero_reintentan_con_el_que_sigue(base):
    """El índice único parcial cierra la carrera y el `SAVEPOINT` deja la transacción viva (en PostgreSQL
    un error la aborta). Se simula la carrera haciendo que el primer cálculo devuelva un número ya tomado."""
    _crear()
    real = pre_facturas._siguiente_numero
    llamadas = []

    def con_carrera(c, producto, instancia):
        llamadas.append(1)
        return "PF-0001" if len(llamadas) == 1 else real(c, producto, instancia)

    with patch.object(pre_facturas, "_siguiente_numero", con_carrera):
        pf = _crear(origen_id="otra-orden")
    assert pf["numero_interno"] == "PF-0002" and len(llamadas) == 2


def test_dentro_de_la_transaccion_de_quien_llama_el_reintento_no_la_aborta(base):
    _crear()
    real = pre_facturas._siguiente_numero
    llamadas = []

    def con_carrera(c, producto, instancia):
        llamadas.append(1)
        return "PF-0001" if len(llamadas) == 1 else real(c, producto, instancia)

    conn = core.get_connection()
    try:
        with patch.object(pre_facturas, "_siguiente_numero", con_carrera):
            pf = _crear(origen_id="otra-orden", conn=conn)
        assert pf["numero_interno"] == "PF-0002"
        assert pre_facturas.get(pf["id"], conn=conn) is not None   # la transacción sigue usable
        conn.rollback()
    finally:
        conn.close()
    assert [p["numero_interno"] for p in pre_facturas.listar()] == ["PF-0001"]


def test_un_origen_repetido_es_un_error_y_no_un_reintento(base):
    _crear(origen_id="orden-7")
    with pytest.raises(pre_facturas.PreFacturaYaExiste):
        _crear(origen_id="orden-7")
    assert len(pre_facturas.listar()) == 1


@pytest.mark.parametrize("kwargs, mensaje", [
    (dict(cliente_razon="  "), "razón social"),
    (dict(items=[]), "al menos un ítem"),
    (dict(items=[{"description": "", "qty": 1, "unit_price": 1}]), "descripción"),
    (dict(items=[{"description": "x", "qty": True, "unit_price": 1}]), "booleano"),
    (dict(items=[{"description": "x", "qty": "mucho", "unit_price": 1}]), "número"),
    (dict(tipo_comprobante=3), "tipo_comprobante"),
    (dict(tipo_comprobante=True), "tipo_comprobante"),
    (dict(origen_tipo="otro"), "origen_tipo"),
    (dict(origen_producto=" "), "origen_producto"),
    (dict(origen_id="  "), "origen_id"),
    (dict(tipo_comprobante=11), "clase C"),     # el ítem trae 21% de IVA
])
def test_los_datos_invalidos_se_rechazan_y_no_escriben(base, kwargs, mensaje):
    with pytest.raises(ValueError, match=mensaje):
        _crear(**kwargs)
    assert pre_facturas.listar() == []


def test_un_comprobante_clase_c_va_sin_iva(base):
    pf = _crear(tipo_comprobante=11, items=[{**FLETE, "iva_rate": 0}])
    assert pf["total"] == 100000.0


def test_la_fecha_vacia_es_hoy_y_el_tipo_puede_faltar(base):
    pf = _crear(fecha_sugerida="", tipo_comprobante=None)
    assert len(pf["fecha_sugerida"]) == 10 and pf["tipo_comprobante"] is None


def test_el_emisor_tiene_que_existir(base):
    with pytest.raises(EmisorDesconocido):
        _crear(emisor_id=999)
    emisor = _arca()
    assert _crear(emisor_id=emisor)["emisor_id"] == emisor
    with pytest.raises(ValueError):
        _crear(emisor_id=True)


def test_la_fce_guarda_su_vencimiento(base):
    pf = _crear(tipo_comprobante=206, fecha_vencimiento_pago="2026-11-05")
    assert pf["fecha_vencimiento_pago"] == "2026-11-05"


def test_los_items_conservan_las_claves_del_producto(base):
    pf = _crear(items=[{**FLETE, "orden_id": 41, "detalle": "Remito 0001-00000012"}])
    assert pf["items"][0]["orden_id"] == 41 and pf["items"][0]["detalle"] == "Remito 0001-00000012"


# ── Listados ─────────────────────────────────────────────────────────────────


def test_listar_filtra_por_estado_cliente_y_origen(base):
    a = _crear(cliente_razon="Juan Pérez", cliente_id=None)
    b = _crear(cliente_razon="Acopio del Sur S.A.", cliente_cuit="30-87654321-0")
    _crear(origen_instancia="otra", cliente_razon="Cliente de Otra Instancia")
    pre_facturas.marcar_aceptada(b["id"], "mariano")

    todas = pre_facturas.listar(origen_producto="libracargo", origen_instancia="demo")
    assert [p["id"] for p in todas] == [b["id"], a["id"]]          # las más nuevas primero
    assert [p["id"] for p in pre_facturas.listar(estado="aceptado")] == [b["id"]]
    assert [p["id"] for p in pre_facturas.listar(cliente="pérez")] == [a["id"]]
    assert [p["id"] for p in pre_facturas.listar(cliente="87654321")] == [b["id"]]
    assert len(pre_facturas.listar(limit=1)) == 1
    assert pre_facturas.contar_por_estado(origen_producto="libracargo", origen_instancia="demo") == {
        "pendiente": 1, "enviado": 0, "aceptado": 1, "facturado": 0, "descartado": 0}
    with pytest.raises(ValueError):
        pre_facturas.listar(estado="inexistente")


# ── El ciclo ─────────────────────────────────────────────────────────────────


def test_el_ciclo_completo_pendiente_enviado_aceptado_facturado(base):
    pf = _crear()
    enviada = pre_facturas.marcar_enviada(pf["id"], "cliente@example.com")
    assert enviada["estado"] == "enviado" and enviada["enviado_a"] == "cliente@example.com"
    assert len(enviada["enviado_at"]) == 19
    aceptada = pre_facturas.marcar_aceptada(pf["id"], "mariano")
    assert (aceptada["estado"], aceptada["aceptado_por"]) == ("aceptado", "mariano")
    assert len(aceptada["aceptado_at"]) == 19
    factura_id = _factura()
    facturada = pre_facturas.marcar_facturada(pf["id"], factura_id, "mariano")
    assert (facturada["estado"], facturada["factura_id"], facturada["resuelto_por"]) == (
        "facturado", factura_id, "mariano")
    assert facturada["resuelto_at"]
    assert facturada["enviado_a"] == "cliente@example.com"         # el rastro queda


def test_se_puede_aceptar_sin_haberla_enviado_desde_la_app(base):
    """El PDF se puede bajar y mandar por otro medio: el operador marca que el cliente está de acuerdo."""
    pf = _crear()
    assert pre_facturas.marcar_aceptada(pf["id"], "mariano")["estado"] == "aceptado"


def test_aceptar_dos_veces_no_pisa_quien_la_acepto(base):
    pf = _crear()
    primera = pre_facturas.marcar_aceptada(pf["id"], "mariano")
    segunda = pre_facturas.marcar_aceptada(pf["id"], "otra-persona")
    assert segunda["aceptado_por"] == "mariano" and segunda["aceptado_at"] == primera["aceptado_at"]


def test_reenviar_actualiza_a_quien_y_una_aceptada_sigue_aceptada(base):
    pf = _crear()
    pre_facturas.marcar_enviada(pf["id"], "uno@example.com")
    assert pre_facturas.marcar_enviada(pf["id"], "dos@example.com")["enviado_a"] == "dos@example.com"
    pre_facturas.marcar_aceptada(pf["id"], "mariano")
    reenviada = pre_facturas.marcar_enviada(pf["id"], "tres@example.com")
    assert (reenviada["estado"], reenviada["enviado_a"]) == ("aceptado", "tres@example.com")
    with pytest.raises(ValueError):
        pre_facturas.marcar_enviada(pf["id"], "  ")


def test_facturar_se_puede_desde_cualquier_estado_abierto_y_exigir_aceptada_lo_restringe(base):
    a, b = _crear(), _crear()
    with pytest.raises(pre_facturas.TransicionInvalida, match="falta que se acepte"):
        pre_facturas.marcar_facturada(a["id"], _factura(), exigir_aceptada=True)
    assert pre_facturas.get(a["id"])["estado"] == "pendiente"
    assert pre_facturas.marcar_facturada(b["id"], _factura())["estado"] == "facturado"   # sin exigir


def test_facturado_y_descartado_son_finales(base):
    facturada, anulada = _crear(), _crear()
    pre_facturas.marcar_facturada(facturada["id"], _factura())
    pre_facturas.anular(anulada["id"], "mariano", "pedido duplicado")
    for pf in (facturada, anulada):
        i = pf["id"]
        with pytest.raises(pre_facturas.TransicionInvalida):
            pre_facturas.editar(i, observaciones="otra")
        with pytest.raises(pre_facturas.TransicionInvalida):
            pre_facturas.marcar_enviada(i, "a@example.com")
        with pytest.raises(pre_facturas.TransicionInvalida):
            pre_facturas.marcar_aceptada(i, "mariano")
        with pytest.raises(pre_facturas.TransicionInvalida):
            pre_facturas.anular(i, "mariano", "x")
        with pytest.raises(pre_facturas.TransicionInvalida):
            pre_facturas.marcar_facturada(i, 1)
    assert pre_facturas.get(facturada["id"])["estado"] == "facturado"
    assert pre_facturas.get(anulada["id"])["estado"] == "descartado"


def test_anular_deja_quien_y_por_que_desde_cada_estado_abierto(base):
    pendiente, enviada, aceptada = _crear(), _crear(), _crear()
    pre_facturas.marcar_enviada(enviada["id"], "a@example.com")
    pre_facturas.marcar_aceptada(aceptada["id"], "mariano")
    for pf in (pendiente, enviada, aceptada):
        anulada = pre_facturas.anular(pf["id"], "mariano", "el cliente la rechazó")
        assert (anulada["estado"], anulada["resuelto_por"], anulada["motivo_descarte"]) == (
            "descartado", "mariano", "el cliente la rechazó")
        assert anulada["resuelto_at"] and anulada["factura_id"] is None


def test_editar_recalcula_el_total_y_no_mueve_el_estado_de_una_pendiente(base):
    pf = _crear()
    nuevo = pre_facturas.editar(pf["id"], items=[{**FLETE, "qty": 2}], observaciones="Entrega en planta",
                                tipo_comprobante=6, cliente_domicilio="Calle Falsa 123")
    assert nuevo["total"] == 242000.0 and nuevo["estado"] == "pendiente"
    assert (nuevo["observaciones"], nuevo["tipo_comprobante"], nuevo["cliente_domicilio"]) == (
        "Entrega en planta", 6, "Calle Falsa 123")
    assert nuevo["numero_interno"] == "PF-0001" and nuevo["origen_id"] == "PF-0001"


def test_editar_una_enviada_o_aceptada_la_devuelve_a_pendiente_y_borra_el_rastro(base):
    enviada, aceptada = _crear(), _crear()
    pre_facturas.marcar_enviada(enviada["id"], "a@example.com")
    pre_facturas.marcar_enviada(aceptada["id"], "a@example.com")
    pre_facturas.marcar_aceptada(aceptada["id"], "mariano")
    for pf in (enviada, aceptada):
        editada = pre_facturas.editar(pf["id"], items=[{**FLETE, "unit_price": 90000.0}])
        assert editada["estado"] == "pendiente" and editada["total"] == 108900.0
        assert [editada[c] for c in ("enviado_at", "enviado_a", "aceptado_at", "aceptado_por")] == [None] * 4


def test_un_editar_que_no_cambia_nada_no_la_devuelve_a_pendiente(base):
    pf = _crear()
    pre_facturas.marcar_aceptada(pf["id"], "mariano")
    igual = pre_facturas.editar(pf["id"], cliente_razon="Juan Pérez", items=[FLETE], fecha_vencimiento_pago="",
                                tipo_comprobante=1)
    assert igual["estado"] == "aceptado" and igual["aceptado_por"] == "mariano"


def test_editar_valida_y_no_escribe_si_algo_esta_mal(base):
    pf = _crear()
    for campos, excepcion in [({"items": []}, ValueError), ({"cliente_razon": ""}, ValueError),
                              ({"tipo_comprobante": 11}, ValueError), ({"emisor_id": 999}, EmisorDesconocido),
                              ({"origen_id": "x"}, TypeError), ({"numero_interno": "PF-0099"}, TypeError),
                              ({"estado": "facturado"}, TypeError), ({"total": 1}, TypeError)]:
        with pytest.raises(excepcion):
            pre_facturas.editar(pf["id"], **campos)
    assert pre_facturas.get(pf["id"]) == pf


def test_editar_el_vencimiento_y_el_emisor(base):
    emisor = _arca()
    pf = _crear(tipo_comprobante=206)
    nuevo = pre_facturas.editar(pf["id"], fecha_vencimiento_pago="2026-12-01", emisor_id=emisor)
    assert (nuevo["fecha_vencimiento_pago"], nuevo["emisor_id"]) == ("2026-12-01", emisor)
    assert pre_facturas.editar(pf["id"], emisor_id=None, fecha_vencimiento_pago="")["emisor_id"] is None


def test_una_pre_factura_que_no_existe_es_un_error_claro(base):
    for llamada in (lambda: pre_facturas.editar(999, observaciones="x"),
                    lambda: pre_facturas.marcar_aceptada(999),
                    lambda: pre_facturas.anular(999),
                    lambda: pre_facturas.pdf(999)):
        with pytest.raises(pre_facturas.PreFacturaNoEncontrada):
            llamada()


# ── conn= (ADR-025) ──────────────────────────────────────────────────────────


def test_con_la_conexion_de_quien_llama_no_se_confirma_nada(base):
    conn = core.get_connection()
    try:
        pf = _crear(conn=conn)
        pre_facturas.marcar_enviada(pf["id"], "a@example.com", conn=conn)
        pre_facturas.editar(pf["id"], observaciones="x", conn=conn)
        assert pre_facturas.get(pf["id"], conn=conn)["estado"] == "pendiente"   # la edición la devolvió
        pre_facturas.marcar_aceptada(pf["id"], "mariano", conn=conn)
        conn.rollback()
    finally:
        conn.close()
    assert pre_facturas.listar() == []
    assert _crear()["numero_interno"] == "PF-0001"      # y el número no se gastó


def test_facturar_en_la_transaccion_del_producto_se_deshace_con_ella(base):
    pf = _crear()
    factura_id = _factura()
    conn = core.get_connection()
    try:
        assert pre_facturas.marcar_facturada(pf["id"], factura_id, "mariano", conn=conn)["estado"] == "facturado"
        conn.rollback()
    finally:
        conn.close()
    assert pre_facturas.get(pf["id"])["estado"] == "pendiente"
    conn = core.get_connection()
    try:
        pre_facturas.marcar_facturada(pf["id"], factura_id, conn=conn)
        conn.commit()
    finally:
        conn.close()
    assert pre_facturas.get(pf["id"])["estado"] == "facturado"


# ── La bandeja de siempre sigue igual ────────────────────────────────────────


def _cuota(**kwargs):
    base_ = dict(origen_producto="libradesk", origen_instancia="compulibra", origen_tipo="cuota_contrato",
                 origen_id="42", cliente_razon="Ferretería San Martín",
                 items=[{"description": "Alquiler impresora", "qty": 1, "unit_price": 45000.0, "iva_rate": 0.21}])
    base_.update(kwargs)
    return bandeja.upsert_comprobante(**base_)


def test_una_fila_de_la_bandeja_no_es_una_pre_factura(base):
    cid, creado = _cuota()
    assert creado
    assert pre_facturas.get(cid) is None and pre_facturas.listar() == []
    for llamada in (lambda: pre_facturas.editar(cid, observaciones="x"),
                    lambda: pre_facturas.marcar_enviada(cid, "a@example.com"),
                    lambda: pre_facturas.marcar_aceptada(cid, "m"),
                    lambda: pre_facturas.anular(cid, "m")):
        with pytest.raises(pre_facturas.PreFacturaNoEncontrada):
            llamada()
    fila = bandeja.get_comprobante(cid)
    assert fila["estado"] == "pendiente" and fila["numero_interno"] is None and fila["tipo_comprobante"] is None
    assert [c["id"] for c in bandeja.list_por_estado("pendiente")] == [cid]
    assert bandeja.contar_pendientes() == 1


def test_el_productor_sigue_reenviando_y_la_bandeja_marca_facturado_desde_pendiente(base):
    cid, _ = _cuota()
    assert _cuota(items=[{"description": "Alquiler", "qty": 2, "unit_price": 45000.0, "iva_rate": 0.21}]) == (cid, False)
    assert bandeja.get_comprobante(cid)["total"] == 108900.0
    assert bandeja.marcar_facturado(cid, _factura(), "mariano") is True
    assert bandeja.marcar_facturado(cid, _factura()) is False         # ya resuelto: no se pisa
    with pytest.raises(bandeja.ComprobanteYaResuelto):
        _cuota()


def test_una_pre_factura_que_una_persona_ya_movio_no_la_pisa_el_reenvio_de_un_productor(base):
    """`upsert_comprobante` acepta el origen `pre_factura` (es de la lista cerrada): si el producto reenvía una
    que el cliente ya vio, no se le cambian los datos por debajo."""
    pf = _crear(origen_id="orden-9")
    pre_facturas.marcar_enviada(pf["id"], "a@example.com")
    with pytest.raises(bandeja.ComprobanteYaResuelto, match="enviado"):
        bandeja.upsert_comprobante("libracargo", "pre_factura", "orden-9", "Otro", [FLETE], origen_instancia="demo")


def test_la_bandeja_descarta_y_factura_una_pre_factura_enviada_o_aceptada(base):
    a, b = _crear(), _crear()
    pre_facturas.marcar_enviada(a["id"], "a@example.com")
    pre_facturas.marcar_aceptada(b["id"], "m")
    assert bandeja.descartar(a["id"], "no va", "m") is True
    assert bandeja.marcar_facturado(b["id"], _factura(), "m") is True
    assert bandeja.contar_pendientes() == 0


# ── El PDF ───────────────────────────────────────────────────────────────────


def _texto(pdf_bytes: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)


def test_el_pdf_dice_pre_factura_trae_el_numero_interno_y_nada_fiscal(base):
    emisor = _arca()
    pf = _crear(emisor_id=emisor, cliente_domicilio="Calle Falsa 123", observaciones="Entrega en planta",
                periodo_desde="2026-09-01", periodo_hasta="2026-09-30")
    pdf = pre_facturas.pdf(pf["id"])
    assert pdf.startswith(b"%PDF-")
    texto = _texto(pdf)
    assert "PRE FACTURA" in texto and "COMPROBANTE FISCAL" in texto and LEYENDA_PRE_FACTURA in texto
    assert "PF-0001" in texto
    assert "Juan Pérez" in texto and "20-12345678-6" in texto and "Flete Rosario - Buenos Aires" in texto
    assert "Transportes del Plata S.R.L." in texto and "30-12345678-1" in texto
    assert "$ 100.000,00" in texto and "$ 21.000,00" in texto and "$ 121.000,00" in texto
    assert "Factura A" in texto and "Entrega en planta" in texto
    assert "Per. facturado: 01-09-2026 al 30-09-2026" in texto
    # Lo que NO tiene: una pre factura no es un comprobante de ARCA.
    for fiscal in ("CAE", "Punto de venta", "punto de venta", "Pto. Vta", "QR", "ARCA", "AFIP", "0001-"):
        assert fiscal not in texto, fiscal


def test_el_pdf_es_determinista_y_se_puede_pedir_de_cualquier_estado(base):
    pf = _crear()
    antes = pre_facturas.pdf(pf["id"])
    assert pre_facturas.pdf(pf["id"]) == antes             # la fecha del PDF es la del documento
    pre_facturas.marcar_facturada(pf["id"], _factura())
    assert "PRE FACTURA" in _texto(pre_facturas.pdf(pf["id"]))
    otra = _crear()
    pre_facturas.anular(otra["id"])
    assert "PF-0002" in _texto(pre_facturas.pdf(otra["id"]))


def test_el_pdf_toma_el_emisor_que_pasa_quien_llama(base):
    pf = _crear(emisor_id=_arca())
    texto = _texto(pre_facturas.pdf(pf["id"], emisor={"nombre": "Logística Austral S.A.", "cuit": "30-71111111-3",
                                                      "direccion": "Ruta 9 km 300", "iva_condition": "Responsable Inscripto"}))
    assert "Logística Austral S.A." in texto and "30-71111111-3" in texto and "Ruta 9 km 300" in texto
    assert "Transportes del Plata" not in texto


def test_el_pdf_de_clase_c_no_trae_columna_de_iva_y_la_fce_su_vencimiento(base):
    c = _crear(tipo_comprobante=11, items=[{**FLETE, "iva_rate": 0}])
    texto_c = _texto(pre_facturas.pdf(c["id"]))
    assert "Factura C" in texto_c and "$ 100.000,00" in texto_c and "IVA 0%" in texto_c
    assert "PRECIO UNIT. IVA" not in texto_c
    fce = _crear(tipo_comprobante=206, fecha_vencimiento_pago="2026-11-05")
    texto = _texto(pre_facturas.pdf(fce["id"]))
    assert "FCE MiPyME B" in texto and "Vto. pago: 05-11-2026" in texto


def test_el_pdf_con_alicuotas_mezcladas_y_muchos_items_pagina_sin_romper(base):
    items = [{"description": f"Viaje {i} - Rosario a Córdoba con una descripción bastante larga para envolver "
                             f"en más de un renglón y comprobar el salto de página", "qty": 1,
              "unit_price": 1000.0 + i, "iva_rate": 0.105 if i % 2 else 0.21} for i in range(40)]
    pf = _crear(items=items)
    pdf = pre_facturas.pdf(pf["id"])
    paginas = PdfReader(io.BytesIO(pdf)).pages
    assert len(paginas) > 1
    for pagina in paginas:
        assert "PRE FACTURA" in pagina.extract_text()      # el sello va en cada página
    assert f"$ {pre_facturas.pdf_generator._ar(pf['total'])}" in _texto(pdf)


def test_el_pdf_aguanta_caracteres_fuera_de_latin1(base):
    pf = _crear(cliente_razon="Ñandú “Fábrica” — Soporte ≥ 24hs ā", observaciones="Cobrar en € o 😀")
    assert "PRE FACTURA" in _texto(pre_facturas.pdf(pf["id"]))


# ── El correo ────────────────────────────────────────────────────────────────


def _resolver(configurado=True):
    return lambda: SimpleNamespace(configurado=configurado, host="smtp.example.com", port=587,
                                   user="facturacion@example.com", password="clave-de-prueba",
                                   from_email="facturacion@example.com", from_name="Transportes del Plata")


@patch("libracore.email_sender.smtplib.SMTP")
def test_enviar_por_correo_adjunta_el_pdf_y_la_marca_enviada(mock_smtp, base):
    servidor = MagicMock()
    mock_smtp.return_value.__enter__.return_value = servidor
    pf = _crear()

    enviada = pre_facturas.enviar_por_correo(pf["id"], "cliente@example.com", smtp_resolver=_resolver())

    assert (enviada["estado"], enviada["enviado_a"]) == ("enviado", "cliente@example.com")
    mock_smtp.assert_called_once_with("smtp.example.com", 587)
    servidor.starttls.assert_called_once()
    servidor.login.assert_called_once_with("facturacion@example.com", "clave-de-prueba")
    mensaje = servidor.send_message.call_args[0][0]
    assert mensaje["To"] == "Juan Pérez <cliente@example.com>"
    assert mensaje["Subject"] == "Pre factura PF-0001 - Transportes del Plata S.R.L."
    cuerpo = mensaje.get_body(preferencelist=("plain",)).get_content()
    assert "PF-0001" in cuerpo and "$ 121.000,00" in cuerpo and "no es una factura" in cuerpo
    adjunto = next(mensaje.iter_attachments())
    assert adjunto.get_filename() == "PF-0001.pdf"
    contenido = adjunto.get_content()
    assert contenido.startswith(b"%PDF-") and "PF-0001" in _texto(contenido)


@patch("libracore.email_sender.smtplib.SMTP")
def test_el_asunto_y_el_cuerpo_se_pueden_pasar(mock_smtp, base):
    servidor = MagicMock()
    mock_smtp.return_value.__enter__.return_value = servidor
    pf = _crear()
    pre_facturas.enviar_por_correo(pf["id"], "cliente@example.com", smtp_resolver=_resolver(),
                                   asunto="Su pre factura", cuerpo="Hola, revise los datos.")
    mensaje = servidor.send_message.call_args[0][0]
    assert mensaje["Subject"] == "Su pre factura"
    assert mensaje.get_body(preferencelist=("plain",)).get_content().strip() == "Hola, revise los datos."


@patch("libracore.email_sender.smtplib.SMTP")
def test_si_el_smtp_falla_la_pre_factura_queda_como_estaba(mock_smtp, base):
    import smtplib

    mock_smtp.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"mal")
    pf = _crear()
    with pytest.raises(smtplib.SMTPException):
        pre_facturas.enviar_por_correo(pf["id"], "cliente@example.com", smtp_resolver=_resolver())
    sin_cambios = pre_facturas.get(pf["id"])
    assert sin_cambios["estado"] == "pendiente" and sin_cambios["enviado_a"] is None


@patch("libracore.email_sender.smtplib.SMTP")
def test_sin_servidor_smtp_no_manda_ni_marca_nada(mock_smtp, base):
    pf = _crear()
    with pytest.raises(pre_facturas.SmtpNoConfigurado):
        pre_facturas.enviar_por_correo(pf["id"], "cliente@example.com", smtp_resolver=_resolver(configurado=False))
    mock_smtp.assert_not_called()
    assert pre_facturas.get(pf["id"])["estado"] == "pendiente"


@patch("libracore.email_sender.smtplib.SMTP")
def test_una_facturada_no_se_envia_y_el_destinatario_es_obligatorio(mock_smtp, base):
    pf = _crear()
    with pytest.raises(ValueError):
        pre_facturas.enviar_por_correo(pf["id"], " ", smtp_resolver=_resolver())
    pre_facturas.marcar_facturada(pf["id"], _factura())
    with pytest.raises(pre_facturas.TransicionInvalida):
        pre_facturas.enviar_por_correo(pf["id"], "cliente@example.com", smtp_resolver=_resolver())
    mock_smtp.assert_not_called()


@patch("libracore.email_sender.smtplib.SMTP")
def test_sin_resolver_cae_a_la_configuracion_de_la_instancia(mock_smtp, base, monkeypatch):
    """Es `smtp_efectivo`, el mismo camino que el envío de comprobantes y de presupuestos."""
    servidor = MagicMock()
    mock_smtp.return_value.__enter__.return_value = servidor
    cfg = {"empresa_nombre": "Transportes del Plata S.R.L.", "email_smtp_host": "smtp.config.example",
           "email_smtp_port": "2525", "email_smtp_user": "config@example.com", "email_smtp_password": "x",
           "email_from": "config@example.com", "email_from_name": "Config"}
    monkeypatch.setattr(config_manager, "load", lambda *a, **k: dict(cfg))
    pf = _crear()
    pre_facturas.enviar_por_correo(pf["id"], "cliente@example.com")
    mock_smtp.assert_called_once_with("smtp.config.example", 2525)


# ── Una instancia que ya tenía la bandeja ────────────────────────────────────

BANDEJA_VIEJA = """
    CREATE TABLE comprobantes_pendientes (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        origen_producto   TEXT NOT NULL,
        origen_instancia  TEXT NOT NULL DEFAULT '',
        origen_tipo       TEXT NOT NULL,
        origen_id         TEXT NOT NULL,
        cliente_id        INTEGER REFERENCES clients(id) ON DELETE SET NULL,
        cliente_cuit      TEXT DEFAULT '',
        cliente_razon     TEXT NOT NULL,
        cliente_domicilio TEXT DEFAULT '',
        fecha_sugerida    TEXT DEFAULT '',
        periodo_desde     TEXT DEFAULT '',
        periodo_hasta     TEXT DEFAULT '',
        concepto          TEXT DEFAULT '',
        condicion_venta   TEXT DEFAULT '',
        observaciones     TEXT DEFAULT '',
        items             TEXT NOT NULL DEFAULT '[]',
        total             REAL NOT NULL DEFAULT 0,
        estado            TEXT NOT NULL DEFAULT 'pendiente',
        factura_id        INTEGER REFERENCES facturas(id) ON DELETE SET NULL,
        motivo_descarte   TEXT DEFAULT '',
        resuelto_at       TEXT DEFAULT '',
        resuelto_por      TEXT DEFAULT '',
        created_at        TEXT DEFAULT (datetime('now','-3 hours'))
    );
    CREATE UNIQUE INDEX idx_comprobantes_pendientes_origen
        ON comprobantes_pendientes(origen_producto, origen_instancia, origen_tipo, origen_id);
"""

COLUMNAS_NUEVAS = {"numero_interno", "emisor_id", "tipo_comprobante", "fecha_vencimiento_pago", "enviado_at",
                   "enviado_a", "aceptado_at", "aceptado_por"}


def test_una_bandeja_de_antes_recibe_las_columnas_y_conserva_sus_filas(tmp_path):
    """El arranque de una instancia (`init_core_schema`, que corre en cada arranque y en la `0021`) agrega las
    ocho columnas a la tabla que ya existía, sin tocar lo que tenía, y repetirlo no hace nada."""
    import sqlite3

    ruta = tmp_path / "vieja.db"
    viejo = sqlite3.connect(str(ruta))
    viejo.executescript(BANDEJA_VIEJA)
    viejo.execute("INSERT INTO comprobantes_pendientes (origen_producto, origen_instancia, origen_tipo, origen_id, "
                  "cliente_razon, items, total, estado) VALUES ('libradesk','compulibra','cuota_contrato','42',"
                  "'Ferretería San Martín','[]', 54450, 'pendiente')")
    viejo.commit()
    viejo.close()

    core.configure(db_path=str(ruta))
    try:
        for _ in range(2):
            with core.get_connection() as conn:
                init_core_schema(conn)
                conn.commit()
        with core.get_connection() as conn:
            columnas = {r[1] for r in conn.execute("PRAGMA table_info(comprobantes_pendientes)")}
        assert COLUMNAS_NUEVAS <= columnas

        fila = bandeja.get_comprobante(1)
        assert (fila["cliente_razon"], fila["total"], fila["estado"], fila["numero_interno"]) == (
            "Ferretería San Martín", 54450.0, "pendiente", None)
        assert pre_facturas.listar() == []                       # la cuota de LibraDesk no es una pre factura
        assert pre_facturas.crear(origen_producto="libradesk", origen_instancia="compulibra",
                                  cliente_razon="Juan Pérez", items=[FLETE])["numero_interno"] == "PF-0001"
        assert bandeja.upsert_comprobante("libradesk", "cuota_contrato", "42", "Ferretería San Martín", [FLETE],
                                          origen_instancia="compulibra") == (1, False)
    finally:
        core._db_path = None
        core._database_url = None
