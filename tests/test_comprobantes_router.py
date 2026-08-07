"""La capa HTTP de la bandeja, montada como la montaría un producto.

Los dos routers no se protegen igual —uno lo usa otro sistema con un token, el
otro una persona con rol de admin— y eso es lo primero que se prueba acá: que
el gate del producto llegue a cada uno, y que el de ingesta no abra la bandeja
ni el de bandeja permita depositar.
"""
import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore import comprobantes_router as cr
from libracore.db import comprobantes_pendientes as cp
from libracore.db import core
from libracore.db.schema import init_core_schema

TOKEN = "token-de-servicio-de-prueba"


# `comprobantes_pendientes.factura_id` es una FK real y la base la hace cumplir,
# así que marcar contra un id inventado revienta en el UPDATE. Esta suite emite
# una factura de mentira pero **existente**.
FACTURA_ID = 1


@pytest.fixture
def client(tmp_path):
    core.configure(db_path=str(tmp_path / "bandeja.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.execute(
        "INSERT INTO facturas (tipo, punto_venta, numero, fecha, items, "
        "subtotal, iva_amount, total) VALUES (11, 1, 1, '2026-09-05', '[]', "
        "1000.0, 0.0, 1000.0)"
    )
    conn.commit()
    conn.close()

    def gate_servicio(x_internal_auth: str = Header(default="")):
        # Hace de `json_api_require_admin_o_servicio` de libraauth: lo que
        # importa es que el gate lo pone el producto, no este paquete.
        if x_internal_auth != TOKEN:
            raise HTTPException(401, "token de servicio invalido")

    def gate_admin(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    app = FastAPI()
    app.include_router(cr.build_comprobantes_ingesta_router(),
                       dependencies=[Depends(gate_servicio)])
    app.include_router(
        cr.build_comprobantes_bandeja_router(
            usuario_actual=lambda request: request.headers.get("x-usuario", ""),
        ),
        dependencies=[Depends(gate_admin)],
    )
    yield TestClient(app)
    core._db_path = None


SERVICIO = {"x-internal-auth": TOKEN}
ADMIN = {"x-rol": "admin", "x-usuario": "mariano"}


def _payload(**kwargs):
    base = dict(
        origen_producto="libradesk",
        origen_instancia="compulibra",
        origen_tipo="cuota_contrato",
        origen_id="42",
        cliente_razon="Ferretería San Martín",
        cliente_cuit="30-71234567-9",
        periodo_desde="2026-08-01",
        periodo_hasta="2026-08-31",
        items=[{"description": "Alquiler impresora — agosto", "qty": 1,
                "unit_price": 45000.0, "iva_rate": 0.21}],
    )
    base.update(kwargs)
    return base


def _depositar(client, **kwargs):
    return client.post("/api/comprobantes-pendientes", json=_payload(**kwargs),
                       headers=SERVICIO)


# ── Los gates ────────────────────────────────────────────────────────────────

def test_depositar_sin_token_de_servicio_da_401(client):
    r = client.post("/api/comprobantes-pendientes", json=_payload())
    assert r.status_code == 401


def test_el_token_de_servicio_no_abre_la_bandeja(client):
    """Control negativo: el que deposita no puede leer lo que hay adentro ni
    resolverlo. Si esto pasara a 200, el token de un producto alcanzaría para
    descartar la facturación de otro."""
    assert client.get("/api/comprobantes-pendientes", headers=SERVICIO).status_code == 403


def test_el_rol_admin_no_alcanza_para_depositar(client):
    assert client.post("/api/comprobantes-pendientes", json=_payload(),
                       headers=ADMIN).status_code == 401


def test_la_bandeja_sin_rol_da_403(client):
    assert client.get("/api/comprobantes-pendientes").status_code == 403


# ── Ingesta ──────────────────────────────────────────────────────────────────

def test_depositar_devuelve_201_y_creado(client):
    r = _depositar(client)
    assert r.status_code == 201
    assert r.json()["creado"] is True


def test_reenviar_lo_mismo_no_duplica_y_dice_creado_false(client):
    primero = _depositar(client).json()
    segundo = _depositar(client).json()
    assert segundo["id"] == primero["id"]
    assert segundo["creado"] is False

    bandeja = client.get("/api/comprobantes-pendientes", headers=ADMIN).json()
    assert bandeja["total_pendientes"] == 1


def test_reenviar_algo_ya_facturado_da_409(client):
    comprobante_id = _depositar(client).json()["id"]
    client.post("/api/comprobantes-pendientes/marcar-facturado",
                json={"ids": [comprobante_id], "factura_id": FACTURA_ID}, headers=ADMIN)

    r = _depositar(client)
    assert r.status_code == 409
    assert "facturado" in r.json()["detail"]


def test_un_origen_tipo_desconocido_da_422(client):
    assert _depositar(client, origen_tipo="lo_que_sea").status_code == 422


# ── Bandeja ──────────────────────────────────────────────────────────────────

def test_la_bandeja_separa_pendientes_de_resueltos(client):
    facturado = _depositar(client, origen_id="1").json()["id"]
    _depositar(client, origen_id="2")
    client.post("/api/comprobantes-pendientes/marcar-facturado",
                json={"ids": [facturado], "factura_id": FACTURA_ID}, headers=ADMIN)

    bandeja = client.get("/api/comprobantes-pendientes", headers=ADMIN).json()
    assert bandeja["total_pendientes"] == 1
    assert len(bandeja["pendientes"]) == 1
    assert len(bandeja["facturados"]) == 1


def test_descartar_guarda_motivo_y_quien(client):
    comprobante_id = _depositar(client).json()["id"]
    r = client.post(f"/api/comprobantes-pendientes/{comprobante_id}/descartar",
                    json={"motivo": "se cobró por fuera"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["estado"] == "descartado"
    assert r.json()["motivo_descarte"] == "se cobró por fuera"
    assert r.json()["resuelto_por"] == "mariano"


def test_descartar_dos_veces_da_409(client):
    comprobante_id = _depositar(client).json()["id"]
    url = f"/api/comprobantes-pendientes/{comprobante_id}/descartar"
    client.post(url, json={"motivo": ""}, headers=ADMIN)
    assert client.post(url, json={"motivo": ""}, headers=ADMIN).status_code == 409


def test_descartar_algo_que_no_existe_da_404(client):
    assert client.post("/api/comprobantes-pendientes/999/descartar",
                       json={"motivo": ""}, headers=ADMIN).status_code == 404


# ── Facturar ─────────────────────────────────────────────────────────────────

def test_la_ruta_estatica_no_la_come_el_parametro(client):
    """`/facturar-prefill` va declarada antes que `/{comprobante_id}`. Si el
    orden se invierte, esto devuelve 422 de tipo en vez de llegar al endpoint,
    y es un fallo que sólo se ve corriendo la ruta."""
    comprobante_id = _depositar(client).json()["id"]
    r = client.post("/api/comprobantes-pendientes/facturar-prefill",
                    json={"ids": [comprobante_id]}, headers=ADMIN)
    assert r.status_code == 200


def test_el_prefill_agrupa_los_items_y_el_periodo(client):
    uno = _depositar(client, origen_id="1", periodo_desde="2026-07-01",
                     periodo_hasta="2026-07-31").json()["id"]
    dos = _depositar(client, origen_id="2").json()["id"]

    prefill = client.post("/api/comprobantes-pendientes/facturar-prefill",
                          json={"ids": [uno, dos]}, headers=ADMIN).json()
    assert len(prefill["items"]) == 2
    assert prefill["fch_serv_desde"] == "2026-07-01"
    assert prefill["fch_serv_hasta"] == "2026-08-31"
    assert prefill["comprobantes_ids"] == [uno, dos]


def test_el_prefill_no_cambia_ningun_estado(client):
    """Es la mitad de la decisión de "borrador + confirmación humana": mirar el
    formulario no factura nada. El pendiente se cierra recién cuando el CAE
    volvió."""
    comprobante_id = _depositar(client).json()["id"]
    client.post("/api/comprobantes-pendientes/facturar-prefill",
                json={"ids": [comprobante_id]}, headers=ADMIN)

    assert client.get("/api/comprobantes-pendientes",
                      headers=ADMIN).json()["total_pendientes"] == 1


def test_el_prefill_de_dos_clientes_distintos_da_422(client):
    uno = _depositar(client, origen_id="1").json()["id"]
    dos = _depositar(client, origen_id="2", cliente_cuit="27-99999999-4",
                     cliente_razon="Otra SRL").json()["id"]

    r = client.post("/api/comprobantes-pendientes/facturar-prefill",
                    json={"ids": [uno, dos]}, headers=ADMIN)
    assert r.status_code == 422
    assert "clientes distintos" in r.json()["detail"]


def test_el_prefill_de_algo_ya_resuelto_da_409(client):
    comprobante_id = _depositar(client).json()["id"]
    client.post(f"/api/comprobantes-pendientes/{comprobante_id}/descartar",
                json={"motivo": ""}, headers=ADMIN)

    r = client.post("/api/comprobantes-pendientes/facturar-prefill",
                    json={"ids": [comprobante_id]}, headers=ADMIN)
    assert r.status_code == 409


def test_el_prefill_de_un_id_inexistente_da_404(client):
    r = client.post("/api/comprobantes-pendientes/facturar-prefill",
                    json={"ids": [999]}, headers=ADMIN)
    assert r.status_code == 404


def test_marcar_facturado_informa_lo_que_no_pudo_marcar(client):
    """No falla entero a propósito: para cuando esto se llama la factura ya
    tiene CAE, así que un error acá no desharía nada."""
    uno = _depositar(client, origen_id="1").json()["id"]
    dos = _depositar(client, origen_id="2").json()["id"]
    client.post(f"/api/comprobantes-pendientes/{dos}/descartar",
                json={"motivo": ""}, headers=ADMIN)

    r = client.post("/api/comprobantes-pendientes/marcar-facturado",
                    json={"ids": [uno, dos], "factura_id": FACTURA_ID}, headers=ADMIN)
    assert r.json() == {"marcados": [uno], "ya_resueltos": [dos]}
    assert cp.get_comprobante(uno)["factura_id"] == FACTURA_ID
    assert cp.get_comprobante(uno)["resuelto_por"] == "mariano"
