"""`true`/`false` no son un número en los cuerpos de `facturas_router` (ADR-013).

`tipo`, `concepto` y `punto_venta` viajan al comprobante que se pide a ARCA: un `{"tipo": true}` pedía una Factura A (tipo 1). Mismo mecanismo que `tests/test_routers_booleanos.py`: para cada campo,
`true` y `false` dan 422 con «<campo> tiene que ser un número, no un booleano» y no escriben nada (se compara el contenido de TODAS las tablas); el cuerpo numérico y cada número como texto siguen
dando 200. Se monta el router con el `_montar` de `tests/test_facturas_router.py` (ENV de desarrollo: no sale a ARCA).
"""

from __future__ import annotations

import pytest
from test_facturas_router import API, _factura, _montar
from test_routers_booleanos import _instantanea, _verificar

from libracore.db import caja as db_caja
from libracore.db import clients as db_clients


def _cuerpo(cliente: int):
    return lambda n: _factura(concepto=1, tax_rate=0.21, client_id=cliente, punto_venta=1,
                              items=[{"description": "Alquiler de cancha", "qty": 1, "unit_price": 14000.0}])


_CAMPOS = [("tipo",), ("punto_venta",), ("concepto",), ("tax_rate",), ("client_id",), ("items", 0, "qty"), ("items", 0, "unit_price")]


def test_emision_de_una_factura(tmp_path, monkeypatch):
    client = _montar(tmp_path, monkeypatch)
    cliente = db_clients.create_client("Juan Perez")
    _verificar(client, "POST", API, _cuerpo(cliente), _CAMPOS)


def test_borrador_pdf(tmp_path, monkeypatch):
    client = _montar(tmp_path, monkeypatch)
    cliente = db_clients.create_client("Juan Perez")
    _verificar(client, "POST", f"{API}/borrador-pdf", _cuerpo(cliente), _CAMPOS)


def test_cobro_de_una_factura(tmp_path, monkeypatch):
    client = _montar(tmp_path, monkeypatch)
    cliente = db_clients.create_client("Juan Perez")
    caja = db_caja.create_caja_config("Mostrador", "", ["efectivo"])
    factura = client.post(API, json=_cuerpo(cliente)(0)).json()
    ok = lambda n: {"caja_id": caja, "pagos": [{"medio_id": "efectivo", "monto": 100.0}]}  # noqa: E731
    _verificar(client, "POST", f"{API}/{factura['id']}/cobrar", ok, [("caja_id",)])


def test_un_booleano_no_pide_una_factura_a(tmp_path, monkeypatch):
    """El defecto de punta a punta: antes `tipo: true` entraba como el comprobante tipo 1 (Factura A) y no escribía el 422."""
    client = _montar(tmp_path, monkeypatch)
    antes = _instantanea()
    r = client.post(API, json=_factura(tipo=True))
    assert r.status_code == 422 and "tipo tiene que ser un número, no un booleano" in r.text
    assert _instantanea() == antes


# ═══════════════════════════════════════════════════ `pagos`: dicts sin tipar (ADR-013)


def test_un_booleano_en_un_pago_no_es_un_cobro_de_un_peso(tmp_path, monkeypatch):
    """`pagos` es `list[dict]` a propósito (las filas vacías del formulario traen `""`), así que `sin_booleanos` no llega: `{"monto": true}` era `float(True)`, un cobro de 1 peso."""
    client = _montar(tmp_path, monkeypatch)
    cliente = db_clients.create_client("Juan Perez")
    caja = db_caja.create_caja_config("Mostrador", "", ["efectivo"])
    factura = client.post(API, json=_cuerpo(cliente)(0)).json()
    url = f"{API}/{factura['id']}/cobrar"
    antes = _instantanea()
    for campo, valor, mensaje in [("monto", True, "pagos[].monto tiene que ser un número, no un booleano"),
                                  ("monto", False, "pagos[].monto tiene que ser un número, no un booleano"),
                                  ("medio_id", True, "pagos[].medio_id tiene que ser un texto, no un booleano")]:
        pago = {"medio_id": "efectivo", "monto": 100.0, "referencia": ""} | {campo: valor}
        # En cualquier fila, no sólo en la primera; con `caja_id` y sin él.
        for pagos in ([pago], [{"medio_id": "efectivo", "monto": 50.0}, pago]):
            r = client.post(url, json={"caja_id": caja, "pagos": pagos})
            assert r.status_code == 422 and mensaje in r.text, (campo, valor, r.status_code, r.text)
    assert _instantanea() == antes, "un 422 por booleano en `pagos` escribió algo"
    assert client.get(f"{API}/{factura['id']}").json()["pendiente"] == 14000.0


def test_el_contrato_de_pagos_no_cambia(tmp_path, monkeypatch):
    """Mismas claves, mismos opcionales, mismas filas vacías, el monto como texto y los campos de más: lo de siempre."""
    client = _montar(tmp_path, monkeypatch)
    factura = client.post(API, json=_cuerpo(db_clients.create_client("Juan Perez"))(0)).json()
    pagos = [{"medio_id": "efectivo", "monto": "100.5"},                      # texto numérico
             {"medio_id": "efectivo", "monto": ""},                           # fila vacía del formulario
             {"medio_id": "efectivo"},                                        # sin monto
             {"medio_id": "efectivo", "monto": None, "referencia": "x"},
             {"monto": 20, "referencia": "r", "medio_nombre": "Efectivo"}]    # sin medio_id y con una clave de más
    r = client.post(f"{API}/{factura['id']}/cobrar", json={"pagos": pagos})
    assert r.status_code == 200, r.text
    assert r.json()["total_cobrado"] == 120.5


def test_registrar_cobro_factura_rechaza_un_booleano_si_otro_llamador_pasa_el_dict_directo():
    from test_cobros import FACTURA_CC, Escrituras, registrar

    for pagos in ([{"medio_id": "efectivo", "monto": True}], [{"medio_id": "efectivo", "monto": False}],
                  [{"medio_id": "efectivo", "monto": 5}, {"medio_id": "efectivo", "monto": True}],
                  [{"medio_id": True, "monto": 5}]):
        esc = Escrituras()
        with pytest.raises(ValueError, match="no un booleano"):
            registrar(FACTURA_CC, pagos, esc)
        assert esc.movimientos == [] and esc.cc_pagos == [], "rechazó después de escribir"
    esc, res = registrar(FACTURA_CC, [{"medio_id": "efectivo", "monto": "7.5"}, {"medio_id": "efectivo", "monto": ""}])
    assert res["total"] == 7.5 and len(esc.movimientos) == 1
