"""
Registro del cobro de un comprobante: qué se escribe, en qué orden, y qué se
rechaza sin escribir nada.

El caso que motivó el módulo: el diálogo de cobro ofrecía "Cuenta corriente"
como medio, y elegirla registraba un cobro que el propio motor ignoraba —la
factura seguía "Sin cobrar" y la cuenta corriente sumaba la misma deuda dos
veces (ver libracore/cobros.py).

Las escrituras se sustituyen por dobles: acá interesa QUÉ se escribe, no cómo
llega a la base, que ya tienen cubierto los tests de `db/`.
"""
import pytest

from libracore.cobros import (
    CobroNoPermitido,
    MedioNoEsDeCobro,
    centavos,
    es_medio_cuenta_corriente,
    registrar_cobro_factura,
    saldo_de_cobro,
)

HOY = "2026-08-03"

FACTURA_CC = {
    "id": 15,
    "tipo": 11,
    "punto_venta": 5,
    "numero": 5,
    "cliente_cuit": "30665138166",
    "cliente_razon": "MUNICIPALIDAD DE SUIPACHA",
    "condicion_venta": "Cuenta Corriente",
    "total": 90000.0,
}

FACTURA_CONTADO = {**FACTURA_CC, "condicion_venta": "Contado", "id": 16}


_SIN_ESPECIFICAR = object()


class Escrituras:
    """Doble de las tres escrituras, que además registra el orden real."""

    def __init__(self, cliente=_SIN_ESPECIFICAR, *, total=90000.0, cobrado=0.0, anulada=False):
        self.movimientos = []
        self.cc_pagos = []
        self.lecturas = 0
        # Lo que la base diría de la factura (ADR-058): por omisión, sin cobrar y de 90.000, que es
        # la de los ejemplos de abajo. Se arma con `saldo_de_cobro`, la misma cuenta que usa el motor.
        self.estado = {
            **saldo_de_cobro({"total": total, "tipo": 11}, [{"monto": cobrado}] if cobrado else []),
            "anulada": anulada, "con_tope": True,
        }
        # Centinela y no `None` como default: `Escrituras(cliente=None)` es el
        # caso real de un comprobante a nombre libre, y tiene que poder
        # distinguirse de "no me importa quien sea el cliente".
        self.cliente = {"id": 1} if cliente is _SIN_ESPECIFICAR else cliente

    def crear_movimiento(self, **kw):
        self.movimientos.append(kw)
        return 100 + len(self.movimientos)

    def crear_cc_pago(self, **kw):
        self.cc_pagos.append(kw)
        return 200 + len(self.cc_pagos)

    def resolver_cliente(self, cuit):
        return self.cliente

    def leer_estado(self, factura_id, conn):
        self.lecturas += 1
        return self.estado


def registrar(factura, pagos, esc=None, **kw):
    esc = esc or Escrituras()
    res = registrar_cobro_factura(
        factura, pagos, fecha=HOY, caja_id=1, usuario_id=2,
        crear_movimiento=esc.crear_movimiento,
        crear_cc_pago=esc.crear_cc_pago,
        resolver_cliente=esc.resolver_cliente,
        leer_estado=esc.leer_estado,
        tipo_label="FACTURA C",
        **kw,
    )
    return esc, res


def test_cobro_simple_escribe_un_movimiento_de_ingreso():
    esc, res = registrar(FACTURA_CONTADO, [{"medio_id": "transferencia", "monto": 90000.0}])
    assert res["total"] == 90000.0
    assert len(esc.movimientos) == 1
    mov = esc.movimientos[0]
    assert mov["tipo"] == "ingreso"
    assert mov["medio_pago"] == "transferencia"
    assert mov["monto"] == 90000.0
    assert mov["factura_id"] == 16
    assert mov["fecha"] == HOY


def test_el_concepto_conserva_el_formato_historico():
    """Hay movimientos viejos con este texto y la cuenta corriente se lo
    muestra al usuario: cambiarlo partiría el historial en dos formatos."""
    esc, _ = registrar(FACTURA_CC, [{"medio_id": "efectivo", "monto": 1.0}])
    assert esc.movimientos[0]["concepto"] == (
        "Cobro FACTURA C 0005-00000005 — MUNICIPALIDAD DE SUIPACHA"
    )
    # El abono en cuenta corriente lleva la misma linea sin la razon social.
    assert esc.cc_pagos[0]["concepto"] == "Cobro FACTURA C 0005-00000005"


def test_factura_en_cuenta_corriente_acredita_el_saldo():
    esc, res = registrar(FACTURA_CC, [{"medio_id": "transferencia", "monto": 90000.0}])
    assert len(esc.cc_pagos) == 1
    assert esc.cc_pagos[0]["cliente_id"] == 1
    assert esc.cc_pagos[0]["monto"] == 90000.0
    assert res["cc_pago_id"] == 201


def test_factura_al_contado_no_toca_la_cuenta_corriente():
    esc, res = registrar(FACTURA_CONTADO, [{"medio_id": "efectivo", "monto": 90000.0}])
    assert esc.cc_pagos == []
    assert res["cc_pago_id"] is None


def test_cuenta_corriente_sin_cliente_en_el_padron_igual_registra_la_caja():
    """Comprobante emitido a un nombre libre: no hay cuenta que mover, pero la
    plata entró y el movimiento de caja tiene que quedar."""
    esc = Escrituras(cliente=None)
    esc, res = registrar(FACTURA_CC, [{"medio_id": "efectivo", "monto": 500.0}], esc=esc)
    assert len(esc.movimientos) == 1
    assert esc.cc_pagos == []
    assert res["cc_pago_id"] is None


def test_varios_medios_suman_y_escriben_uno_por_pago():
    esc, res = registrar(FACTURA_CC, [
        {"medio_id": "efectivo", "monto": 40000.0},
        {"medio_id": "transferencia", "monto": 50000.0, "referencia": "TRF-9"},
    ])
    assert res["total"] == 90000.0
    assert [m["medio_pago"] for m in esc.movimientos] == ["efectivo", "transferencia"]
    assert esc.movimientos[1]["referencia"] == "TRF-9"
    # Un solo abono por el total, no uno por medio.
    assert len(esc.cc_pagos) == 1
    assert esc.cc_pagos[0]["monto"] == 90000.0


def test_las_filas_vacias_del_formulario_se_ignoran():
    esc, res = registrar(FACTURA_CONTADO, [
        {"medio_id": "efectivo", "monto": 100.0},
        {"medio_id": "efectivo", "monto": 0},
        {"medio_id": "efectivo", "monto": None},
        {"medio_id": "efectivo"},
    ])
    assert res["total"] == 100.0
    assert len(esc.movimientos) == 1


@pytest.mark.parametrize("medio", [
    "cuenta_corriente",
    "Cuenta Corriente",
    "CUENTA CORRIENTE",
    "  cuenta_corriente  ",
])
def test_la_cuenta_corriente_no_es_un_medio_de_cobro(medio):
    """Las dos grafias conviven en la base y la comparacion es insensible a
    mayusculas y a espacios: el rechazo tiene que verlas todas."""
    esc = Escrituras()
    with pytest.raises(MedioNoEsDeCobro):
        registrar(FACTURA_CC, [{"medio_id": medio, "monto": 90000.0}], esc=esc)
    assert esc.movimientos == [], "escribio el movimiento antes de rechazar"
    assert esc.cc_pagos == []


def test_el_rechazo_ocurre_antes_de_escribir_ningun_medio_valido():
    """El caso que motivo validar todo por adelantado: con la cuenta corriente
    en segundo lugar, el primer medio no debe quedar registrado."""
    esc = Escrituras()
    with pytest.raises(MedioNoEsDeCobro):
        registrar(FACTURA_CC, [
            {"medio_id": "efectivo", "monto": 40000.0},
            {"medio_id": "cuenta_corriente", "monto": 50000.0},
        ], esc=esc)
    assert esc.movimientos == []
    assert esc.cc_pagos == []


def test_una_fila_vacia_en_cuenta_corriente_no_rompe_el_cobro():
    """Solo se valida lo que se iba a registrar: una fila con monto 0 no se
    escribe, asi que tampoco tiene por que abortar el cobro entero."""
    esc, res = registrar(FACTURA_CONTADO, [
        {"medio_id": "efectivo", "monto": 100.0},
        {"medio_id": "cuenta_corriente", "monto": 0},
    ], esc=Escrituras())
    assert res["total"] == 100.0


def test_el_mensaje_del_rechazo_explica_que_elegir():
    with pytest.raises(MedioNoEsDeCobro) as exc:
        registrar(FACTURA_CC, [{"medio_id": "cuenta_corriente", "monto": 1.0}])
    mensaje = str(exc.value)
    assert "no es un medio de cobro" in mensaje
    assert "transferencia" in mensaje


@pytest.mark.parametrize("medio,esperado", [
    ("cuenta_corriente", True),
    ("Cuenta Corriente", True),
    ("efectivo", False),
    ("transferencia", False),
    ("", False),
    (None, False),
])
def test_es_medio_cuenta_corriente(medio, esperado):
    assert es_medio_cuenta_corriente(medio) is esperado


# ── Un cobro no puede superar lo que falta (ADR-058) ──────────────────────
#
# Acá sin base: el doble de `Escrituras` dice qué hay cobrado. Los mismos casos contra
# el router, la base y los dos motores están en `test_cobro_no_supera_lo_pendiente.py`.


def test_un_cobro_mayor_que_lo_pendiente_se_rechaza_sin_escribir_nada():
    esc = Escrituras(total=4500.0, cobrado=1000.0)

    with pytest.raises(CobroNoPermitido) as exc:
        registrar(FACTURA_CC, [{"medio_id": "efectivo", "monto": 9999.0}], esc=esc)

    assert exc.value.codigo == CobroNoPermitido.EXCEDE
    assert exc.value.pendiente == 3500.0
    assert "$ 3.500,00" in str(exc.value) and "$ 9.999,00" in str(exc.value)
    assert esc.movimientos == [] and esc.cc_pagos == []


def test_lo_pendiente_justo_y_un_parcial_se_aceptan():
    esc, res = registrar(FACTURA_CONTADO, [{"medio_id": "efectivo", "monto": 3500.0}],
                         esc=Escrituras(total=4500.0, cobrado=1000.0))
    assert res["total"] == 3500.0

    esc, res = registrar(FACTURA_CONTADO, [{"medio_id": "efectivo", "monto": 100.0}],
                         esc=Escrituras(total=4500.0, cobrado=1000.0))
    assert res["total"] == 100.0


def test_lo_que_no_puede_superar_lo_pendiente_es_la_suma_de_los_medios():
    """Cada medio cabe solo; juntos no. Y no queda escrito el primero."""
    esc = Escrituras(total=4500.0, cobrado=1000.0)

    with pytest.raises(CobroNoPermitido) as exc:
        registrar(FACTURA_CC, [
            {"medio_id": "efectivo", "monto": 2000.0},
            {"medio_id": "transferencia", "monto": 2000.0},
        ], esc=esc)

    assert exc.value.codigo == CobroNoPermitido.EXCEDE
    assert esc.movimientos == [] and esc.cc_pagos == []


def test_una_factura_ya_cobrada_no_admite_nada_mas():
    esc = Escrituras(total=4500.0, cobrado=4500.0)

    with pytest.raises(CobroNoPermitido) as exc:
        registrar(FACTURA_CONTADO, [{"medio_id": "efectivo", "monto": 1.0}], esc=esc)

    assert exc.value.codigo == CobroNoPermitido.SIN_SALDO
    assert "ya está cobrada" in str(exc.value)
    assert esc.movimientos == []


def test_una_factura_anulada_no_se_cobra():
    esc = Escrituras(anulada=True)

    with pytest.raises(CobroNoPermitido) as exc:
        registrar(FACTURA_CONTADO, [{"medio_id": "efectivo", "monto": 1.0}], esc=esc)

    assert exc.value.codigo == CobroNoPermitido.ANULADA
    assert esc.movimientos == []


@pytest.mark.parametrize("monto,entra", [
    (3500.0, True),
    (3500.004, True),        # menos de medio centavo: es 3.500,00
    (3500.005, False),       # medio centavo redondea para arriba: 3.500,01
    (3500.01, False),
    (0.1 + 0.2 + 3499.7, True),   # 3.500,0000000000005 en float
])
def test_la_tolerancia_es_medio_centavo(monto, entra):
    esc = Escrituras(total=4500.0, cobrado=1000.0)
    if entra:
        registrar(FACTURA_CONTADO, [{"medio_id": "efectivo", "monto": monto}], esc=esc)
        assert len(esc.movimientos) == 1
    else:
        with pytest.raises(CobroNoPermitido):
            registrar(FACTURA_CONTADO, [{"medio_id": "efectivo", "monto": monto}], esc=esc)
        assert esc.movimientos == []


def test_un_cobro_sin_pagos_validos_no_mira_la_base_ni_rechaza():
    """Las filas vacías del formulario siguen siendo un cobro de cero: ni escribe ni protesta."""
    esc = Escrituras(total=4500.0, cobrado=4500.0)

    _, res = registrar(FACTURA_CONTADO, [{"medio_id": "efectivo", "monto": 0}], esc=esc)

    assert res["total"] == 0.0 and esc.lecturas == 0


def test_un_comprobante_que_no_es_factura_no_tiene_tope():
    """Una nota de débito no cobra «lo que falta» de ningún total: esa cuenta no existe en el motor."""
    esc = Escrituras(total=100.0)
    esc.estado["con_tope"] = False

    registrar(FACTURA_CONTADO, [{"medio_id": "efectivo", "monto": 5000.0}], esc=esc)

    assert len(esc.movimientos) == 1


def test_un_comprobante_que_ya_no_existe_se_rechaza():
    esc = Escrituras()
    esc.estado = None

    with pytest.raises(CobroNoPermitido) as exc:
        registrar(FACTURA_CONTADO, [{"medio_id": "efectivo", "monto": 1.0}], esc=esc)

    assert exc.value.codigo == CobroNoPermitido.NO_EXISTE


def test_el_pendiente_no_baja_de_cero_aunque_la_base_ya_tenga_un_cobro_de_mas():
    """Los cobros de más de antes del tope no se muestran como pendiente negativo (ni habilitan otro)."""
    saldo = saldo_de_cobro({"total": 4500.0}, [{"monto": 1000.0}, {"monto": 9999.0}])

    assert saldo["cobrado"] == 10999.0
    assert saldo["pendiente"] == 0.0 and saldo["pendiente_centavos"] == 0


@pytest.mark.parametrize("valor,esperado", [
    (0, 0), (None, 0), (1, 100), (3500.0, 350000), (0.1 + 0.2, 30), (3500.004, 350000),
    (3500.005, 350001), (19.99, 1999), (1e-7, 0), ("12.34", 1234),
])
def test_centavos(valor, esperado):
    assert centavos(valor) == esperado
