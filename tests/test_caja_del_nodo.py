"""La caja de un nodo sube al central (ADR-076): los ganchos de caja, el origen de las filas y la guarda
`solo_en_el_central`.

Lo que se fija, en el orden del pedido:

- cada gancho corre **una vez**, **adentro de la transacción** del cambio (lo prueba una segunda conexión que todavía
  no ve la fila) y, si levanta, **el cambio no queda**;
- **sin ganchos todo es idéntico**: las mismas funciones, los mismos resultados, sin llamar a nadie;
- `origen` / `origen_id` hacen idempotente al central (índice único parcial) y no estorban a las filas propias;
- los tres routers cablean los ganchos, y uno con un `cerrar_turno` que no los acepta no arranca;
- `solo_en_el_central` contesta 409 en un nodo y deja pasar en el central.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from libracore import nodo_offline
from libracore.caja_router import build_caja_router, build_cierre_diario_router, build_turnos_router
from libracore.db import caja as db_caja
from libracore.db import cierre_diario as cd
from libracore.db import core
from libracore.db import turnos as db_turnos
from libracore.db.schema import init_core_schema
from libracore.ganchos_de_caja import SIN_GANCHOS, VERSION, GanchosDeCaja, validar_origen

USUARIO = {"id": 7, "username": "cajero", "role": "admin"}
DIA = "2026-10-20"
APERTURA = f"{DIA} 09:00:00"
CIERRE = f"{DIA} 18:00:00"


@pytest.fixture
def db(tmp_path):
    core.configure(db_path=str(tmp_path / "nodo.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    cd.crear_tablas(conn)
    conn.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (7,'cajero','Cajero','x','admin')")
    conn.commit()
    conn.close()
    yield
    core._db_path = None


@pytest.fixture
def caja_id(db):
    """Un mostrador de la sucursal 3: así el payload trae una sucursal que no es `None`."""
    return db_caja.create_caja_config("Mostrador", "", ["efectivo"], sucursal_id=3)


class Registro:
    """Los ganchos de prueba: anotan qué se llamó, con qué, y si la fila ya existía en una conexión **ajena** a la
    del cambio (la prueba de que corren dentro de la transacción: lo que no se confirmó no se ve desde afuera)."""

    def __init__(self, levantar: str | None = None):
        self.llamadas: list[tuple[str, dict]] = []
        self.levantar = levantar
        self.conexiones: list[object] = []
        self.visible_desde_afuera: list[int] = []

    def _gancho(self, nombre: str, tabla: str):
        def gancho(conn, datos):
            self.llamadas.append((nombre, datos))
            self.conexiones.append(conn)
            ajena = core.get_connection()
            try:
                self.visible_desde_afuera.append(ajena.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0])
            finally:
                ajena.close()
            if self.levantar == nombre:
                raise RuntimeError(f"el outbox falló en {nombre}")
        return gancho

    @property
    def ganchos(self) -> GanchosDeCaja:
        return GanchosDeCaja(
            al_abrir_turno=self._gancho("al_abrir_turno", "turnos_caja"),
            al_cerrar_turno=self._gancho("al_cerrar_turno", "turnos_caja WHERE estado='cerrado'"),
            al_registrar_movimiento=self._gancho("al_registrar_movimiento", "caja_movimientos"),
            al_anular_movimiento=self._gancho("al_anular_movimiento", "caja_movimientos WHERE anulado=1"),
            al_cerrar_dia=self._gancho("al_cerrar_dia", "cierres_diarios"),
        )

    def nombres(self) -> list[str]:
        return [n for n, _ in self.llamadas]


def _contar(tabla: str, donde: str = "1=1") -> int:
    conn = core.get_connection()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {tabla} WHERE {donde}").fetchone()[0]
    finally:
        conn.close()


# ── El turno ────────────────────────────────────────────────────────────


def test_abrir_turno_llama_al_gancho_una_vez_adentro_de_la_transaccion(caja_id):
    reg = Registro()
    tid = db_turnos.create_turno(7, 1500, "apertura", caja_id=caja_id, apertura=APERTURA, ganchos=reg.ganchos)
    assert reg.nombres() == ["al_abrir_turno"]
    datos = reg.llamadas[0][1]
    assert datos == {
        "version": VERSION, "turno_id": tid, "usuario_id": 7, "caja_id": caja_id, "sucursal_id": 3,
        "apertura": APERTURA, "monto_inicial": 1500.0, "notas": "apertura",
    }
    # Adentro de la transacción: una conexión ajena todavía no ve el turno.
    assert reg.visible_desde_afuera == [0]
    # Y ya confirmado, sí.
    assert _contar("turnos_caja") == 1


def test_si_el_gancho_de_apertura_levanta_el_turno_no_queda(caja_id):
    reg = Registro(levantar="al_abrir_turno")
    with pytest.raises(RuntimeError, match="el outbox falló"):
        db_turnos.create_turno(7, 0, caja_id=caja_id, ganchos=reg.ganchos)
    assert _contar("turnos_caja") == 0
    assert reg.nombres() == ["al_abrir_turno"]


def test_cerrar_turno_arquea_y_llama_al_gancho_con_el_arqueo(caja_id):
    reg = Registro()
    tid = db_turnos.create_turno(7, 1000, caja_id=caja_id, apertura=APERTURA)
    db_caja.create_caja_movimiento(DIA, "ingreso", "Venta", 500, medio_pago="efectivo", turno_id=tid)
    db_caja.create_caja_movimiento(DIA, "egreso", "Retiro", 200, medio_pago="efectivo", turno_id=tid)
    db_caja.create_caja_movimiento(DIA, "ingreso", "Con tarjeta", 700, medio_pago="tarjeta_debito", turno_id=tid)

    cerrado = db_turnos.cerrar_turno_caja(tid, 1250, "cierre", ganchos=reg.ganchos, cierre=CIERRE)

    assert reg.nombres() == ["al_cerrar_turno"]
    datos = reg.llamadas[0][1]
    # Esperado: apertura 1000 + efectivo (500 - 200). Declarado 1250: sobran 50.
    assert datos["monto_esperado"] == 1300.0 and datos["monto_declarado"] == 1250.0
    assert datos["diferencia"] == -50.0
    assert (datos["turno_id"], datos["sucursal_id"], datos["cierre"]) == (tid, 3, CIERRE)
    assert {m["medio_pago"]: m["neto"] for m in datos["medios"]} == {"efectivo": 300.0, "tarjeta_debito": 700.0}
    assert reg.visible_desde_afuera == [0], "el cierre todavía no se confirmó para una conexión ajena"
    assert cerrado["estado"] == "cerrado" and cerrado["monto_esperado_cierre"] == 1300.0


def test_si_el_gancho_de_cierre_levanta_el_turno_sigue_abierto(caja_id):
    tid = db_turnos.create_turno(7, 1000, caja_id=caja_id)
    reg = Registro(levantar="al_cerrar_turno")
    with pytest.raises(RuntimeError):
        db_turnos.cerrar_turno_caja(tid, 1000, ganchos=reg.ganchos)
    turno = db_turnos.get_turno(tid)
    assert turno["estado"] == "abierto" and turno["cierre"] is None and turno["monto_declarado_cierre"] is None


def test_cerrar_turno_con_ventas_del_motor_tambien_llama_al_gancho(caja_id):
    tid = db_turnos.create_turno(7, 100, caja_id=caja_id)
    reg = Registro()
    assert db_turnos.cerrar_turno(tid, 100, ganchos=reg.ganchos) is None
    assert reg.nombres() == ["al_cerrar_turno"]
    assert db_turnos.get_turno(tid)["estado"] == "cerrado"


def test_cerrar_un_turno_que_no_existe_no_llama_al_gancho(db):
    reg = Registro()
    assert db_turnos.cerrar_turno_caja(999, 0, ganchos=reg.ganchos) is None
    assert reg.llamadas == []


def test_el_monto_esperado_del_nodo_se_guarda_tal_cual(caja_id):
    """El central no recalcula: puede no tener todavía los movimientos que vio el cajero."""
    tid = db_turnos.create_turno(7, 1000, caja_id=caja_id)
    db_turnos.cerrar_turno_caja(tid, 1800, monto_esperado=1800.0)
    assert db_turnos.get_turno(tid)["monto_esperado_cierre"] == 1800.0


def test_con_conn_el_turno_va_en_la_transaccion_de_quien_llama(caja_id):
    reg = Registro(levantar="al_abrir_turno")
    conn = core.get_connection()
    try:
        with pytest.raises(RuntimeError):
            db_turnos.create_turno(7, 0, caja_id=caja_id, conn=conn, ganchos=reg.ganchos)
        conn.rollback()
    finally:
        conn.close()
    assert _contar("turnos_caja") == 0

    conn = core.get_connection()
    try:
        tid = db_turnos.create_turno(7, 0, caja_id=caja_id, conn=conn)
        assert _contar("turnos_caja") == 0, "con conn la confirmación es de quien llama"
        db_turnos.cerrar_turno_caja(tid, 0, conn=conn)
        assert db_turnos.get_turno(tid, conn=conn)["estado"] == "cerrado"
        conn.commit()
    finally:
        conn.close()
    assert _contar("turnos_caja", "estado='cerrado'") == 1


# ── El movimiento ──────────────────────────────────────────────────────


def test_registrar_movimiento_llama_al_gancho_una_vez_adentro_de_la_transaccion(caja_id):
    reg = Registro()
    tid = db_turnos.create_turno(7, 0, caja_id=caja_id)
    mid = db_caja.create_caja_movimiento(
        DIA, "egreso", "Retiro de caja", 800, referencia="ret-1", usuario_id=7, medio_pago="efectivo",
        turno_id=tid, ganchos=reg.ganchos)
    assert reg.nombres() == ["al_registrar_movimiento"]
    assert reg.llamadas[0][1] == {
        "version": VERSION, "movimiento_id": mid, "fecha": DIA, "tipo": "egreso", "concepto": "Retiro de caja",
        "monto": 800.0, "referencia": "ret-1", "factura_id": None, "usuario_id": 7, "caja_id": caja_id,
        "sucursal_id": 3, "medio_pago": "efectivo", "turno_id": tid, "cc_pago_id": None, "anulado": 0,
    }
    assert reg.visible_desde_afuera == [0]


def test_una_referencia_repetida_no_vuelve_a_subir(caja_id):
    reg = Registro()
    uno = db_caja.create_caja_movimiento(DIA, "ingreso", "Seña", 100, referencia="r-1", ganchos=reg.ganchos)
    dos = db_caja.create_caja_movimiento(DIA, "ingreso", "Seña", 100, referencia="r-1", ganchos=reg.ganchos)
    assert uno == dos
    assert reg.nombres() == ["al_registrar_movimiento"], "no hubo fila nueva: no hay nada que subir"


def test_si_el_gancho_del_movimiento_levanta_no_queda_el_movimiento(caja_id):
    reg = Registro(levantar="al_registrar_movimiento")
    with pytest.raises(RuntimeError):
        db_caja.create_caja_movimiento(DIA, "ingreso", "Seña", 100, ganchos=reg.ganchos)
    assert _contar("caja_movimientos") == 0


def test_anular_movimiento_llama_al_gancho_una_vez_y_solo_si_cambio_algo(caja_id):
    mid = db_caja.create_caja_movimiento(DIA, "ingreso", "Seña", 100, medio_pago="efectivo")
    reg = Registro()
    db_caja.anular_caja_movimiento(mid, ganchos=reg.ganchos)
    assert reg.nombres() == ["al_anular_movimiento"]
    assert reg.llamadas[0][1]["movimiento_id"] == mid and reg.llamadas[0][1]["anulado"] == 1
    assert reg.visible_desde_afuera == [0], "la anulación todavía no se confirmó para una conexión ajena"
    # Anular de nuevo, o anular lo que no existe, no sube nada.
    db_caja.anular_caja_movimiento(mid, ganchos=reg.ganchos)
    db_caja.anular_caja_movimiento(9999, ganchos=reg.ganchos)
    assert reg.nombres() == ["al_anular_movimiento"]


def test_si_el_gancho_de_anulacion_levanta_el_movimiento_sigue_vigente(caja_id):
    mid = db_caja.create_caja_movimiento(DIA, "ingreso", "Seña", 100)
    reg = Registro(levantar="al_anular_movimiento")
    with pytest.raises(RuntimeError):
        db_caja.anular_caja_movimiento(mid, ganchos=reg.ganchos)
    assert _contar("caja_movimientos", "anulado=0") == 1


# ── El cierre del día ──────────────────────────────────────────────────


def _turno_cerrado_del_dia(caja_id, declarado=1100.0) -> int:
    tid = db_turnos.create_turno(7, 1000, caja_id=caja_id, apertura=APERTURA)
    db_caja.create_caja_movimiento(DIA, "ingreso", "Venta", 100, medio_pago="efectivo", turno_id=tid)
    db_turnos.cerrar_turno_caja(tid, declarado, cierre=CIERRE)
    return tid


def test_cerrar_dia_llama_al_gancho_con_el_arqueo_de_cada_turno(caja_id):
    tid = _turno_cerrado_del_dia(caja_id, declarado=1090.0)
    reg = Registro()
    cierre = cd.cerrar_dia(7, 3, DIA, "fin del día", ganchos=reg.ganchos)
    assert reg.nombres() == ["al_cerrar_dia"]
    datos = reg.llamadas[0][1]
    assert datos["cierre_id"] == cierre["id"] and datos["numero"] == 1 and datos["fecha"] == DIA
    assert (datos["sucursal_id"], datos["usuario_id"], datos["notas"]) == (3, 7, "fin del día")
    assert datos["monto_esperado_total"] == 1100.0 and datos["monto_declarado_total"] == 1090.0
    assert datos["diferencia_total"] == -10.0
    assert datos["turnos"] == [{"turno_id": tid, "caja_id": caja_id, "monto_inicial": 1000.0,
                                "monto_esperado": 1100.0, "monto_declarado": 1090.0, "diferencia": -10.0}]
    assert datos["medios"] == [{"medio_pago": "efectivo", "ingresos": 100.0, "egresos": 0.0, "neto": 100.0}]
    assert reg.visible_desde_afuera == [0]


def test_si_el_gancho_del_cierre_levanta_el_dia_no_queda_cerrado(caja_id):
    _turno_cerrado_del_dia(caja_id)
    reg = Registro(levantar="al_cerrar_dia")
    with pytest.raises(RuntimeError):
        cd.cerrar_dia(7, 3, DIA, ganchos=reg.ganchos)
    assert _contar("cierres_diarios") == 0
    assert _contar("cierres_diarios_turnos") == 0
    assert not cd.dia_cerrado(DIA, 3)
    # Y el día se puede cerrar después, sin ganchos.
    assert cd.cerrar_dia(7, 3, DIA)["numero"] == 1


def test_cerrar_dia_con_conn_devuelve_el_cierre_sin_confirmar(caja_id):
    _turno_cerrado_del_dia(caja_id)
    conn = core.get_connection()
    try:
        cierre = cd.cerrar_dia(7, 3, DIA, conn=conn)
        assert cierre["numero"] == 1 and len(cierre["turnos"]) == 1
        assert _contar("cierres_diarios") == 0
        conn.commit()
    finally:
        conn.close()
    assert _contar("cierres_diarios") == 1


# ── Sin ganchos todo es idéntico ───────────────────────────────────────


def test_sin_ganchos_el_comportamiento_es_el_de_siempre(caja_id):
    """Mismo recorrido completo sin ganchos y con `SIN_GANCHOS` (todos no-op): mismos ids, mismos resultados."""
    tid = db_turnos.create_turno(7, 1000, caja_id=caja_id, apertura=APERTURA)
    mid = db_caja.create_caja_movimiento(DIA, "ingreso", "Venta", 100, medio_pago="efectivo", turno_id=tid)
    db_caja.anular_caja_movimiento(mid)
    cerrado = db_turnos.cerrar_turno_caja(tid, 1000, cierre=CIERRE)
    cierre = cd.cerrar_dia(7, 3, DIA)
    assert cerrado["estado"] == "cerrado" and cerrado["monto_esperado_cierre"] == 1000.0
    assert cierre["numero"] == 1 and cierre["monto_esperado_total"] == 1000.0
    # Ninguna fila del recorrido lleva origen.
    assert _contar("turnos_caja", "origen IS NOT NULL") == 0
    assert _contar("caja_movimientos", "origen IS NOT NULL") == 0
    assert _contar("cierres_diarios", "origen IS NOT NULL") == 0

    # Con el conjunto vacío, ídem (y no explota).
    tid2 = db_turnos.create_turno(7, 0, ganchos=SIN_GANCHOS)
    db_caja.create_caja_movimiento(DIA, "ingreso", "Otra", 5, ganchos=SIN_GANCHOS)
    db_turnos.cerrar_turno_caja(tid2, 0, ganchos=SIN_GANCHOS)
    assert db_turnos.get_turno(tid2)["estado"] == "cerrado"


# ── El origen ───────────────────────────────────────────────────────────


def test_origen_y_origen_id_van_juntos():
    validar_origen(None, None)
    validar_origen("nodo-1", 5)
    for par in (("nodo-1", None), (None, 5), ("  ", 5)):
        with pytest.raises(ValueError):
            validar_origen(*par)


def test_un_turno_del_nodo_se_aplica_una_sola_vez(caja_id):
    tid = db_turnos.create_turno(7, 500, caja_id=caja_id, apertura=APERTURA, origen="nodo-1", origen_id=41)
    turno = db_turnos.get_turno_de_origen("nodo-1", 41)
    assert turno["id"] == tid and turno["apertura"] == APERTURA
    assert (turno["origen"], turno["origen_id"]) == ("nodo-1", 41)
    assert db_turnos.get_turno_de_origen("nodo-1", 42) is None
    assert db_turnos.get_turno_de_origen("nodo-2", 41) is None, "el mismo id de otro nodo es otro turno"
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db_turnos.create_turno(7, 500, caja_id=caja_id, origen="nodo-1", origen_id=41)
    db_turnos.create_turno(7, 500, caja_id=caja_id, origen="nodo-2", origen_id=41)  # otro nodo: entra
    assert _contar("turnos_caja") == 2


def test_las_filas_propias_no_chocan_entre_si(caja_id):
    """Dos `NULL` no se pisan: el índice único es parcial."""
    db_turnos.create_turno(7, 0, caja_id=caja_id)
    db_caja.create_caja_movimiento(DIA, "ingreso", "A", 1)
    db_caja.create_caja_movimiento(DIA, "ingreso", "B", 1)
    assert _contar("caja_movimientos", "origen IS NULL") == 2


def test_un_movimiento_del_nodo_se_aplica_una_sola_vez(caja_id):
    mid = db_caja.create_caja_movimiento(DIA, "egreso", "Retiro", 50, origen="nodo-1", origen_id=900)
    assert db_caja.get_movimiento_de_origen("nodo-1", 900)["id"] == mid
    assert db_caja.get_movimiento_de_origen("nodo-1", 901) is None
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db_caja.create_caja_movimiento(DIA, "egreso", "Retiro", 50, origen="nodo-1", origen_id=900)
    with pytest.raises(ValueError):
        db_caja.create_caja_movimiento(DIA, "egreso", "Retiro", 50, origen="nodo-1")


def test_un_cierre_del_nodo_guarda_su_origen(caja_id):
    _turno_cerrado_del_dia(caja_id)
    cierre = cd.cerrar_dia(7, 3, DIA, origen="nodo-1", origen_id=12)
    assert cierre["origen"] == "nodo-1" and cierre["origen_id"] == 12
    assert cd.get_cierre_de_origen("nodo-1", 12)["id"] == cierre["id"]
    assert cd.get_cierre_de_origen("nodo-1", 13) is None


def test_crear_tablas_no_muere_en_una_base_sin_las_columnas_de_origen(tmp_path):
    """`crear_tablas()` se llama en cada arranque de LibraClub: en una base de antes de la `0028` no puede fallar
    por el índice del origen (las columnas llegan con la migración)."""
    core.configure(db_path=str(tmp_path / "vieja.db"))
    conn = core.get_connection()
    try:
        init_core_schema(conn)
        conn.executescript("""
            CREATE TABLE cierres_diarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sucursal_id INTEGER, numero INTEGER NOT NULL,
                fecha TEXT NOT NULL, usuario_id INTEGER NOT NULL, monto_esperado_total REAL NOT NULL DEFAULT 0,
                monto_declarado_total REAL NOT NULL DEFAULT 0, diferencia_total REAL NOT NULL DEFAULT 0,
                notas TEXT DEFAULT '', created_at TEXT, anulado_en TEXT, anulado_por INTEGER,
                motivo_anulacion TEXT);
        """)
        cd.crear_tablas(conn)  # no levanta
        indices = {f[0] for f in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='cierres_diarios'").fetchall()}
        assert "idx_cierres_diarios_origen" not in indices
    finally:
        conn.close()
        core._db_path = None


# ── Los routers ─────────────────────────────────────────────────────────


def _app(ganchos: GanchosDeCaja | None, **kw_turnos) -> TestClient:
    app = FastAPI()
    app.include_router(build_caja_router(usuario_actual=lambda: USUARIO, ganchos_de_caja=ganchos))
    app.include_router(build_turnos_router(usuario_actual=lambda: USUARIO, ganchos_de_caja=ganchos, **kw_turnos))
    app.include_router(build_cierre_diario_router(usuario_actual=lambda: USUARIO, ganchos_de_caja=ganchos))
    return TestClient(app)


def test_los_routers_llaman_a_cada_gancho_una_vez(caja_id):
    reg = Registro()
    client = _app(reg.ganchos)

    turno = client.post("/api/turnos/abrir", json={"monto_inicial": 1000, "caja_id": caja_id}).json()
    assert reg.nombres() == ["al_abrir_turno"]
    assert client.post("/api/turnos/abrir", json={"monto_inicial": 1000, "caja_id": caja_id}).status_code == 200
    assert reg.nombres() == ["al_abrir_turno"], "abrir con un turno ya abierto devuelve ese turno y no sube nada"

    mov = client.post("/api/caja", json={
        "fecha": DIA, "tipo": "egreso", "concepto": "Retiro", "monto": 100, "medio_pago": "efectivo",
        "caja_id": caja_id}).json()["id"]
    assert reg.nombres() == ["al_abrir_turno", "al_registrar_movimiento"]
    assert client.delete(f"/api/caja/{mov}").json() == {"ok": True}
    assert reg.nombres()[-1] == "al_anular_movimiento"

    assert client.post(f"/api/turnos/{turno['id']}/cerrar", json={"monto_declarado": 1000}).status_code == 200
    assert reg.nombres()[-1] == "al_cerrar_turno"
    assert client.post(f"/api/turnos/{turno['id']}/cerrar", json={"monto_declarado": 1}).status_code == 422
    assert reg.nombres().count("al_cerrar_turno") == 1

    # El turno abrió hoy: cerrar el día de hoy en la sucursal 3.
    hoy = turno["apertura"][:10]
    r = client.post("/api/cierre-diario/cerrar", json={"sucursal_id": 3, "fecha": hoy})
    assert r.status_code == 200, r.text
    assert reg.nombres()[-1] == "al_cerrar_dia" and reg.nombres().count("al_cerrar_dia") == 1


def test_un_gancho_que_levanta_frena_el_cambio_de_la_ruta(caja_id):
    reg = Registro(levantar="al_registrar_movimiento")
    client = TestClient(_app(reg.ganchos).app, raise_server_exceptions=False)
    r = client.post("/api/caja", json={"fecha": DIA, "tipo": "ingreso", "concepto": "X", "monto": 1})
    assert r.status_code == 500
    assert _contar("caja_movimientos") == 0


def test_los_routers_sin_ganchos_no_llaman_a_nadie(caja_id):
    client = _app(None)
    turno = client.post("/api/turnos/abrir", json={"monto_inicial": 0, "caja_id": caja_id}).json()
    assert client.post("/api/caja", json={"fecha": DIA, "tipo": "ingreso", "concepto": "X", "monto": 1}).status_code == 200
    assert client.post(f"/api/turnos/{turno['id']}/cerrar", json={"monto_declarado": 0}).status_code == 200


def test_un_cerrar_turno_del_producto_sin_ganchos_sigue_andando_sin_ganchos_de_caja(caja_id):
    """Compatibilidad: el `cerrar_turno(tid, declarado, notas)` de un producto, sin `ganchos_de_caja`, se llama con
    tres argumentos como siempre."""
    llamadas = []

    def cerrar_del_producto(tid, declarado, notas):
        llamadas.append((tid, declarado, notas))
        db_turnos.cerrar_turno_caja(tid, declarado, notas)

    client = _app(None, cerrar_turno=cerrar_del_producto)
    turno = client.post("/api/turnos/abrir", json={"monto_inicial": 0, "caja_id": caja_id}).json()
    assert client.post(f"/api/turnos/{turno['id']}/cerrar", json={"monto_declarado": 5, "notas": " n "}).status_code == 200
    assert llamadas == [(turno["id"], 5, "n")]


def test_con_ganchos_un_cerrar_turno_que_no_los_acepta_no_arranca(db):
    def cerrar_viejo(tid, declarado, notas):
        pass

    with pytest.raises(TypeError, match="ganchos="):
        build_turnos_router(usuario_actual=lambda: USUARIO, cerrar_turno=cerrar_viejo,
                            ganchos_de_caja=GanchosDeCaja())

    def cerrar_con_kwargs(tid, declarado, notas, **kw):
        pass

    build_turnos_router(usuario_actual=lambda: USUARIO, cerrar_turno=cerrar_con_kwargs,
                        ganchos_de_caja=GanchosDeCaja())
    build_turnos_router(usuario_actual=lambda: USUARIO, cerrar_turno=db_turnos.cerrar_turno_caja,
                        ganchos_de_caja=GanchosDeCaja())


# ── La guarda: solo_en_el_central ──────────────────────────────────────


def _app_de_la_guarda(dependencia) -> TestClient:
    app = FastAPI()

    @app.post("/api/agenda/turnos", dependencies=[Depends(dependencia)])
    def agendar():
        return {"ok": True}

    @app.get("/api/libre")
    def libre():
        return {"ok": True}

    return TestClient(app)


def test_solo_en_el_central_deja_pasar_fuera_de_un_nodo(monkeypatch):
    monkeypatch.delenv(nodo_offline.VARIABLE_NODO, raising=False)
    r = _app_de_la_guarda(nodo_offline.solo_en_el_central).post("/api/agenda/turnos")
    assert r.status_code == 200 and r.json() == {"ok": True}
    monkeypatch.setenv(nodo_offline.VARIABLE_NODO, "   ")
    assert _app_de_la_guarda(nodo_offline.solo_en_el_central).post("/api/agenda/turnos").status_code == 200


def test_solo_en_el_central_contesta_409_en_un_nodo(monkeypatch):
    monkeypatch.setenv(nodo_offline.VARIABLE_NODO, "nodo-1")
    client = _app_de_la_guarda(nodo_offline.solo_en_el_central)
    r = client.post("/api/agenda/turnos")
    assert r.status_code == 409
    assert r.json()["detail"] == {"codigo": "solo_en_el_central", "mensaje": nodo_offline.MENSAJE_SOLO_EN_EL_CENTRAL}
    assert nodo_offline.CODIGO_SOLO_EN_EL_CENTRAL == "solo_en_el_central"
    # Sólo la ruta guardada: el resto del nodo anda.
    assert client.get("/api/libre").status_code == 200


def test_solo_en_el_central_se_lee_en_cada_pedido(monkeypatch):
    """No al montar la ruta: el mismo cliente, el entorno cambia entre dos pedidos."""
    monkeypatch.delenv(nodo_offline.VARIABLE_NODO, raising=False)
    client = _app_de_la_guarda(nodo_offline.solo_en_el_central)
    assert client.post("/api/agenda/turnos").status_code == 200
    monkeypatch.setenv(nodo_offline.VARIABLE_NODO, "nodo-1")
    assert client.post("/api/agenda/turnos").status_code == 409


def test_la_guarda_acepta_la_identidad_y_el_mensaje_del_producto(monkeypatch):
    monkeypatch.delenv(nodo_offline.VARIABLE_NODO, raising=False)
    guarda = nodo_offline.guarda_solo_en_el_central(nodo=lambda: "medlibra-1", mensaje="La agenda es del central.")
    r = _app_de_la_guarda(guarda).post("/api/agenda/turnos")
    assert r.status_code == 409
    assert r.json()["detail"] == {"codigo": "solo_en_el_central", "mensaje": "La agenda es del central."}
    assert _app_de_la_guarda(nodo_offline.guarda_solo_en_el_central(nodo=lambda: None)).post(
        "/api/agenda/turnos").status_code == 200
    assert nodo_offline.SoloEnElCentral().detalle()["codigo"] == "solo_en_el_central"


# ── La migración ───────────────────────────────────────────────────────


RAIZ = Path(__file__).resolve().parents[1]


def _alembic(destino: str, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=RAIZ, env={**os.environ, "DATABASE_URL": destino}, capture_output=True, text=True)


def test_la_0028_agrega_las_columnas_a_una_base_de_antes_y_no_se_baja(tmp_path):
    """Una instancia que corrió hasta la `0027`: la `0028` le pone el origen a las tres tablas, con su índice."""
    import sqlite3

    destino = str(tmp_path / "antes.db")
    assert _alembic(destino, "upgrade", "0027_reintegros_pendientes").returncode == 0

    # Una base de antes de la 0028 no tiene las columnas (la 0009 de hoy ya las crea: se las sacamos).
    conn = sqlite3.connect(destino)
    conn.execute("DROP INDEX IF EXISTS idx_cierres_diarios_origen")
    conn.execute("ALTER TABLE cierres_diarios DROP COLUMN origen")
    conn.execute("ALTER TABLE cierres_diarios DROP COLUMN origen_id")
    for tabla in ("turnos_caja", "caja_movimientos"):
        conn.execute(f"DROP INDEX IF EXISTS idx_{tabla}_origen")
        conn.execute(f"ALTER TABLE {tabla} DROP COLUMN origen")
        conn.execute(f"ALTER TABLE {tabla} DROP COLUMN origen_id")
    conn.commit()
    conn.close()

    assert _alembic(destino, "upgrade", "head").returncode == 0
    conn = sqlite3.connect(destino)
    try:
        for tabla in ("turnos_caja", "caja_movimientos", "cierres_diarios"):
            columnas = {f[1] for f in conn.execute(f"PRAGMA table_info({tabla})").fetchall()}
            assert {"origen", "origen_id"} <= columnas, tabla
        indices = {f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert {"idx_turnos_caja_origen", "idx_caja_movimientos_origen", "idx_cierres_diarios_origen"} <= indices
    finally:
        conn.close()

    # Idempotente, y no se baja.
    assert _alembic(destino, "upgrade", "head").returncode == 0
    bajar = _alembic(destino, "downgrade", "0027_reintegros_pendientes")
    assert bajar.returncode != 0 and "No se baja" in bajar.stderr


# ── `cerrar_turno_con` (v1.188.1): el cierre común, con el resumen de quien llama ──

def test_cerrar_turno_con_arquea_con_el_resumen_de_quien_llama_y_corre_el_gancho(caja_id):
    reg = Registro()
    tid = db_turnos.create_turno(7, 100, caja_id=caja_id, apertura=APERTURA)

    cerrado = db_turnos.cerrar_turno_con(tid, 130, "fin", resumen=lambda t, c: {"efectivo_ventas": 25.0},
                                         ganchos=reg.ganchos, cierre=CIERRE)

    assert reg.nombres() == ["al_cerrar_turno"]
    datos = reg.llamadas[0][1]
    assert (datos["turno_id"], datos["monto_esperado"], datos["monto_declarado"]) == (tid, 125.0, 130.0)
    assert cerrado["estado"] == "cerrado" and cerrado["monto_esperado_cierre"] == 125.0


def test_cerrar_turno_con_un_turno_que_no_existe_da_none_y_no_corre_el_gancho(caja_id):
    reg = Registro()
    assert db_turnos.cerrar_turno_con(999999, 1, resumen=lambda t, c: {"efectivo_ventas": 0.0},
                                      ganchos=reg.ganchos) is None
    assert reg.nombres() == []
