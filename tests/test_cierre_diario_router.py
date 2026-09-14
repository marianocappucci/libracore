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


@pytest.fixture
def client(entorno):
    app = FastAPI()
    app.include_router(build_cierre_diario_router(
        usuario_actual=lambda: _USUARIO_ACTUAL["actual"],
        resolver_sucursal_nombre=lambda sid: {1: "Sucursal Centro"}.get(sid, "Sin sucursal"),
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
