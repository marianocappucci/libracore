"""Cierre diario: el acto registrado de cerrar el día operativo de una
sucursal.

Convención de estos tests, heredada de `test_caja_por_sucursal.py` y
`test_turnos_caja.py`: el schema se arma con `init_core_schema()` +
`cierre_diario.crear_tablas()` (no con Alembic — eso lo cubre
`tests/db/test_migraciones.py`), y los turnos que necesitan una `apertura`/
`cierre` controlada se insertan por SQL directo, porque `turnos.create_turno()`
siempre usa el reloj real (`_ar_now()`).
"""
from __future__ import annotations

import sqlite3

import pytest

from libracore.db import caja as db_caja
from libracore.db import cierre_diario as cd
from libracore.db import core
from libracore.db import turnos as db_turnos
from libracore.db.schema import init_core_schema


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "cierre_diario.db"))
    c = core.get_connection()
    init_core_schema(c)
    cd.crear_tablas(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def _turno(conn, usuario_id, apertura, cierre=None, monto_inicial=0.0,
          monto_esperado_cierre=None, monto_declarado_cierre=None,
          estado="cerrado", caja_id=None):
    """Inserta un turno con fechas CONTROLADAS — `create_turno()` sólo sabe
    usar el reloj real, y estos tests necesitan fijar apertura/cierre para
    probar el día operativo (incluido el turno que cruza la medianoche)."""
    cur = conn.execute(
        """INSERT INTO turnos_caja
           (usuario_id, apertura, cierre, monto_inicial, monto_esperado_cierre,
            monto_declarado_cierre, estado, caja_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (usuario_id, apertura, cierre, monto_inicial, monto_esperado_cierre,
         monto_declarado_cierre, estado, caja_id),
    )
    conn.commit()
    return cur.lastrowid


def _movimiento(conn, turno_id, tipo, monto, medio_pago, fecha="2026-09-13",
                anulado=0, caja_id=None):
    cur = conn.execute(
        """INSERT INTO caja_movimientos
           (fecha, tipo, concepto, monto, medio_pago, turno_id, anulado, caja_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (fecha, tipo, "mov", monto, medio_pago, turno_id, anulado, caja_id),
    )
    conn.commit()
    return cur.lastrowid


def _usuario(conn, username):
    cur = conn.execute(
        "INSERT INTO usuarios (username, nombre, email, password_hash, role, activo)"
        " VALUES (?, ?, '', 'sin-hash-real--este-test-no-autentica', 'admin', TRUE)",
        (username, username.title()),
    )
    conn.commit()
    return cur.lastrowid


def _caja(conn, nombre, sucursal_id=None):
    return db_caja.create_caja_config(nombre, "", ["efectivo"], sucursal_id=sucursal_id)


# ── Consolidación ──────────────────────────────────────────────────────────


def test_consolidacion_dos_cajeros_dos_medios(conn):
    admin = _usuario(conn, "admin1")
    cajero1 = _usuario(conn, "cajero1")
    cajero2 = _usuario(conn, "cajero2")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)

    t1 = _turno(conn, cajero1, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
               monto_inicial=1000.0, monto_esperado_cierre=3500.0,
               monto_declarado_cierre=3500.0, caja_id=caja1)
    _movimiento(conn, t1, "ingreso", 2000.0, "efectivo")
    _movimiento(conn, t1, "ingreso", 500.0, "tarjeta_debito")

    t2 = _turno(conn, cajero2, "2026-09-13 14:00:00", "2026-09-13 20:00:00",
               monto_inicial=3500.0, monto_esperado_cierre=5500.0,
               monto_declarado_cierre=5400.0, caja_id=caja1)
    _movimiento(conn, t2, "ingreso", 2000.0, "efectivo")
    _movimiento(conn, t2, "ingreso", 1000.0, "tarjeta_debito")

    cierre = cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")

    assert cierre["numero"] == 1
    assert cierre["monto_esperado_total"] == 9000.0
    assert cierre["monto_declarado_total"] == 8900.0
    assert cierre["diferencia_total"] == -100.0
    assert len(cierre["turnos"]) == 2

    medios = {m["medio_pago"]: m for m in cierre["medios"]}
    assert medios["efectivo"]["ingresos"] == 4000.0
    assert medios["tarjeta_debito"]["ingresos"] == 1500.0


def test_turno_nocturno_cae_en_el_dia_de_apertura(conn):
    """22:00 a 03:00: el día operativo es el de la APERTURA, no el del cierre."""
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)

    t1 = _turno(conn, cajero, "2026-09-13 22:00:00", "2026-09-14 03:00:00",
               monto_inicial=1000.0, monto_esperado_cierre=2000.0,
               monto_declarado_cierre=2000.0, caja_id=caja1)
    _movimiento(conn, t1, "ingreso", 1000.0, "efectivo")

    # No aparece en el preview del 14: pertenece al 13.
    preview_14 = cd.preview_cierre_dia(sucursal_id=1, fecha="2026-09-14")
    assert preview_14["turnos"] == []
    assert preview_14["turnos_abiertos"] == []

    cierre = cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")
    assert len(cierre["turnos"]) == 1
    assert cierre["turnos"][0]["turno_id"] == t1

    # Y el 14 puede cerrar limpio, sin ese turno (ya se contó en el 13).
    cierre_14 = cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-14")
    assert cierre_14["turnos"] == []
    assert cierre_14["numero"] == 2


def test_rechaza_con_turno_abierto(conn):
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)
    _turno(conn, cajero, "2026-09-13 08:00:00", cierre=None,
          monto_inicial=1000.0, estado="abierto", caja_id=caja1)

    with pytest.raises(cd.TurnosAbiertosError) as exc:
        cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")

    mensaje = str(exc.value)
    assert "Cajero1" in mensaje
    assert "Mostrador" in mensaje
    assert "2026-09-13 08:00:00" in mensaje


def test_rechaza_el_segundo_cierre_del_mismo_dia_y_sucursal(conn):
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)
    _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
          monto_inicial=1000.0, monto_esperado_cierre=1000.0,
          monto_declarado_cierre=1000.0, caja_id=caja1)

    cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")

    with pytest.raises(cd.DiaYaCerradoError):
        cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")


def test_dos_sucursales_distintas_cierran_el_mismo_dia(conn):
    admin = _usuario(conn, "admin1")
    cajero1 = _usuario(conn, "cajero1")
    cajero2 = _usuario(conn, "cajero2")
    caja1 = _caja(conn, "Centro", sucursal_id=1)
    caja2 = _caja(conn, "Norte", sucursal_id=2)
    _turno(conn, cajero1, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
          monto_inicial=0.0, monto_esperado_cierre=100.0,
          monto_declarado_cierre=100.0, caja_id=caja1)
    _turno(conn, cajero2, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
          monto_inicial=0.0, monto_esperado_cierre=200.0,
          monto_declarado_cierre=200.0, caja_id=caja2)

    c1 = cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")
    c2 = cd.cerrar_dia(usuario_id=admin, sucursal_id=2, fecha="2026-09-13")

    assert c1["numero"] == 1
    assert c2["numero"] == 1
    assert c1["monto_declarado_total"] == 100.0
    assert c2["monto_declarado_total"] == 200.0


def test_sucursal_null_cierra_y_no_dos_veces(conn):
    """VentaLibra hoy, o cualquier turno sin caja / caja sin sucursal."""
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
          monto_inicial=500.0, monto_esperado_cierre=500.0,
          monto_declarado_cierre=500.0, caja_id=None)

    cierre = cd.cerrar_dia(usuario_id=admin, sucursal_id=None, fecha="2026-09-13")
    assert cierre["sucursal_id"] is None
    assert cierre["numero"] == 1

    with pytest.raises(cd.DiaYaCerradoError):
        cd.cerrar_dia(usuario_id=admin, sucursal_id=None, fecha="2026-09-13")

    # Y no interfiere con una sucursal real que cierra el mismo día.
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)
    _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
          monto_inicial=0.0, monto_esperado_cierre=0.0,
          monto_declarado_cierre=0.0, caja_id=caja1)
    otro = cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")
    assert otro["numero"] == 1


def test_numeracion_correlativa_por_sucursal(conn):
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)

    for dia in ("2026-09-10", "2026-09-11", "2026-09-12"):
        _turno(conn, cajero, f"{dia} 08:00:00", f"{dia} 14:00:00",
              monto_inicial=0.0, monto_esperado_cierre=0.0,
              monto_declarado_cierre=0.0, caja_id=caja1)

    numeros = [cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha=d)["numero"]
              for d in ("2026-09-10", "2026-09-11", "2026-09-12")]
    assert numeros == [1, 2, 3]


# ── La guarda de apertura ────────────────────────────────────────────────


def test_dia_cerrado_y_la_guarda_del_alta_de_turno(conn):
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)
    _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
          monto_inicial=0.0, monto_esperado_cierre=0.0,
          monto_declarado_cierre=0.0, caja_id=caja1)
    cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")

    assert cd.dia_cerrado("2026-09-13", 1) is True
    assert cd.dia_cerrado("2026-09-13", 2) is False
    assert cd.dia_cerrado("2026-09-14", 1) is False

    with pytest.raises(cd.DiaCerradoError):
        cd.verificar_dia_abierto("2026-09-13", 1)

    # Y sobre otra sucursal (o sin sucursal) el mismo día sigue abierto.
    cd.verificar_dia_abierto("2026-09-13", 2)
    cd.verificar_dia_abierto("2026-09-13", None)


def test_create_turno_respeta_la_guarda_via_caja(conn, monkeypatch):
    """El camino real: `create_turno()` resuelve la sucursal DESDE la caja."""
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)
    _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
          monto_inicial=0.0, monto_esperado_cierre=0.0,
          monto_declarado_cierre=0.0, caja_id=caja1)
    cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")

    # `_ar_now()` real cae "hoy", no el 2026-09-13 — se fija el reloj del
    # módulo `turnos` para que la apertura caiga en el día ya cerrado.
    monkeypatch.setattr(db_turnos, "_ar_now", lambda: "2026-09-13 09:00:00")

    with pytest.raises(cd.DiaCerradoError):
        db_turnos.create_turno(cajero, 100.0, caja_id=caja1)

    # Otra caja, sin sucursal: sigue abriendo sin problema el mismo día.
    caja_suelta = _caja(conn, "Suelta")
    tid = db_turnos.create_turno(cajero, 100.0, caja_id=caja_suelta)
    assert db_turnos.get_turno(tid) is not None


def test_dia_cerrado_sin_tabla_no_rompe(tmp_path):
    """Si `cierres_diarios` no existe todavía (instancia sin migrar), la
    guarda no revienta: nadie cerró nunca nada, así que nada está cerrado."""
    core.configure(db_path=str(tmp_path / "sin_migrar.db"))
    c = core.get_connection()
    init_core_schema(c)  # SIN cd.crear_tablas()
    c.commit()
    try:
        assert cd.dia_cerrado("2026-09-13", 1) is False
        cd.verificar_dia_abierto("2026-09-13", 1)  # no debe levantar nada
    finally:
        c.close()
        core._db_path = None


# ── La foto, contra la vista viva ───────────────────────────────────────


def test_la_foto_no_cambia_si_despues_se_anula_un_movimiento(conn):
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)
    t1 = _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
               monto_inicial=0.0, monto_esperado_cierre=1000.0,
               monto_declarado_cierre=1000.0, caja_id=caja1)
    mid = _movimiento(conn, t1, "ingreso", 1000.0, "efectivo")

    cierre = cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")
    antes = cd.get_cierre(cierre["id"])

    db_caja.anular_caja_movimiento(mid)

    despues = cd.get_cierre(cierre["id"])
    assert despues["turnos"] == antes["turnos"]
    assert despues["medios"] == antes["medios"]
    assert despues["monto_declarado_total"] == 1000.0


def test_anulados_excluidos_de_la_foto(conn):
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)
    t1 = _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
               monto_inicial=0.0, monto_esperado_cierre=1000.0,
               monto_declarado_cierre=1000.0, caja_id=caja1)
    _movimiento(conn, t1, "ingreso", 1000.0, "efectivo")
    # Un movimiento anulado ANTES de cerrar: no debe contarse.
    _movimiento(conn, t1, "ingreso", 5000.0, "efectivo", anulado=1)

    cierre = cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")

    medios = {m["medio_pago"]: m for m in cierre["medios"]}
    assert medios["efectivo"]["ingresos"] == 1000.0


def test_cuenta_corriente_no_entra_al_arqueo(conn):
    """No es plata que entró al cajón — mismo criterio que `get_caja_resumen`."""
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)
    t1 = _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
               monto_inicial=0.0, monto_esperado_cierre=1000.0,
               monto_declarado_cierre=1000.0, caja_id=caja1)
    _movimiento(conn, t1, "ingreso", 1000.0, "efectivo")
    _movimiento(conn, t1, "ingreso", 3000.0, "cuenta_corriente")

    cierre = cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")

    medios = {m["medio_pago"] for m in cierre["medios"]}
    assert "cuenta_corriente" not in medios
    assert "cuenta corriente" not in medios


# ── Desglose por caja (varias cajas por sucursal) ───────────────────────


def test_desglose_por_caja_dentro_de_la_sucursal(conn):
    admin = _usuario(conn, "admin1")
    cajero1 = _usuario(conn, "cajero1")
    cajero2 = _usuario(conn, "cajero2")
    mostrador = _caja(conn, "Mostrador", sucursal_id=1)
    buffet = _caja(conn, "Buffet", sucursal_id=1)

    t1 = _turno(conn, cajero1, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
               monto_inicial=0.0, monto_esperado_cierre=1000.0,
               monto_declarado_cierre=1000.0, caja_id=mostrador)
    _movimiento(conn, t1, "ingreso", 1000.0, "efectivo")

    t2 = _turno(conn, cajero2, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
               monto_inicial=0.0, monto_esperado_cierre=300.0,
               monto_declarado_cierre=300.0, caja_id=buffet)
    _movimiento(conn, t2, "ingreso", 300.0, "efectivo")

    cierre = cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")

    cajas = {c["caja_nombre"]: c for c in cierre["cajas"]}
    assert set(cajas) == {"Mostrador", "Buffet"}
    assert len(cajas["Mostrador"]["turnos"]) == 1
    assert len(cajas["Buffet"]["turnos"]) == 1
    medios_mostrador = {m["medio_pago"]: m["ingresos"] for m in cajas["Mostrador"]["medios"]}
    medios_buffet = {m["medio_pago"]: m["ingresos"] for m in cajas["Buffet"]["medios"]}
    assert medios_mostrador == {"efectivo": 1000.0}
    assert medios_buffet == {"efectivo": 300.0}


# ── listar_cierres / get_cierre_turno ───────────────────────────────────


def test_listar_cierres_filtra_por_sucursal(conn):
    """`sucursal_id=None` filtra "sin sucursal" — igual que `cerrar_dia()` —,
    no "todas". Para todas hay que pedirlo con `todas=True`."""
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Centro", sucursal_id=1)
    caja2 = _caja(conn, "Norte", sucursal_id=2)
    _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
          monto_inicial=0.0, monto_esperado_cierre=0.0,
          monto_declarado_cierre=0.0, caja_id=caja1)
    _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
          monto_inicial=0.0, monto_esperado_cierre=0.0,
          monto_declarado_cierre=0.0, caja_id=caja2)
    # Y un tercero sin sucursal, para que "None filtra sin sucursal" tenga
    # algo real que distinguir de "todas".
    _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
          monto_inicial=0.0, monto_esperado_cierre=0.0,
          monto_declarado_cierre=0.0, caja_id=None)
    cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")
    cd.cerrar_dia(usuario_id=admin, sucursal_id=2, fecha="2026-09-13")
    cd.cerrar_dia(usuario_id=admin, sucursal_id=None, fecha="2026-09-13")

    assert len(cd.listar_cierres(sucursal_id=1)) == 1
    assert len(cd.listar_cierres(sucursal_id=2)) == 1
    assert len(cd.listar_cierres(sucursal_id=None)) == 1   # "sin sucursal", no "todas"
    assert len(cd.listar_cierres()) == 1                   # el default es sucursal_id=None
    assert len(cd.listar_cierres(todas=True)) == 3


def test_get_cierre_turno_para_reimprimir(conn):
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)
    t1 = _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
               monto_inicial=500.0, monto_esperado_cierre=1500.0,
               monto_declarado_cierre=1400.0, caja_id=caja1)
    _movimiento(conn, t1, "ingreso", 1000.0, "efectivo")

    cierre = cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")
    cierre_turno_id = cierre["turnos"][0]["id"]

    arqueo = cd.get_cierre_turno(cierre_turno_id)
    assert arqueo["turno_id"] == t1
    assert arqueo["cajero_nombre"] == "Cajero1"
    assert arqueo["monto_declarado"] == 1400.0
    assert arqueo["diferencia"] == -100.0
    assert {m["medio_pago"] for m in arqueo["medios"]} == {"efectivo"}

    assert cd.get_cierre_turno(999999) is None


# ── El ticket EN VIVO (arqueo_de_turno / arqueo_turno_en_vivo) ──────────


def test_arqueo_en_vivo_y_el_de_la_foto_dan_lo_mismo(conn):
    """El humano pidió imprimir al CERRAR EL TURNO, antes de que exista
    cualquier cierre diario. `arqueo_turno_en_vivo()` calcula eso; una vez que
    el día se cierra, `arqueo_de_turno()` tiene que devolver EXACTAMENTE los
    mismos números —vienen de la misma cuenta (`_medios_de_turno`,
    `_diferencia`), no de dos implementaciones."""
    admin = _usuario(conn, "admin1")
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)
    t1 = _turno(conn, cajero, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
               monto_inicial=500.0, monto_esperado_cierre=1500.0,
               monto_declarado_cierre=1400.0, caja_id=caja1)
    _movimiento(conn, t1, "ingreso", 1000.0, "efectivo")

    en_vivo = cd.arqueo_turno_en_vivo(t1)
    assert en_vivo["turno_id"] == t1
    assert en_vivo["cajero_nombre"] == "Cajero1"
    assert en_vivo["caja_nombre"] == "Mostrador"
    assert en_vivo["monto_declarado"] == 1400.0
    assert en_vivo["diferencia"] == -100.0
    assert {m["medio_pago"] for m in en_vivo["medios"]} == {"efectivo"}

    # Antes de que exista un cierre diario, `arqueo_de_turno` cae al vivo.
    assert cd.arqueo_de_turno(t1) == en_vivo

    # Se cierra el día: ahora hay una foto, y tiene que decir lo MISMO que
    # decía el cálculo en vivo (salvo las claves propias de la foto).
    cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")
    de_la_foto = cd.arqueo_de_turno(t1)
    assert de_la_foto["turno_id"] == en_vivo["turno_id"]
    assert de_la_foto["cajero_nombre"] == en_vivo["cajero_nombre"]
    assert de_la_foto["caja_nombre"] == en_vivo["caja_nombre"]
    assert de_la_foto["apertura"] == en_vivo["apertura"]
    assert de_la_foto["cierre"] == en_vivo["cierre"]
    assert de_la_foto["monto_inicial"] == en_vivo["monto_inicial"]
    assert de_la_foto["monto_esperado"] == en_vivo["monto_esperado"]
    assert de_la_foto["monto_declarado"] == en_vivo["monto_declarado"]
    assert de_la_foto["diferencia"] == en_vivo["diferencia"]
    assert {m["medio_pago"]: (m["ingresos"], m["egresos"]) for m in de_la_foto["medios"]} == {
        m["medio_pago"]: (m["ingresos"], m["egresos"]) for m in en_vivo["medios"]
    }
    # Y ahora `arqueo_de_turno` usa la foto, no vuelve a calcular en vivo:
    # se nota anulando el movimiento después. La foto sigue mostrando el
    # efectivo que había — el vivo, recalculado a mano, ya no lo mostraría
    # (`_medios_de_turno` excluye lo anulado), y es justo el contraste que
    # demuestra que `arqueo_de_turno` usó la foto y no volvió a calcular.
    db_caja.anular_caja_movimiento(
        conn.execute("SELECT id FROM caja_movimientos WHERE turno_id=?", (t1,)).fetchone()[0]
    )
    medios_de_la_foto = {m["medio_pago"]: m["ingresos"] for m in cd.arqueo_de_turno(t1)["medios"]}
    assert medios_de_la_foto == {"efectivo": 1000.0}
    medios_recalculados_a_mano = {
        m["medio_pago"]: m["ingresos"] for m in cd.arqueo_turno_en_vivo(t1)["medios"]
    }
    assert medios_recalculados_a_mano == {}, "el vivo SÍ tiene que excluir lo anulado"


def test_arqueo_de_turno_abierto_levanta_turno_abierto_error(conn):
    cajero = _usuario(conn, "cajero1")
    caja1 = _caja(conn, "Mostrador", sucursal_id=1)
    t1 = _turno(conn, cajero, "2026-09-13 08:00:00", cierre=None,
               monto_inicial=500.0, estado="abierto", caja_id=caja1)

    with pytest.raises(cd.TurnoAbiertoError):
        cd.arqueo_turno_en_vivo(t1)
    with pytest.raises(cd.TurnoAbiertoError):
        cd.arqueo_de_turno(t1)


def test_arqueo_de_turno_inexistente_es_none(conn):
    assert cd.arqueo_turno_en_vivo(999999) is None
    assert cd.arqueo_de_turno(999999) is None


# ── `dia_cerrado`: el except angosto ─────────────────────────────────────


def test_dia_cerrado_propaga_un_error_que_no_es_tabla_ausente(tmp_path):
    """El `except` de `dia_cerrado` es angosto: sólo se traga "esta tabla no
    existe". Cualquier OTRO error —acá, una conexión cerrada, que en sqlite3
    es un `ProgrammingError` REAL, la misma clase que usa la traducción de
    PostgreSQL para 'tabla ausente'— tiene que propagarse tal cual."""
    core.configure(db_path=str(tmp_path / "cerrada.db"))
    c = core.get_connection()
    init_core_schema(c)
    cd.crear_tablas(c)
    c.commit()
    c.close()  # la conexión ya no sirve para nada

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        cd.dia_cerrado("2026-09-13", 1, conn=c)

    core._db_path = None


class _ConexionQueRompeConOtroError:
    """Una conexión falsa que rompe con un error QUE NO ES "tabla ausente" —
    `rollback()` no hace nada (no rompe a su vez), así que discrimina de
    verdad: si `dia_cerrado` se tragara el error igual (un `except Exception`
    demasiado ancho, por ejemplo), acá no habría ningún segundo error que lo
    disimulara — simplemente devolvería `False` en silencio."""

    def __init__(self, excepcion: Exception):
        self._excepcion = excepcion
        self.rollback_llamado = False

    def execute(self, *a, **kw):
        raise self._excepcion

    def rollback(self):
        self.rollback_llamado = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_dia_cerrado_propaga_un_error_inyectado_que_no_es_tabla_ausente():
    """El mismo chequeo que el de arriba, pero con un doble que no puede
    confundirse con el error que se busca aislar."""
    conexion = _ConexionQueRompeConOtroError(
        sqlite3.ProgrammingError("permission denied for table cierres_diarios")
    )
    with pytest.raises(sqlite3.ProgrammingError, match="permission denied"):
        cd.dia_cerrado("2026-09-13", 1, conn=conexion)
    assert conexion.rollback_llamado is False, (
        "no debería ni haber llegado al rollback: el error no es 'tabla ausente'"
    )


def test_dia_cerrado_atrapa_la_tabla_ausente_con_conexion_inyectada():
    """El control positivo del doble: CON el mensaje de tabla ausente, sí se
    traga el error, hace `rollback()` y devuelve `False`."""
    conexion = _ConexionQueRompeConOtroError(
        sqlite3.OperationalError("no such table: cierres_diarios")
    )
    assert cd.dia_cerrado("2026-09-13", 1, conn=conexion) is False
    assert conexion.rollback_llamado is True


def test_tabla_cierres_ausente_distingue_el_mensaje():
    assert cd._tabla_cierres_ausente("no such table: cierres_diarios") is True
    assert cd._tabla_cierres_ausente('relation "cierres_diarios" does not exist') is True
    assert cd._tabla_cierres_ausente("Cannot operate on a closed database.") is False
    assert cd._tabla_cierres_ausente("no such table: turnos_caja") is False
