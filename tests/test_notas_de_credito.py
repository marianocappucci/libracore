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
