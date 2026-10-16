"""Un cobro no puede superar lo que falta de la factura (ADR-058).

Medido el 2026-10-15 en el e2e de Contalibra (`facturacion.e2e.ts`): `POST /api/facturas/{id}/cobrar`
aceptó 9.999 sobre una factura de 4.500 con 1.000 cobrados y 3.500 pendientes. 200, «Cobrado completo
— $ 10.999,00», y la caja asentó un ingreso de 9.999 que nadie debía.

Los tests van por HTTP, contra el router montado como lo monta un producto, y **en SQLite y PostgreSQL**
(el candado de la factura es distinto en cada motor). Cada uno mira lo escrito en la base y no sólo el
status: un 409 que dejara el primer medio de un cobro de dos escrito sería peor que el defecto.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_facturas_router import ADMIN, API, USUARIO, _emitir, _montar

from libracore import caja_router
from libracore.db import core
from libracore.db.clients import create_client

#: 3 × 1.500 = 4.500, una factura C (sin IVA), como la del e2e.
ITEMS = [{"description": "Servicio", "qty": 3, "unit_price": 1500.0}]


@pytest.fixture(params=["sqlite", "postgres"])
def client(request, tmp_path, monkeypatch):
    destino = None if request.param == "sqlite" else request.getfixturevalue("bases").nueva()
    yield _montar(tmp_path, monkeypatch, db_path=destino)
    core._db_path = None
    core._database_url = None


def _factura(client, **extra) -> dict:
    return _emitir(client, items=extra.pop("items", ITEMS), **extra)


def _cobrar(client, factura, *pagos, ref=""):
    return client.post(
        f"{API}/{factura['id']}/cobrar",
        json={"pagos": [{"medio_id": medio, "monto": monto, "referencia": ref} for medio, monto in pagos]},
    )


def _detalle(client, factura) -> dict:
    return client.get(f"{API}/{factura['id']}").json()


def _escrito(factura) -> dict:
    """Lo que hay en la base por esta factura: movimientos de ingreso (sin la marca de cuenta
    corriente) y su suma, y los abonos de cuenta corriente que hay en total."""
    with core.get_connection() as c:
        movs = c.execute(
            "SELECT monto FROM caja_movimientos WHERE factura_id=? AND tipo='ingreso'"
            " AND LOWER(medio_pago) NOT IN ('cuenta corriente','cuenta_corriente')", (factura["id"],),
        ).fetchall()
        abonos = c.execute("SELECT COUNT(*) FROM cc_pagos").fetchone()[0]
    return {"movimientos": len(movs), "suma": round(sum(m[0] for m in movs), 2), "abonos": abonos}


# ── El defecto medido ─────────────────────────────────────────────────────


def test_no_se_puede_cobrar_mas_de_lo_que_falta_el_caso_del_e2e(client):
    """4.500 con 1.000 cobrados y 3.500 pendientes: 9.999 se rechaza, y no queda nada de ese intento."""
    factura = _factura(client)
    assert factura["total"] == 4500.0
    assert _cobrar(client, factura, ("efectivo", 1000.0)).status_code == 200

    r = _cobrar(client, factura, ("efectivo", 9999.0))

    assert r.status_code == 409, r.text
    # El mensaje dice cuánto falta, en castellano y como se lee en la pantalla.
    assert "$ 3.500,00" in r.json()["detail"], r.json()
    assert "$ 9.999,00" in r.json()["detail"], r.json()
    assert _escrito(factura) == {"movimientos": 1, "suma": 1000.0, "abonos": 0}
    detalle = _detalle(client, factura)
    assert detalle["total_cobrado"] == 1000.0 and detalle["pendiente"] == 3500.0


def test_acepta_exactamente_lo_pendiente(client):
    factura = _factura(client)
    _cobrar(client, factura, ("efectivo", 1000.0))

    r = _cobrar(client, factura, ("transferencia", 3500.0))

    assert r.status_code == 200, r.text
    assert r.json()["pendiente"] == 0.0
    assert r.json()["total_cobrado"] == 4500.0


def test_acepta_un_cobro_parcial(client):
    factura = _factura(client)

    r = _cobrar(client, factura, ("efectivo", 2000.0))

    assert r.status_code == 200, r.text
    assert r.json()["pendiente"] == 2500.0
    assert _escrito(factura)["suma"] == 2000.0


def test_una_factura_ya_cobrada_no_admite_otro_cobro(client):
    factura = _factura(client)
    assert _cobrar(client, factura, ("efectivo", 4500.0)).status_code == 200

    r = _cobrar(client, factura, ("efectivo", 1.0))

    assert r.status_code == 409, r.text
    assert "ya está cobrada" in r.json()["detail"]
    assert _escrito(factura) == {"movimientos": 1, "suma": 4500.0, "abonos": 0}


def test_dos_cobros_sucesivos_que_juntos_superan_rechazan_el_segundo(client):
    """El segundo mira lo que dejó el primero: cada uno solo cabe, los dos juntos no."""
    factura = _factura(client)
    assert _cobrar(client, factura, ("efectivo", 3000.0)).status_code == 200

    r = _cobrar(client, factura, ("efectivo", 3000.0))

    assert r.status_code == 409, r.text
    assert "$ 1.500,00" in r.json()["detail"]
    assert _escrito(factura)["suma"] == 3000.0


# ── Varios medios en un mismo cobro ───────────────────────────────────────


def test_varios_medios_que_suman_de_mas_se_rechazan_enteros(client):
    """Cada medio cabe solo (2.000 ≤ 3.500) y la suma no (4.000): lo que no puede superar lo
    pendiente es la suma, y no se escribe ni el primero."""
    factura = _factura(client)
    _cobrar(client, factura, ("efectivo", 1000.0))

    r = _cobrar(client, factura, ("efectivo", 2000.0), ("transferencia", 2000.0))

    assert r.status_code == 409, r.text
    assert "$ 4.000,00" in r.json()["detail"] and "$ 3.500,00" in r.json()["detail"]
    assert _escrito(factura) == {"movimientos": 1, "suma": 1000.0, "abonos": 0}


def test_varios_medios_que_suman_exacto_se_aceptan(client):
    factura = _factura(client)
    _cobrar(client, factura, ("efectivo", 1000.0))

    r = _cobrar(client, factura, ("efectivo", 1500.0), ("transferencia", 2000.0))

    assert r.status_code == 200, r.text
    assert r.json()["pendiente"] == 0.0
    assert _escrito(factura)["movimientos"] == 3


def test_las_filas_vacias_del_formulario_no_cuentan(client):
    factura = _factura(client)

    r = _cobrar(client, factura, ("efectivo", 4500.0), ("transferencia", 0), ("tarjeta_debito", None))

    assert r.status_code == 200, r.text
    assert _escrito(factura)["movimientos"] == 1


# ── Centavos ──────────────────────────────────────────────────────────────


def test_el_polvo_de_la_coma_flotante_no_rechaza_un_cobro_justo(client):
    """0,1 + 0,2 da 0,30000000000000004 en `float`: pagar justo los 0,30 de la factura no puede fallar."""
    factura = _factura(client, items=[{"description": "Cargo", "qty": 1, "unit_price": 0.3}])
    assert factura["total"] == 0.3

    r = _cobrar(client, factura, ("efectivo", 0.1), ("transferencia", 0.2))

    assert r.status_code == 200, r.text
    assert r.json()["pendiente"] == 0.0


def test_la_tolerancia_es_medio_centavo_y_no_mas(client):
    """3.500,004 se redondea al centavo (3.500,00) y entra; 3.500,01 es un centavo de más y no."""
    factura = _factura(client)
    _cobrar(client, factura, ("efectivo", 1000.0))

    de_mas = _cobrar(client, factura, ("efectivo", 3500.01))
    assert de_mas.status_code == 409, de_mas.text
    assert "$ 3.500,00" in de_mas.json()["detail"] and "$ 3.500,01" in de_mas.json()["detail"]
    assert _escrito(factura)["suma"] == 1000.0

    r = _cobrar(client, factura, ("efectivo", 3500.004))
    assert r.status_code == 200, r.text
    assert r.json()["pendiente"] == 0.0


# ── Cuenta corriente ──────────────────────────────────────────────────────


def test_el_cobro_de_mas_de_una_factura_a_credito_tampoco_toca_la_cuenta_corriente(client):
    """Una factura a crédito se cobra con el mismo tope, y el rechazo no deja un abono suelto."""
    create_client("Juan Perez", cuit_dni="20304050607")
    factura = _factura(client, condicion_venta="Cuenta Corriente")

    r = _cobrar(client, factura, ("efectivo", 5000.0))

    assert r.status_code == 409, r.text
    assert "$ 4.500,00" in r.json()["detail"]
    assert _escrito(factura) == {"movimientos": 0, "suma": 0.0, "abonos": 0}

    ok = _cobrar(client, factura, ("efectivo", 4500.0))
    assert ok.status_code == 200, ok.text
    assert _escrito(factura) == {"movimientos": 1, "suma": 4500.0, "abonos": 1}


def test_una_factura_anulada_no_se_cobra(client):
    factura = _factura(client)
    # Sin CAE se puede anular: el modo de prueba la autoriza al emitirla, así que se anula a mano.
    with core.get_connection() as c:
        c.execute("UPDATE facturas SET cae='', anulada_en='2026-10-16 10:00:00' WHERE id=?", (factura["id"],))

    r = _cobrar(client, factura, ("efectivo", 100.0))

    assert r.status_code == 409, r.text
    assert _escrito(factura)["movimientos"] == 0


# ── Las otras entradas ────────────────────────────────────────────────────


def test_el_movimiento_manual_de_caja_con_factura_tambien_tiene_el_tope(client):
    """`POST /api/caja` con `factura_id` es otro cobro de la misma factura (así lo cuenta `get_cobros_factura`)."""
    factura = _factura(client)
    _cobrar(client, factura, ("efectivo", 1000.0))
    app = FastAPI()
    app.include_router(caja_router.build_caja_router(usuario_actual=lambda: USUARIO))
    caja = TestClient(app)
    cuerpo = {"fecha": "2026-10-16", "tipo": "ingreso", "concepto": "Cobro manual", "factura_id": factura["id"],
              "medio_pago": "efectivo"}

    de_mas = caja.post("/api/caja", json={**cuerpo, "monto": 9999.0})
    assert de_mas.status_code == 409, de_mas.text
    assert "$ 3.500,00" in de_mas.json()["detail"]
    assert _escrito(factura)["suma"] == 1000.0

    justo = caja.post("/api/caja", json={**cuerpo, "monto": 3500.0})
    assert justo.status_code == 200, justo.text
    assert _escrito(factura)["suma"] == 4500.0

    # Lo que no es un cobro de la factura no cambia: un egreso, un movimiento sin factura, y la factura inexistente.
    assert caja.post("/api/caja", json={**cuerpo, "tipo": "egreso", "monto": 50.0}).status_code == 200
    assert caja.post("/api/caja", json={**cuerpo, "factura_id": None, "monto": 50.0}).status_code == 200
    assert caja.post("/api/caja", json={**cuerpo, "factura_id": 99999, "monto": 50.0}).status_code == 404


def test_el_reemplazo_del_producto_recibe_el_cobro_ya_validado(tmp_path, monkeypatch):
    """Un producto que reemplaza la escritura (`registrar_cobro=`) no se saltea el tope."""
    llamadas = []

    def registrar(factura, pagos, *, fecha, caja_id, usuario):
        llamadas.append(pagos)

    client = _montar(tmp_path, monkeypatch, registrar_cobro=registrar)
    factura = _factura(client)

    de_mas = _cobrar(client, factura, ("efectivo", 9999.0))
    assert de_mas.status_code == 409, de_mas.text
    assert llamadas == [], "el reemplazo no tenía que enterarse de un cobro rechazado"

    justo = _cobrar(client, factura, ("efectivo", 4500.0))
    assert justo.status_code == 200, justo.text
    assert len(llamadas) == 1


def test_la_pantalla_sugiere_lo_que_el_tope_acepta(client):
    """El diálogo se llena con el `pendiente` del detalle: tiene que ser un cobro aceptado."""
    factura = _factura(client)
    _cobrar(client, factura, ("efectivo", 1234.56))

    pendiente = _detalle(client, factura)["pendiente"]
    assert pendiente == 3265.44

    assert _cobrar(client, factura, ("efectivo", pendiente)).status_code == 200


# ── Concurrencia ──────────────────────────────────────────────────────────


def test_dos_cobros_simultaneos_que_juntos_superan_solo_deja_pasar_uno(client):
    """Cada cobro (4.000) cabe solo en los 4.500 de la factura; los seis juntos no. Seis pedidos a la vez,
    una sola vez entra: sin el candado de la fila, los seis miraban 4.500 pendientes y los seis entraban."""
    factura = _factura(client)
    estados = []
    salida = threading.Barrier(6)

    def pedir():
        salida.wait()
        estados.append(_cobrar(client, factura, ("efectivo", 4000.0)).status_code)

    hilos = [threading.Thread(target=pedir) for _ in range(6)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert sorted(estados) == [200, 409, 409, 409, 409, 409], estados
    assert _escrito(factura) == {"movimientos": 1, "suma": 4000.0, "abonos": 0}


def test_el_candado_cubre_el_hueco_entre_mirar_y_escribir(client):
    """Con la lectura lenta, sin candado el hueco entre «miré» y «escribí» es enorme: ninguno de los
    seis hilos vería la escritura de otro. Acá el candado se toma antes de leer, así que se hacen cola."""
    from libracore import cobros

    factura = _factura(client)
    fila = {"id": factura["id"], "tipo": 11, "punto_venta": 1, "numero": factura["numero"],
            "cliente_cuit": "20304050607", "cliente_razon": "Juan Perez", "condicion_venta": "Contado",
            "total": 4500.0}

    def lento(factura_id, conn):
        estado = cobros.estado_de_cobro(factura_id, conn=conn)
        time.sleep(0.05)
        return estado

    resultados = []
    salida = threading.Barrier(6)

    def cobrar():
        salida.wait()
        try:
            cobros.registrar_cobro_factura(fila, [{"medio_id": "efectivo", "monto": 4000.0}], leer_estado=lento)
            resultados.append("ok")
        except cobros.CobroNoPermitido as exc:
            resultados.append(exc.codigo)

    hilos = [threading.Thread(target=cobrar) for _ in range(6)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert sorted(resultados) == ["excede"] * 5 + ["ok"], resultados
    assert _escrito(factura)["suma"] == 4000.0


def test_los_cobros_de_siempre_siguen_pasando_por_el_router(client):
    """El control de todo lo de arriba: un tope que rechazara todo pasaría los tests de rechazo."""
    factura = _factura(client)

    r = client.post(f"{API}/{factura['id']}/cobrar", json={"pagos": [{"medio_id": "efectivo", "monto": 4500.0}]},
                    headers=ADMIN)

    assert r.status_code == 200, r.text
    assert r.json()["pendiente"] == 0.0
