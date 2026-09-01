"""Convertir un pago de MercadoPago en una factura.

El test que manda es el primero: **el alias gana sobre el match directo**. Es
el que fija el defecto que costó dos comprobantes emitidos contra ARCA al CUIT
equivocado, y el que Restolibra no tenía porque su copia del módulo resolvía el
cliente sin mirar los alias.

Corre con `ENV=development`, así que la numeración es local y el CAE es
simulado: lo que se prueba acá es la lógica de negocio, no el protocolo de ARCA
—eso ya lo cubren `test_arca_wsaa.py`/`test_arca_wsfe.py`—.
"""

import asyncio
import importlib

import pytest

from libracore.db import caja as db_caja
from libracore.db import clients as db_clients
from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db import mp as db_mp
from libracore.db.schema import init_core_schema

EMAIL_PAGADOR = "contador@estudio.test"
CUIT_REAL = "30712345678"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENV", "development")
    import libracore.config_manager as cm
    importlib.reload(cm)

    core.configure(db_path=str(tmp_path / "mp_facturacion_test.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


@pytest.fixture
def mp_fact():
    from libracore import mp_facturacion
    return mp_facturacion


CFG_MONOTRIBUTO = {
    "empresa_iva_condition": "Monotributista",
    "empresa_nombre": "Mi Empresa",
    "mp_iva_rate": "0",
    "mp_concepto_descripcion": "Abono mensual",
}


def _facturar(mp_fact, cfg=None, **kw):
    datos = dict(
        monto=10000.0,
        payer_email=EMAIL_PAGADOR,
        payer_name="Estudio Contable",
        referencia="mp-1",
        cfg=cfg or CFG_MONOTRIBUTO,
    )
    datos.update(kw)
    return asyncio.run(mp_fact.generar_factura_mp(**datos))


# ── El alias, que es la razón de que este módulo sea uno solo ────────────────

def test_el_alias_le_gana_al_cliente_placeholder_mas_nuevo(conn, mp_fact):
    """🔑 El caso RIPEHO/VISCO, reproducido.

    Dos clientes con el mismo email: el real, y el placeholder que el propio
    fallback de este módulo creó la primera vez que un pago no matcheó.
    `get_client_by_email` ordena `activo DESC, id DESC`, así que **gana el
    placeholder** — el de id más alto. El alias es lo único que lo desempata.
    """
    real = db_clients.create_client(
        name="Cliente Real SA", email=EMAIL_PAGADOR,
        cuit_dni=CUIT_REAL, iva_condition="Responsable Inscripto",
    )
    placeholder = db_clients.create_client(
        name=EMAIL_PAGADOR, email=EMAIL_PAGADOR, iva_condition="Consumidor Final",
    )
    assert placeholder > real, "el placeholder tiene que ser el más nuevo"

    # Control negativo: sin alias, el match directo elige mal.
    assert db_mp.resolver_cliente_pago(EMAIL_PAGADOR, "")["id"] == placeholder

    db_mp.crear_alias_facturacion("email", EMAIL_PAGADOR, real)

    factura_id, _, _, _ = _facturar(mp_fact)
    factura = db_facturas.get_factura(factura_id)
    assert factura["cliente_razon"] == "Cliente Real SA"
    assert factura["cliente_cuit"] == CUIT_REAL


def test_sin_alias_matchea_por_email(conn, mp_fact):
    """El control positivo del match directo: sin alias tiene que seguir
    funcionando, no quedar tapado por el camino nuevo."""
    cliente = db_clients.create_client(
        name="Unico SA", email=EMAIL_PAGADOR, cuit_dni=CUIT_REAL,
        iva_condition="Responsable Inscripto",
    )
    factura_id, _, _, _ = _facturar(mp_fact)
    assert db_facturas.get_factura(factura_id)["cliente_razon"] == "Unico SA"
    assert db_clients.get_client(cliente)["name"] == "Unico SA"


def test_sin_ningun_cliente_se_crea_uno(conn, mp_fact):
    factura_id, _, _, _ = _facturar(mp_fact)
    factura = db_facturas.get_factura(factura_id)
    assert factura["cliente_razon"] == "Estudio Contable"
    assert factura["cliente_cuit"] == ""


def test_cliente_override_no_resuelve_nada(conn, mp_fact):
    """El botón *Facturar* sobre un pago que el operador ya vinculó a mano: lo
    que eligió la persona manda sobre el alias y sobre el match."""
    elegido = db_clients.create_client(
        name="El Que Eligio El Operador", email="otro@test", cuit_dni="20111111112",
        iva_condition="Monotributista",
    )
    otro = db_clients.create_client(name="Match Por Email", email=EMAIL_PAGADOR)
    db_mp.crear_alias_facturacion("email", EMAIL_PAGADOR, otro)

    factura_id, _, _, _ = _facturar(
        mp_fact, cliente_override=db_clients.get_client(elegido)
    )
    assert db_facturas.get_factura(factura_id)["cliente_razon"] == "El Que Eligio El Operador"


# ── Los importes ─────────────────────────────────────────────────────────────

def test_factura_c_va_con_iva_cero(conn, mp_fact):
    """No es una simplificación: ARCA exige `ImpIVA = 0` e `ImpNeto = ImpTotal`
    para los tipos C, y rechaza el comprobante si se manda el bloque de
    alícuotas."""
    factura_id, _, tipo_lb, _ = _facturar(mp_fact, monto=12100.0)
    factura = db_facturas.get_factura(factura_id)
    assert tipo_lb == "Factura C"
    assert factura["iva_amount"] == 0.0
    assert factura["subtotal"] == 12100.0
    assert factura["total"] == 12100.0


def test_factura_b_separa_el_iva_del_total_cobrado(conn, mp_fact):
    """🔑 El monto que llega de MercadoPago es **lo cobrado**, o sea el total
    con IVA adentro. Tratarlo como neto facturaría de más."""
    cfg = {**CFG_MONOTRIBUTO,
           "empresa_iva_condition": "Responsable Inscripto",
           "mp_iva_rate": "0.21"}
    factura_id, _, tipo_lb, _ = _facturar(mp_fact, monto=12100.0, cfg=cfg)
    factura = db_facturas.get_factura(factura_id)
    assert tipo_lb == "Factura B"
    assert factura["total"] == 12100.0, "el total es lo que cobró MP"
    assert factura["subtotal"] == 10000.0
    assert factura["iva_amount"] == 2100.0
    assert round(factura["subtotal"] + factura["iva_amount"], 2) == factura["total"]


# ── El número que se nombra ──────────────────────────────────────────────────

def test_la_caja_nombra_el_numero_que_quedo_no_el_que_se_pidio(
    conn, mp_fact, monkeypatch
):
    """🔴 `create_factura()` reintenta con otro número si el que le pasaron ya
    está ocupado — es la carrera de numeración que documenta `db/facturas.py`.

    ⚠️ **El número tiene que llegar ya ocupado**, y por eso se fuerza el
    devuelto por ARCA en vez de ocupar una fila y confiar en la numeración
    local: `get_next_factura_numero` lee `MAX(numero) + 1`, así que si sólo se
    inserta la factura 1, el camino normal ya pide la 2 y **no hay reintento
    ninguno**. Escrito así, este test pasaba en verde con el defecto puesto.
    """
    db_facturas.create_factura(
        tipo=11, punto_venta=1, numero=1, fecha="2026-08-01",
        cliente_cuit="", cliente_razon="Ocupa el 1", cliente_iva_cond=5,
        items=[], subtotal=1.0, iva_amount=0.0, total=1.0,
        ambiente="produccion",
    )

    async def numero_ya_ocupado(punto_venta, tipo):
        # Lo que devolvería ARCA si su último autorizado quedó atrás del local
        # —o si dos comprobantes salen a la vez—: el 1, que ya está tomado.
        return 1, "_dev_mock_", "_dev_mock_"

    monkeypatch.setattr(
        mp_fact.arca_facturacion, "get_next_numero_with_arca", numero_ya_ocupado
    )

    factura_id, numero_str, _, _ = _facturar(mp_fact)
    factura = db_facturas.get_factura(factura_id)
    assert factura["numero"] == 2, "create_factura tuvo que reintentar"
    assert numero_str == "0001-00000002", "lo devuelto nombra el comprobante real"

    movimientos = db_caja.get_caja_movimientos()
    concepto = [m for m in movimientos if m["factura_id"] == factura_id][0]["concepto"]
    assert "00000002" in concepto, concepto
    assert "00000001" not in concepto


# ── Caja y correo ────────────────────────────────────────────────────────────

def test_deja_el_ingreso_en_caja(conn, mp_fact):
    factura_id, etiqueta, _, _ = _facturar(mp_fact, monto=5000.0, referencia="mp-777")
    movimientos = [m for m in db_caja.get_caja_movimientos() if m["factura_id"] == factura_id]
    assert len(movimientos) == 1
    assert movimientos[0]["tipo"] == "ingreso"
    assert movimientos[0]["monto"] == 5000.0
    assert movimientos[0]["referencia"] == "mp-777"


def test_sin_smtp_no_manda_mail_y_lo_dice(conn, mp_fact):
    _, _, _, enviado = _facturar(mp_fact)
    assert enviado is False


def test_el_cae_simulado_de_dev_queda_en_la_factura(conn, mp_fact):
    """En desarrollo el CAE es falso pero **existe**: sin eso el PDF sale con
    "Pendiente de autorización ARCA" y la pantalla de dev no se parece a la
    de producción."""
    factura_id, _, _, _ = _facturar(mp_fact)
    assert db_facturas.get_factura(factura_id)["cae"], "dev tiene que estampar un CAE simulado"
