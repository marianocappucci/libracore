"""La nota de crédito de una factura **al contado ya cobrada** mueve la plata adonde la persona elige (ADR-065).

Hasta la v1.175.0 esa nota no movía plata: ARCA quedaba al día y la caja, no. Ahora el cuerpo de
`POST /{factura_id}/nota-credito` acepta `destino` (`devolver` | `a_favor`) y `medio_pago`:

- `devolver`: un egreso de caja, con la marca `nc:factura:<id>:<nota_id>`, por el turno abierto de quien emite;
- `a_favor`: un abono en la cuenta corriente del cliente (hace falta uno identificado);
- sin `destino` no se mueve nada, como siempre; en una factura a cuenta corriente no se manda (422).

Si la nota no obtiene CAE, el destino queda anotado (`reintegros_pendientes`, ADR-067) y la plata se mueve cuando
se autoriza con `/autorizar`: una sola vez, y nada si no se mandó destino.

Cada test mira lo escrito en la base, no sólo el status. En SQLite y en PostgreSQL.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fechas import hoy
from test_facturas_router import ADMIN, API, USUARIO, _emitir, _montar

from libracore import facturas_router as fr
from libracore import notas_de_credito, reintegro_de_nota
from libracore.db import caja as db_caja
from libracore.db import core, libro_de_clientes
from libracore.db import turnos as db_turnos
from libracore.db.clients import create_client

ITEMS = [{"description": "Servicio", "qty": 1, "unit_price": 1000.0}]
SIN_TURNO = "No hay una caja abierta. Abrí el turno antes de devolver."
CUIT = "20304050607"


@pytest.fixture(params=["sqlite", "postgres"])
def motor(request, tmp_path):
    return None if request.param == "sqlite" else request.getfixturevalue("bases").nueva()


@pytest.fixture
def emisor(motor, tmp_path, monkeypatch):
    """`(client de los doce endpoints, función que monta el router con las opciones que se pidan)`."""
    base = _montar(tmp_path, monkeypatch, db_path=motor)
    cliente_id = create_client("Juan Perez", cuit_dni=CUIT)

    def montar(usuario=USUARIO, **opciones) -> TestClient:
        app = FastAPI()
        app.include_router(fr.build_comprobantes_router(
            usuario_actual=lambda: usuario, solo_admin=lambda: None, **opciones))
        return TestClient(app)

    montar.cliente_id = cliente_id
    yield base, montar
    core._db_path = None
    core._database_url = None


# ── Utilidades ────────────────────────────────────────────────────────────


def _cobrar(client, factura, monto=1000.0, medio="efectivo"):
    r = client.post(f"{API}/{factura['id']}/cobrar", json={"pagos": [{"medio_id": medio, "monto": monto}]})
    assert r.status_code == 200, r.text


def _cobrada(base, monto=1000.0, medio="efectivo", **extra) -> dict:
    """Una factura al contado de 1000, con `monto` cobrado."""
    factura = _emitir(base, items=ITEMS, **extra)
    if monto:
        _cobrar(base, factura, monto, medio)
    return factura


def _nota(client, factura, **cuerpo):
    return client.post(f"{API}/{factura['id']}/nota-credito", headers=ADMIN, json=cuerpo or None)


def _egresos(factura) -> list[dict]:
    with core.get_connection() as c:
        filas = c.execute("SELECT * FROM caja_movimientos WHERE factura_id=? AND tipo='egreso' ORDER BY id",
                          (factura["id"],)).fetchall()
    return [dict(f) for f in filas]


def _abonos(factura) -> list[dict]:
    with core.get_connection() as c:
        filas = c.execute("SELECT * FROM cc_pagos WHERE referencia LIKE ? ORDER BY id",
                          (f"nc:factura:{factura['id']}%",)).fetchall()
    return [dict(f) for f in filas]


def _notas_en_la_base() -> int:
    with core.get_connection() as c:
        return c.execute("SELECT COUNT(*) FROM facturas WHERE tipo = 13").fetchone()[0]


# ── Devolver ──────────────────────────────────────────────────────────────


def test_devolver_escribe_un_egreso_de_caja_con_la_marca_de_la_nota(emisor):
    base, _ = emisor
    factura = _cobrada(base)

    r = _nota(base, factura, destino="devolver", medio_pago="efectivo")

    assert r.status_code == 200, r.text
    nota = r.json()
    (egreso,) = _egresos(factura)
    marca = f"nc:factura:{factura['id']}:{nota['id']}"
    assert (egreso["tipo"], egreso["monto"], egreso["medio_pago"], egreso["referencia"]) == (
        "egreso", 1000.0, "efectivo", marca)
    assert egreso["fecha"] == hoy() and "Devolución" in egreso["concepto"]
    assert nota["reintegro"] == {
        "destino": "devolver", "medio_pago": "efectivo", "movido": True, "monto": 1000.0, "excedente": 0.0,
        "referencia": marca}
    # Y la caja lo ve: el egreso resta del saldo del cajón.
    resumen = db_caja.get_caja_resumen()
    assert resumen["egresos"] == 1000.0


def test_sin_medio_devuelve_por_el_unico_con_el_que_se_cobro(emisor):
    base, _ = emisor
    factura = _cobrada(base, medio="transferencia")

    r = _nota(base, factura, destino="devolver")

    assert r.status_code == 200, r.text
    assert [e["medio_pago"] for e in _egresos(factura)] == ["transferencia"]


def test_cobrada_con_dos_medios_no_adivina_cual_y_no_emite_nada(emisor):
    base, _ = emisor
    factura = _emitir(base, items=ITEMS)
    _cobrar(base, factura, 600.0, "efectivo")
    _cobrar(base, factura, 400.0, "transferencia")

    r = _nota(base, factura, destino="devolver")

    assert r.status_code == 422 and "más de un medio" in r.json()["detail"], r.text
    assert _egresos(factura) == [] and _notas_en_la_base() == 0, "se rechaza ANTES de pedirle nada a ARCA"
    # Eligiendo el medio, sale.
    assert _nota(base, factura, destino="devolver", medio_pago="transferencia").status_code == 200
    assert [e["medio_pago"] for e in _egresos(factura)] == ["transferencia"]


@pytest.mark.parametrize("medio", ["cuenta_corriente", "Cuenta Corriente", "bitcoin"])
def test_un_medio_que_no_es_plata_se_rechaza_antes_de_emitir(emisor, medio):
    base, _ = emisor
    factura = _cobrada(base)

    r = _nota(base, factura, destino="devolver", medio_pago=medio)

    assert r.status_code == 422, r.text
    assert _egresos(factura) == [] and _notas_en_la_base() == 0


def test_un_medio_historico_se_acepta_si_es_con_el_que_se_cobro(emisor):
    base, _ = emisor
    factura = _cobrada(base)
    with core.get_connection() as c:
        c.execute("UPDATE caja_movimientos SET medio_pago='tarjeta' WHERE factura_id=?", (factura["id"],))

    assert _nota(base, factura, destino="devolver").status_code == 200
    assert [e["medio_pago"] for e in _egresos(factura)] == ["tarjeta"]


# ── Dejar a favor ─────────────────────────────────────────────────────────


def test_a_favor_abona_la_cuenta_del_cliente_y_no_toca_la_caja(emisor):
    base, montar = emisor
    factura = _cobrada(base)

    r = _nota(base, factura, destino="a_favor")

    assert r.status_code == 200, r.text
    nota = r.json()
    (abono,) = _abonos(factura)
    assert (abono["cliente_id"], abono["monto"], abono["referencia"]) == (
        montar.cliente_id, 1000.0, f"nc:factura:{factura['id']}:{nota['id']}")
    assert _egresos(factura) == [], "dejar a favor no saca plata del cajón"
    # El cliente queda con 1000 a su favor (saldo negativo: haber sin debe).
    assert libro_de_clientes.saldo_de(montar.cliente_id) == -1000.0
    assert nota["reintegro"]["destino"] == "a_favor" and nota["reintegro"]["monto"] == 1000.0


def test_a_favor_sin_cliente_identificado_es_422_y_no_emite_nada(emisor):
    base, _ = emisor
    factura = _cobrada(base, client_name="Consumidor", client_cuit="20111111112")

    r = _nota(base, factura, destino="a_favor")

    assert r.status_code == 422 and "cliente identificado" in r.json()["detail"], r.text
    assert _abonos(factura) == [] and _notas_en_la_base() == 0
    # Devolver sí se puede: no hace falta una cuenta.
    assert _nota(base, factura, destino="devolver").status_code == 200


def test_a_favor_con_medio_de_pago_es_422(emisor):
    base, _ = emisor
    factura = _cobrada(base)

    r = _nota(base, factura, destino="a_favor", medio_pago="efectivo")

    assert r.status_code == 422 and _notas_en_la_base() == 0, r.text


# ── Sin destino, sin cobro, factura a cuenta corriente ────────────────────


def test_sin_destino_la_nota_no_mueve_plata_como_siempre(emisor):
    base, _ = emisor
    factura = _cobrada(base)

    r = _nota(base, factura)

    assert r.status_code == 200 and "reintegro" not in r.json(), r.text
    assert _egresos(factura) == [] and _abonos(factura) == []


def test_si_no_habia_nada_cobrado_el_destino_se_ignora(emisor):
    base, _ = emisor
    factura = _cobrada(base, monto=0)

    r = _nota(base, factura, destino="devolver")

    assert r.status_code == 200, r.text
    assert r.json()["reintegro"]["movido"] is False and r.json()["reintegro"]["monto"] == 0.0
    assert _egresos(factura) == [] and _abonos(factura) == []


def test_una_factura_a_cuenta_corriente_no_lleva_destino(emisor):
    base, montar = emisor
    factura = _emitir(base, items=ITEMS, condicion_venta="Cuenta Corriente")

    r = _nota(base, factura, destino="a_favor")

    assert r.status_code == 422 and "cuenta corriente" in r.json()["detail"], r.text
    assert _notas_en_la_base() == 0
    # Sin destino sigue como siempre: la nota abona la cuenta, una vez.
    assert _nota(base, factura).status_code == 200
    (abono,) = _abonos(factura)
    assert abono["monto"] == 1000.0 and _egresos(factura) == []


def test_un_destino_que_no_existe_es_422(emisor):
    base, _ = emisor
    factura = _cobrada(base)

    assert _nota(base, factura, destino="regalar").status_code == 422
    assert _notas_en_la_base() == 0


# ── Cuánto se mueve ───────────────────────────────────────────────────────


def test_dos_notas_parciales_devuelven_cada_una_lo_suyo_y_juntas_no_pasan_de_lo_cobrado(emisor):
    base, _ = emisor
    factura = _cobrada(base)

    primera = _nota(base, factura, importe=400, destino="devolver").json()
    segunda = _nota(base, factura, importe=600, destino="devolver").json()

    assert [e["monto"] for e in _egresos(factura)] == [400.0, 600.0]
    assert primera["reintegro"]["monto"] == 400.0 and segunda["reintegro"]["monto"] == 600.0
    assert segunda["reintegro"]["referencia"] == f"nc:factura:{factura['id']}:{segunda['id']}"
    with core.get_connection() as c:
        assert notas_de_credito.devuelto_por_notas(c, factura["id"]) == Decimal("1000.00")


def test_cobrada_a_medias_con_nota_total_devuelve_solo_lo_que_entro_y_documenta_el_resto(emisor):
    base, _ = emisor
    factura = _cobrada(base, monto=400.0)

    r = _nota(base, factura, destino="devolver")

    assert r.status_code == 200, r.text
    (egreso,) = _egresos(factura)
    assert egreso["monto"] == 400.0, "no se devuelve lo que no entró"
    assert r.json()["reintegro"]["monto"] == 400.0 and r.json()["reintegro"]["excedente"] == 600.0
    assert "el resto no estaba cobrado" in egreso["concepto"] and "$ 400,00 de $ 1.000,00" in egreso["concepto"]


def test_cobrada_a_medias_con_una_nota_parcial_que_no_pasa_de_lo_que_falta_no_devuelve_nada(emisor):
    """Se pagaron 400 de 1000 y se acreditan 300: el cliente sigue debiendo 300 de los 700 que quedan."""
    base, _ = emisor
    factura = _cobrada(base, monto=400.0)

    r = _nota(base, factura, importe=300, destino="devolver")

    assert r.status_code == 200, r.text
    assert r.json()["reintegro"]["movido"] is False
    assert _egresos(factura) == []


def test_una_nota_que_deja_pagado_de_mas_devuelve_solo_el_sobrante(emisor):
    """Se pagaron 400 de 1000 y se acreditan 800: quedan 200 por cobrar y entraron 400, o sea 200 de más."""
    base, _ = emisor
    factura = _cobrada(base, monto=400.0)

    r = _nota(base, factura, importe=800, destino="devolver")

    assert r.status_code == 200, r.text
    assert [e["monto"] for e in _egresos(factura)] == [200.0]
    assert r.json()["reintegro"]["excedente"] == 600.0


def test_lo_devuelto_no_cambia_lo_pendiente_ni_lo_cobrado_de_la_factura(emisor):
    """El egreso lleva el `factura_id` pero no es un cobro: la factura sigue «cobrada y acreditada», en 0."""
    base, _ = emisor
    factura = _cobrada(base)
    _nota(base, factura, destino="devolver")

    detalle = base.get(f"{API}/{factura['id']}").json()

    assert detalle["pendiente"] == 0.0 and detalle["total_cobrado"] == 1000.0 and detalle["total_acreditado"] == 1000.0


def test_una_segunda_nota_total_no_devuelve_otra_vez(emisor):
    base, _ = emisor
    factura = _cobrada(base)
    assert _nota(base, factura, destino="devolver").status_code == 200

    otra = _nota(base, factura, destino="devolver")

    assert otra.status_code == 409, otra.text
    assert len(_egresos(factura)) == 1


# ── La nota no obtuvo CAE ─────────────────────────────────────────────────


def test_si_la_nota_no_obtiene_CAE_la_plata_no_se_mueve(tmp_path, monkeypatch):
    """Sin ARCA configurado la nota queda sin CAE (`solicitar_cae` no levanta): no sale nada de la caja."""
    base = _montar(tmp_path, monkeypatch, dev=False)
    create_client("Juan Perez", cuit_dni=CUIT)
    factura = _cobrada(base)
    assert not factura["cae"]

    r = _nota(base, factura, destino="devolver")

    assert r.status_code == 200, r.text
    nota = r.json()
    assert not nota["cae"]
    assert nota["reintegro"]["movido"] is False and "CAE" in nota["reintegro"]["motivo"]
    # Ya no se pide hacerlo a mano: el destino queda anotado y la plata sale sola al autorizarse (ADR-067).
    assert nota["reintegro"]["pendiente"] is True and "cuando la autorices" in nota["reintegro"]["motivo"]
    assert _egresos(factura) == [] and _abonos(factura) == []


# ── La nota sin CAE se autoriza después (ADR-067) ─────────────────────────


@pytest.fixture
def sin_arca(motor, tmp_path, monkeypatch):
    """Como `emisor`, pero sin ARCA: toda nota sale sin CAE. `(client, montar, habilitar_arca)`.

    `habilitar_arca()` carga un par de certificados y simula a ARCA: desde ahí `/autorizar` obtiene CAE."""
    base = _montar(tmp_path, monkeypatch, dev=False, db_path=motor)
    cliente_id = create_client("Juan Perez", cuit_dni=CUIT)

    def montar(usuario=USUARIO, **opciones) -> TestClient:
        app = FastAPI()
        app.include_router(fr.build_comprobantes_router(
            usuario_actual=lambda: usuario, solo_admin=lambda: None, **opciones))
        return TestClient(app)

    pedidos = []

    def habilitar_arca(*, rechaza: str | None = None):
        from libracore.db import arca_config as db_arca

        cert, clave = tmp_path / "cert.pem", tmp_path / "clave.key"
        cert.write_text("x")
        clave.write_text("x")
        if not db_arca.obtener_arca_config("empresa"):
            db_arca.crear_arca_config("empresa", "30712345679", 1, "", "", "homologacion")
            db_arca.actualizar_arca_config(
                "empresa", clave_path_homologacion=str(clave), certificado_path_homologacion=str(cert))

        async def autenticar(*a, **kw):
            return {"token": "t", "sign": "s"}

        async def cae(comprobante, *a, **kw):
            pedidos.append(comprobante["id"])
            if rechaza:
                raise RuntimeError(rechaza)
            return {"cae": "75123456789012", "cae_vto": "20261231"}

        monkeypatch.setattr(fr.arca_wsaa, "autenticar", autenticar)
        monkeypatch.setattr(fr.arca_wsfe, "solicitar_cae", cae)

    montar.cliente_id = cliente_id
    montar.pedidos_de_cae = pedidos
    yield base, montar, habilitar_arca
    core._db_path = None
    core._database_url = None


def _anotados() -> list[dict]:
    with core.get_connection() as c:
        return [dict(f) for f in c.execute("SELECT * FROM reintegros_pendientes ORDER BY nota_id").fetchall()]


def _autorizar(client, nota):
    return client.post(f"{API}/{nota['id']}/autorizar")


def test_la_nota_sin_CAE_anota_el_destino_y_dice_que_va_a_pasar(sin_arca):
    base, _, _ = sin_arca
    factura = _cobrada(base)

    nota = _nota(base, factura, destino="devolver", medio_pago="efectivo").json()

    assert not nota["cae"]
    reintegro = nota["reintegro"]
    assert reintegro["movido"] is False and reintegro["pendiente"] is True
    assert "van a salir $ 1.000,00 de la caja" in reintegro["motivo"] and "autorices" in reintegro["motivo"]
    assert _anotados() == [{"nota_id": nota["id"], "factura_id": factura["id"], "destino": "devolver",
                            "medio_pago": "efectivo", "created_at": _anotados()[0]["created_at"]}]
    assert _egresos(factura) == [], "hasta que se autorice no sale nada"


def test_la_nota_sin_CAE_que_dejaba_a_favor_lo_dice_con_otras_palabras(sin_arca):
    base, _, _ = sin_arca
    factura = _cobrada(base)

    nota = _nota(base, factura, destino="a_favor").json()

    assert "van a quedar $ 1.000,00 a favor del cliente" in nota["reintegro"]["motivo"]
    assert [(a["destino"], a["medio_pago"]) for a in _anotados()] == [("a_favor", "")]


def test_autorizar_la_nota_sin_CAE_devuelve_la_plata_con_el_destino_elegido(sin_arca):
    base, _, habilitar_arca = sin_arca
    factura = _cobrada(base)
    nota = _nota(base, factura, destino="devolver", medio_pago="efectivo").json()
    habilitar_arca()

    r = _autorizar(base, nota)

    assert r.status_code == 200, r.text
    marca = f"nc:factura:{factura['id']}:{nota['id']}"
    assert r.json()["reintegro"] == {
        "destino": "devolver", "medio_pago": "efectivo", "movido": True, "monto": 1000.0, "excedente": 0.0,
        "referencia": marca}
    (egreso,) = _egresos(factura)
    assert (egreso["monto"], egreso["medio_pago"], egreso["referencia"]) == (1000.0, "efectivo", marca)
    assert base.get(f"{API}/{nota['id']}").json()["factura"]["cae"], "y la nota quedó con su CAE"
    assert _anotados() == [], "la anotación se consumió"


def test_autorizar_la_nota_sin_CAE_deja_a_favor_del_cliente(sin_arca):
    base, montar, habilitar_arca = sin_arca
    factura = _cobrada(base)
    nota = _nota(base, factura, destino="a_favor").json()
    habilitar_arca()

    r = _autorizar(base, nota)

    assert r.status_code == 200, r.text
    assert r.json()["reintegro"]["movido"] is True and r.json()["reintegro"]["destino"] == "a_favor"
    assert len(_abonos(factura)) == 1 and _egresos(factura) == []
    assert libro_de_clientes.saldo_de(montar.cliente_id) == -1000.0


def test_autorizar_dos_veces_no_mueve_la_plata_dos_veces(sin_arca):
    base, _, habilitar_arca = sin_arca
    factura = _cobrada(base)
    nota = _nota(base, factura, destino="devolver", medio_pago="efectivo").json()
    habilitar_arca()

    primera, segunda = _autorizar(base, nota), _autorizar(base, nota)

    assert primera.status_code == segunda.status_code == 200
    assert "reintegro" in primera.json() and "reintegro" not in segunda.json()
    assert len(_egresos(factura)) == 1


def test_si_la_anotacion_sobrevive_a_un_movimiento_ya_hecho_no_se_escribe_otro(sin_arca):
    """Un reintento después de que el movimiento quedó escrito (la marca de la nota manda, no la anotación)."""
    base, _, habilitar_arca = sin_arca
    factura = _cobrada(base)
    nota = _nota(base, factura, destino="devolver", medio_pago="efectivo").json()
    habilitar_arca()
    assert _autorizar(base, nota).status_code == 200
    from libracore.db import reintegros_pendientes
    reintegros_pendientes.anotar(nota["id"], factura["id"], "devolver", "efectivo")   # como si no se hubiera borrado

    r = _autorizar(base, nota)

    assert r.status_code == 200 and r.json()["reintegro"]["ya_estaba"] is True
    assert len(_egresos(factura)) == 1 and _anotados() == []


def test_sin_destino_autorizar_la_nota_no_mueve_plata_ni_agrega_nada(sin_arca):
    base, _, habilitar_arca = sin_arca
    factura = _cobrada(base)
    nota = _nota(base, factura).json()
    assert "reintegro" not in nota and _anotados() == []
    habilitar_arca()

    r = _autorizar(base, nota)

    assert r.status_code == 200 and "reintegro" not in r.json()
    assert _egresos(factura) == [] and _abonos(factura) == []


def test_autorizar_una_factura_cualquiera_no_cambia_nada(sin_arca):
    base, _, habilitar_arca = sin_arca
    factura = _cobrada(base)
    habilitar_arca()

    r = _autorizar(base, factura)

    assert r.status_code == 200 and "reintegro" not in r.json()
    assert _egresos(factura) == []


def test_si_ARCA_vuelve_a_rechazar_la_nota_la_anotacion_sigue_y_no_sale_plata(sin_arca):
    base, _, habilitar_arca = sin_arca
    factura = _cobrada(base)
    nota = _nota(base, factura, destino="devolver", medio_pago="efectivo").json()
    habilitar_arca(rechaza="ARCA dijo que no")

    r = _autorizar(base, nota)

    assert r.status_code == 502, r.text
    assert len(_anotados()) == 1 and _egresos(factura) == []


def test_borrar_la_nota_sin_CAE_borra_su_anotacion_y_no_mueve_nada(sin_arca):
    base, _, _ = sin_arca
    factura = _cobrada(base)
    nota = _nota(base, factura, destino="devolver", medio_pago="efectivo").json()
    assert len(_anotados()) == 1

    assert base.delete(f"{API}/{nota['id']}", headers=ADMIN).status_code == 200

    assert _anotados() == [] and _egresos(factura) == []


def test_si_la_plata_no_se_pudo_mover_la_nota_queda_autorizada_y_reautorizar_lo_reintenta(sin_arca, monkeypatch):
    base, _, habilitar_arca = sin_arca
    factura = _cobrada(base)
    nota = _nota(base, factura, destino="devolver", medio_pago="efectivo").json()
    habilitar_arca()
    real = reintegro_de_nota.aplicar_pendiente

    def falla(*a, **k):
        raise RuntimeError("se cayó la base")

    monkeypatch.setattr(reintegro_de_nota, "aplicar_pendiente", falla)
    primera = _autorizar(base, nota)

    assert primera.status_code == 200, "la nota ya está autorizada: no se tumba la request"
    assert primera.json()["reintegro"]["movido"] is False and "no se pudo mover" in primera.json()["reintegro"]["motivo"]
    assert base.get(f"{API}/{nota['id']}").json()["factura"]["cae"]
    assert len(_anotados()) == 1 and _egresos(factura) == []

    monkeypatch.setattr(reintegro_de_nota, "aplicar_pendiente", real)
    segunda = _autorizar(base, nota)

    assert segunda.json()["reintegro"]["movido"] is True
    assert len(_egresos(factura)) == 1 and _anotados() == []


def test_al_autorizar_sin_turno_abierto_y_con_turno_exigido_es_409_antes_de_pedir_el_CAE(sin_arca):
    base, montar, habilitar_arca = sin_arca
    factura = _cobrada(base)
    nota = _nota(base, factura, destino="devolver", medio_pago="efectivo").json()
    habilitar_arca()
    con_turno = montar(exigir_turno=True)

    r = _autorizar(con_turno, nota)

    assert r.status_code == 409 and "turno" in r.json()["detail"], r.text
    assert montar.pedidos_de_cae == [], "no se le pidió nada a ARCA"
    assert not base.get(f"{API}/{nota['id']}").json()["factura"]["cae"] and _egresos(factura) == []

    turno, caja = _turno()
    ok = _autorizar(con_turno, nota)

    assert ok.status_code == 200, ok.text
    (egreso,) = _egresos(factura)
    assert (egreso["turno_id"], egreso["caja_id"]) == (turno, caja)


def test_al_autorizar_dejar_a_favor_sin_cliente_es_422_antes_de_pedir_el_CAE(sin_arca):
    base, montar, habilitar_arca = sin_arca
    factura = _cobrada(base)
    nota = _nota(base, factura, destino="a_favor").json()
    with core.get_connection() as c:
        c.execute("DELETE FROM clients")
    habilitar_arca()

    r = _autorizar(base, nota)

    assert r.status_code == 422 and "cliente" in r.json()["detail"], r.text
    assert montar.pedidos_de_cae == [] and len(_anotados()) == 1


# ── Caja por turno ────────────────────────────────────────────────────────


def _turno(usuario_id=1) -> tuple[int, int]:
    caja_id = db_caja.create_caja_config("Mostrador", "", ["efectivo", "transferencia"])
    return db_turnos.create_turno(usuario_id, 0.0, caja_id=caja_id), caja_id


def test_el_egreso_entra_en_el_turno_abierto_de_quien_emite(emisor):
    base, montar = emisor
    factura = _cobrada(base)
    turno, caja = _turno()

    r = _nota(montar(exigir_turno=True), factura, destino="devolver")

    assert r.status_code == 200, r.text
    (egreso,) = _egresos(factura)
    assert (egreso["turno_id"], egreso["caja_id"], egreso["usuario_id"]) == (turno, caja, 1)
    # Es lo que el arqueo cuenta: la razón de ser de la caja por turno. El efectivo del turno baja lo devuelto.
    assert db_turnos.get_resumen_turno_caja(turno)["efectivo_ventas"] == -1000.0


def test_sin_turno_abierto_y_con_turno_exigido_es_409_y_no_se_emite_nada(emisor):
    base, montar = emisor
    factura = _cobrada(base)

    r = _nota(montar(exigir_turno=True), factura, destino="devolver")

    assert r.status_code == 409 and r.json()["detail"] == SIN_TURNO, r.text
    assert _egresos(factura) == [] and _notas_en_la_base() == 0, "se frena antes del CAE"


def test_dejar_a_favor_no_necesita_turno(emisor):
    base, montar = emisor
    factura = _cobrada(base)

    assert _nota(montar(exigir_turno=True), factura, destino="a_favor").status_code == 200
    assert len(_abonos(factura)) == 1


def test_con_el_usuario_del_dominio_el_turno_es_el_de_quien_emite(emisor):
    base, montar = emisor
    factura = _cobrada(base)
    turno, _ = _turno()
    del_dominio = {"id": None, "id_dominio": 1, "username": "cajero"}

    sin = _nota(montar(usuario=del_dominio, exigir_turno=True), factura, destino="devolver")
    assert sin.status_code == 409
    con = _nota(montar(usuario=del_dominio, exigir_turno=True, usuario_del_turno=lambda u: u["id_dominio"]),
                factura, destino="devolver")

    assert con.status_code == 200, con.text
    assert [e["turno_id"] for e in _egresos(factura)] == [turno]


def test_el_router_solo_de_la_nota_tambien_acepta_el_destino(emisor):
    base, _ = emisor
    factura = _cobrada(base)
    turno, _caja = _turno()
    app = FastAPI()
    app.include_router(fr.build_nota_de_credito_router(
        usuario_actual=lambda: USUARIO, solo_admin=lambda: None, exigir_turno=True))

    r = TestClient(app).post(f"{API}/{factura['id']}/nota-credito", json={"destino": "devolver"})

    assert r.status_code == 200, r.text
    assert [e["turno_id"] for e in _egresos(factura)] == [turno]


# ── Que no se devuelva dos veces ──────────────────────────────────────────


def test_aplicar_dos_veces_la_misma_nota_no_devuelve_dos_veces(emisor):
    base, montar = emisor
    factura = _cobrada(base)
    nota = _nota(base, factura, destino="devolver").json()
    original = base.get(f"{API}/{factura['id']}").json()["factura"]
    plan = reintegro_de_nota.Plan(reintegro_de_nota.DEVOLVER, "efectivo", None, None)

    otra_vez = reintegro_de_nota.aplicar(plan, original, nota, 1)

    assert otra_vez["ya_estaba"] is True and otra_vez["monto"] == 1000.0
    assert len(_egresos(factura)) == 1


def test_aplicar_dos_veces_el_saldo_a_favor_no_abona_dos_veces(emisor):
    base, montar = emisor
    factura = _cobrada(base)
    nota = _nota(base, factura, destino="a_favor").json()
    original = base.get(f"{API}/{factura['id']}").json()["factura"]
    plan = reintegro_de_nota.Plan(reintegro_de_nota.A_FAVOR, None, montar.cliente_id, None)

    otra_vez = reintegro_de_nota.aplicar(plan, original, nota, 1)

    assert otra_vez["ya_estaba"] is True
    assert len(_abonos(factura)) == 1 and libro_de_clientes.saldo_de(montar.cliente_id) == -1000.0


def test_un_egreso_anulado_no_cuenta_como_devuelto(emisor):
    base, _ = emisor
    factura = _cobrada(base)
    _nota(base, factura, importe=400, destino="devolver")
    with core.get_connection() as c:
        c.execute("UPDATE caja_movimientos SET anulado=1 WHERE factura_id=? AND tipo='egreso'", (factura["id"],))
        assert notas_de_credito.devuelto_por_notas(c, factura["id"]) == Decimal("0.00")


# ── Lo que la pantalla pregunta ───────────────────────────────────────────


def _destino(client, factura, **params):
    return client.get(f"{API}/{factura['id']}/nota-credito/destino", headers=ADMIN, params=params)


def test_la_pantalla_pregunta_cuanto_medios_y_si_hay_cliente(emisor):
    base, _ = emisor
    factura = _cobrada(base, medio="transferencia")

    r = _destino(base, factura)

    assert r.status_code == 200, r.text
    datos = r.json()
    assert datos["aplica"] is True and datos["monto"] == 1000.0 and datos["excedente"] == 0.0
    assert datos["medio_sugerido"] == "transferencia" and datos["cliente_identificado"] is True
    ids = [m["id"] for m in datos["medios"]]
    assert "transferencia" in ids and "cuenta_corriente" not in ids
    assert {"id": "efectivo", "label": "Efectivo"} in datos["medios"]
    assert datos["sin_turno"] is False
    assert _egresos(factura) == [] and _notas_en_la_base() == 0, "preguntar no escribe nada"


def test_la_pregunta_con_un_importe_usa_la_cuenta_de_ese_importe(emisor):
    base, _ = emisor
    factura = _cobrada(base, monto=400.0)

    assert _destino(base, factura).json()["monto"] == 400.0
    assert _destino(base, factura, importe=800).json()["monto"] == 200.0
    assert _destino(base, factura, importe=300).json()["aplica"] is False


def test_la_pregunta_no_aplica_a_cuenta_corriente_ni_sin_cobro(emisor):
    base, _ = emisor
    a_credito = _emitir(base, items=ITEMS, condicion_venta="Cuenta Corriente")
    sin_cobro = _cobrada(base, monto=0)

    assert _destino(base, a_credito).json()["aplica"] is False
    assert _destino(base, sin_cobro).json()["aplica"] is False
    assert _destino(base, {"id": 9999}).status_code == 404


def test_la_pregunta_sin_cliente_dice_que_no_se_puede_dejar_a_favor(emisor):
    base, _ = emisor
    factura = _cobrada(base, client_name="Consumidor", client_cuit="20111111112")

    assert _destino(base, factura).json()["cliente_identificado"] is False


def test_la_pregunta_avisa_si_el_turno_se_exige_y_no_hay_uno_abierto(emisor):
    base, montar = emisor
    factura = _cobrada(base)
    exigente = montar(exigir_turno=True)

    assert _destino(exigente, factura).json()["sin_turno"] is True
    _turno()
    assert _destino(exigente, factura).json()["sin_turno"] is False


# ── La cuenta pura ────────────────────────────────────────────────────────

# (total, cobrado, acreditado con esta, ya reintegrado, importe) → a mover
CUENTAS = [
    ("cobrada_completa_nota_parcial", 1000, 1000, 300, 0, 300, 300),
    ("cobrada_completa_nota_total", 1000, 1000, 1000, 0, 1000, 1000),
    ("cobrada_a_medias_nota_total", 1000, 400, 1000, 0, 1000, 400),
    ("cobrada_a_medias_nota_que_no_pasa", 1000, 400, 300, 0, 300, 0),
    ("cobrada_a_medias_nota_que_pasa", 1000, 400, 800, 0, 800, 200),
    ("nada_cobrado", 1000, 0, 1000, 0, 1000, 0),
    ("segunda_nota_tras_una_devuelta", 1000, 1000, 1000, 400, 600, 600),
    ("ya_devuelto_todo", 1000, 1000, 1000, 1000, 100, 0),
    ("nota_vieja_que_no_movio_plata", 1000, 1000, 800, 0, 400, 400),
]


@pytest.mark.parametrize("nombre,total,cobrado,acreditado,ya,importe,esperado", CUENTAS, ids=[c[0] for c in CUENTAS])
def test_la_cuenta_de_cuanto_se_mueve(nombre, total, cobrado, acreditado, ya, importe, esperado):
    assert reintegro_de_nota.reintegrable(total, cobrado, acreditado, ya, importe) == Decimal(esperado)


def test_planear_sin_base_decide_con_el_estado_que_se_le_da():
    estado = reintegro_de_nota.Estado(Decimal(1000), Decimal(0), Decimal(0), ("efectivo",))
    original = {"id": 7, "total": 1000.0, "condicion_venta": "Contado", "cliente_cuit": ""}

    plan = reintegro_de_nota.planear(original, 1000, "devolver", leer=lambda _: estado)

    assert plan == reintegro_de_nota.Plan("devolver", "efectivo", None, None)
    assert reintegro_de_nota.planear(original, 1000, None, leer=lambda _: estado) is None
    with pytest.raises(notas_de_credito.NotaNoPermitida) as e:
        reintegro_de_nota.planear(original, 1000, "devolver", exigir_turno=True, turno_de=lambda: None,
                                  leer=lambda _: estado)
    assert e.value.codigo == notas_de_credito.NotaNoPermitida.SIN_TURNO


def test_preguntar_es_solo_de_admin(emisor):
    base, _ = emisor
    factura = _cobrada(base)

    assert base.get(f"{API}/{factura['id']}/nota-credito/destino").status_code == 403
