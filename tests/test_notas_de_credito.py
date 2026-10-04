"""El núcleo de la nota de crédito: UNA implementación para todos los productos.

Se prueba sin base de datos ni HTTP, que es el punto: lo que se decide acá vale para los siete productos que
emiten por ARCA. El router de facturas del motor es **un consumidor** (sus tests están en
`test_facturas_router.py`); acá se prueba también un **segundo consumidor con otro modelo de datos**, que es la
prueba de que la lógica no depende de la tabla `facturas`.
"""

import asyncio
import datetime
import threading

import pytest

from libracore import notas_de_credito as nc
from libracore.notas_de_credito import NotaNoPermitida

CUIT_BIEN = "30709332852"
CUIT_MAL = "20105800539"      # 11 dígitos, el verificador no cierra
HOY = datetime.date(2026, 10, 4)


def _factura(**cambios):
    f = {
        "tipo": 11, "punto_venta": 1, "numero": 7, "fecha": "2026-09-20",
        "cliente_cuit": CUIT_BIEN, "cliente_razon": "Agro Norte", "cliente_iva_cond": 1,
        "subtotal": 1000.0, "iva_amount": 210.0, "total": 1210.0, "concepto": 1,
        "items": [{"description": "Flete", "qty": 1, "unit_price": 1000.0}],
    }
    f.update(cambios)
    return f


def _nota_previa(cae="75123456789012", **cambios):
    n = {"tipo": 13, "punto_venta": 1, "numero": 3, "cae": cae}
    n.update(cambios)
    return n


# ── Qué nota corresponde ────────────────────────────────────────────────────

@pytest.mark.parametrize("factura, nota", [(1, 3), (6, 8), (11, 13), (201, 203), (206, 208), (211, 213)])
def test_cada_factura_tiene_su_nota_y_conserva_la_letra_y_la_fce(factura, nota):
    assert nc.tipo_de_nota_de_credito(factura) == nota


@pytest.mark.parametrize("tipo", [3, 8, 13, 203, 2, 7, 202, 999])
def test_una_nota_o_un_tipo_desconocido_no_se_acredita(tipo):
    with pytest.raises(NotaNoPermitida) as e:
        nc.tipo_de_nota_de_credito(tipo)
    assert e.value.codigo == NotaNoPermitida.TIPO and "no admite nota de crédito" in str(e.value)


def test_el_nombre_y_la_etiqueta_se_leen_como_en_un_papel():
    assert nc.nombre_de_tipo(203) == "Nota de Crédito FCE A"
    assert nc.etiqueta({"tipo": 11, "punto_venta": 1, "numero": 123}) == "Factura C 0001-00000123"


# ── Las guardas ─────────────────────────────────────────────────────────────

def test_una_factura_sin_notas_se_puede_acreditar():
    assert nc.validar_nota_de_credito(_factura(), []) == 13


def test_una_factura_ya_acreditada_dice_cual_es_la_nota():
    with pytest.raises(NotaNoPermitida) as e:
        nc.validar_nota_de_credito(_factura(), [_nota_previa()])
    assert e.value.codigo == NotaNoPermitida.YA_TIENE_NOTA
    assert "Nota de Crédito C 0001-00000003" in str(e.value) and "una sola vez" in str(e.value)


@pytest.mark.parametrize("cae", ["", None, "PENDIENTE"])
def test_una_nota_sin_cae_se_autoriza_o_se_borra_no_se_pide_otra(cae):
    with pytest.raises(NotaNoPermitida) as e:
        nc.validar_nota_de_credito(_factura(), [_nota_previa(cae=cae)])
    assert e.value.codigo == NotaNoPermitida.NOTA_SIN_CAE
    assert "autorizala o eliminala" in str(e.value)


def test_una_clase_a_sin_cuit_se_rechaza_antes_de_ir_a_arca():
    with pytest.raises(NotaNoPermitida) as e:
        nc.validar_nota_de_credito(_factura(tipo=1, cliente_cuit="1"), [])
    assert e.value.codigo == NotaNoPermitida.RECEPTOR and "Agro Norte" in str(e.value)


def test_una_nota_a_un_cuit_que_no_cierra_se_deja_corregir():
    """Medido en homologación el 2026-10-04: ARCA autoriza la factura A a un CUIT inexistente (con aviso) y
    también la nota que la corrige. La guarda no puede impedir la corrección."""
    assert nc.validar_nota_de_credito(_factura(tipo=1, cliente_cuit=CUIT_MAL), []) == 3


# ── El armado ───────────────────────────────────────────────────────────────

def test_la_nota_copia_los_importes_y_los_items_del_original():
    nota = nc.armar_nota(_factura(), 13, hoy=HOY)
    assert (nota["subtotal"], nota["iva_amount"], nota["total"]) == (1000.0, 210.0, 1210.0)
    assert nota["items"] == _factura()["items"]
    assert nota["cliente_cuit"] == CUIT_BIEN and nota["cliente_razon"] == "Agro Norte"


def test_un_producto_sin_items_no_recibe_una_clave_items():
    original = _factura()
    del original["items"]
    assert "items" not in nc.armar_nota(original, 13, hoy=HOY)


def test_la_fecha_de_la_nota_es_la_de_hoy_y_no_la_del_original():
    nota = nc.armar_nota(_factura(fecha="2026-01-02"), 13, hoy=HOY)
    assert nota["fecha"] == "2026-10-04" and nota["fch_vto_pago"] == "2026-10-04"
    assert nc.armar_nota(_factura(), 13)["fecha"] == datetime.date.today().isoformat()


def test_la_nota_lleva_el_comprobante_asociado_que_la_ata_a_su_factura():
    nota = nc.armar_nota(_factura(punto_venta=5, numero=42, fecha="2026-09-20"), 13, hoy=HOY)
    assert (nota["cbte_asoc_tipo"], nota["cbte_asoc_pv"], nota["cbte_asoc_nro"], nota["cbte_asoc_fecha"]) == (
        11, 5, 42, "2026-09-20")
    assert nota["punto_venta"] == 5 and "numero" not in nota   # el número lo da `numerar`


@pytest.mark.parametrize("tipo_original, marca", [(11, ""), (1, ""), (201, "N"), (206, "N"), (211, "N")])
def test_la_nota_de_una_fce_dice_que_no_anula_porque_eso_lo_decide_el_comprador(tipo_original, marca):
    """`S` sólo lo acepta ARCA si el comprador rechazó la factura (10154)."""
    nota = nc.armar_nota(_factura(tipo=tipo_original), nc.tipo_de_nota_de_credito(tipo_original), hoy=HOY)
    assert nota["fce_anulacion"] == marca


def test_la_observacion_nombra_la_factura_y_el_motivo_si_lo_hay():
    assert nc.armar_nota(_factura(), 13, hoy=HOY)["observaciones"] == "Anula Factura C 0001-00000007"
    assert nc.armar_nota(_factura(), 13, hoy=HOY, motivo="kilos mal cargados")["observaciones"] == (
        "Anula Factura C 0001-00000007 — kilos mal cargados")


# ── El candado ──────────────────────────────────────────────────────────────

def test_el_candado_se_libera_aunque_falle_lo_de_adentro():
    with pytest.raises(RuntimeError), nc.una_nota_a_la_vez("k1"):
        assert nc.en_curso("k1")
        raise RuntimeError("ARCA no contesta")
    assert not nc.en_curso("k1")
    with nc.una_nota_a_la_vez("k1"):
        pass


def test_el_candado_es_por_comprobante():
    with nc.una_nota_a_la_vez("k1"), nc.una_nota_a_la_vez("k2"):
        with pytest.raises(NotaNoPermitida) as e, nc.una_nota_a_la_vez("k1"):
            pass
        assert e.value.codigo == NotaNoPermitida.EN_CURSO


# ── El orden de las operaciones, con un producto que NO es el router del motor ──

class ProductoDePrueba:
    """Un producto con su propio modelo: sin tabla `facturas`, sin ítems y con ids de texto."""

    def __init__(self, notas_existentes=(), falla_en=None):
        self.notas = list(notas_existentes)
        self.llamadas = []
        self.falla_en = falla_en

    def cargar_previas(self):
        self.llamadas.append("cargar_previas")
        return [n for n in self.notas]

    def numerar(self, tipo_nota, punto_venta):
        self.llamadas.append(f"numerar({tipo_nota},{punto_venta})")
        return 100 + len(self.notas), {"ticket": "TKN"}

    async def registrar(self, nota, contexto):                        # una corrutina: también vale
        self.llamadas.append("registrar")
        assert contexto == {"ticket": "TKN"} and nota["numero"] == 100
        registro = {"id": f"nota-{nota['numero']}", **nota, "cae": None}
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
    return asyncio.run(nc.emitir_nota_de_credito(
        original, clave=("prod", 1), cargar_previas=prod.cargar_previas, numerar=prod.numerar,
        registrar=prod.registrar, pedir_cae=prod.pedir_cae, hoy=HOY, **extra))


def test_el_orden_es_siempre_previas_numerar_registrar_pedir_cae():
    prod = ProductoDePrueba()
    emitida = _emitir(prod)
    assert prod.llamadas == ["cargar_previas", "numerar(13,1)", "registrar", "pedir_cae"]
    assert emitida.nota["numero"] == 100 and emitida.registro == "nota-100"
    assert emitida.resultado == {"cae": "75123456789012"}
    assert prod.notas[0]["cae"] == "75123456789012", "el producto cierra lo suyo con lo que le devuelven"


def test_un_producto_distinto_obtiene_las_mismas_guardas_sin_escribirlas():
    """La prueba de la normalización: otro modelo de datos, la misma regla."""
    prod = ProductoDePrueba()
    _emitir(prod)
    with pytest.raises(NotaNoPermitida) as e:
        _emitir(prod)
    assert e.value.codigo == NotaNoPermitida.YA_TIENE_NOTA


def test_si_la_nota_no_corresponde_no_se_le_pide_nada_a_arca():
    prod = ProductoDePrueba(notas_existentes=[{"id": "x", "tipo": 13, "punto_venta": 1, "numero": 3, "cae": "75"}])
    with pytest.raises(NotaNoPermitida):
        _emitir(prod)
    assert prod.llamadas == ["cargar_previas"], "ni numerar, ni registrar, ni pedir el CAE"


def test_las_previas_se_leen_adentro_del_candado():
    """Si el producto las leyera antes, dos pedidos simultáneos las contestarían vacías los dos."""
    visto = []

    def cargar():
        visto.append(nc.en_curso(("prod", 1)))
        return []

    prod = ProductoDePrueba()
    asyncio.run(nc.emitir_nota_de_credito(
        _factura(), clave=("prod", 1), cargar_previas=cargar, numerar=prod.numerar,
        registrar=prod.registrar, pedir_cae=prod.pedir_cae, hoy=HOY))
    assert visto == [True]


def test_si_arca_rechaza_la_excepcion_sube_y_el_candado_se_libera():
    prod = ProductoDePrueba(falla_en="cae")
    with pytest.raises(RuntimeError, match="WSFE rechazó"):
        _emitir(prod)
    assert not nc.en_curso(("prod", 1))


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
            resultados.append(asyncio.run(nc.emitir_nota_de_credito(
                original, clave=("prod", 1), cargar_previas=prod.cargar_previas, numerar=numerar_lento,
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
    assert len(prod.notas) == 1


def test_funciona_igual_con_costuras_comunes_o_corrutinas():
    prod = ProductoDePrueba()

    async def numerar(tipo_nota, punto_venta):
        return 100, {"ticket": "TKN"}

    emitida = asyncio.run(nc.emitir_nota_de_credito(
        _factura(), clave="k", cargar_previas=lambda: [], numerar=numerar,
        registrar=prod.registrar, pedir_cae=prod.pedir_cae, hoy=HOY))
    assert emitida.registro == "nota-100"


# ── La nota PARCIAL y el tope acumulado (fase 2, ADR-018) ───────────────────

from decimal import Decimal  # noqa: E402

D = Decimal


def _con_total(total, **cambios):
    """Una nota previa que informa su `total` (para que sume en el tope)."""
    return _nota_previa(total=total, **cambios)


def _factura_a(**cambios):
    """Factura A de 1210.00 (1000 + 210 de IVA al 21 %)."""
    return _factura(tipo=1, **cambios)


def _codigo(importe, previas=(), original=None):
    with pytest.raises(NotaNoPermitida) as e:
        nc.validar_nota_de_credito(original or _factura_a(), previas, importe)
    return e.value.codigo


def test_el_neto_sale_del_importe_y_el_iva_es_la_resta_sin_perder_un_centavo():
    assert nc.repartir_importe(_factura_a(), "121.00") == (D("100.00"), D("21.00"))
    neto, iva = nc.repartir_importe(_factura_a(), "100.00")
    assert (neto, iva) == (D("82.64"), D("17.36")) and neto + iva == D("100.00")
    # En muchos importes seguidos la suma nunca se desvía: es la garantía de la resta.
    for centavos in range(1, 2500, 7):
        importe = D(centavos) / 100
        n, i = nc.repartir_importe(_factura_a(), importe)
        assert n + i == importe.quantize(D("0.01")) and n >= 0 and i >= 0


def test_una_factura_c_no_discrimina_iva_en_la_nota_parcial():
    c = _factura(tipo=11, subtotal=1210.0, iva_amount=0.0, total=1210.0)
    assert nc.repartir_importe(c, "400.50") == (D("400.50"), D("0.00"))
    # Y una alícuota cero (exenta) tampoco.
    assert nc.repartir_importe(_factura_a(subtotal=1000.0, iva_amount=0.0, total=1000.0), "10.00") == (D("10.00"), D("0.00"))


def test_una_nota_parcial_se_admite_hasta_el_total_de_la_factura():
    assert nc.validar_nota_de_credito(_factura_a(), [], "100.00") == 3
    assert nc.validar_nota_de_credito(_factura_a(), [], D("1210.00")) == 3, "por el total entero también"


def test_el_tope_es_acumulado_las_notas_suman():
    previas = [_con_total(400.0), _con_total(300.0, numero=4)]                 # 700 acreditados, quedan 510
    assert nc.acreditado(_factura_a(), previas) == D("700.00")
    assert nc.saldo_acreditable(_factura_a(), previas) == D("510.00")
    assert nc.validar_nota_de_credito(_factura_a(), previas, "510.00") == 3     # justo el saldo
    assert _codigo("510.01", previas) == NotaNoPermitida.SUPERA_SALDO           # un centavo de más


def test_una_nota_parcial_no_puede_pasarse_del_total_aunque_no_haya_previas():
    assert _codigo("1210.01") == NotaNoPermitida.SUPERA_SALDO


def test_con_el_saldo_en_cero_ninguna_nota_nueva_se_admite():
    previas = [_con_total(1210.0)]
    assert _codigo("0.01", previas) == NotaNoPermitida.SUPERA_SALDO
    assert nc.saldo_acreditable(_factura_a(), previas) == D("0.00")


@pytest.mark.parametrize("importe", ["0", "-5.00", "10.005", "NaN", "Infinity", "abc"])
def test_un_importe_que_no_es_un_monto_se_rechaza(importe):
    assert _codigo(importe) == NotaNoPermitida.IMPORTE


def test_una_nota_previa_sin_cae_frena_tambien_a_la_parcial():
    for cae in (None, "", "PENDIENTE"):
        assert _codigo("10.00", [_con_total(100.0, cae=cae)]) == NotaNoPermitida.NOTA_SIN_CAE


def test_la_nota_sin_cae_no_suma_en_lo_acreditado():
    assert nc.acreditado(_factura_a(), [_con_total(400.0, cae=None)]) == D("0.00")


def test_la_nota_total_se_niega_si_ya_hay_notas_parciales_y_dice_el_saldo():
    with pytest.raises(NotaNoPermitida) as e:
        nc.validar_nota_de_credito(_factura_a(), [_con_total(400.0)])
    assert e.value.codigo == NotaNoPermitida.YA_TIENE_NOTA
    assert "810.00" in str(e.value), "dice cuánto queda para pedir una nota por ese saldo"


def test_una_previa_que_no_informa_el_total_cuenta_como_la_factura_entera():
    """Un producto de la fase 1 (que no manda `total`): el lado seguro es que bloquee, como siempre."""
    assert nc.acreditado(_factura_a(), [_nota_previa()]) == D("1210.00")
    assert _codigo("10.00", [_nota_previa()]) == NotaNoPermitida.SUPERA_SALDO


def test_la_nota_de_una_fce_tiene_que_ser_por_menos_que_el_saldo():
    """Medido (2026-10-03): una nota de FCE sin anulación sólo puede acreditar menos que el saldo (10184)."""
    fce = _factura(tipo=201, fch_vto_pago="2026-10-20")
    assert nc.validar_nota_de_credito(fce, [], "1209.99") == 203
    assert _codigo("1210.00", original=fce) == NotaNoPermitida.IMPORTE
    assert _codigo("810.00", [_con_total(400.0)], original=fce) == NotaNoPermitida.IMPORTE


def test_la_nota_parcial_lleva_su_importe_su_iva_y_un_solo_item():
    nota = nc.armar_nota(_factura_a(), 3, hoy=HOY, importe="100.00", motivo="Diferencia de kilos")
    assert (nota["subtotal"], nota["iva_amount"], nota["total"]) == (82.64, 17.36, 100.0)
    assert nota["items"] == [{"description": "Acredita 100.00 de Factura A 0001-00000007", "qty": 1,
                              "unit_price": 82.64, "subtotal": 82.64}], "los ítems del original no se copian"
    assert nota["observaciones"] == "Acredita 100.00 de Factura A 0001-00000007 — Diferencia de kilos"
    # Sigue siendo una nota de ESTA factura, de hoy.
    assert (nota["cbte_asoc_tipo"], nota["cbte_asoc_nro"], nota["fecha"]) == (1, 7, "2026-10-04")


def test_un_importe_igual_al_total_es_una_nota_total_con_los_items_del_original():
    nota = nc.armar_nota(_factura_a(), 3, hoy=HOY, importe="1210.00")
    assert nota["total"] == 1210.0 and nota["items"] == _factura()["items"]
    assert nota["observaciones"].startswith("Anula Factura A")


def test_sin_importe_la_nota_total_no_cambia():
    assert nc.armar_nota(_factura_a(), 3, hoy=HOY) == nc.armar_nota(_factura_a(), 3, hoy=HOY, importe=None)


class _ProductoVarias(ProductoDePrueba):
    """Como `ProductoDePrueba`, pero admite varias notas: el número sigue a las que ya hay."""

    async def registrar(self, nota, contexto):
        self.llamadas.append("registrar")
        registro = {"id": f"nota-{nota['numero']}", **nota, "cae": None}
        self.notas.append(registro)
        return registro["id"]


def test_una_factura_se_acredita_en_varias_notas_hasta_su_total_y_ni_un_centavo_mas():
    prod = _ProductoVarias()
    original = {k: v for k, v in _factura_a().items() if k != "items"}
    for importe in ("400.00", "300.00"):
        _emitir(prod, original, importe=importe)
    assert [n["total"] for n in prod.notas] == [400.0, 300.0]
    assert nc.saldo_acreditable(original, prod.notas) == D("510.00")

    with pytest.raises(NotaNoPermitida) as e:                                  # 510.01: el tope acumulado
        _emitir(prod, original, importe="510.01")
    assert e.value.codigo == NotaNoPermitida.SUPERA_SALDO and len(prod.notas) == 2, "no se le pidió nada a ARCA"

    _emitir(prod, original, importe="510.00")                                  # completa la factura
    assert nc.saldo_acreditable(original, prod.notas) == D("0.00")
    with pytest.raises(NotaNoPermitida):
        _emitir(prod, original, importe="0.01")


def test_la_nota_total_sigue_siendo_una_sola():
    prod = _ProductoVarias()
    original = {k: v for k, v in _factura_a().items() if k != "items"}
    _emitir(prod, original)
    with pytest.raises(NotaNoPermitida) as e:
        _emitir(prod, original)
    assert e.value.codigo == NotaNoPermitida.YA_TIENE_NOTA


def test_la_marca_de_cuenta_corriente_es_por_nota_y_la_vieja_sigue_valiendo():
    assert nc.referencia_cc_de_nota(9) == "nc:factura:9"
    assert nc.referencia_cc_de_nota(9, 31) == "nc:factura:9:31"
