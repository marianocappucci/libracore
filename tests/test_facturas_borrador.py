"""
Borrador de un comprobante duplicado: recálculo de las fechas de servicio y
del vencimiento de pago, y copia del resto del comprobante.

El caso que motivó el módulo: duplicar una factura de servicios dejaba el
período y el vencimiento en el pasado, porque se copiaban tal cual mientras
la fecha de emisión se reseteaba a hoy (ver libracore/facturas_borrador.py).

El padrón de clientes se sustituye por un doble: acá interesa qué borrador
sale, no cómo se lee la base.
"""
from libracore.facturas_borrador import (
    armar_borrador,
    tasa_iva,
    vencimiento_duplicado,
)

HOY = "2026-07-28"

FACTURA_SERVICIOS = {
    "id": 1,
    "tipo": 11,
    "punto_venta": 4,
    "numero": 123,
    "fecha": "2026-06-30",
    "cliente_cuit": "20304050607",
    "cliente_razon": "Cliente Servicios SA",
    "concepto": 2,
    "condicion_venta": "Cuenta Corriente",
    "observaciones": "Periodo junio",
    "subtotal": 50000.0,
    "iva_amount": 10500.0,
    "total": 60500.0,
    "items": [{"description": "Abono mensual", "qty": 1, "unit_price": 50000.0, "subtotal": 50000.0}],
    "fch_serv_desde": "2026-06-01",
    "fch_serv_hasta": "2026-06-30",
    "fch_vto_pago": "2026-07-10",
    "cae": "75123456789012",
}


def _sin_padron(cuit):
    return None


def _padron_con(cliente):
    return lambda cuit: cliente if cuit == cliente["cuit_dni"] else None


# --- vencimiento de pago ------------------------------------------------


def test_conserva_los_dias_de_plazo_del_original():
    # vencía 10 días después de cerrar el período -> 10 días después de hoy
    assert vencimiento_duplicado("2026-06-30", "2026-07-10", HOY) == "2026-08-07"


def test_vencimiento_el_mismo_dia_que_cierra_el_periodo():
    assert vencimiento_duplicado("2026-06-30", "2026-06-30", HOY) == HOY


def test_servicio_prepago_no_devuelve_una_fecha_pasada():
    # el original vencía ANTES de cerrar el período: el plazo negativo no
    # puede empujar el vencimiento nuevo al pasado
    assert vencimiento_duplicado("2026-06-30", "2026-06-15", HOY) == HOY


def test_sin_fechas_de_servicio_vence_hoy():
    assert vencimiento_duplicado("", "", HOY) == HOY
    assert vencimiento_duplicado("2026-06-30", "", HOY) == HOY


def test_fecha_invalida_no_rompe():
    assert vencimiento_duplicado("no-es-fecha", "2026-07-10", HOY) == HOY
    assert vencimiento_duplicado("2026-06-30", "31/12/2026", HOY) == HOY


def test_plazo_largo_cruza_meses_y_anios():
    assert vencimiento_duplicado("2026-12-31", "2027-02-28", "2026-12-31") == "2027-02-28"
    assert vencimiento_duplicado("2024-02-29", "2024-03-30", "2024-02-29") == "2024-03-30"


# --- tasa de IVA --------------------------------------------------------


def test_tasa_iva_se_deduce_de_los_montos():
    assert tasa_iva({"subtotal": 50000.0, "iva_amount": 10500.0}) == 0.21


def test_tasa_iva_default_si_no_hay_subtotal():
    # monotributista: no discrimina IVA, subtotal 0 no puede dividir
    assert tasa_iva({"subtotal": 0, "iva_amount": 0}) == 0.21


# --- borrador completo --------------------------------------------------


def test_el_periodo_de_servicio_arranca_de_nuevo():
    b = armar_borrador(FACTURA_SERVICIOS, hoy=HOY, resolver_cliente=_sin_padron)
    assert b["fch_serv_desde"] == HOY
    assert b["fch_serv_hasta"] == HOY
    assert b["fch_vto_pago"] == "2026-08-07"


def test_ninguna_fecha_del_borrador_queda_antes_de_hoy():
    b = armar_borrador(FACTURA_SERVICIOS, hoy=HOY, resolver_cliente=_sin_padron)
    for campo in ("fch_serv_desde", "fch_serv_hasta", "fch_vto_pago"):
        assert b[campo] >= HOY, campo


def test_copia_el_resto_del_comprobante():
    b = armar_borrador(FACTURA_SERVICIOS, hoy=HOY, resolver_cliente=_sin_padron)
    assert b["tipo"] == 11
    assert b["punto_venta"] == 4
    assert b["concepto"] == 2
    assert b["condicion_venta"] == "Cuenta Corriente"
    assert b["observations"] == "Periodo junio"
    assert b["tax_rate"] == 0.21
    assert b["items"] == [{"description": "Abono mensual", "qty": 1, "unit_price": 50000.0}]


def test_no_arrastra_el_cae_ni_el_numero_del_original():
    b = armar_borrador(FACTURA_SERVICIOS, hoy=HOY, resolver_cliente=_sin_padron)
    assert "cae" not in b
    assert "numero" not in b
    assert "id" not in b


def test_reencuentra_al_cliente_en_el_padron():
    cliente = {"id": 7, "cuit_dni": "20304050607", "name": "Cliente Servicios SA"}
    b = armar_borrador(FACTURA_SERVICIOS, hoy=HOY, resolver_cliente=_padron_con(cliente))
    assert b["client_id"] == 7
    assert b["client_name"] == ""


def test_cliente_que_ya_no_existe_viaja_como_nombre_libre():
    b = armar_borrador(FACTURA_SERVICIOS, hoy=HOY, resolver_cliente=_sin_padron)
    assert b["client_id"] is None
    assert b["client_name"] == "Cliente Servicios SA"


def test_factura_a_consumidor_final_sin_cuit_no_consulta_el_padron():
    consultas = []

    def _espia(cuit):
        consultas.append(cuit)
        return None

    factura = dict(FACTURA_SERVICIOS, cliente_cuit="", cliente_razon="Consumidor Final")
    b = armar_borrador(factura, hoy=HOY, resolver_cliente=_espia)
    assert consultas == []
    assert b["client_id"] is None
    assert b["client_name"] == "Consumidor Final"


def test_factura_de_productos_sin_periodo_de_servicio():
    factura = dict(
        FACTURA_SERVICIOS, concepto=1, fch_serv_desde="", fch_serv_hasta="", fch_vto_pago="",
    )
    b = armar_borrador(factura, hoy=HOY, resolver_cliente=_sin_padron)
    assert b["concepto"] == 1
    # el borrador las lleva igual: si el usuario cambia el concepto a
    # Servicios en el formulario, ya vienen en hoy y no vacías
    assert b["fch_serv_desde"] == HOY
    assert b["fch_vto_pago"] == HOY


def test_comprobante_sin_items_no_rompe():
    factura = dict(FACTURA_SERVICIOS, items=[])
    b = armar_borrador(factura, hoy=HOY, resolver_cliente=_sin_padron)
    assert b["items"] == []


def test_hoy_por_defecto_es_la_fecha_del_sistema():
    import datetime

    b = armar_borrador(FACTURA_SERVICIOS, resolver_cliente=_sin_padron)
    assert b["fch_serv_desde"] == datetime.date.today().isoformat()
