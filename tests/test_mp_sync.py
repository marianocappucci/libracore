"""El cron nocturno de MercadoPago — el camino que emite la mayoría de las
facturas y corre sin nadie mirando.

El test que manda es el del alias: **este es el camino que se quedó afuera**
cuando los alias se agregaron el 2026-07-13, y por eso facturó RIPEHO y VISCO al
CUIT equivocado tres semanas después.
"""

import asyncio
import importlib

import pytest

from libracore.db import clients as db_clients
from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db import mp as db_mp
from libracore.db.schema import init_core_schema

MI_USER_ID = "555"
MI_EMAIL = "comercio@miempresa.test"
EMAIL_PAGADOR = "contador@estudio.test"
CUIT_PAGADOR = "20111111112"


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENV", "development")
    import libracore.config_manager as cm
    importlib.reload(cm)

    core.configure(db_path=str(tmp_path / "mp_sync_test.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    cm.save({"mp_access_token": "TOKEN", "empresa_iva_condition": "Monotributista"})
    yield cm
    conn.close()
    core._db_path = None


@pytest.fixture
def sync(entorno):
    """Devuelve el módulo con `mp_api` ya mockeado; se le cargan movimientos."""
    import libracore.mp_sync as ms
    importlib.reload(ms)

    async def usuario_info(token):
        return {"id": MI_USER_ID, "email": MI_EMAIL}

    ms.mp_api.obtener_usuario_info = usuario_info

    def cargar(movimientos):
        async def obtener_movimientos(token, desde, hasta):
            return movimientos
        ms.mp_api.obtener_movimientos = obtener_movimientos

    ms.cargar = cargar
    cargar([])
    return ms


def _cobro(**over):
    mov = {
        "id": "800001",
        "collector_id": MI_USER_ID,
        "transaction_amount": 12000.0,
        "external_reference": "",
        "description": "Abono mensual",
        "payment_type_id": "bank_transfer",
        "payment_method_id": "cvu",
        "date_approved": "2026-08-20T10:00:00.000-03:00",
        "payer": {
            "email": EMAIL_PAGADOR, "first_name": "Estudio", "last_name": "Contable",
            "identification": {"type": "CUIT", "number": CUIT_PAGADOR},
        },
    }
    mov.update(over)
    return mov


def _correr(sync, **kw):
    return asyncio.run(sync.sincronizar_y_facturar(**kw))


# ── Lo que no puede tumbar el cron ───────────────────────────────────────────

def test_sin_token_devuelve_el_motivo_y_no_explota(sync, entorno):
    """El cron corre desatendido: una excepción sin manejar acá es un job que
    falla en silencio todas las noches."""
    entorno.save({**entorno.load(), "mp_access_token": ""})
    assert _correr(sync) == {"error": "sin_token"}


def test_si_mercadopago_no_contesta_devuelve_el_motivo(sync):
    async def explota(token, desde, hasta):
        raise RuntimeError("timeout contra MP")

    sync.mp_api.obtener_movimientos = explota
    resultado = _correr(sync)
    assert "timeout" in resultado["error"]


# ── El alias, que es la razón de este módulo ─────────────────────────────────

def test_el_cron_resuelve_por_alias_y_no_por_email(sync):
    """🔑 El caso RIPEHO/VISCO en el camino donde realmente pasó.

    El pagador es el contador; el cliente real es otro. Sin alias, el match
    directo elegiría el placeholder que el propio fallback creó.
    """
    real = db_clients.create_client(
        name="Cliente Real SA", email="admin@real.test", cuit_dni="30712345678",
        iva_condition="Monotributista",
    )
    db_clients.toggle_auto_facturar(real)
    placeholder = db_clients.create_client(
        name=EMAIL_PAGADOR, email=EMAIL_PAGADOR, iva_condition="Consumidor Final",
    )
    db_clients.toggle_auto_facturar(placeholder)
    db_mp.crear_alias_facturacion("email", EMAIL_PAGADOR, real)

    sync.cargar([_cobro()])
    resultado = _correr(sync)
    assert resultado["facturados"] == 1, resultado

    movimiento = db_mp.get_mp_movimiento_by_mp_id("800001")
    factura = db_facturas.get_factura(movimiento["factura_id"])
    assert factura["cliente_razon"] == "Cliente Real SA"


def test_sin_alias_ni_bandera_queda_pendiente(sync):
    """El control negativo: un cobro de alguien que existe pero no pidió
    auto-facturación **no** se factura solo."""
    db_clients.create_client(name="Existe", email=EMAIL_PAGADOR,
                             iva_condition="Monotributista")
    sync.cargar([_cobro()])
    resultado = _correr(sync)
    assert resultado == {"nuevos": 1, "facturados": 0, "pendientes": 1}
    assert db_mp.get_mp_movimiento_by_mp_id("800001")["estado_factura"] == "pendiente"


def test_sin_cliente_registrado_queda_pendiente(sync):
    sync.cargar([_cobro()])
    resultado = _correr(sync)
    assert resultado["pendientes"] == 1
    assert db_mp.get_mp_movimiento_by_mp_id("800001") is not None, "el cobro no se pierde"


def test_con_la_bandera_se_factura(sync):
    cliente = db_clients.create_client(
        name="Auto SA", email=EMAIL_PAGADOR, cuit_dni=CUIT_PAGADOR,
        iva_condition="Monotributista",
    )
    db_clients.toggle_auto_facturar(cliente)
    sync.cargar([_cobro()])
    assert _correr(sync)["facturados"] == 1
    movimiento = db_mp.get_mp_movimiento_by_mp_id("800001")
    assert movimiento["estado_factura"] == "facturado"
    assert db_facturas.get_factura(movimiento["factura_id"])["total"] == 12000.0


def test_la_regla_del_producto_puede_facturar_sin_bandera(sync):
    """La costura: es como Contalibra factura sus cobros de *Hosting Mensual*
    sin que el motor sepa qué es eso."""
    db_clients.create_client(name="Sin Bandera", email=EMAIL_PAGADOR,
                             iva_condition="Monotributista")

    def por_descripcion(client, contexto):
        return contexto["descripcion"].lower().startswith("abono")

    sync.cargar([_cobro()])
    assert _correr(sync, debe_auto_facturar=por_descripcion)["facturados"] == 1


# ── Un lote con un cobro problemático ────────────────────────────────────────

def test_un_error_al_facturar_no_se_lleva_puesto_el_resto_del_lote(sync, monkeypatch):
    """🔴 El cron procesa lo de toda la noche. Que un cobro falle no puede
    dejar sin facturar a los que venían detrás."""
    cliente = db_clients.create_client(
        name="Auto SA", email=EMAIL_PAGADOR, cuit_dni=CUIT_PAGADOR,
        iva_condition="Monotributista",
    )
    db_clients.toggle_auto_facturar(cliente)

    original = sync.mp_facturacion.generar_factura_mp
    llamadas = []

    async def falla_la_primera(*a, **kw):
        llamadas.append(kw.get("referencia"))
        if len(llamadas) == 1:
            raise RuntimeError("ARCA rechazo el comprobante")
        return await original(*a, **kw)

    monkeypatch.setattr(sync.mp_facturacion, "generar_factura_mp", falla_la_primera)

    sync.cargar([_cobro(id="800001"), _cobro(id="800002")])
    resultado = _correr(sync)
    assert len(llamadas) == 2, "tuvo que intentar los dos"
    assert resultado["facturados"] == 1
    assert resultado["pendientes"] == 1
    assert db_mp.get_mp_movimiento_by_mp_id("800001")["estado_factura"] == "pendiente"
    assert db_mp.get_mp_movimiento_by_mp_id("800002")["estado_factura"] == "facturado"


# ── La ingesta es una sola ───────────────────────────────────────────────────

def test_el_cron_y_la_bandeja_comparten_la_ingesta(sync, entorno):
    """🔑 El punto de este módulo.

    La bandeja llama a `ingerir` y el cron también: correr el cron después de
    sincronizar a mano **no puede traer el mismo cobro dos veces**. Con dos
    implementaciones separadas eso dependía de que las dos tuvieran los mismos
    cortes, que es exactamente lo que se cayó una vez.
    """
    sync.cargar([_cobro()])
    primeros = asyncio.run(sync.ingerir(entorno.load(), dias=7))
    assert len(primeros) == 1

    resultado = _correr(sync)
    assert resultado["nuevos"] == 0, "el cron no puede re-ingerir lo que ya está"


def test_no_ingiere_cobros_de_otra_cuenta_ni_referencias_omitidas(sync, entorno):
    """Las dos mitades de los dos cortes: lo que se descarta y lo que entra."""
    sync.cargar([
        _cobro(id="800010", collector_id="999"),
        _cobro(id="800011", external_reference="venta-3"),
        _cobro(id="800012"),
    ])
    nuevos = asyncio.run(
        sync.ingerir(entorno.load(), dias=7, referencias_a_omitir=("venta-",))
    )
    assert [n.payment_id for n in nuevos] == ["800012"]
