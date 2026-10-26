"""El recibo de un cobro asentado en el libro de terceros (ADR-087): `recibos.emitir_recibo_de_asiento`.

Lo que se fija acá, contra los dos motores:
- el recibo sale del **haber** del tercero, con los datos del cliente y el «por cuenta y orden de» del mandatario;
- es idempotente por asiento, y un debe no tiene recibo;
- los medios de pago tienen que sumar el cobro;
- en la transacción del producto (`conn=`), el cobro y su recibo entran o salen juntos;
- el PDF del recibo lo imprime.
"""
import io
import os

import pytest
from pypdf import PdfReader

from libracore import recibos
from libracore.db import core
from libracore.db import libro_de_terceros as libro
from libracore.db import recibos as db_recibos
from libracore.db.schema import init_core_schema
from libracore.pdf_generator import generate_pdf_recibo_doc


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
        core.configure(db_path=str(tmp_path / "recibo_asiento.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.execute("INSERT INTO clients (id, name, cuit_dni, address) "
                     "VALUES (7, 'Inquilina Ficticia', '27-11111111-3', 'Calle Inventada 100')")
        conn.commit()
    yield request.param
    core._db_path = None
    core._database_url = None


def _cobro(monto=150000, fecha="2026-11-05"):
    return libro.asentar(7, "inquilino", fecha, "Alquiler noviembre 2026", haber=monto)


EFECTIVO = [{"fecha": "2026-11-05", "medio_pago": "efectivo", "referencia": "", "monto": 150000}]


def test_el_recibo_sale_del_haber_con_el_mandato(base):
    asiento = _cobro()
    r = recibos.emitir_recibo_de_asiento(asiento, pagos=EFECTIVO, por_cuenta_de="Propietario Ficticio")
    assert (r["origen_tipo"], r["origen_id"]) == (db_recibos.ORIGEN_ASIENTO, asiento)
    assert r["total"] == 150000
    assert (r["cliente_id"], r["cliente_razon"], r["cliente_cuit"]) == (7, "Inquilina Ficticia", "27-11111111-3")
    assert r["concepto"] == "Alquiler noviembre 2026, por cuenta y orden de Propietario Ficticio"
    assert r["fecha"] == "2026-11-05" and r["numero"] == 1
    assert r["pagos"][0]["medio_pago"] == "efectivo"


def test_es_idempotente_y_numera_correlativo(base):
    a, b = _cobro(), _cobro(90000.50)
    primero = recibos.emitir_recibo_de_asiento(a, pagos=EFECTIVO)
    assert recibos.emitir_recibo_de_asiento(a, pagos=EFECTIVO)["id"] == primero["id"]
    segundo = recibos.emitir_recibo_de_asiento(
        b, pagos=[{"medio_pago": "transferencia", "referencia": "op 123", "monto": "90000.50"}])
    assert (primero["numero"], segundo["numero"]) == (1, 2)
    assert segundo["pagos"][0]["fecha"] == "2026-11-05"           # sin fecha, la del asiento


def test_un_debe_no_tiene_recibo(base):
    cargo = libro.asentar(7, "inquilino", "2026-11-01", "Alquiler noviembre 2026", debe=150000)
    with pytest.raises(recibos.SinCobros):
        recibos.emitir_recibo_de_asiento(cargo, pagos=EFECTIVO)
    with pytest.raises(recibos.SinCobros):
        recibos.emitir_recibo_de_asiento(999, pagos=EFECTIVO)


def test_los_medios_tienen_que_sumar_el_cobro(base):
    asiento = _cobro()
    with pytest.raises(ValueError, match="suman 100000.00"):
        recibos.emitir_recibo_de_asiento(asiento, pagos=[{"medio_pago": "efectivo", "monto": 100000}])
    with pytest.raises(ValueError):
        recibos.emitir_recibo_de_asiento(asiento, pagos=[])
    assert db_recibos.get_recibos_de_origen(db_recibos.ORIGEN_ASIENTO, asiento) == []


def test_en_la_transaccion_del_producto_entran_o_salen_juntos(base):
    conn = core.get_connection()
    try:
        asiento = libro.asentar(7, "inquilino", "2026-11-05", "Alquiler", haber=150000, conn=conn)
        r = recibos.emitir_recibo_de_asiento(asiento, pagos=EFECTIVO, conn=conn)
        assert r["numero"] == 1
        conn.rollback()
    finally:
        conn.close()
    with core.get_connection() as c:
        assert c.execute("SELECT COUNT(*) FROM recibos").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM cc_asientos").fetchone()[0] == 0

    conn = core.get_connection()
    try:
        asiento = libro.asentar(7, "inquilino", "2026-11-05", "Alquiler", haber=150000, conn=conn)
        recibos.emitir_recibo_de_asiento(asiento, pagos=EFECTIVO, conn=conn)
        conn.commit()
    finally:
        conn.close()
    assert len(db_recibos.get_recibos_de_origen(db_recibos.ORIGEN_ASIENTO, asiento)) == 1


def test_el_pdf_lo_imprime(base):
    r = recibos.emitir_recibo_de_asiento(_cobro(), pagos=EFECTIVO, por_cuenta_de="Propietario Ficticio")
    pdf = generate_pdf_recibo_doc(r)
    texto = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "Inquilina Ficticia" in texto
    assert "por cuenta y orden de Propietario Ficticio" in texto.replace("\n", " ")


def test_el_origen_es_parte_de_la_lista_cerrada():
    assert db_recibos.ORIGEN_ASIENTO in db_recibos.ORIGENES
    with pytest.raises(ValueError):
        db_recibos.create_recibo("2026-11-05", "X", "suelto", 1, [])


def test_anular_en_la_transaccion_del_producto(base):
    r = recibos.emitir_recibo_de_asiento(_cobro(), pagos=EFECTIVO)
    conn = core.get_connection()
    try:
        assert db_recibos.anular_recibo(r["id"], motivo="Rebotó", conn=conn) is True
        conn.rollback()
    finally:
        conn.close()
    assert db_recibos.get_recibo(r["id"])["anulado"] is False
    conn = core.get_connection()
    try:
        db_recibos.anular_recibo(r["id"], motivo="Rebotó", conn=conn)
        conn.commit()
    finally:
        conn.close()
    assert db_recibos.get_recibo(r["id"])["anulado"] is True
    assert db_recibos.anular_recibo(r["id"]) is False
