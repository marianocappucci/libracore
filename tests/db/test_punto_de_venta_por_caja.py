"""El punto de venta de ARCA por mostrador.

Hasta acá habia uno solo por instancia, el de `arca_config`. Un cliente con
varios POS en el mismo salon necesita numeracion fiscal separada por mostrador,
porque ARCA numera por (tipo, punto de venta).

La cadena que lo resuelve es **usuario -> turno abierto -> caja -> punto de
venta**, y no hay ningun concepto nuevo de "terminal": el turno ya sabe en que
caja esta abierto.
"""

import pytest

from libracore.db import caja as db_caja
from libracore.db import core
from libracore.db import turnos as db_turnos
from libracore.db.caja import PuntoDeVentaRepetido
from libracore.db.schema import init_core_schema


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "pv.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def _usuario(conn, username="cajero1") -> int:
    """Una fila real en `usuarios`: los turnos le declaran FK.

    Misma forma que el helper `crear_usuario` de `tests/db/conftest.py` — la
    columna es `role`, no `rol`, y `activo` es BOOLEAN en esta tabla.
    """
    cur = conn.execute(
        "INSERT INTO usuarios (username, nombre, email, password_hash, role, activo)"
        " VALUES (?, ?, '', 'sin-hash-real--este-test-no-autentica', 'operador', TRUE)",
        (username, username.title()),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def usuario(conn):
    return _usuario(conn)


# ── La columna y su unicidad ─────────────────────────────────────────────

def test_una_caja_puede_no_tener_punto_de_venta_propio(conn):
    """🔴 El caso de TODAS las instancias que existen hoy.

    `None` significa "usá el de la empresa". Si la columna fuera obligatoria, o
    tuviera un default, la migración les inventaría un punto de venta por caja a
    clientes que nunca lo pidieron.
    """
    cid = db_caja.create_caja_config("Mostrador", "", ["efectivo"])
    assert db_caja.get_caja_config(cid)["punto_venta"] is None


def test_dos_cajas_no_pueden_compartir_el_punto_de_venta(conn):
    """ARCA numera por (tipo, punto de venta).

    Dos mostradores con el mismo comparten la serie y compiten por el próximo
    número. El choque no lo detectamos nosotros: lo detecta ARCA al rechazar el
    segundo comprobante, con el cliente esperando el ticket.
    """
    db_caja.create_caja_config("Mostrador 1", "", ["efectivo"], punto_venta=3)

    with pytest.raises(PuntoDeVentaRepetido, match="Mostrador 1"):
        db_caja.create_caja_config("Mostrador 2", "", ["efectivo"], punto_venta=3)


def test_varias_cajas_sin_punto_de_venta_no_chocan_entre_si(conn):
    """`None` no es un valor repetido: es la ausencia de uno.

    Sin esto, la segunda caja de cualquier instancia existente dejaría de poder
    crearse — que es el modo de fallar más caro de una guarda de unicidad.
    """
    db_caja.create_caja_config("Mostrador 1", "", ["efectivo"])
    db_caja.create_caja_config("Mostrador 2", "", ["efectivo"])
    db_caja.create_caja_config("Mostrador 3", "", ["efectivo"])

    sin_pv = [c for c in db_caja.get_all_cajas() if c["punto_venta"] is None]
    assert len(sin_pv) >= 3


def test_editar_una_caja_no_choca_consigo_misma(conn):
    """Guardar la misma caja con el punto de venta que ya tenía tiene que andar.

    Es lo que hace la pantalla de configuración cada vez que se toca cualquier
    otro campo.
    """
    cid = db_caja.create_caja_config("Mostrador", "", ["efectivo"], punto_venta=5)
    db_caja.update_caja_config(cid, "Mostrador renombrado", "", ["efectivo"], 1,
                               punto_venta=5)
    assert db_caja.get_caja_config(cid)["nombre"] == "Mostrador renombrado"


def test_editar_hacia_un_punto_de_venta_ocupado_se_rechaza(conn):
    db_caja.create_caja_config("Mostrador 1", "", ["efectivo"], punto_venta=3)
    cid2 = db_caja.create_caja_config("Mostrador 2", "", ["efectivo"], punto_venta=4)

    with pytest.raises(PuntoDeVentaRepetido):
        db_caja.update_caja_config(cid2, "Mostrador 2", "", ["efectivo"], 1,
                                   punto_venta=3)


# ── La resolución desde el POS ───────────────────────────────────────────

def test_sin_turno_abierto_no_hay_punto_de_venta_del_pos(conn, usuario):
    """Cae al de la empresa, que es lo que hace el llamador con el `or`."""
    assert db_caja.resolver_punto_venta(usuario) is None


def test_con_turno_abierto_sale_el_de_su_caja(conn, usuario):
    """🔴 La cadena completa: usuario -> turno -> caja -> punto de venta."""
    cid = db_caja.create_caja_config("Mostrador 2", "", ["efectivo"], punto_venta=7)
    db_turnos.create_turno(usuario, 0, "", caja_id=cid)

    assert db_caja.resolver_punto_venta(usuario) == 7


def test_si_la_caja_del_turno_no_tiene_propio_cae_al_de_la_empresa(conn, usuario):
    """Un cliente de un solo POS abre turno igual, y nada tiene que cambiarle."""
    cid = db_caja.create_caja_config("Mostrador", "", ["efectivo"])
    db_turnos.create_turno(usuario, 0, "", caja_id=cid)

    assert db_caja.resolver_punto_venta(usuario) is None


def test_sin_usuario_no_hay_punto_de_venta(conn):
    assert db_caja.resolver_punto_venta(None) is None


def test_dos_cajeros_en_dos_cajas_resuelven_distinto(conn, usuario):
    """🔴 El escenario que motiva todo esto: dos POS en el mismo salón.

    Y muestra la condición operativa: la resolución es **por usuario**, así que
    cada POS necesita el suyo logueado. Dos POS con el mismo usuario comparten
    turno y por lo tanto comparten punto de venta.
    """
    otro = _usuario(conn, "cajero2")

    caja_a = db_caja.create_caja_config("POS 1", "", ["efectivo"], punto_venta=11)
    caja_b = db_caja.create_caja_config("POS 2", "", ["efectivo"], punto_venta=12)
    db_turnos.create_turno(usuario, 0, "", caja_id=caja_a)
    db_turnos.create_turno(otro, 0, "", caja_id=caja_b)

    assert db_caja.resolver_punto_venta(usuario) == 11
    assert db_caja.resolver_punto_venta(otro) == 12


def test_un_turno_cerrado_no_cuenta(conn, usuario):
    """El punto de venta sale del turno ABIERTO. Uno cerrado es historia."""
    cid = db_caja.create_caja_config("Mostrador", "", ["efectivo"], punto_venta=9)
    turno = db_turnos.create_turno(usuario, 0, "", caja_id=cid)
    assert db_caja.resolver_punto_venta(usuario) == 9, "el control: abierto resuelve"

    conn.execute("UPDATE turnos_caja SET estado='cerrado' WHERE id=?", (turno,))
    conn.commit()

    assert db_caja.resolver_punto_venta(usuario) is None
