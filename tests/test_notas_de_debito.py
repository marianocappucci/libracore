"""El núcleo de la nota de débito: UNA implementación para todos los productos (ADR-049).

Como `test_notas_de_credito.py`, se prueba sin HTTP: lo que se decide acá vale para todo producto que emita. Y,
como allá, con un **producto de prueba con otro modelo de datos** (sin tabla `facturas`), que es la prueba de que la
lógica no depende del router del motor.

Lo que distingue a la nota de débito de la de crédito se prueba con su control: va **siempre por un importe**, **no
tiene tope** y **exige que el original tenga CAE**.
"""

import asyncio
import datetime
import threading
from decimal import Decimal

import pytest

from libracore import notas_de_credito as nc
from libracore import notas_de_debito as nd
from libracore.notas_de_credito import NotaNoPermitida

CUIT_BIEN = "30709332852"
CUIT_MAL = "20105800539"      # 11 dígitos, el verificador no cierra
HOY = datetime.date(2026, 10, 13)


def _factura(**cambios):
    f = {
        "tipo": 1, "punto_venta": 1, "numero": 7, "fecha": "2026-09-20", "cae": "75123456789012",
        "cliente_cuit": CUIT_BIEN, "cliente_razon": "Agro Norte", "cliente_iva_cond": 1,
        "subtotal": 1000.0, "iva_amount": 210.0, "total": 1210.0, "concepto": 1,
        "items": [{"description": "Flete", "qty": 1, "unit_price": 1000.0}],
    }
    f.update(cambios)
    return f


def _previa(cae="75123456789099", **cambios):
    n = {"tipo": 2, "punto_venta": 1, "numero": 3, "cae": cae}
    n.update(cambios)
    return n


# ── Qué nota corresponde ────────────────────────────────────────────────────

@pytest.mark.parametrize("factura, nota", [(1, 2), (6, 7), (11, 12), (201, 202), (206, 207), (211, 212)])
def test_cada_factura_tiene_su_nota_de_debito_y_conserva_la_letra_y_la_fce(factura, nota):
    assert nd.tipo_de_nota_de_debito(factura) == nota


@pytest.mark.parametrize("tipo", [2, 7, 12, 202, 3, 8, 13, 203, 999])
def test_una_nota_o_un_tipo_desconocido_no_admite_nota_de_debito(tipo):
    with pytest.raises(NotaNoPermitida) as e:
        nd.tipo_de_nota_de_debito(tipo)
    assert e.value.codigo == NotaNoPermitida.TIPO and "no admite nota de débito" in str(e.value)


# ── Las guardas ─────────────────────────────────────────────────────────────

def test_una_factura_con_cae_admite_nota_de_debito():
    assert nd.validar_nota_de_debito(_factura(), "500.00") == 2


@pytest.mark.parametrize("cae", [None, "", "PENDIENTE"])
def test_un_original_sin_cae_no_admite_nota_porque_arca_no_lo_conoce(cae):
    with pytest.raises(NotaNoPermitida) as e:
        nd.validar_nota_de_debito(_factura(cae=cae), "500.00")
    assert e.value.codigo == NotaNoPermitida.ORIGINAL_SIN_CAE
    assert "no tiene CAE" in str(e.value) and "Factura A 0001-00000007" in str(e.value)


def test_la_nota_de_debito_exige_un_importe_a_diferencia_de_la_de_credito():
    """No hay «nota de débito total»: no copia la factura."""
    with pytest.raises(NotaNoPermitida) as e:
        nd.validar_nota_de_debito(_factura(), None)
    assert e.value.codigo == NotaNoPermitida.IMPORTE and "va por un importe" in str(e.value)


@pytest.mark.parametrize("importe", [0, "0.00", -5, "-1", "10.005", "abc", "nan", "inf", float("nan")])
def test_un_importe_que_no_es_un_monto_se_rechaza(importe):
    with pytest.raises(NotaNoPermitida) as e:
        nd.validar_nota_de_debito(_factura(), importe)
    assert e.value.codigo == NotaNoPermitida.IMPORTE


def test_no_hay_tope_una_nota_de_debito_puede_superar_el_total_de_la_factura():
    """Una de crédito nunca supera lo que queda; una de débito agrega plata (intereses, una diferencia)."""
    assert nd.validar_nota_de_debito(_factura(total=1210.0), "999999.99") == 2


def test_una_factura_admite_varias_notas_de_debito_con_cae():
    previas = [_previa(numero=3), _previa(numero=4)]
    assert nd.validar_nota_de_debito(_factura(), "100.00", previas) == 2


@pytest.mark.parametrize("cae", ["", None, "PENDIENTE"])
def test_una_nota_previa_sin_cae_se_autoriza_o_se_borra_no_se_pide_otra(cae):
    with pytest.raises(NotaNoPermitida) as e:
        nd.validar_nota_de_debito(_factura(), "100.00", [_previa(cae=cae)])
    assert e.value.codigo == NotaNoPermitida.NOTA_SIN_CAE
    assert "autorizala o eliminala" in str(e.value) and "Nota de Débito A 0001-00000003" in str(e.value)


def test_una_clase_a_sin_cuit_se_rechaza_antes_de_ir_a_arca():
    with pytest.raises(NotaNoPermitida) as e:
        nd.validar_nota_de_debito(_factura(cliente_cuit=""), "100.00")
    assert e.value.codigo == NotaNoPermitida.RECEPTOR and "CUIT de 11 dígitos" in str(e.value)


def test_una_nota_a_un_cuit_que_no_cierra_se_deja_corregir():
    """Hereda el receptor de una factura que ARCA ya autorizó: bloquearla la dejaría sin salida."""
    assert nd.validar_nota_de_debito(_factura(cliente_cuit=CUIT_MAL), "100.00") == 2


def test_una_clase_b_a_un_consumidor_final_sin_cuit_sale_bien():
    assert nd.validar_nota_de_debito(_factura(tipo=6, cliente_cuit=""), "100.00") == 7


def test_la_nota_de_credito_sigue_igual_con_el_importe_extraido():
    """`importe_valido` salió de `validar_nota_de_credito`: mismos rechazos, mismo código."""
    for malo in ("0", "-1", "10.005", "abc", "nan"):
        with pytest.raises(NotaNoPermitida) as e:
            nc.validar_nota_de_credito(_factura(tipo=11), [], malo)
        assert e.value.codigo == NotaNoPermitida.IMPORTE


# ── El armado ───────────────────────────────────────────────────────────────

def test_la_nota_reparte_el_importe_con_la_alicuota_de_la_factura_sin_perder_un_centavo():
    nota = nd.armar_nota_de_debito(_factura(), 2, importe=Decimal("605.00"), hoy=HOY)
    assert (nota["subtotal"], nota["iva_amount"], nota["total"]) == (500.0, 105.0, 605.0)
    nota = nd.armar_nota_de_debito(_factura(), 2, importe=Decimal("100.01"), hoy=HOY)
    assert round(nota["subtotal"] + nota["iva_amount"], 2) == nota["total"] == 100.01


def test_una_clase_c_no_discrimina_iva():
    nota = nd.armar_nota_de_debito(
        _factura(tipo=11, subtotal=1210.0, iva_amount=0.0), 12, importe=Decimal("300.00"), hoy=HOY)
    assert (nota["subtotal"], nota["iva_amount"], nota["total"]) == (300.0, 0.0, 300.0)


def test_un_importe_igual_al_total_va_por_el_importe_y_no_copia_la_factura():
    """Con la de crédito, igual al total es la nota total (copia ítems). Con la de débito, nunca copia."""
    nota = nd.armar_nota_de_debito(_factura(), 2, importe=Decimal("1210.00"), motivo="Repite el cobro", hoy=HOY)
    assert nota["total"] == 1210.0
    assert nota["items"] == [{"description": "Débito s/ Factura A 0001-00000007", "qty": 1,
                              "unit_price": 1000.0, "subtotal": 1000.0}]
    assert "Flete" not in str(nota["items"])


def test_la_nota_lleva_leyenda_motivo_asociado_y_la_fecha_de_hoy():
    nota = nd.armar_nota_de_debito(
        _factura(), 2, importe=Decimal("121.00"), motivo="Intereses por mora", hoy=HOY)
    assert nota["observaciones"] == "Débito s/ Factura A 0001-00000007 — Intereses por mora"
    assert nota["fecha"] == "2026-10-13", "la fecha de hoy, no la del original"
    assert (nota["tipo"], nota["punto_venta"]) == (2, 1)
    assert (nota["cbte_asoc_tipo"], nota["cbte_asoc_pv"], nota["cbte_asoc_nro"], nota["cbte_asoc_fecha"]) == (
        1, 1, 7, "2026-09-20")
    assert nota["fce_anulacion"] == ""


@pytest.mark.parametrize("tipo_original, tipo_nota", [(201, 202), (206, 207), (211, 212)])
def test_la_nota_de_debito_de_una_fce_dice_que_no_anula(tipo_original, tipo_nota):
    nota = nd.armar_nota_de_debito(_factura(tipo=tipo_original), tipo_nota, importe=Decimal("100.00"), hoy=HOY)
    assert nota["fce_anulacion"] == "N"


def test_armar_nota_de_credito_no_cambia_sin_los_parametros_nuevos():
    """`leyenda` y `por_importe` son aditivos: sin ellos, la nota de crédito sale como siempre."""
    total = nc.armar_nota(_factura(tipo=11), 13, hoy=HOY)
    assert total["observaciones"] == "Anula Factura C 0001-00000007" and total["items"] == _factura()["items"]
    parcial = nc.armar_nota(_factura(tipo=11), 13, hoy=HOY, importe="500.00")
    assert parcial["observaciones"] == "Acredita 500.00 de Factura C 0001-00000007"
    igual = nc.armar_nota(_factura(tipo=11), 13, hoy=HOY, importe="1210.00")
    assert igual["items"] == _factura()["items"], "igual al total sigue siendo la nota total"


# ── El candado ──────────────────────────────────────────────────────────────

def test_el_rechazo_del_candado_dice_que_es_de_debito():
    with nc.una_nota_a_la_vez("k1", clase="débito"):
        with pytest.raises(NotaNoPermitida) as e, nc.una_nota_a_la_vez("k1", clase="débito"):
            pass
    assert e.value.codigo == NotaNoPermitida.EN_CURSO and "nota de débito en curso" in str(e.value)
    with nc.una_nota_a_la_vez("k1"):
        with pytest.raises(NotaNoPermitida) as e, nc.una_nota_a_la_vez("k1"):
            pass
    assert "nota de crédito en curso" in str(e.value), "sin `clase` el texto de siempre"


# ── El orden de las operaciones, con un producto que NO es el router del motor ──

class ProductoDePrueba:
    def __init__(self, notas_existentes=(), falla_en=None):
        self.notas = list(notas_existentes)
        self.llamadas = []
        self.falla_en = falla_en

    def cargar_previas(self):
        self.llamadas.append("cargar_previas")
        return list(self.notas)

    def numerar(self, tipo_nota, punto_venta):
        self.llamadas.append(f"numerar({tipo_nota},{punto_venta})")
        return 100 + len(self.notas), {"ticket": "TKN"}

    async def registrar(self, nota, contexto):
        self.llamadas.append("registrar")
        assert contexto == {"ticket": "TKN"}
        registro = {"id": f"nd-{nota['numero']}", **nota, "cae": None}
        self.notas.append(registro)
        return registro["id"]

    def pedir_cae(self, registro, nota, contexto):
        self.llamadas.append("pedir_cae")
        if self.falla_en == "cae":
            raise RuntimeError("WSFE rechazó el comprobante")
        for n in self.notas:
            if n["id"] == registro:
                n["cae"] = "75123456789012"
        return {"cae": "75123456789012"}


def _emitir(prod, original=None, **extra):
    original = original or {k: v for k, v in _factura().items() if k != "items"}
    return asyncio.run(nd.emitir_nota_de_debito(
        original, clave=("prod-nd", 1), importe=extra.pop("importe", "500.00"),
        cargar_previas=prod.cargar_previas, numerar=prod.numerar, registrar=prod.registrar,
        pedir_cae=prod.pedir_cae, hoy=HOY, **extra))


def test_el_orden_es_siempre_previas_numerar_registrar_pedir_cae():
    prod = ProductoDePrueba()
    emitida = _emitir(prod, motivo="Intereses")
    assert prod.llamadas == ["cargar_previas", "numerar(2,1)", "registrar", "pedir_cae"]
    assert emitida.nota["numero"] == 100 and emitida.registro == "nd-100"
    assert emitida.nota["total"] == 500.0 and emitida.nota["observaciones"].endswith("Intereses")
    assert emitida.resultado == {"cae": "75123456789012"}


def test_se_pueden_emitir_varias_notas_seguidas_a_la_misma_factura():
    prod = ProductoDePrueba()
    _emitir(prod)
    prod.llamadas.clear()
    # La segunda: la previa ya tiene CAE (la completó `pedir_cae`), no frena.
    prod.notas[0]["numero"] = 100
    segunda = asyncio.run(nd.emitir_nota_de_debito(
        _factura(), clave=("prod-nd", 1), importe="10.00", cargar_previas=prod.cargar_previas,
        numerar=lambda t, pv: (101, {"ticket": "TKN"}), registrar=lambda n, c: "nd-101",
        pedir_cae=lambda r, n, c: {"cae": "75"}, hoy=HOY))
    assert segunda.registro == "nd-101"


def test_si_la_nota_no_corresponde_no_se_le_pide_nada_a_arca():
    prod = ProductoDePrueba()
    with pytest.raises(NotaNoPermitida):
        _emitir(prod, original={**_factura(cae=None)})
    assert prod.llamadas == ["cargar_previas"], "ni numerar, ni registrar, ni pedir el CAE"


def test_una_previa_sin_cae_frena_antes_de_numerar():
    prod = ProductoDePrueba(notas_existentes=[{"id": "x", "tipo": 2, "punto_venta": 1, "numero": 3, "cae": None}])
    with pytest.raises(NotaNoPermitida) as e:
        _emitir(prod)
    assert e.value.codigo == NotaNoPermitida.NOTA_SIN_CAE
    assert prod.llamadas == ["cargar_previas"]


def test_las_previas_se_leen_adentro_del_candado():
    visto = []

    def cargar():
        visto.append(nc.en_curso(("prod-nd", 1)))
        return []

    prod = ProductoDePrueba()
    asyncio.run(nd.emitir_nota_de_debito(
        _factura(), clave=("prod-nd", 1), importe="500.00", cargar_previas=cargar, numerar=prod.numerar,
        registrar=prod.registrar, pedir_cae=prod.pedir_cae, hoy=HOY))
    assert visto == [True]


def test_las_previas_son_opcionales():
    prod = ProductoDePrueba()
    emitida = asyncio.run(nd.emitir_nota_de_debito(
        _factura(), clave="k", importe="500.00", numerar=prod.numerar,
        registrar=prod.registrar, pedir_cae=prod.pedir_cae, hoy=HOY))
    assert emitida.registro == "nd-100"
    assert "cargar_previas" not in prod.llamadas


def test_si_arca_rechaza_la_excepcion_sube_y_el_candado_se_libera():
    prod = ProductoDePrueba(falla_en="cae")
    with pytest.raises(RuntimeError, match="WSFE rechazó"):
        _emitir(prod)
    assert not nc.en_curso(("prod-nd", 1))


def test_dos_pedidos_simultaneos_emiten_una_sola_nota():
    prod = ProductoDePrueba()
    adentro = threading.Event()
    original = {k: v for k, v in _factura().items() if k != "items"}

    async def numerar_lento(tipo_nota, punto_venta):
        adentro.set()
        await asyncio.sleep(0.3)
        return 100, {"ticket": "TKN"}

    resultados = []

    def pedir():
        try:
            resultados.append(asyncio.run(nd.emitir_nota_de_debito(
                original, clave=("prod-nd", 1), importe="500.00", numerar=numerar_lento,
                registrar=prod.registrar, pedir_cae=prod.pedir_cae, hoy=HOY)))
        except NotaNoPermitida as e:
            resultados.append(e)

    primero = threading.Thread(target=pedir)
    primero.start()
    assert adentro.wait(5)
    segundo = threading.Thread(target=pedir)
    segundo.start()
    primero.join()
    segundo.join()

    emitidas = [r for r in resultados if isinstance(r, nc.NotaEmitida)]
    rechazos = [r for r in resultados if isinstance(r, NotaNoPermitida)]
    assert len(emitidas) == 1 and len(rechazos) == 1 and rechazos[0].codigo == NotaNoPermitida.EN_CURSO
    assert "débito" in str(rechazos[0])
    assert len(prod.notas) == 1


def test_con_postgres_dos_procesos_no_emiten_la_misma_nota_de_debito(bases):
    """Lo que la guarda en memoria no ve: el otro pedido llega a **otro proceso** (el candado de la base)."""
    from libracore.db import core

    url = bases.nueva()
    primero, segundo = core.conectar(url), core.conectar(url)
    prod = ProductoDePrueba()
    try:
        with nc.una_nota_a_la_vez(("prod-nd", 1), primero, clase="débito"):
            nc._EN_CURSO.clear()                      # «el otro worker»: no tiene nada en memoria
            with pytest.raises(NotaNoPermitida) as e:
                asyncio.run(nd.emitir_nota_de_debito(
                    _factura(), clave=("prod-nd", 1), importe="500.00", numerar=prod.numerar,
                    registrar=prod.registrar, pedir_cae=prod.pedir_cae, hoy=HOY, conn=segundo))
            assert e.value.codigo == NotaNoPermitida.EN_CURSO
        assert prod.llamadas == [], "al otro proceso no se le pidió nada a ARCA"
        primero.commit()
        asyncio.run(nd.emitir_nota_de_debito(
            _factura(), clave=("prod-nd", 1), importe="500.00", numerar=prod.numerar,
            registrar=prod.registrar, pedir_cae=prod.pedir_cae, hoy=HOY, conn=segundo))
        assert len(prod.notas) == 1
    finally:
        primero.close()
        segundo.close()


def test_una_nota_de_credito_en_camino_no_frena_a_la_de_debito_del_mismo_comprobante():
    """Cada una con su `clave`: el producto elige claves distintas (ver `emitir_nota_de_debito`)."""
    prod = ProductoDePrueba()
    with nc.una_nota_a_la_vez(("prod", 1)):
        emitida = asyncio.run(nd.emitir_nota_de_debito(
            _factura(), clave=("prod-nd", 1), importe="500.00", numerar=prod.numerar,
            registrar=prod.registrar, pedir_cae=prod.pedir_cae, hoy=HOY))
    assert emitida.registro == "nd-100"
