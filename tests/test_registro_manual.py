"""El registro de un comprobante cuyo número viene de afuera: `db.facturas.registrar_comprobante`.

Un producto que no emite por ARCA para una razón social registra el número que
el operador tipea (LibraCargo, su ADR-024). Lo que se fija acá:

- **El número es el dato**: nunca se cambia. Si ya está registrado para ese
  emisor, tipo y punto de venta, `NumeroYaRegistrado`, y no un número nuevo
  como haría `create_factura`.
- Por emisor: otra razón social puede tener el mismo número.
- Siempre `produccion`: entra al libro IVA.
- Un error que no es de número repetido (una FK que no existe) sale tal cual.
"""

import sqlite3

import pytest

from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db import libros_iva as db_libros_iva
from libracore.db.schema import init_core_schema

FECHA = "2026-10-05"


@pytest.fixture
def emisores(tmp_path):
    core.configure(db_path=str(tmp_path / "registro.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    ids = [
        conn.execute(
            "INSERT INTO arca_config (empresa, cuit, punto_venta, clave_path, certificado_path) "
            "VALUES (?, ?, 1, '', '')", (empresa, cuit)).lastrowid
        for empresa, cuit in (("agencia", "20111111112"), ("transporte", "30222222223"))
    ]
    conn.commit()
    conn.close()
    return ids


def _registrar(numero, emisor_id=None, **extra):
    return db_facturas.registrar_comprobante(
        1, 3, numero, FECHA, "30555555556", "Cerealera SA", 1,
        [{"description": "Flete", "qty": 1, "unit_price": 100.0, "subtotal": 100.0}], 100.0, 21.0, 121.0,
        emisor_id=emisor_id, **extra)


def test_registra_con_el_numero_tipeado_y_entra_al_libro_iva(emisores):
    factura = db_facturas.get_factura(_registrar(4567, emisores[0], cae="76543210987654",
                                                 observaciones="Talonario"))
    assert (factura["numero"], factura["punto_venta"], factura["emisor_id"]) == (4567, 3, emisores[0])
    assert (factura["ambiente"], factura["cae"], factura["observaciones"]) == ("produccion", "76543210987654", "Talonario")
    assert [f["id"] for f in db_libros_iva.get_facturas_para_iva(FECHA, FECHA)] == [factura["id"]]


def test_un_numero_repetido_no_se_cambia_por_otro(emisores):
    _registrar(10, emisores[0])
    with pytest.raises(db_facturas.NumeroYaRegistrado):
        _registrar(10, emisores[0])
    conn = core.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM facturas").fetchone()[0] == 1
    conn.close()


def test_otro_emisor_puede_tener_el_mismo_numero(emisores):
    _registrar(10, emisores[0])
    _registrar(10, emisores[1])
    _registrar(10)


def test_un_repetido_que_gana_la_carrera_tambien_es_numero_ya_registrado(emisores, monkeypatch):
    """Entre la consulta y el INSERT lo registró otro: lo frena el índice único, y se dice igual."""
    _registrar(10, emisores[0])
    respuestas = iter([False, True])  # antes del INSERT «no está»; después, sí
    monkeypatch.setattr(db_facturas, "_ya_existe", lambda *a: next(respuestas))
    with pytest.raises(db_facturas.NumeroYaRegistrado):
        _registrar(10, emisores[0])


def test_otra_violacion_no_se_disfraza_de_numero_repetido(emisores):
    with pytest.raises(sqlite3.IntegrityError):
        _registrar(10, 999)


@pytest.mark.parametrize("numero", [0, -1, "10", True, 1.5])
def test_el_numero_es_un_entero_positivo(emisores, numero):
    with pytest.raises(ValueError):
        _registrar(numero)


def test_un_campo_desconocido_no_se_traga(emisores):
    with pytest.raises(TypeError):
        _registrar(10, observacion="mal escrito")


def test_create_factura_sigue_reintentando_con_el_siguiente(emisores):
    """El reintento de `create_factura` no cambió: su número lo calcula el motor."""
    _registrar(1, emisores[0])
    nuevo = db_facturas.create_factura(
        1, 3, 1, FECHA, "", "x", 1, [], 1, 0, 1, ambiente="produccion", emisor_id=emisores[0])
    assert db_facturas.get_factura(nuevo)["numero"] == 2
