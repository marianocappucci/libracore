"""El motivo de la nota de crédito y la función pública sin HTTP (ADR-089).

`POST /{factura_id}/nota-credito` acepta un `motivo` opcional que va a las `observaciones` de la nota (y con ellas a su PDF), y
lo que hace la ruta vive en `emitir_nota_de_credito_de_factura`, que un producto con su propio borde llama directo.

Compatibilidad: **sin `motivo` la nota sale exactamente como siempre**; es el caso de todos los consumidores de hoy.
SQLite, con el CAE simulado de `ENV=development` (ver `test_facturas_router.py`).
"""

import pytest
from test_facturas_router import ADMIN, API, USUARIO, _emitir, _montar

from libracore import facturas_router as fr
from libracore import notas_de_credito
from libracore.db import core
from libracore.db import facturas as db_facturas

CODIGOS = notas_de_credito.NotaNoPermitida


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _montar(tmp_path, monkeypatch)


def _nota(client, factura, **cuerpo):
    return client.post(f"{API}/{factura['id']}/nota-credito", json=cuerpo or None, headers=ADMIN)


# ── La ruta ───────────────────────────────────────────────────────────────


def test_sin_motivo_la_nota_sale_como_siempre(client):
    """El caso de todos los consumidores de hoy: ni cuerpo, ni `motivo`, ni la marca « — » en las observaciones."""
    original = _emitir(client)
    r = client.post(f"{API}/{original['id']}/nota-credito", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["observaciones"] == "Anula Factura C 0001-00000001"


def test_un_cuerpo_sin_motivo_tampoco_cambia_nada(client):
    original = _emitir(client)
    r = client.post(f"{API}/{original['id']}/nota-credito", json={}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["observaciones"] == "Anula Factura C 0001-00000001"


def test_con_motivo_va_a_las_observaciones_de_la_nota_y_se_relee_de_la_base(client):
    original = _emitir(client)
    r = _nota(client, original, motivo="  Se facturó   el servicio equivocado ")
    assert r.status_code == 200, r.text
    nota = r.json()
    assert nota["observaciones"] == "Anula Factura C 0001-00000001 — Se facturó el servicio equivocado"
    # Lo que importa es lo guardado, no lo que contestó el POST.
    assert db_facturas.get_factura(nota["id"])["observaciones"] == nota["observaciones"]


def test_el_pdf_de_la_nota_se_arma_con_esas_observaciones(client, monkeypatch):
    """El PDF toma las observaciones de la fila de la nota: se mira lo que recibe el generador."""
    from libracore import pdf_generator

    vistas = []
    original_generar = pdf_generator.generate_pdf_factura

    def espiar(factura, *args, **kwargs):
        vistas.append(factura.get("observaciones"))
        return original_generar(factura, *args, **kwargs)

    monkeypatch.setattr(pdf_generator, "generate_pdf_factura", espiar)
    original = _emitir(client)
    nota = _nota(client, original, motivo="Cobro duplicado").json()
    assert vistas[-1] == "Anula Factura C 0001-00000001 — Cobro duplicado"
    assert nota["pdf_path"] and nota["observaciones"] == vistas[-1]


@pytest.mark.parametrize("motivo", ["", "   ", None])
def test_un_motivo_vacio_es_no_mandarlo(client, motivo):
    original = _emitir(client)
    r = _nota(client, original, motivo=motivo)
    assert r.status_code == 200, r.text
    assert " — " not in r.json()["observaciones"]


def test_un_motivo_demasiado_largo_es_422_y_no_emite(client):
    original = _emitir(client)
    r = _nota(client, original, motivo="x" * (fr.MOTIVO_MAXIMO + 1))
    assert r.status_code == 422, r.text
    assert db_facturas.get_nc_de_factura(original["tipo"], original["punto_venta"], original["numero"]) == []
    assert _nota(client, original, motivo="x" * fr.MOTIVO_MAXIMO).status_code == 200


def test_el_motivo_no_cambia_ninguna_guarda(client):
    original = _emitir(client)
    assert _nota(client, original, motivo="Primera").status_code == 200
    assert _nota(client, original, motivo="Segunda").status_code == 409


# ── La función pública ────────────────────────────────────────────────────


def test_la_funcion_publica_emite_la_nota_sin_http(client):
    original = _emitir(client)
    nota = fr.emitir_nota_de_credito_de_factura(original["id"], USUARIO["id"], motivo="Error de carga")
    assert nota["tipo"] == 13 and nota["total"] == original["total"] and nota["cae"]
    assert nota["observaciones"].endswith(" — Error de carga")
    assert (nota["cbte_asoc_tipo"], nota["cbte_asoc_pv"], nota["cbte_asoc_nro"]) == (
        original["tipo"], original["punto_venta"], original["numero"])
    assert nota["usuario_id"] == USUARIO["id"]
    assert "reintegro" not in nota, "sin destino no se mueve plata"


def test_la_funcion_publica_acepta_una_nota_parcial(client):
    original = _emitir(client)
    nota = fr.emitir_nota_de_credito_de_factura(original["id"], USUARIO["id"], importe=4000)
    assert nota["total"] == 4000.0
    assert nota["observaciones"] == "Acredita 4000.00 de Factura C 0001-00000001"


def test_la_funcion_publica_levanta_las_guardas_del_nucleo_y_no_http(client):
    original = _emitir(client)
    fr.emitir_nota_de_credito_de_factura(original["id"], USUARIO["id"])
    with pytest.raises(notas_de_credito.NotaNoPermitida) as e:
        fr.emitir_nota_de_credito_de_factura(original["id"], USUARIO["id"])
    assert e.value.codigo == CODIGOS.YA_TIENE_NOTA


def test_una_factura_que_no_existe_o_esta_anulada_es_una_nota_no_permitida(client):
    with pytest.raises(notas_de_credito.NotaNoPermitida) as e:
        fr.emitir_nota_de_credito_de_factura(9999, USUARIO["id"])
    assert e.value.codigo == CODIGOS.NO_ENCONTRADA

    original = _emitir(client)
    with core.get_connection() as conn:
        conn.execute("UPDATE facturas SET anulada_en = '2026-10-01 10:00:00' WHERE id = ?", (original["id"],))
        conn.commit()
    with pytest.raises(notas_de_credito.NotaNoPermitida) as e:
        fr.emitir_nota_de_credito_de_factura(original["id"], USUARIO["id"])
    assert e.value.codigo == CODIGOS.ANULADA
    # La ruta las traduce como siempre: 404 y 409, con el mismo texto de antes.
    assert client.post(f"{API}/9999/nota-credito", headers=ADMIN).status_code == 404
    r = client.post(f"{API}/{original['id']}/nota-credito", headers=ADMIN)
    assert r.status_code == 409 and r.json()["detail"] == "El comprobante está anulado."


def test_la_funcion_publica_decide_la_plata_antes_de_pedir_el_cae(client):
    """Un `destino` que no existe: levanta antes de emitir, y no queda ninguna nota."""
    original = _emitir(client)
    with pytest.raises(notas_de_credito.NotaNoPermitida) as e:
        fr.emitir_nota_de_credito_de_factura(original["id"], USUARIO["id"], destino="regalar")
    assert e.value.codigo == CODIGOS.DESTINO
    assert db_facturas.get_nc_de_factura(original["tipo"], original["punto_venta"], original["numero"]) == []


def test_todo_codigo_que_levanta_la_funcion_tiene_su_status_en_la_ruta():
    for codigo in (CODIGOS.NO_ENCONTRADA, CODIGOS.ANULADA, CODIGOS.TIPO, CODIGOS.YA_TIENE_NOTA, CODIGOS.NOTA_SIN_CAE,
                   CODIGOS.EN_CURSO, CODIGOS.RECEPTOR, CODIGOS.IMPORTE, CODIGOS.SUPERA_SALDO, CODIGOS.DESTINO,
                   CODIGOS.SIN_TURNO):
        assert codigo in fr.STATUS_DE_NOTA, codigo
