"""`build_cierre_diario_router`: preview, cerrar, listar, detalle y tickets.

Mismo armado que `tests/test_caja_router.py`: schema por `init_core_schema()`
más `cierre_diario.crear_tablas()`, usuario fijo inyectado por
`usuario_actual`.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracore.caja_router import build_cierre_diario_router
from libracore.db import cierre_diario as cd
from libracore.db import core
from libracore.db.schema import init_core_schema

ADMIN = {"id": 1, "username": "admin", "role": "admin"}
CAJERO = {"id": 2, "username": "cajero", "role": "cajero"}
_USUARIO_ACTUAL = {"actual": ADMIN}


@pytest.fixture
def entorno(tmp_path):
    core.configure(db_path=str(tmp_path / "cierre_router.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    cd.crear_tablas(conn)
    for u in (ADMIN, CAJERO):
        conn.execute(
            "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?,?,?,?,?)",
            (u["id"], u["username"], u["username"].title(), "x", u["role"]),
        )
    caja1 = conn.execute(
        "INSERT INTO cajas (nombre, sucursal_id) VALUES ('Mostrador', 1)"
    ).lastrowid
    conn.execute(
        """INSERT INTO turnos_caja
           (usuario_id, apertura, cierre, monto_inicial, monto_esperado_cierre,
            monto_declarado_cierre, estado, caja_id)
           VALUES (?, '2026-09-13 08:00:00', '2026-09-13 14:00:00', 0, 1000, 1000, 'cerrado', ?)""",
        (CAJERO["id"], caja1),
    )
    conn.commit()
    conn.close()
    yield
    core._db_path = None


@pytest.fixture(autouse=True)
def _reset_usuario_actual():
    """Algunos tests de `reabrir` cambian `_USUARIO_ACTUAL["actual"]` a
    `CAJERO` para probar el 403 — se restaura a `ADMIN` después de cada test,
    así ninguno hereda el usuario que dejó el anterior."""
    yield
    _USUARIO_ACTUAL["actual"] = ADMIN


def _autorizar_admin():
    """`autorizar_reabrir` de prueba: sólo `ADMIN` pasa — lee
    `_USUARIO_ACTUAL["actual"]`, así que un test puede alternar el usuario
    activo entre pedidos con sólo reasignar esa entrada."""
    if _USUARIO_ACTUAL["actual"]["role"] != "admin":
        raise HTTPException(403, "sólo un admin puede reabrir el día")


@pytest.fixture
def client(entorno):
    app = FastAPI()
    app.include_router(build_cierre_diario_router(
        usuario_actual=lambda: _USUARIO_ACTUAL["actual"],
        resolver_sucursal_nombre=lambda sid: {1: "Sucursal Centro"}.get(sid, "Sin sucursal"),
        autorizar_reabrir=Depends(_autorizar_admin),
    ))
    return TestClient(app)


def test_preview_lista_turnos_y_dice_si_se_puede_cerrar(client):
    r = client.get("/api/cierre-diario/preview", params={"sucursal_id": 1, "fecha": "2026-09-13"})
    assert r.status_code == 200
    datos = r.json()
    assert datos["puede_cerrar"] is True
    assert len(datos["turnos"]) == 1
    assert datos["turnos_abiertos"] == []


def test_cerrar_y_listar_y_detalle(client):
    r = client.post("/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"})
    assert r.status_code == 200
    cierre = r.json()
    assert cierre["numero"] == 1

    listado = client.get("/api/cierre-diario", params={"sucursal_id": 1}).json()
    assert len(listado) == 1

    detalle = client.get(f"/api/cierre-diario/{cierre['id']}").json()
    assert detalle["id"] == cierre["id"]
    assert len(detalle["turnos"]) == 1

    assert client.get("/api/cierre-diario/999999").status_code == 404


def test_listar_expone_todas_de_forma_explicita(client):
    """`sucursal_id=None` (el default) filtra "sin sucursal" — mismo target
    que `cerrar_dia()` —, no "todas". "Todas" es `todas=True`, explícito."""
    client.post("/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"})
    assert client.get("/api/cierre-diario").json() == []            # nadie cerró "sin sucursal"
    assert len(client.get("/api/cierre-diario", params={"sucursal_id": 1}).json()) == 1
    assert len(client.get("/api/cierre-diario", params={"todas": True}).json()) == 1


def test_cerrar_dos_veces_da_409(client):
    client.post("/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"})
    r = client.post("/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"})
    assert r.status_code == 409


def test_cerrar_con_turno_abierto_da_422(client, entorno):
    with core.get_connection() as conn:
        conn.execute(
            """INSERT INTO turnos_caja (usuario_id, apertura, monto_inicial, estado)
               VALUES (?, '2026-09-13 09:00:00', 0, 'abierto')""",
            (CAJERO["id"],),
        )
    r = client.post("/api/cierre-diario/cerrar", json={"sucursal_id": None, "fecha": "2026-09-13"})
    assert r.status_code == 422
    assert "abierto" in r.json()["detail"].lower()


def test_ticket_de_turno_en_vivo_antes_de_cualquier_cierre(client):
    """El caso normal: el cajero cierra SU turno e imprime, sin que exista
    ningún cierre diario todavía — el turno de la fixture (id 1) ya está
    'cerrado' en `turnos_caja` pero no entró a ninguna foto."""
    r = client.get("/api/cierre-diario/turno/1/ticket")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "inline" in r.headers["content-disposition"]
    assert r.content.startswith(b"%PDF-")


def test_ticket_de_turno_abierto_da_409(client):
    with core.get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO turnos_caja (usuario_id, apertura, monto_inicial, estado)
               VALUES (?, '2026-09-13 09:00:00', 0, 'abierto')""",
            (CAJERO["id"],),
        )
        tid = cur.lastrowid
    r = client.get(f"/api/cierre-diario/turno/{tid}/ticket")
    assert r.status_code == 409


def test_tickets_pdf(client):
    cierre = client.post("/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"}).json()

    r = client.get(f"/api/cierre-diario/{cierre['id']}/ticket")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "inline" in r.headers["content-disposition"]
    assert r.content.startswith(b"%PDF-")

    # Por `turno_id` (`turnos_caja.id`), NO por el id de la foto — pueden
    # coincidir por casualidad en un caso tan chico, así que se verifica
    # también que son campos distintos en la respuesta.
    turno_id = cierre["turnos"][0]["turno_id"]
    assert "id" in cierre["turnos"][0] and "turno_id" in cierre["turnos"][0]
    r2 = client.get(f"/api/cierre-diario/turno/{turno_id}/ticket")
    assert r2.status_code == 200
    assert r2.content.startswith(b"%PDF-")

    assert client.get("/api/cierre-diario/turno/999999/ticket").status_code == 404


def test_autorizar_cierre_es_inyectable_y_no_hay_admin_fijo(tmp_path):
    """El motor no decide quién cierra: si el producto no pasa
    `autorizar_cierre`, el endpoint no impone ningún rol — es cosa del
    producto ponerle el gate que quiera (hoy: admin o cajero, con SU nombre
    de rol)."""
    core.configure(db_path=str(tmp_path / "sin_gate.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    cd.crear_tablas(conn)
    conn.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (3,'op','Op','x','operador')"
    )
    conn.commit()
    conn.close()

    def _rechaza_todo():
        raise HTTPException(403, "nadie puede cerrar en este test")

    app = FastAPI()
    app.include_router(build_cierre_diario_router(
        usuario_actual=lambda: {"id": 3, "role": "operador"},
        autorizar_cierre=Depends(_rechaza_todo),
    ))
    client = TestClient(app)

    r = client.post("/api/cierre-diario/cerrar", json={"sucursal_id": None, "fecha": "2026-09-13"})
    assert r.status_code == 403

    # Y "preview"/"listar" NO pasan por `autorizar_cierre`: sólo el cierre en sí.
    assert client.get("/api/cierre-diario/preview").status_code == 200

    core._db_path = None


def test_sin_autorizar_reabrir_la_ruta_no_existe(tmp_path):
    """Sin `autorizar_reabrir` el router arma igual —un consumidor que sube el
    pin sin tocar su `main.py` sigue arrancando— pero `POST /{id}/reabrir` no
    se monta: cerrado por defecto, nunca abierto a quien sólo pasó el gate de
    módulo. Ver el docstring de `build_cierre_diario_router`."""
    router = build_cierre_diario_router(usuario_actual=lambda: ADMIN)
    rutas = {(r.path, tuple(sorted(r.methods))) for r in router.routes}
    assert not any(path.endswith("/reabrir") for path, _ in rutas), rutas
    # Y lo demás sigue montado.
    assert any(path.endswith("/cerrar") for path, _ in rutas), rutas


# ------------------------------------------------------------ POST /reabrir


def test_reabrir_admin_libera_el_dia_y_permite_re_cerrar(client):
    cierre = client.post(
        "/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"}
    ).json()

    r = client.post(f"/api/cierre-diario/{cierre['id']}/reabrir", json={"motivo": "error de carga"})
    assert r.status_code == 200
    reabierto = r.json()
    assert reabierto["anulado_en"] is not None
    assert reabierto["anulado_por"] == ADMIN["id"]
    assert reabierto["motivo_anulacion"] == "error de carga"
    # El cierre anulado conserva su número.
    assert reabierto["numero"] == cierre["numero"]

    # El día vuelve a estar abierto: un turno nuevo ya no choca con
    # `DiaCerradoError`. `caja_id` sale de la foto del cierre y no se
    # hardcodea: `init_core_schema()` puede sembrar filas propias en
    # `cajas`, así que el id de "Mostrador" no está garantizado en 1.
    caja_id = cierre["turnos"][0]["caja_id"]
    conn = core.get_connection()
    conn.execute(
        """INSERT INTO turnos_caja (usuario_id, apertura, monto_inicial, estado, caja_id)
           VALUES (?, '2026-09-13 15:00:00', 0, 'abierto', ?)""",
        (CAJERO["id"], caja_id),
    )
    conn.commit()
    conn.close()

    # Y re-cerrar da un cierre NUEVO, con el número siguiente.
    r2 = client.post("/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"})
    assert r2.status_code == 422  # el turno recién abierto sigue abierto
    with core.get_connection() as conn:
        conn.execute("UPDATE turnos_caja SET estado='cerrado', cierre='2026-09-13 16:00:00' "
                     "WHERE apertura='2026-09-13 15:00:00'")
    r3 = client.post("/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"})
    assert r3.status_code == 200
    assert r3.json()["numero"] == cierre["numero"] + 1


def test_reabrir_no_admin_da_403(client):
    cierre = client.post(
        "/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"}
    ).json()
    _USUARIO_ACTUAL["actual"] = CAJERO
    r = client.post(f"/api/cierre-diario/{cierre['id']}/reabrir", json={"motivo": "algo"})
    assert r.status_code == 403


def test_reabrir_inexistente_da_404(client):
    r = client.post("/api/cierre-diario/999999/reabrir", json={"motivo": "algo"})
    assert r.status_code == 404


def test_reabrir_motivo_vacio_da_422(client):
    cierre = client.post(
        "/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"}
    ).json()
    assert client.post(
        f"/api/cierre-diario/{cierre['id']}/reabrir", json={"motivo": "   "}
    ).status_code == 422
    # Falta el campo directamente: lo rechaza Pydantic, también 422.
    assert client.post(f"/api/cierre-diario/{cierre['id']}/reabrir", json={}).status_code == 422


def test_reabrir_dos_veces_da_409(client):
    cierre = client.post(
        "/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"}
    ).json()
    client.post(f"/api/cierre-diario/{cierre['id']}/reabrir", json={"motivo": "primera vez"})
    r = client.post(f"/api/cierre-diario/{cierre['id']}/reabrir", json={"motivo": "otra vez"})
    assert r.status_code == 409


def test_reabrir_con_cierre_posterior_da_409(client):
    cierre_13 = client.post(
        "/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"}
    ).json()
    client.post("/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-14"})

    r = client.post(f"/api/cierre-diario/{cierre_13['id']}/reabrir", json={"motivo": "tarde"})
    assert r.status_code == 409
    assert "posterior" in r.json()["detail"].lower()


def test_listar_y_detalle_traen_los_campos_de_anulacion(client):
    cierre = client.post(
        "/api/cierre-diario/cerrar", json={"sucursal_id": 1, "fecha": "2026-09-13"}
    ).json()
    assert cierre["anulado_en"] is None
    assert cierre["anulado_por"] is None
    assert cierre["motivo_anulacion"] is None

    client.post(f"/api/cierre-diario/{cierre['id']}/reabrir", json={"motivo": "prueba"})

    listado = client.get("/api/cierre-diario", params={"sucursal_id": 1}).json()
    assert listado[0]["anulado_en"] is not None
    assert listado[0]["motivo_anulacion"] == "prueba"
