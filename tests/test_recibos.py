"""Emisión de recibos: cuándo se emite uno nuevo y cuándo se reimprime.

El caso que manda es el de la factura cobrada en cuotas — es el único origen
donde apretar el botón dos veces podría emitir dos recibos por la misma plata,
y es exactamente lo que el modelo viejo (PDF armado al vuelo) no podía evitar
porque no registraba nada.
"""
import io

import pytest
from pypdf import PdfReader

from libracore import recibos
from libracore.db import core, cuenta_corriente
from libracore.db import recibos as db_recibos
from libracore.db.schema import init_core_schema
from libracore.pdf_generator import generate_pdf_recibo, generate_pdf_recibo_doc


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "emision.db"))
    c = core.get_connection()
    init_core_schema(c)
    # El cliente tiene que existir de verdad: `recibos.cliente_id` es una FK, y
    # los pagos de prueba de abajo apuntan al 17. Que la FK esté puesta es
    # parte de lo que se prueba — un recibo a nombre de un cliente que no está
    # en el padrón no debería poder escribirse.
    c.execute("INSERT INTO clients (id, name, cuit_dni, address) "
              "VALUES (17, 'Municipalidad de Suipacha', '30-99999999-7', 'Calle Falsa 123')")
    c.commit()
    yield c
    c.close()
    core._db_path = None


FACTURA = {
    "id": 43, "tipo": 11, "punto_venta": 5, "numero": 43,
    "fecha": "2026-07-13", "cliente_razon": "Municipalidad de Suipacha",
    "cliente_cuit": "30-99999999-7", "cliente_domicilio": "Calle Falsa 123",
    "total": 40000.0,
}


def _texto_del_pdf(pdf: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def _mov(mid, monto, medio="efectivo", fecha="2026-07-13"):
    return {"id": mid, "fecha": fecha, "medio_pago": medio,
            "referencia": "", "monto": monto}


def _emitir_factura(cobros, factura=None, **kwargs):
    return recibos.emitir_recibo_factura(
        (factura or FACTURA)["id"],
        get_factura=lambda _id: factura or FACTURA,
        get_cobros_factura=lambda _id: cobros,
        **kwargs,
    )


# ── Cobranza de cuenta corriente: el caso que no existía ─────────────────────

PAGO = {
    "id": 3, "cliente_id": 17, "monto": 10500.0, "fecha": "2026-07-03",
    "concepto": "Pago a cuenta", "referencia": "", "medio_pago": "transferencia",
    "cliente_nombre": "Municipalidad de Suipacha",
    "cliente_cuit": "30-99999999-7", "cliente_domicilio": "Calle Falsa 123",
}


def test_un_pago_a_cuenta_emite_su_recibo(conn):
    recibo = recibos.emitir_recibo_cobranza(3, get_cc_pago=lambda _id: PAGO)
    assert recibo["numero"] == 1
    assert recibo["origen_tipo"] == "cc_pago"
    assert recibo["total"] == 10500.0
    assert recibo["cliente_razon"] == "Municipalidad de Suipacha"
    assert recibo["pagos"][0]["medio_pago"] == "transferencia"


def test_pedir_dos_veces_el_recibo_de_una_cobranza_devuelve_el_mismo(conn):
    primero = recibos.emitir_recibo_cobranza(3, get_cc_pago=lambda _id: PAGO)
    segundo = recibos.emitir_recibo_cobranza(3, get_cc_pago=lambda _id: PAGO)
    assert primero["id"] == segundo["id"]
    assert db_recibos.contar_recibos() == 1


def test_dos_pagos_distintos_del_mismo_cliente_son_dos_recibos(conn):
    otro = {**PAGO, "id": 4, "monto": 2000.0}
    a = recibos.emitir_recibo_cobranza(3, get_cc_pago=lambda _id: PAGO)
    b = recibos.emitir_recibo_cobranza(4, get_cc_pago=lambda _id: otro)
    assert (a["numero"], b["numero"]) == (1, 2)


def test_anular_el_recibo_de_una_cobranza_permite_emitir_otro(conn):
    primero = recibos.emitir_recibo_cobranza(3, get_cc_pago=lambda _id: PAGO)
    db_recibos.anular_recibo(primero["id"], motivo="se imprimio mal")
    segundo = recibos.emitir_recibo_cobranza(3, get_cc_pago=lambda _id: PAGO)
    assert segundo["id"] != primero["id"]
    assert segundo["numero"] == 2


def test_un_pago_que_no_existe_no_emite_nada(conn):
    with pytest.raises(recibos.SinCobros):
        recibos.emitir_recibo_cobranza(999, get_cc_pago=lambda _id: None)


def test_el_circuito_real_sin_inyectar_ningun_lector(conn):
    """Los demás tests pasan un `get_cc_pago` de mentira, así que ninguno toca
    el de verdad — y ése es el que corre en producción. Acá el pago se escribe
    con `create_cc_pago` y se lee con el lector real, que es el que tiene que
    resolver el nombre y el CUIT del cliente contra `clients`."""
    pago_id = cuenta_corriente.create_cc_pago(
        cliente_id=17, monto=10500.50, fecha="2026-08-04",
        concepto="Pago a cuenta", referencia="transf. 8842",
        medio_pago="transferencia", caja_id=None, usuario_id=None,
    )

    recibo = recibos.emitir_recibo_cobranza(pago_id)

    assert recibo["cliente_razon"] == "Municipalidad de Suipacha"
    assert recibo["cliente_cuit"] == "30-99999999-7"
    assert recibo["cliente_domicilio"] == "Calle Falsa 123"
    assert recibo["total"] == 10500.50
    assert recibo["pagos"][0]["referencia"] == "transf. 8842"
    assert recibos.emitir_recibo_cobranza(pago_id)["id"] == recibo["id"]


# ── Venta de mostrador ───────────────────────────────────────────────────────

VENTA = {
    "id": 12, "numero": "V-0012", "fecha": "2026-08-04",
    "cliente_nombre": "Consumidor Final", "cliente_cuit": "",
    "pagos": [{"medio": "efectivo", "monto": 3000.0, "referencia": ""},
              {"medio": "tarjeta", "monto": 1500.0, "referencia": "lote 22"}],
}


def test_una_venta_emite_un_recibo_por_el_total_cobrado(conn):
    recibo = recibos.emitir_recibo_venta(12, get_venta=lambda _id: VENTA)
    assert recibo["total"] == 4500.0
    assert len(recibo["pagos"]) == 2
    assert "V-0012" in recibo["concepto"]


def test_el_recibo_de_venta_es_idempotente(conn):
    a = recibos.emitir_recibo_venta(12, get_venta=lambda _id: VENTA)
    b = recibos.emitir_recibo_venta(12, get_venta=lambda _id: VENTA)
    assert a["id"] == b["id"]


def test_una_venta_sin_pagos_no_emite_recibo(conn):
    vacia = {**VENTA, "pagos": []}
    with pytest.raises(recibos.SinCobros):
        recibos.emitir_recibo_venta(12, get_venta=lambda _id: vacia)


# ── Factura: el único origen que acumula ─────────────────────────────────────

def test_el_recibo_de_factura_cubre_los_cobros_registrados(conn):
    recibo = _emitir_factura([_mov(1, 40000.0)])
    assert recibo["total"] == 40000.0
    assert recibo["pagos"][0]["caja_movimiento_id"] == 1


def test_apretar_el_boton_dos_veces_no_emite_dos_recibos_por_la_misma_plata(conn):
    cobros = [_mov(1, 40000.0)]
    primero = _emitir_factura(cobros)
    segundo = _emitir_factura(cobros)
    assert primero["id"] == segundo["id"]
    assert db_recibos.contar_recibos() == 1


def test_un_cobro_posterior_emite_un_recibo_nuevo_solo_por_lo_nuevo(conn):
    """La factura se cobra en dos cuotas: dos papeles, cada uno por lo suyo."""
    primera = _emitir_factura([_mov(1, 20000.0)])
    segunda = _emitir_factura([_mov(1, 20000.0), _mov(2, 20000.0, fecha="2026-07-20")])

    assert segunda["id"] != primera["id"]
    assert primera["total"] == 20000.0
    assert segunda["total"] == 20000.0
    assert [p["caja_movimiento_id"] for p in segunda["pagos"]] == [2]
    assert (primera["numero"], segunda["numero"]) == (1, 2)


def test_el_primer_recibo_no_cambia_cuando_llega_el_segundo_cobro(conn):
    """Lo que el modelo viejo no podía sostener: el papel ya entregado."""
    primera = _emitir_factura([_mov(1, 20000.0)])
    _emitir_factura([_mov(1, 20000.0), _mov(2, 20000.0)])
    assert db_recibos.get_recibo(primera["id"])["total"] == 20000.0
    assert db_recibos.get_recibo(primera["id"])["pagos"] == primera["pagos"]


def test_un_cobro_parcial_lo_dice_y_el_que_cancela_tambien(conn):
    parcial = _emitir_factura([_mov(1, 20000.0)])
    assert parcial["concepto"].startswith("Pago parcial de")

    total = _emitir_factura([_mov(1, 20000.0), _mov(2, 20000.0)])
    assert total["concepto"].startswith("Cancelacion de")
    assert "0005-00000043" in total["concepto"]


def test_una_factura_sin_cobros_no_emite_recibo(conn):
    with pytest.raises(recibos.SinCobros):
        _emitir_factura([])


def test_anular_el_recibo_devuelve_sus_cobros_al_pozo(conn):
    """Un recibo anulado no cubre nada: esa plata puede volver a recibirse."""
    primero = _emitir_factura([_mov(1, 40000.0)])
    db_recibos.anular_recibo(primero["id"], motivo="numero salteado")
    segundo = _emitir_factura([_mov(1, 40000.0)])
    assert segundo["id"] != primero["id"]
    assert segundo["total"] == 40000.0


def test_el_recibo_de_factura_encuentra_al_cliente_por_cuit(conn):
    """Las facturas no tienen FK al cliente — se emiten a un CUIT suelto. Si el
    recibo no lo resolviera, quedaría en NULL y el historial del cliente no lo
    mostraria."""
    recibo = _emitir_factura([_mov(1, 40000.0)])
    assert recibo["cliente_id"] == 17
    assert [r["id"] for r in db_recibos.get_recibos(cliente_id=17)] == [recibo["id"]]


def test_una_factura_a_nombre_libre_igual_emite_su_recibo(conn):
    """Sin cliente en el padrón no hay a quién apuntar, pero el CUIT y la razón
    social quedan guardados en la fila y el papel sale igual."""
    suelta = {**FACTURA, "id": 90, "cliente_cuit": "", "cliente_razon": "Juan Perez"}
    recibo = _emitir_factura([_mov(1, 40000.0)], factura=suelta)
    assert recibo["cliente_id"] is None
    assert recibo["cliente_razon"] == "Juan Perez"


def test_los_recibos_de_facturas_distintas_no_se_pisan(conn):
    otra = {**FACTURA, "id": 44, "numero": 44}
    a = _emitir_factura([_mov(1, 40000.0)])
    b = _emitir_factura([_mov(9, 5000.0)], factura=otra)
    assert a["id"] != b["id"]
    assert "0005-00000044" in b["concepto"]


# ── El PDF ───────────────────────────────────────────────────────────────────

def test_el_pdf_del_documento_lleva_el_numero_impreso(conn):
    """Se lee el texto del PDF, no su tamaño: que salgan bytes no prueba que el
    número haya llegado al papel, que es lo único que vuelve citable al
    recibo."""
    recibo = recibos.emitir_recibo_cobranza(3, get_cc_pago=lambda _id: PAGO)
    texto = _texto_del_pdf(generate_pdf_recibo_doc(recibo))
    assert "0001-00000001" in texto
    assert "Recibo N" in texto
    assert "Municipalidad de Suipacha" in texto
    assert "10.500,00" in texto


def test_reimprimir_devuelve_exactamente_el_mismo_papel(conn):
    """El corazón del cambio: el mismo recibo, byte a byte, las veces que se
    pida. Con el modelo viejo esto no se podía afirmar."""
    recibo = recibos.emitir_recibo_cobranza(3, get_cc_pago=lambda _id: PAGO)
    guardado = db_recibos.get_recibo(recibo["id"])
    assert generate_pdf_recibo_doc(recibo) == generate_pdf_recibo_doc(guardado)


def test_el_pdf_del_documento_marca_los_anulados(conn):
    recibo = recibos.emitir_recibo_cobranza(3, get_cc_pago=lambda _id: PAGO)
    db_recibos.anular_recibo(recibo["id"], motivo="cargado dos veces")
    texto = _texto_del_pdf(generate_pdf_recibo_doc(
        db_recibos.get_recibo(recibo["id"])))
    assert "ANULADO" in texto
    assert "cargado dos veces" in texto
    # Y el vigente no lo dice, que es la mitad que importa de la afirmación.
    vigente = recibos.emitir_recibo_venta(12, get_venta=lambda _id: VENTA)
    assert "ANULADO" not in _texto_del_pdf(generate_pdf_recibo_doc(vigente))


def test_el_recibo_de_factura_imprime_el_comprobante_que_cobra(conn):
    recibo = _emitir_factura([_mov(1, 40000.0)])
    texto = _texto_del_pdf(generate_pdf_recibo_doc(recibo))
    assert "0005-00000043" in texto
    assert "40.000,00" in texto


def test_un_caracter_fuera_de_cp1252_ya_no_tumba_el_recibo(conn):
    """El recibo era el último comprobante que instanciaba FPDF pelado, así que
    se había quedado afuera del arreglo de v1.7.0 — un nombre pegado de un mail
    lo mandaba a un 500."""
    recibo = recibos.emitir_recibo_cobranza(
        3, get_cc_pago=lambda _id: {**PAGO, "cliente_nombre": "Nguyễn Café 🎉"})
    assert generate_pdf_recibo_doc(recibo).startswith(b"%PDF")


def test_la_firma_vieja_sigue_generando_su_pdf_sin_numero(conn):
    """Contalibra y Restolibra la llaman hoy; no puede romperse en el bump."""
    pdf = generate_pdf_recibo(FACTURA, [_mov(1, 40000.0)])
    assert pdf.startswith(b"%PDF")
