"""La auditoría de escrituras de `clients` (`libracore.auditoria`, ADR-043), contra los dos motores.

Lo que se fija, en orden de lo que se rompe sin que se note:

1. **Sin registrador, nada cambia** y no se lee de más: ningún `SELECT` extra.
2. **Con registrador**, cada escritura de `clients` emite UN `Cambio` con la fila de antes y la
   de después: alta, edición, desactivar, activar, `auto_facturar`, el cliente externo y la baja.
3. **Es transaccional**: si el registrador falla, no queda la fila, ni su espejo en `parties`,
   ni el cambio de `activo`. Es la diferencia de fondo con `al_cambiar` de ARCA.
4. `crear_cliente` / `actualizar_cliente` escriben exactamente las claves que reciben, con
   columnas propias de un producto, y no cambian lo que hacen `create_client` / `update_client`.
5. La baja sigue bloqueada si el cliente tiene remitos o presupuestos, y no deja registro.
6. La línea de tiempo de Logs (`libracore.db.logs`) muestra las filas de `actividad_log`
   cuando la tabla existe, y no se rompe cuando no.
"""
import os

import pytest

from libracore import auditoria
from libracore.db import clients, core, logs
from libracore.db.schema import init_core_schema

# El DDL de `actividad_log` que crea `libracommerce` (Contalibra, Restolibra, VentaLibra).
_ACTIVIDAD_LOG_DDL = """
    CREATE TABLE actividad_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        usuario TEXT NOT NULL DEFAULT 'Sistema',
        accion TEXT NOT NULL,
        entidad TEXT NOT NULL,
        entidad_id INTEGER,
        descripcion TEXT NOT NULL DEFAULT '',
        cambios TEXT
    )
"""

# Subconjunto de `parties` de LibraCommerce (ver test_clients_parties.py).
_PARTIES_DDL = """
    CREATE TABLE parties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        party_type TEXT NOT NULL,
        display_name TEXT NOT NULL,
        legal_name TEXT,
        tax_id TEXT,
        tax_id_type TEXT,
        email TEXT,
        phone TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
"""


@pytest.fixture(params=["sqlite", "postgres"])
def base(request, tmp_path):
    if request.param == "postgres":
        url = os.environ.get("LIBRACORE_POSTGRES_URL")
        if not url:
            pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
        import psycopg

        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True) as c:
            c.execute("DROP SCHEMA IF EXISTS public CASCADE")
            c.execute("CREATE SCHEMA public")
        core.configure(db_path=url)
    else:
        core.configure(db_path=str(tmp_path / "auditoria.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
    yield request.param
    auditoria.configurar_auditoria(None)
    core._db_path = None
    core._database_url = None


@pytest.fixture
def con_parties(base):
    with core.get_connection() as conn:
        conn.execute(_PARTIES_DDL)
    return base


@pytest.fixture
def registrados(base):
    """Un registrador falso que anota los cambios que recibe."""
    anotados = []
    auditoria.configurar_auditoria(lambda conn, cambio: anotados.append(cambio))
    return anotados


def _contar(tabla, donde="1=1", params=()):
    with core.get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {tabla} WHERE {donde}", params).fetchone()[0]


def _remito(client_id):
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO remitos (number, date, client_id, client_name, items, subtotal, tax_amount, total) "
            "VALUES ('R-1', '2026-10-12', ?, 'x', '[]', 0, 0, 0)", (client_id,))


# ── 1. Sin registrador, nada cambia ──────────────────────────────────────────

def test_sin_registrador_no_hay_auditoria_ni_selects_de_mas(base, monkeypatch):
    assert not auditoria.auditando()

    def _no_deberia_leer(*a, **k):
        raise AssertionError("se leyó la fila de clients sin registrador")

    monkeypatch.setattr(clients, "_fila_cliente", _no_deberia_leer)

    cid = clients.create_client("Ana", cuit_dni="20-11111111-2")
    clients.update_client(cid, name="Ana B")
    clients.desactivar_cliente(cid)
    clients.activar_cliente(cid)
    assert clients.toggle_auto_facturar(cid) is True
    ext = clients.resolver_cliente_externo("party:1", "Externo")
    clients.resolver_cliente_externo("party:1", "Externo 2")
    nuevo = clients.crear_cliente({"name": "Beto", "activo": 0})
    assert clients.actualizar_cliente(nuevo, {"name": "Beto B"}) is True
    clients.delete_client(ext)
    clients.delete_client(nuevo)

    assert clients.get_client(cid)["name"] == "Ana B"
    assert clients.get_client(ext) is None


def test_registrar_sin_registrador_no_toca_la_conexion():
    auditoria.configurar_auditoria(None)

    class _Conexion:
        def __getattr__(self, nombre):
            raise AssertionError(f"tocó la conexión: {nombre}")

    auditoria.registrar(_Conexion(), "clients", auditoria.CREAR, 1, None, {"id": 1})


def test_configurar_auditoria_es_idempotente_y_none_lo_quita():
    def uno(conn, cambio):
        pass

    auditoria.configurar_auditoria(uno)
    auditoria.configurar_auditoria(uno)
    assert auditoria.auditando()
    auditoria.configurar_auditoria(None)
    assert not auditoria.auditando()


def test_el_cambio_es_inmutable():
    cambio = auditoria.Cambio("clients", auditoria.CREAR, 1)
    with pytest.raises(AttributeError):
        cambio.id = 2  # type: ignore[misc]


# ── 2. Con registrador: antes y después ──────────────────────────────────────

def test_el_alta_emite_crear_con_la_fila_de_despues(base, registrados):
    cid = clients.create_client("Ana", cuit_dni="20-11111111-2", email="ana@x.com")

    assert len(registrados) == 1
    c = registrados[0]
    assert (c.tabla, c.accion, c.id, c.antes) == ("clients", "crear", cid, None)
    assert c.despues["name"] == "Ana" and c.despues["email"] == "ana@x.com"
    assert c.despues["id"] == cid and c.despues["activo"] == 1


def test_la_edicion_emite_editar_con_antes_y_despues(base, registrados):
    cid = clients.create_client("Ana", phone="111")
    registrados.clear()

    clients.update_client(cid, name="Ana B", phone="222")

    (c,) = registrados
    assert (c.accion, c.id) == ("editar", cid)
    assert (c.antes["name"], c.antes["phone"]) == ("Ana", "111")
    assert (c.despues["name"], c.despues["phone"]) == ("Ana B", "222")


def test_desactivar_y_activar_son_ediciones_de_activo(base, registrados):
    cid = clients.create_client("Ana")
    registrados.clear()

    clients.desactivar_cliente(cid)
    clients.activar_cliente(cid)

    des, act = registrados
    assert (des.accion, des.antes["activo"], des.despues["activo"]) == ("editar", 1, 0)
    assert (act.accion, act.antes["activo"], act.despues["activo"]) == ("editar", 0, 1)


def test_toggle_auto_facturar_emite_la_edicion(base, registrados):
    cid = clients.create_client("Ana")
    registrados.clear()

    assert clients.toggle_auto_facturar(cid) is True

    (c,) = registrados
    assert (c.accion, c.antes["auto_facturar"], c.despues["auto_facturar"]) == ("editar", 0, 1)


def test_el_cliente_externo_emite_crear_y_despues_editar_el_nombre(base, registrados):
    cid = clients.resolver_cliente_externo("party:9", "Externo")
    assert clients.resolver_cliente_externo("party:9", "Externo SA") == cid

    alta, edicion = registrados
    assert (alta.accion, alta.id, alta.despues["external_ref"]) == ("crear", cid, "party:9")
    assert (edicion.accion, edicion.antes["name"], edicion.despues["name"]) == ("editar", "Externo", "Externo SA")


def test_la_baja_emite_borrar_con_la_fila_leida_antes_del_delete(base, registrados):
    cid = clients.create_client("Ana", cuit_dni="20-11111111-2")
    registrados.clear()

    clients.delete_client(cid)

    (c,) = registrados
    assert (c.accion, c.id, c.despues) == ("borrar", cid, None)
    assert c.antes["name"] == "Ana" and c.antes["cuit_dni"] == "20-11111111-2"
    assert clients.get_client(cid) is None


def test_lo_que_no_existia_no_se_registra(base, registrados):
    clients.desactivar_cliente(999)
    clients.update_client(999, name="x")
    clients.delete_client(999)
    assert clients.actualizar_cliente(999, {"name": "x"}) is False
    assert registrados == []


def test_el_registrador_recibe_la_conexion_de_la_escritura(base):
    """Es lo que permite el INSERT en la misma transacción: la misma conexión, no otra."""
    recibidas = []
    auditoria.configurar_auditoria(lambda conn, cambio: recibidas.append(conn))

    clients.create_client("Ana")

    assert len(recibidas) == 1 and hasattr(recibidas[0], "execute")


# ── 3. Transaccional ─────────────────────────────────────────────────────────

class _FalloDelLog(Exception):
    pass


def _registrador_roto(conn, cambio):
    raise _FalloDelLog("el INSERT del log falló")


def test_si_el_registrador_falla_no_queda_el_cliente_ni_su_espejo(con_parties):
    auditoria.configurar_auditoria(_registrador_roto)

    with pytest.raises(_FalloDelLog):
        clients.create_client("Ana", cuit_dni="20-11111111-2")
    with pytest.raises(_FalloDelLog):
        clients.crear_cliente({"name": "Beto"})

    assert _contar("clients") == 0
    assert _contar("parties") == 0


def test_si_el_registrador_falla_la_edicion_no_se_aplica(con_parties):
    cid = clients.create_client("Ana")
    auditoria.configurar_auditoria(_registrador_roto)

    with pytest.raises(_FalloDelLog):
        clients.update_client(cid, name="Ana B")
    with pytest.raises(_FalloDelLog):
        clients.actualizar_cliente(cid, {"name": "Ana C"})
    with pytest.raises(_FalloDelLog):
        clients.desactivar_cliente(cid)
    with pytest.raises(_FalloDelLog):
        clients.toggle_auto_facturar(cid)

    fila = clients.get_client(cid)
    assert (fila["name"], fila["activo"], fila["auto_facturar"]) == ("Ana", 1, 0)
    with core.get_connection() as conn:
        espejo = conn.execute("SELECT display_name, active FROM parties WHERE id = ?", (cid,)).fetchone()
    assert (espejo["display_name"], espejo["active"]) == ("Ana", 1)


def test_si_el_registrador_falla_la_baja_no_borra(base):
    cid = clients.create_client("Ana")
    auditoria.configurar_auditoria(_registrador_roto)

    with pytest.raises(_FalloDelLog):
        clients.delete_client(cid)

    assert clients.get_client(cid) is not None


def test_si_el_registrador_falla_el_cliente_externo_no_se_crea(base):
    auditoria.configurar_auditoria(_registrador_roto)

    with pytest.raises(_FalloDelLog):
        clients.resolver_cliente_externo("party:1", "Externo")

    assert _contar("clients") == 0


def test_el_insert_del_log_y_la_escritura_viven_y_mueren_juntos(base):
    """Con un registrador de verdad (un INSERT en `actividad_log` con la conexión que recibe)."""
    with core.get_connection() as conn:
        conn.execute(_ACTIVIDAD_LOG_DDL)

    def registrar_en_la_tabla(conn, cambio):
        conn.execute(
            "INSERT INTO actividad_log (ts, usuario, accion, entidad, entidad_id, descripcion) "
            "VALUES ('2026-10-12 10:00:00', 'u', ?, 'cliente', ?, 'x')", (cambio.accion, str(cambio.id)))

    auditoria.configurar_auditoria(registrar_en_la_tabla)
    cid = clients.create_client("Ana")
    assert _contar("actividad_log", "entidad_id = ?", (cid,)) == 1

    # Y si después de escribir el log algo falla en la misma transacción, caen los dos.
    def registra_y_revienta(conn, cambio):
        registrar_en_la_tabla(conn, cambio)
        raise _FalloDelLog

    auditoria.configurar_auditoria(registra_y_revienta)
    with pytest.raises(_FalloDelLog):
        clients.update_client(cid, name="Ana B")

    assert clients.get_client(cid)["name"] == "Ana"
    assert _contar("actividad_log") == 1


# ── 4. crear_cliente / actualizar_cliente ────────────────────────────────────

def test_crear_cliente_escribe_exactamente_las_claves_y_none_es_null(base):
    cid = clients.crear_cliente({"name": "Ana", "address": None, "cuit_dni": "20-11111111-2", "activo": 0})

    fila = clients.get_client(cid)
    assert fila["name"] == "Ana" and fila["address"] is None and fila["activo"] == 0
    # `create_client` escribe "" donde `crear_cliente` no escribió nada.
    assert clients.get_client(clients.create_client("Beto"))["address"] == ""


def test_crear_cliente_acepta_columnas_propias_del_producto(base):
    with core.get_connection() as conn:
        conn.execute("ALTER TABLE clients ADD COLUMN price_list_id INTEGER")

    cid = clients.crear_cliente({"name": "Ana", "price_list_id": 7})
    clients.actualizar_cliente(cid, {"price_list_id": None})

    assert clients.get_client(cid)["price_list_id"] is None
    assert clients.crear_cliente({"name": "Beto", "price_list_id": 3}) != cid


@pytest.mark.parametrize("datos", [{"name": "x", "no_existe": 1}, {"name": "x", "id": 5}, {}])
def test_una_columna_desconocida_o_vacia_es_un_value_error_y_no_escribe(base, datos):
    with pytest.raises(ValueError):
        clients.crear_cliente(datos)
    with pytest.raises(ValueError):
        clients.actualizar_cliente(1, datos)
    assert _contar("clients") == 0


def test_actualizar_cliente_solo_toca_lo_que_recibe(base):
    cid = clients.create_client("Ana", address="Calle 1", phone="111")

    assert clients.actualizar_cliente(cid, {"phone": None, "name": "Ana B"}) is True

    fila = clients.get_client(cid)
    assert (fila["name"], fila["phone"], fila["address"]) == ("Ana B", None, "Calle 1")


def test_crear_y_actualizar_cliente_mantienen_el_espejo_en_parties(con_parties):
    cid = clients.crear_cliente({"name": "Ana", "cuit_dni": "20-11111111-2", "activo": 0})
    clients.actualizar_cliente(cid, {"email": "ana@x.com", "name": "Ana B"})

    with core.get_connection() as conn:
        p = conn.execute("SELECT display_name, tax_id, email, active FROM parties WHERE id = ?", (cid,)).fetchone()
    assert (p["display_name"], p["tax_id"], p["email"], p["active"]) == ("Ana B", "20-11111111-2", "ana@x.com", 0)

    clients.actualizar_cliente(cid, {"activo": 1})
    with core.get_connection() as conn:
        assert conn.execute("SELECT active FROM parties WHERE id = ?", (cid,)).fetchone()[0] == 1


def test_crear_y_actualizar_cliente_emiten_el_cambio(base, registrados):
    cid = clients.crear_cliente({"name": "Ana", "activo": 0})
    clients.actualizar_cliente(cid, {"activo": 1})

    alta, edicion = registrados
    assert (alta.accion, alta.despues["activo"]) == ("crear", 0)
    assert (edicion.accion, edicion.antes["activo"], edicion.despues["activo"]) == ("editar", 0, 1)


def test_create_y_update_client_conservan_su_semantica_con_registrador(base, registrados):
    """Con o sin registrador la escritura es la misma: `None` = no tocar, y los textos de
    las cuatro columnas de la 0002 siguen yendo como "" cuando la instancia las tiene."""
    with core.get_connection() as conn:
        for col in ("empresa", "ciudad", "observaciones"):
            conn.execute(f"ALTER TABLE clients ADD COLUMN {col} TEXT DEFAULT ''")
        conn.execute("ALTER TABLE clients ADD COLUMN tipo_facturacion TEXT NOT NULL DEFAULT 'por_servicio'")

    cid = clients.create_client("Ana", empresa=None, ciudad=None)
    fila = clients.get_client(cid)
    assert (fila["empresa"], fila["ciudad"], fila["tipo_facturacion"]) == ("", "", "por_servicio")

    clients.update_client(cid, phone="1", empresa=None)
    fila = clients.get_client(cid)
    assert (fila["phone"], fila["empresa"], fila["name"]) == ("1", "", "Ana")


# ── 5. La baja sigue bloqueada ───────────────────────────────────────────────

def test_la_baja_sigue_bloqueada_con_remitos_y_no_deja_registro(base, registrados):
    cid = clients.create_client("Ana")
    _remito(cid)
    registrados.clear()

    with pytest.raises(ValueError, match="remito"):
        clients.delete_client(cid)

    assert clients.get_client(cid) is not None
    assert registrados == []


def test_la_baja_sigue_bloqueada_con_presupuestos(base, registrados):
    cid = clients.create_client("Ana")
    with core.get_connection() as conn:
        conn.execute(
            "INSERT INTO presupuestos (number, date, valid_until, client_id, client_name, items, subtotal, "
            "tax_amount, total) VALUES ('P-1', '2026-10-12', '2026-11-12', ?, 'x', '[]', 0, 0, 0)", (cid,))
    registrados.clear()

    with pytest.raises(ValueError, match="presupuesto"):
        clients.delete_client(cid)

    assert registrados == []


_MOVIMIENTOS_DE_CUENTA = {
    "cc_pagos": "INSERT INTO cc_pagos (cliente_id, monto, fecha) VALUES (?, 100, '2026-10-15')",
    "cc_debitos": "INSERT INTO cc_debitos (cliente_id, monto, fecha) VALUES (?, 100, '2026-10-15')",
    "recibos": "INSERT INTO recibos (numero, fecha, cliente_id, cliente_razon, origen_tipo, total) "
               "VALUES (1, '2026-10-15', ?, 'x', 'cc_pago', 100)",
    "cc_asientos": "INSERT INTO cc_asientos (fecha, tercero_id, rol, concepto, debe) "
                   "VALUES ('2026-10-15', ?, 'cliente', 'Saldo', 100)",
}


@pytest.mark.parametrize("tabla", sorted(_MOVIMIENTOS_DE_CUENTA))
def test_la_baja_se_bloquea_con_cuenta_corriente_y_no_se_lleva_los_movimientos(base, registrados, tabla):
    cid = clients.create_client("Ana")
    with core.get_connection() as conn:
        conn.execute(_MOVIMIENTOS_DE_CUENTA[tabla], (cid,))
    registrados.clear()

    with pytest.raises(ValueError, match=r"1 movimiento\(s\) de cuenta corriente.*desactivar"):
        clients.delete_client(cid)

    assert clients.get_client(cid) is not None
    assert _contar(tabla) == 1
    assert registrados == []


def test_un_asiento_de_otro_rol_con_el_mismo_id_no_bloquea_la_baja(base):
    """El libro de terceros es de varios roles: un proveedor con el mismo número no es el cliente."""
    cid = clients.create_client("Ana")
    with core.get_connection() as conn:
        conn.execute(_MOVIMIENTOS_DE_CUENTA["cc_asientos"].replace("'cliente'", "'proveedor'"), (cid,))

    clients.delete_client(cid)

    assert clients.get_client(cid) is None


def test_la_baja_suma_remitos_y_cuenta_corriente_en_el_mensaje(base):
    cid = clients.create_client("Ana")
    _remito(cid)
    with core.get_connection() as conn:
        for tabla in ("cc_pagos", "cc_debitos"):
            conn.execute(_MOVIMIENTOS_DE_CUENTA[tabla], (cid,))

    with pytest.raises(ValueError, match=r"1 remito\(s\) y 2 movimiento\(s\) de cuenta corriente"):
        clients.delete_client(cid)


# ── 6. La línea de tiempo de Logs ────────────────────────────────────────────

def test_logs_sin_la_tabla_no_se_rompe_y_no_muestra_nada(base):
    clients.create_client("Ana")
    assert logs.get_actividad_log() == []
    assert logs.get_actividad_count() == 0


def test_logs_muestra_las_filas_de_actividad_log_cuando_la_tabla_existe(base):
    with core.get_connection() as conn:
        conn.execute(_ACTIVIDAD_LOG_DDL)
        conn.execute(
            "INSERT INTO usuarios (username, nombre, email, password_hash, role, activo) "
            "VALUES ('mcappucci', 'Mariano', '', 'x', 'admin', TRUE)")
        conn.execute(
            "INSERT INTO actividad_log (ts, usuario, accion, entidad, entidad_id, descripcion) VALUES "
            "('2026-10-12 10:30:00', 'mcappucci', 'crear', 'cliente', 4, 'Cliente — Ana'), "
            "('2026-10-12 11:00:00', 'Sistema', 'editar', 'cliente', 4, 'Cliente — Ana B'), "
            "('2026-10-12 12:00:00', 'Sistema', 'borrar', 'cliente', 4, 'Cliente — Ana B')")

    filas = logs.get_actividad_log()

    assert [f["descripcion"] for f in filas] == [
        "Cliente — Ana B (baja)", "Cliente — Ana B (edición)", "Cliente — Ana (alta)"]
    alta = filas[-1]
    assert (alta["tipo"], alta["fecha"], alta["ts"], alta["ref_tabla"]) == (
        "actividad", "2026-10-12", "2026-10-12 10:30:00", "actividad_log")
    # El login se traduce al nombre, como en las otras partes; sin usuario conocido queda como está.
    assert (alta["usuario"], filas[0]["usuario"]) == ("Mariano", "Sistema")
    assert logs.get_actividad_count() == 3
    assert [f["descripcion"] for f in logs.get_actividad_log(tipos=["actividad"])] == [f["descripcion"] for f in filas]
    assert logs.get_actividad_log(tipos=["caja"]) == []
    assert logs.get_actividad_log(desde="2026-10-13") == []


def test_logs_filtra_estas_filas_por_usuario(base):
    with core.get_connection() as conn:
        conn.execute(_ACTIVIDAD_LOG_DDL)
        uid = conn.execute(
            "INSERT INTO usuarios (username, nombre, email, password_hash, role, activo) "
            "VALUES ('mcappucci', 'Mariano', '', 'x', 'admin', TRUE)").lastrowid
        conn.execute(
            "INSERT INTO actividad_log (ts, usuario, accion, entidad, descripcion) VALUES "
            "('2026-10-12 10:30:00', 'mcappucci', 'crear', 'cliente', 'Cliente — Ana'), "
            "('2026-10-12 11:00:00', 'otro', 'crear', 'cliente', 'Cliente — Beto')")

    filas = logs.get_actividad_log(usuario_id=uid)

    assert [f["descripcion"] for f in filas] == ["Cliente — Ana (alta)"]


def test_logs_con_ts_timestamp_como_la_tabla_de_libraauth(base):
    """En las instancias donde `actividad_log` la crea libraauth, `ts` es `timestamp` y no TEXT:
    el UNION exige tipos compatibles."""
    if base != "postgres":
        pytest.skip("en SQLite `timestamp` es texto: no hay nada que comparar")
    with core.get_connection() as conn:
        conn.execute(
            "CREATE TABLE actividad_log (id SERIAL PRIMARY KEY, ts TIMESTAMP NOT NULL, "
            "usuario VARCHAR(100) NOT NULL, accion VARCHAR(20) NOT NULL, entidad VARCHAR(50) NOT NULL, "
            "entidad_id VARCHAR(100), descripcion VARCHAR(500) NOT NULL, cambios TEXT)")
        conn.execute(
            "INSERT INTO actividad_log (ts, usuario, accion, entidad, entidad_id, descripcion) "
            "VALUES ('2026-10-12 10:30:00.123456', 'u', 'crear', 'cliente', 'x-1', 'Cliente — Ana')")

    (fila,) = logs.get_actividad_log()

    assert (fila["fecha"], fila["descripcion"]) == ("2026-10-12", "Cliente — Ana (alta)")
    assert fila["ts"].startswith("2026-10-12 10:30:00")
