"""Caja, cajas y turnos como factories (P9-M3): el contrato que
`tests/test_ventas_caja.py` y `tests/test_punto_de_venta_por_caja.py` de los
productos ya fijaban, sin el gate por módulo."""

from __future__ import annotations

import datetime

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracore.caja_router import (
    OpcionesCajas,
    build_caja_router,
    build_cajas_router,
    build_turnos_router,
)
from libracore.db import core
from libracore.db import turnos as db_turnos
from libracore.db.schema import init_core_schema

HOY = datetime.date.today().isoformat()
USUARIO = {"id": 7, "username": "cajero", "role": "admin"}


@pytest.fixture
def entorno(tmp_path):
    core.configure(db_path=str(tmp_path / "caja.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    for uid, nombre, rol in ((7, "cajero", "admin"), (8, "otro", "operador")):
        conn.execute(
            "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?,?,?,?,?)",
            (uid, nombre, nombre.title(), "x", rol))
    conn.commit()
    conn.close()
    yield
    core._db_path = None


def _app(entorno, resumen_turno=None, cerrar_turno=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_caja_router(usuario_actual=lambda: USUARIO))
    app.include_router(build_cajas_router())
    kw = {}
    if resumen_turno:
        kw["resumen_turno"] = resumen_turno
    if cerrar_turno:
        kw["cerrar_turno"] = cerrar_turno
    app.include_router(build_turnos_router(usuario_actual=lambda: USUARIO, **kw))
    return TestClient(app)


@pytest.fixture
def client(entorno):
    USUARIO.update({"id": 7, "role": "admin"})
    return _app(entorno)


# ── Caja ─────────────────────────────────────────────────────────────────


def test_movimientos_de_caja(client):
    r = client.post("/api/caja", json={"fecha": HOY, "tipo": "ingreso", "concepto": "Venta suelta", "monto": 100.0, "medio_pago": "efectivo"})
    assert r.status_code == 200 and r.json()["id"]
    mid = r.json()["id"]
    client.post("/api/caja", json={"fecha": HOY, "tipo": "egreso", "concepto": "Hielo", "monto": 30.0, "medio_pago": "efectivo"})
    listado = client.get(f"/api/caja?desde={HOY}&hasta={HOY}").json()
    assert len(listado["movimientos"]) == 2
    assert listado["resumen"]["ingresos"] == 100.0 and listado["resumen"]["egresos"] == 30.0
    # Sin fechas, el mes en curso.
    assert len(client.get("/api/caja").json()["movimientos"]) == 2
    # El usuario queda en el movimiento.
    assert all(m["usuario_id"] == USUARIO["id"] for m in listado["movimientos"])
    # Anular: la fila queda, sale de los totales.
    assert client.delete(f"/api/caja/{mid}").json() == {"ok": True}
    listado = client.get(f"/api/caja?desde={HOY}&hasta={HOY}").json()
    assert len(listado["movimientos"]) == 2 and listado["resumen"]["ingresos"] == 0.0


def test_anular_marca_la_fila_y_no_resta_dos_veces(client):
    """Anular no borra: la fila queda marcada y el arqueo deja de contarla.

    Viene de `tests/test_anular_movimiento_caja.py` de Contalibra y Restolibra,
    que lo tenian escrito dos veces contra este mismo router. De ahi quedan
    las dos mitades que el test de arriba no fija:

    - la marca: `anulado == 1` en la fila. Una lista que esconde los anulados
      no se distingue de una que los borra, que es lo que se vino a arreglar
      el 2026-08-28;
    - la idempotencia: un doble click en el boton no puede descontar dos
      veces, ni --lo que hace un `anulado = 1 - anulado`-- volver a sumar lo
      que se dio de baja.

    El otro movimiento es el control del total: sin el, "ingresos == 0" pasaria
    igual con un resumen que no suma nada.
    """
    mid = client.post("/api/caja", json={"fecha": HOY, "tipo": "ingreso", "concepto": "Se anula", "monto": 3000.0, "medio_pago": "efectivo"}).json()["id"]
    client.post("/api/caja", json={"fecha": HOY, "tipo": "ingreso", "concepto": "Queda", "monto": 1000.0, "medio_pago": "efectivo"})
    assert client.get(f"/api/caja?desde={HOY}&hasta={HOY}").json()["resumen"]["ingresos"] == 4000.0, "el control del total"

    client.delete(f"/api/caja/{mid}")
    client.delete(f"/api/caja/{mid}")

    listado = client.get(f"/api/caja?desde={HOY}&hasta={HOY}").json()
    filas = [m for m in listado["movimientos"] if m["id"] == mid]
    assert len(filas) == 1, "el movimiento anulado tiene que seguir en la lista, una sola vez"
    assert filas[0]["anulado"] == 1
    assert listado["resumen"]["ingresos"] == 1000.0, "el arqueo cuenta lo que hay en el cajon, sin el anulado"


@pytest.mark.parametrize("cuerpo", [
    {"fecha": HOY, "tipo": "ingreso", "concepto": "  ", "monto": 10},
    {"fecha": HOY, "tipo": "prestamo", "concepto": "X", "monto": 10},
    {"fecha": HOY, "tipo": "ingreso", "concepto": "X", "monto": 0},
])
def test_movimiento_invalido_422(client, cuerpo):
    assert client.post("/api/caja", json=cuerpo).status_code == 422


# ── Cajas ────────────────────────────────────────────────────────────────


def test_cajas_crud_y_punto_de_venta(client):
    assert any(m["id"] == "efectivo" for m in client.get("/api/cajas/medios-disponibles").json())
    r = client.post("/api/cajas", json={"nombre": "POS 1", "descripcion": "", "medios_pago": ["efectivo"]})
    assert r.status_code == 200 and r.json()["punto_venta"] is None
    uno = r.json()
    r = client.post("/api/cajas", json={"nombre": "POS 2", "medios_pago": ["efectivo"], "punto_venta": 4})
    assert r.status_code == 200 and r.json()["punto_venta"] == 4
    dos = r.json()
    # El mismo punto de venta en otra caja: 409, nombrando cuál lo tiene.
    r = client.post("/api/cajas", json={"nombre": "POS 3", "medios_pago": [], "punto_venta": 4})
    assert r.status_code == 409 and "POS 2" in r.json()["detail"]
    r = client.put(f"/api/cajas/{uno['id']}", json={"nombre": "POS 1", "medios_pago": [], "punto_venta": 4})
    assert r.status_code == 409
    r = client.put(f"/api/cajas/{dos['id']}", json={"nombre": "POS 2 renombrado", "medios_pago": ["efectivo"], "punto_venta": 6, "activo": True})
    assert r.status_code == 200 and r.json()["punto_venta"] == 6 and r.json()["nombre"] == "POS 2 renombrado"
    r = client.put(f"/api/cajas/{dos['id']}", json={"nombre": "POS 2", "medios_pago": []})
    assert r.json()["punto_venta"] is None
    assert client.put("/api/cajas/999", json={"nombre": "X"}).status_code == 404
    assert client.put(f"/api/cajas/{dos['id']}", json={"nombre": " "}).status_code == 422
    assert client.post("/api/cajas", json={"nombre": " "}).status_code == 422
    # Más la "Caja Principal" que siembra el schema del core.
    assert len(client.get("/api/cajas").json()) == 3
    assert client.post(f"/api/cajas/{dos['id']}/set-default").json()["es_default"]
    assert client.post("/api/cajas/999/set-default").status_code == 404
    # La default no se borra; la otra sí.
    assert client.delete(f"/api/cajas/{dos['id']}").status_code == 422
    assert client.delete(f"/api/cajas/{uno['id']}").json() == {"ok": True}
    assert client.delete("/api/cajas/999").status_code == 404


def test_cajas_mp_pos_id(client):
    """El POS de MercadoPago vive en la caja; el external_id debe ser alfanumérico."""
    r = client.post("/api/cajas", json={
        "nombre": "Caja QR", "medios_pago": ["efectivo"],
        "mp_pos_id": "BIOKOCAJA01",
    })
    assert r.status_code == 200
    assert r.json()["mp_pos_id"] == "BIOKOCAJA01"

    # Guiones y espacios no están permitidos por MercadoPago.
    r = client.post("/api/cajas", json={
        "nombre": "Caja QR mala", "medios_pago": [],
        "mp_pos_id": "BIOKO-CAJA-01",
    })
    assert r.status_code == 422 and "alfanumérico" in r.json()["detail"]

    cid = client.get("/api/cajas").json()[-1]["id"]
    r = client.put(f"/api/cajas/{cid}", json={
        "nombre": "Caja QR", "medios_pago": ["efectivo"],
        "mp_pos_id": "BIOKOCAJA02", "activo": True,
    })
    assert r.status_code == 200 and r.json()["mp_pos_id"] == "BIOKOCAJA02"

    # Limpiar el POS deja la caja sin QR.
    r = client.put(f"/api/cajas/{cid}", json={
        "nombre": "Caja QR", "medios_pago": ["efectivo"],
        "mp_pos_id": "", "activo": True,
    })
    assert r.status_code == 200 and r.json()["mp_pos_id"] is None


# ── Turnos ───────────────────────────────────────────────────────────────


def test_turnos_abrir_ver_cerrar(client):
    assert client.get("/api/turnos").json() == {"turnos": [], "turno_activo": None}
    abierto = client.post("/api/turnos/abrir", json={"monto_inicial": 1000.0, "notas": " apertura "})
    assert abierto.status_code == 200 and abierto.json()["estado"] == "abierto"
    tid = abierto.json()["id"]
    assert abierto.json()["notas"] == "apertura"
    # Un segundo abrir devuelve el mismo turno.
    assert client.post("/api/turnos/abrir", json={"monto_inicial": 5.0}).json()["id"] == tid
    assert client.get("/api/turnos").json()["turno_activo"]["id"] == tid
    detalle = client.get(f"/api/turnos/{tid}").json()
    assert detalle["turno"]["id"] == tid and "pagos_por_medio" in detalle["resumen"]
    cerrado = client.post(f"/api/turnos/{tid}/cerrar", json={"monto_declarado": 990.0, "notas": "faltan 10"})
    assert cerrado.status_code == 200 and cerrado.json()["estado"] == "cerrado"
    assert cerrado.json()["monto_esperado_cierre"] == 1000.0 and cerrado.json()["monto_declarado_cierre"] == 990.0
    assert client.post(f"/api/turnos/{tid}/cerrar", json={"monto_declarado": 1}).status_code == 422
    assert client.get("/api/turnos/999").status_code == 404
    assert client.post("/api/turnos/999/cerrar", json={}).status_code == 404


def test_turno_sobre_una_caja(client):
    caja = client.post("/api/cajas", json={"nombre": "POS 2", "medios_pago": [], "punto_venta": 9}).json()
    abierto = client.post("/api/turnos/abrir", json={"monto_inicial": 0, "caja_id": caja["id"]}).json()
    assert abierto["caja_id"] == caja["id"]
    from libracore.db.caja import resolver_punto_venta

    assert resolver_punto_venta(USUARIO["id"]) == 9


def test_el_operador_solo_ve_los_suyos(entorno):
    client = _app(entorno)
    USUARIO.update({"id": 7, "role": "admin"})
    ajeno = client.post("/api/turnos/abrir", json={"monto_inicial": 1}).json()["id"]
    USUARIO.update({"id": 8, "role": "operador"})
    assert client.get("/api/turnos").json()["turnos"] == []
    assert client.get(f"/api/turnos/{ajeno}").status_code == 403
    assert client.post(f"/api/turnos/{ajeno}/cerrar", json={}).status_code == 403
    propio = client.post("/api/turnos/abrir", json={"monto_inicial": 2}).json()["id"]
    assert [t["id"] for t in client.get("/api/turnos").json()["turnos"]] == [propio]
    USUARIO.update({"id": 7, "role": "admin"})
    assert len(client.get("/api/turnos").json()["turnos"]) == 2


def test_el_resumen_y_el_cierre_son_del_producto(entorno):
    """Un producto cuyas ventas no viven en `ventas` pasa los suyos."""
    llamadas = []

    def resumen(tid):
        llamadas.append(("resumen", tid))
        return {"ventas": [], "pagos_por_medio": {"efectivo": 250.0}, "total_ventas": 250.0, "efectivo_ventas": 250.0}

    def cerrar(tid, monto, notas):
        llamadas.append(("cerrar", tid, monto, notas))
        with core.get_connection() as conn:
            conn.execute("UPDATE turnos_caja SET estado='cerrado', monto_esperado_cierre=250 WHERE id=?", (tid,))

    USUARIO.update({"id": 7, "role": "admin"})
    client = _app(entorno, resumen_turno=resumen, cerrar_turno=cerrar)
    tid = client.post("/api/turnos/abrir", json={"monto_inicial": 0}).json()["id"]
    assert client.get(f"/api/turnos/{tid}").json()["resumen"]["efectivo_ventas"] == 250.0
    r = client.post(f"/api/turnos/{tid}/cerrar", json={"monto_declarado": 250, "notas": "ok"})
    assert r.json()["estado"] == "cerrado" and r.json()["monto_esperado_cierre"] == 250.0
    assert llamadas == [("resumen", tid), ("cerrar", tid, 250.0, "ok")]


# ── Variantes de un producto con sucursales (v1.112.0) ────────────────────


def _app_con_ganchos(entorno, *, opciones=None, **turnos) -> TestClient:
    app = FastAPI()
    app.include_router(build_cajas_router(opciones=opciones))
    app.include_router(build_turnos_router(usuario_actual=lambda: USUARIO, **turnos))
    return TestClient(app)


def test_sin_opciones_las_cajas_no_cambian_y_traen_si_tienen_turno(client):
    """El default es Contalibra: sin `opciones` no se llama a ningún gancho. Lo
    único nuevo en la respuesta es `tiene_turno_abierto`."""
    caja = client.post("/api/cajas", json={"nombre": "POS 2", "medios_pago": []}).json()
    assert caja["tiene_turno_abierto"] is False and caja["sucursal_id"] is None
    client.post("/api/turnos/abrir", json={"monto_inicial": 0, "caja_id": caja["id"]})
    por_id = {c["id"]: c for c in client.get("/api/cajas").json()}
    assert por_id[caja["id"]]["tiene_turno_abierto"] is True
    assert all(not c["tiene_turno_abierto"] for i, c in por_id.items() if i != caja["id"])


def test_la_caja_se_crea_en_una_sucursal_y_se_filtra(client):
    a = client.post("/api/cajas", json={"nombre": "A", "sucursal_id": 1}).json()
    client.post("/api/cajas", json={"nombre": "B", "sucursal_id": 2})
    assert a["sucursal_id"] == 1
    assert [c["nombre"] for c in client.get("/api/cajas?sucursal_id=1").json()] == ["A"]
    # Editar no la muda de sede.
    r = client.put(f"/api/cajas/{a['id']}", json={"nombre": "A", "sucursal_id": 2})
    assert r.json()["sucursal_id"] == 1


def test_los_ganchos_de_las_cajas(entorno):
    llamadas = []

    def validar_alta(payload):
        llamadas.append(("alta", payload.sucursal_id))
        if payload.sucursal_id == 99:
            raise HTTPException(422, "No existe esa sucursal.")

    def validar_edicion(payload, actual):
        llamadas.append(("edicion", actual["nombre"], payload.activo))
        if not payload.activo and actual["nombre"] == "Única":
            raise HTTPException(409, "La sucursal necesita una caja activa.")

    def al_desactivar(actual):
        llamadas.append(("baja", actual["nombre"]))

    def predeterminar(cid):
        llamadas.append(("predeterminar", cid))
        with core.get_connection() as conn:
            conn.execute("UPDATE cajas SET es_default=1 WHERE id=?", (cid,))

    client = _app_con_ganchos(entorno, opciones=OpcionesCajas(
        validar_alta=validar_alta, validar_edicion=validar_edicion, al_desactivar=al_desactivar,
        predeterminar=predeterminar, enriquecer=lambda c: {**c, "sucursal_nombre": f"S{c['sucursal_id']}"},
    ))
    assert client.post("/api/cajas", json={"nombre": "X", "sucursal_id": 99}).status_code == 422
    unica = client.post("/api/cajas", json={"nombre": "Única", "sucursal_id": 1}).json()
    otra = client.post("/api/cajas", json={"nombre": "Otra", "sucursal_id": 1}).json()
    assert unica["sucursal_nombre"] == "S1"
    assert client.get("/api/cajas").json()[0]["sucursal_nombre"].startswith("S")
    # La regla del producto frena la baja y NO toca la caja.
    r = client.put(f"/api/cajas/{unica['id']}", json={"nombre": "Única", "activo": False})
    assert r.status_code == 409
    assert client.get("/api/cajas").json() and all(
        c["activo"] for c in client.get("/api/cajas").json() if c["id"] == unica["id"])
    # Una baja permitida avisa a `al_desactivar`; editar sin bajar, no.
    assert client.put(f"/api/cajas/{otra['id']}", json={"nombre": "Otra", "activo": True}).status_code == 200
    assert ("baja", "Otra") not in llamadas
    assert client.put(f"/api/cajas/{otra['id']}", json={"nombre": "Otra", "activo": False}).json()["activo"] == 0
    assert ("baja", "Otra") in llamadas
    # Ya inactiva, volver a guardarla inactiva no es una baja nueva.
    llamadas.clear()
    client.put(f"/api/cajas/{otra['id']}", json={"nombre": "Otra", "activo": False})
    assert ("baja", "Otra") not in llamadas
    # La predeterminada la decide el producto.
    assert client.post(f"/api/cajas/{unica['id']}/set-default").json()["es_default"]
    assert ("predeterminar", unica["id"]) in llamadas


def test_los_ganchos_de_los_turnos(entorno):
    def validar(payload, user):
        if payload.caja_id is None:
            raise HTTPException(422, "La caja es obligatoria.")
        if db_turnos.get_turno_activo(user["id"]):
            raise HTTPException(409, "ya tenés un turno abierto")

    client = _app_con_ganchos(
        entorno, validar_apertura=validar,
        enriquecer=lambda t: {**t, "caja": {"id": t["caja_id"]} if t.get("caja_id") else None},
    )
    USUARIO.update({"id": 7, "role": "admin"})
    assert client.post("/api/turnos/abrir", json={"monto_inicial": 0}).status_code == 422
    caja = client.post("/api/cajas", json={"nombre": "C"}).json()
    abierto = client.post("/api/turnos/abrir", json={"monto_inicial": 5, "caja_id": caja["id"]})
    assert abierto.status_code == 200 and abierto.json()["caja"] == {"id": caja["id"]}
    # Con el gancho, abrir de nuevo ya no devuelve el mismo turno: lo decide el producto.
    assert client.post("/api/turnos/abrir", json={"monto_inicial": 0, "caja_id": caja["id"]}).status_code == 409
    # `/actual` trae el turno abierto de quien pregunta, enriquecido, con su resumen.
    actual = client.get("/api/turnos/actual").json()
    assert actual["turno"]["id"] == abierto.json()["id"] and actual["turno"]["caja"] == {"id": caja["id"]}
    assert "pagos_por_medio" in actual["resumen"]
    # Y el listado, el detalle y el cierre también pasan por `enriquecer`.
    assert client.get("/api/turnos").json()["turno_activo"]["caja"] == {"id": caja["id"]}
    assert client.get(f"/api/turnos/{abierto.json()['id']}").json()["turno"]["caja"] == {"id": caja["id"]}
    cerrado = client.post(f"/api/turnos/{abierto.json()['id']}/cerrar", json={"monto_declarado": 5})
    assert cerrado.json()["estado"] == "cerrado" and cerrado.json()["caja"] == {"id": caja["id"]}
    assert client.get("/api/turnos/actual").json() == {"turno": None}


def test_abrir_en_un_dia_cerrado_es_un_409(client):
    """`create_turno` levanta `DiaCerradoError`; sin traducir era un 500."""
    from libracore.db import cierre_diario

    caja = client.post("/api/cajas", json={"nombre": "C", "sucursal_id": 3}).json()
    with core.get_connection() as conn:
        cierre_diario.crear_tablas(conn)
    cierre_diario.cerrar_dia(usuario_id=7, sucursal_id=3)  # hoy, hora AR
    r = client.post("/api/turnos/abrir", json={"monto_inicial": 0, "caja_id": caja["id"]})
    assert r.status_code == 409


def test_autorizar_escritura_protege_las_escrituras_y_no_la_lectura(entorno):
    def solo_admin():
        if USUARIO["role"] != "admin":
            raise HTTPException(403, "Sólo el administrador.")

    client = _app_con_ganchos(entorno, opciones=OpcionesCajas(autorizar_escritura=Depends(solo_admin)))
    USUARIO.update({"id": 7, "role": "admin"})
    caja = client.post("/api/cajas", json={"nombre": "A"}).json()
    USUARIO.update({"id": 8, "role": "operador"})
    assert client.get("/api/cajas").status_code == 200
    assert client.post("/api/cajas", json={"nombre": "B"}).status_code == 403
    assert client.put(f"/api/cajas/{caja['id']}", json={"nombre": "A2"}).status_code == 403
    assert client.post(f"/api/cajas/{caja['id']}/set-default").status_code == 403
    assert client.delete(f"/api/cajas/{caja['id']}").status_code == 403
    USUARIO.update({"id": 7, "role": "admin"})
    assert client.put(f"/api/cajas/{caja['id']}", json={"nombre": "A2"}).status_code == 200
