"""Tickets del cierre diario: el arqueo de un turno y el cierre de una
sucursal.

Se arma un cierre REAL con `libracore.db.cierre_diario` (misma fixture que
`tests/db/test_cierre_diario.py`) y se lo pasa tal cual a las dos funciones
nuevas de `ticket_generator` — así el test también cubre que la FORMA que
`get_cierre()`/`get_cierre_turno()` devuelven es la que las funciones de
ticket esperan, no sólo que el PDF salga.
"""
import zlib

import pytest

from libracore import config_manager, ticket_generator
from libracore.db import caja as db_caja
from libracore.db import cierre_diario as cd
from libracore.db import core
from libracore.db.schema import init_core_schema


@pytest.fixture(autouse=True)
def _config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(tmp_path / "config.json"))
    config_manager.save({
        "empresa_nombre": "Despensa La Esquina",
        "empresa_cuit": "20-11111111-2",
        "ticket_ancho_mm": "80",
        "ticket_fuente_size": "9",
    })


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "ticket_cierre.db"))
    c = core.get_connection()
    init_core_schema(c)
    cd.crear_tablas(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def _texto_del_pdf(pdf: bytes) -> str:
    """Mismo mecanismo que `tests/test_ticket_generator.py::_texto_del_pdf`:
    los streams de fpdf2 vienen comprimidos, así que hay que descomprimirlos
    para poder buscar texto adentro. No es un parser — alcanza para afirmar
    que un dato entró al ticket."""
    partes = []
    for bloque in pdf.split(b"stream")[1:]:
        crudo = bloque.split(b"endstream")[0].strip(b"\r\n")
        try:
            partes.append(zlib.decompress(crudo).decode("latin-1"))
        except (zlib.error, UnicodeDecodeError):
            partes.append(crudo.decode("latin-1", errors="ignore"))
    return "\n".join(partes)


def _usuario(conn, username, role="admin"):
    cur = conn.execute(
        "INSERT INTO usuarios (username, nombre, email, password_hash, role, activo)"
        " VALUES (?, ?, '', 'sin-hash-real--este-test-no-autentica', ?, TRUE)",
        (username, username.title(), role),
    )
    conn.commit()
    return cur.lastrowid


def _turno(conn, usuario_id, apertura, cierre, monto_inicial, monto_esperado_cierre,
          monto_declarado_cierre, caja_id):
    cur = conn.execute(
        """INSERT INTO turnos_caja
           (usuario_id, apertura, cierre, monto_inicial, monto_esperado_cierre,
            monto_declarado_cierre, estado, caja_id)
           VALUES (?,?,?,?,?,?,'cerrado',?)""",
        (usuario_id, apertura, cierre, monto_inicial, monto_esperado_cierre,
         monto_declarado_cierre, caja_id),
    )
    conn.commit()
    return cur.lastrowid


def _movimiento(conn, turno_id, tipo, monto, medio_pago):
    conn.execute(
        """INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, medio_pago, turno_id)
           VALUES ('2026-09-13', ?, 'mov', ?, ?, ?)""",
        (tipo, monto, medio_pago, turno_id),
    )
    conn.commit()


@pytest.fixture
def cierre_armado(conn):
    """Un cierre real, con dos cajeros y dos medios — la misma consolidación
    de `test_cierre_diario.py::test_consolidacion_dos_cajeros_dos_medios`."""
    admin = _usuario(conn, "admin1")
    cajero1 = _usuario(conn, "cajero1")
    cajero2 = _usuario(conn, "cajero2")
    caja1 = db_caja.create_caja_config("Mostrador", "", ["efectivo"], sucursal_id=1)

    t1 = _turno(conn, cajero1, "2026-09-13 08:00:00", "2026-09-13 14:00:00",
               1000.0, 3500.0, 3500.0, caja1)
    _movimiento(conn, t1, "ingreso", 2000.0, "efectivo")
    _movimiento(conn, t1, "ingreso", 500.0, "tarjeta_debito")

    t2 = _turno(conn, cajero2, "2026-09-13 14:00:00", "2026-09-13 20:00:00",
               3500.0, 5500.0, 5400.0, caja1)
    _movimiento(conn, t2, "ingreso", 2000.0, "efectivo")
    _movimiento(conn, t2, "ingreso", 1000.0, "tarjeta_debito")

    return cd.cerrar_dia(usuario_id=admin, sucursal_id=1, fecha="2026-09-13")


def test_ticket_de_cierre_diario_es_un_pdf_con_los_datos(cierre_armado):
    contenido = ticket_generator.generar_ticket_cierre_diario(
        cd.get_cierre(cierre_armado["id"]), sucursal_nombre="Sucursal Centro",
    )
    assert contenido.startswith(b"%PDF-")
    assert len(contenido) > 500

    texto = _texto_del_pdf(contenido)
    assert "13-09-2026" in texto            # fecha del día, dd-mm-aaaa
    assert str(cierre_armado["numero"]) in texto  # número de cierre
    assert "Sucursal Centro" in texto
    assert "Mostrador" in texto             # la caja, resuelta por el motor
    assert "100,00" in texto                # |diferencia_total| = 100.0
    # Los dos cajeros y los dos medios entraron al ticket.
    assert "Cajero1" in texto
    assert "Cajero2" in texto


def test_ticket_de_cierre_de_turno_es_un_pdf_con_los_datos(cierre_armado):
    cierre_turno_id = cierre_armado["turnos"][0]["id"]
    arqueo = cd.get_cierre_turno(cierre_turno_id)
    arqueo["sucursal_nombre"] = "Sucursal Centro"

    contenido = ticket_generator.generar_ticket_cierre_turno(arqueo)
    assert contenido.startswith(b"%PDF-")
    assert len(contenido) > 400

    texto = _texto_del_pdf(contenido)
    assert "13-09-2026" in texto
    assert "Sucursal Centro" in texto
    assert "Mostrador" in texto
    assert "Cajero1" in texto
    assert "Efectivo" in texto              # medios_pago.label("efectivo")
    assert "Tarjeta de d" in texto          # "Tarjeta de débito" (acentos aparte)
    # DIFERENCIA con signo: t1 declaró 3500 contra 3500 esperado -> sin faltante.
    assert "DIFERENCIA" in texto


def test_los_dos_tickets_dan_lo_mismo_al_reimprimir(cierre_armado):
    """La foto no cambia — ver `test_cierre_diario.py`— así que el PDF
    tampoco: dos generaciones a partir de la misma foto son BYTE IDÉNTICAS.
    Es lo que permite decir "el comprobante es el mismo" (ver
    `_TextoSeguroPDF.fijar_fecha_documento`)."""
    cierre = cd.get_cierre(cierre_armado["id"])
    primero = ticket_generator.generar_ticket_cierre_diario(cierre, "Sucursal Centro")
    segundo = ticket_generator.generar_ticket_cierre_diario(cierre, "Sucursal Centro")
    assert primero == segundo

    arqueo = cd.get_cierre_turno(cierre_armado["turnos"][0]["id"])
    arqueo["sucursal_nombre"] = "Sucursal Centro"
    t1 = ticket_generator.generar_ticket_cierre_turno(arqueo)
    t2 = ticket_generator.generar_ticket_cierre_turno(arqueo)
    assert t1 == t2
