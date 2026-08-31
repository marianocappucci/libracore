"""El estado de acreditación de un pago.

Lo que estos tests fijan, en orden de lo que cuesta si se rompe:

 1. 🔴 **Un pago pendiente NO suma.** Es el defecto entero: en Contalibra la
    venta nace "cobrada" y con un movimiento de caja escrito, antes de que
    nadie escanee el QR.
 2. 🔴 **Un pago sin estado revienta.** Los dos defaults posibles mueven plata
    en silencio y en direcciones opuestas.
 3. **`authorized` de MercadoPago es pendiente**, no aprobado: la plata está
    retenida y todavía puede no entrar.
 4. La suma es `Decimal`, no float.
"""

from decimal import Decimal

import pytest

from libracore import pagos
from libracore.pagos import EstadoAcreditacion as Estado


def pago(monto, estado):
    return {"monto": monto, "estado": estado}


class PagoDelOrm:
    """Un pago como lo pasa LibraClub: objeto, no dict."""

    def __init__(self, monto, estado):
        self.monto = monto
        self.estado = estado


# ── Lo que suma y lo que no ──────────────────────────────────────────────────


def test_un_pago_pendiente_no_suma():
    """🔴 El defecto entero, en una línea.

    Con la aritmética de hoy —`sum(p["monto"] for p in pagos)`— esto daría
    1000: la venta quedaría "cobrada" y con un movimiento de caja por plata que
    no entró.
    """
    assert pagos.acreditado([pago("1000", Estado.PENDIENTE)]) == Decimal("0")


def test_un_pago_aprobado_suma():
    """El control positivo. Sin esto, una implementación que devolviera siempre
    cero dejaría el test de arriba en verde."""
    assert pagos.acreditado([pago("1000", Estado.APROBADO)]) == Decimal("1000")


def test_ni_rechazado_ni_vencido_suman():
    assert pagos.acreditado([
        pago("500", Estado.RECHAZADO),
        pago("700", Estado.VENCIDO),
    ]) == Decimal("0")


def test_la_mezcla_suma_solo_lo_acreditado():
    """El caso real: se pagó una parte en efectivo y el resto quedó esperando
    el QR."""
    assert pagos.acreditado([
        pago("300", Estado.APROBADO),
        pago("700", Estado.PENDIENTE),
    ]) == Decimal("300")


def test_lo_pendiente_es_la_contracara():
    assert pagos.pendiente_de_acreditar([
        pago("300", Estado.APROBADO),
        pago("700", Estado.PENDIENTE),
        pago("900", Estado.VENCIDO),
    ]) == Decimal("700")


def test_sin_pagos_no_hay_nada_acreditado():
    assert pagos.acreditado([]) == Decimal("0")
    assert pagos.pendiente_de_acreditar([]) == Decimal("0")


# ── Un pago sin estado revienta ──────────────────────────────────────────────


@pytest.mark.parametrize("sin_estado", [
    {"monto": "1000"},
    {"monto": "1000", "estado": None},
    {"monto": "1000", "estado": ""},
])
def test_un_pago_sin_estado_levanta(sin_estado):
    """🔴 No hay default seguro y por eso no hay default.

    Suponer `aprobado` cuenta plata que no entró —el defecto que esto viene a
    cerrar— y suponer `pendiente` borra plata que sí entró. Que reviente es lo
    único que no miente.
    """
    with pytest.raises(pagos.PagoSinEstado):
        pagos.acreditado([sin_estado])


def test_un_estado_inventado_tambien_levanta():
    """Un typo en el estado no puede leerse como "no acreditado" en silencio:
    sería plata que desaparece del arqueo sin que nadie se entere."""
    with pytest.raises(pagos.PagoSinEstado):
        pagos.acreditado([pago("1000", "aprovado")])


# ── Las dos formas de un pago ────────────────────────────────────────────────


def test_funciona_con_objetos_del_orm_y_no_solo_con_dicts():
    """LibraClub pasa filas de SQLAlchemy; Contalibra y Restolibra, dicts.
    Hacerlos converger para sumar dos números no vale el cambio."""
    assert pagos.acreditado([
        PagoDelOrm(Decimal("250.50"), Estado.APROBADO),
        PagoDelOrm(Decimal("100"), Estado.PENDIENTE),
    ]) == Decimal("250.50")


def test_el_estado_puede_venir_como_texto():
    """Las bases guardan el valor, no el enum."""
    assert pagos.acreditado([pago("1000", "aprobado")]) == Decimal("1000")
    assert pagos.acreditado([pago("1000", "pendiente")]) == Decimal("0")


# ── La plata se suma en Decimal ──────────────────────────────────────────────


def test_la_suma_no_arrastra_el_error_de_los_float():
    """🔑 Contalibra guarda el monto como `REAL`. Sumado como float, tres pagos
    de 0.1 dan 0.30000000000000004 y la comparación contra el total falla por
    un centavo que no existe."""
    total = pagos.acreditado([pago(0.1, Estado.APROBADO) for _ in range(3)])
    assert total == Decimal("0.3")
    assert isinstance(total, Decimal)


# ── El estado crudo de MercadoPago ───────────────────────────────────────────


@pytest.mark.parametrize("crudo,esperado", [
    ("approved", Estado.APROBADO),
    ("rejected", Estado.RECHAZADO),
    ("cancelled", Estado.RECHAZADO),
    ("pending", Estado.PENDIENTE),
    ("in_process", Estado.PENDIENTE),
])
def test_el_mapeo_de_mercadopago_es_el_que_ya_usaba_libraclub(crudo, esperado):
    assert pagos.estado_desde_mercadopago(crudo) is esperado


def test_authorized_es_pendiente_y_no_aprobado():
    """🔑 MercadoPago retuvo el dinero y todavía no lo capturó. Tratarlo como
    aprobado acreditaría plata que aún puede no entrar — y es el estado que más
    se parece a "ya está" sin serlo."""
    assert pagos.estado_desde_mercadopago("authorized") is Estado.PENDIENTE


def test_un_estado_desconocido_de_mercadopago_queda_pendiente():
    """MercadoPago puede agregar estados. El único default que no acredita
    plata de más es dejarlo esperando."""
    assert pagos.estado_desde_mercadopago("algo_nuevo_de_2027") is Estado.PENDIENTE
    assert pagos.estado_desde_mercadopago(None) is Estado.PENDIENTE


def test_el_vocabulario_es_el_mismo_que_el_de_libraclub():
    """🔴 El guard contra la quinta variante del mismo concepto.

    LibraClub tiene `EstadoPago` con estos valores desde antes que el motor. Si
    alguno se renombra acá sin renombrarlo allá, los dos enums dejan de ser el
    mismo vocabulario y vuelve la divergencia que este módulo vino a cerrar.
    """
    assert {e.value for e in Estado} == {
        "pendiente", "aprobado", "rechazado", "vencido"}


def test_solo_aprobado_acredita():
    """El conjunto está para que agregar un estado que acredite sea una línea
    acá y no un `if` en cada producto. Que hoy sea uno solo es la decisión."""
    assert pagos.ACREDITAN == {Estado.APROBADO}
