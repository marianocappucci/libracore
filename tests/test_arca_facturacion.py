"""
Tests de la orquestación de numeración/CAE (`libracore.arca_facturacion`),
migrada desde `web/helpers/arca_helper.py` de Contalibra. Mockea
`arca_wsaa.autenticar`/`arca_wsfe.*` a nivel de función (no HTTP): lo que
se prueba acá es la lógica de glue (dev vs. prod, fallback a numeración
local, mock de CAE), no el protocolo ARCA en sí — eso ya lo cubren
`test_arca_wsaa.py`/`test_arca_wsfe.py`.
"""
import asyncio

import pytest

from libracore import arca_facturacion
from libracore.db import core
from libracore.db.schema import init_core_schema
from libracore.db import arca_config as db_arca_config
from libracore.db import facturas as db_facturas


@pytest.fixture
def conn(tmp_path):
    core.configure(db_path=str(tmp_path / "arca_facturacion_test.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


@pytest.fixture(autouse=True)
def _dev_off(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)


def _factura_base(**overrides):
    factura = {
        "punto_venta": 1, "tipo": 6, "numero": 1, "fecha": "2026-07-22",
        "concepto": 1, "subtotal": 100.0, "iva_amount": 21.0, "total": 121.0,
        "cliente_cuit": "20123456789", "cliente_razon": "Cliente Test",
        "cliente_iva_cond": "Consumidor Final", "items": [],
    }
    factura.update(overrides)
    return factura


def test_dev_mode_uses_local_numbering_and_mock_ta(conn, monkeypatch):
    monkeypatch.setenv("ENV", "development")
    numero, ta, arca = asyncio.run(arca_facturacion.get_next_numero_with_arca(1, 6))
    assert numero == 1
    assert ta == "_dev_mock_"
    assert arca == "_dev_mock_"


def test_prod_without_arca_config_uses_local_numbering(conn):
    numero, ta, arca = asyncio.run(arca_facturacion.get_next_numero_with_arca(1, 6))
    assert numero == 1
    assert ta is None
    assert arca is None


def test_prod_with_arca_config_calls_wsaa_and_wsfe(conn, monkeypatch):
    # 🔑 El par va a las columnas de SU ambiente. Con las credenciales en las
    # de produccion y el selector en homologacion, `paths_de()` devuelve
    # ("", "") y la instancia no puede emitir -- que es exactamente el caso que
    # la migracion 0007 arregla para las instancias vivas.
    db_arca_config.crear_arca_config(
        "Empresa Test", "20123456789", 1, "", "", ambiente="homologacion",
    )
    db_arca_config.actualizar_arca_config(
        "Empresa Test", clave_path_homologacion="clave.key",
        certificado_path_homologacion="cert.crt",
    )

    async def fake_autenticar(cert_path, key_path, ambiente):
        return {"token": "TKN", "sign": "SGN"}

    async def fake_ultimo_numero(pv, tipo, cuit, token, sign, ambiente):
        return 41

    monkeypatch.setattr(arca_facturacion.arca_wsaa, "autenticar", fake_autenticar)
    monkeypatch.setattr(arca_facturacion.arca_wsfe, "ultimo_numero_autorizado", fake_ultimo_numero)

    numero, ta, arca = asyncio.run(arca_facturacion.get_next_numero_with_arca(1, 6))
    assert numero == 42
    assert ta == {"token": "TKN", "sign": "SGN"}
    assert arca["empresa"] == "Empresa Test"


def test_prod_arca_failure_falls_back_to_local_numbering(conn, monkeypatch, caplog):
    # 🔑 El par va a las columnas de SU ambiente. Con las credenciales en las
    # de produccion y el selector en homologacion, `paths_de()` devuelve
    # ("", "") y la instancia no puede emitir -- que es exactamente el caso que
    # la migracion 0007 arregla para las instancias vivas.
    db_arca_config.crear_arca_config(
        "Empresa Test", "20123456789", 1, "", "", ambiente="homologacion",
    )
    db_arca_config.actualizar_arca_config(
        "Empresa Test", clave_path_homologacion="clave.key",
        certificado_path_homologacion="cert.crt",
    )

    async def failing_autenticar(cert_path, key_path, ambiente):
        raise RuntimeError("ARCA caido")

    monkeypatch.setattr(arca_facturacion.arca_wsaa, "autenticar", failing_autenticar)

    with caplog.at_level("ERROR"):
        numero, ta, arca = asyncio.run(arca_facturacion.get_next_numero_with_arca(1, 6))

    assert numero == 1
    assert ta is None
    assert "ARCA no disponible" in caplog.text


def test_el_fallback_local_numera_en_EL_MISMO_ambiente(conn, monkeypatch, caplog):
    """🔴 Cuando ARCA no contesta se numera local — y tiene que ser en la
    secuencia del ambiente que se estaba pidiendo.

    ARCA lleva numeraciones **independientes** por ambiente. Si el fallback
    mirara todas las filas, un comprobante de prueba numerado 500 haria que el
    proximo real salga 501 cuando produccion va por 84: la secuencia local
    queda desalineada de la de ARCA y cada emision posterior choca contra el
    "ultimo autorizado" real.

    🔑 Nacio de una mutacion que SOBREVIVIO: el test de arriba ya cubria el
    fallback, pero con la tabla vacia --numero == 1 sale igual mirando o no el
    ambiente--. Lo que distingue es que haya filas de LOS DOS.
    """
    db_arca_config.crear_arca_config(
        "Empresa Test", "20123456789", 1, "clave.key", "cert.crt",
        ambiente="homologacion",
    )
    # 83 reales y 500 de prueba: los dos numeros son distintos y ninguno es 1.
    for numero, ambiente in ((83, "produccion"), (500, "homologacion")):
        db_facturas.create_factura(
            6, 1, numero, "2026-09-01", "20123456789", "Cliente", "Consumidor Final",
            [], 100.0, 21.0, 121.0, ambiente=ambiente,
        )

    async def failing_autenticar(cert_path, key_path, ambiente):
        raise RuntimeError("ARCA caido")

    monkeypatch.setattr(arca_facturacion.arca_wsaa, "autenticar", failing_autenticar)
    with caplog.at_level("ERROR"):
        numero, ta, arca = asyncio.run(arca_facturacion.get_next_numero_with_arca(1, 6))

    # La instancia esta en homologacion: sigue SU secuencia, no la real.
    assert numero == 501, (
        "el fallback numero %s: mezclo las secuencias de los dos ambientes" % numero)


def test_solicitar_cae_dev_mock(conn):
    fid = db_facturas.create_factura(
        6, 1, 1, "2026-07-22", "20123456789", "Cliente Test", "Consumidor Final",
        [], 100.0, 21.0, 121.0,
        ambiente="produccion",
    )
    factura = db_facturas.get_factura(fid)
    result = asyncio.run(arca_facturacion.solicitar_cae(fid, factura, "_dev_mock_", "_dev_mock_"))
    assert result["cae"]
    assert result["cae_vto"]


def test_solicitar_cae_without_ta_or_arca_returns_factura_unchanged():
    factura = _factura_base()
    result = asyncio.run(arca_facturacion.solicitar_cae(999, factura, None, None))
    assert result == factura


def test_solicitar_cae_prod_success(conn, monkeypatch):
    fid = db_facturas.create_factura(
        6, 1, 1, "2026-07-22", "20123456789", "Cliente Test", "Consumidor Final",
        [], 100.0, 21.0, 121.0,
        ambiente="produccion",
    )
    factura = db_facturas.get_factura(fid)

    async def fake_solicitar_cae(factura, cuit, token, sign, ambiente):
        return {"cae": "75312345678901", "cae_vto": "20260801"}

    monkeypatch.setattr(arca_facturacion.arca_wsfe, "solicitar_cae", fake_solicitar_cae)

    result = asyncio.run(arca_facturacion.solicitar_cae(
        fid, factura, {"token": "TKN", "sign": "SGN"}, {"cuit": "20123456789", "ambiente": "homologacion"},
    ))
    assert result["cae"] == "75312345678901"
    assert result["cae_vto"] == "20260801"


def test_solicitar_cae_prod_failure_returns_original_factura(conn, monkeypatch, caplog):
    fid = db_facturas.create_factura(
        6, 1, 1, "2026-07-22", "20123456789", "Cliente Test", "Consumidor Final",
        [], 100.0, 21.0, 121.0,
        ambiente="produccion",
    )
    factura = db_facturas.get_factura(fid)

    async def failing_solicitar_cae(factura, cuit, token, sign, ambiente):
        raise RuntimeError("ARCA rechazo el comprobante")

    monkeypatch.setattr(arca_facturacion.arca_wsfe, "solicitar_cae", failing_solicitar_cae)

    with caplog.at_level("ERROR"):
        result = asyncio.run(arca_facturacion.solicitar_cae(
            fid, factura, {"token": "TKN", "sign": "SGN"}, {"cuit": "20123456789", "ambiente": "homologacion"},
        ))

    assert result == factura
    assert result["cae"] == ""
    assert "Error al solicitar CAE" in caplog.text
