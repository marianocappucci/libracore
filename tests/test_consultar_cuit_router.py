"""`build_consultar_cuit_router`: la consulta de CUIT contra el padrón de ARCA,
extraída de Contalibra. Ver el docstring de `libracore.consultar_cuit_router`
para el porqué de cada pieza -- acá se prueba el contrato HTTP.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore.config_manager import ARCHIVOS_POR_AMBIENTE
from libracore.consultar_cuit_router import build_consultar_cuit_router
from libracore.db import arca_config as db_arca_config
from libracore.db import core
from libracore.db.schema import init_core_schema

CERT_HOMO, CLAVE_HOMO = ARCHIVOS_POR_AMBIENTE["homologacion"]
CERT_PROD, CLAVE_PROD = ARCHIVOS_POR_AMBIENTE["produccion"]

USUARIO = {"id": 1, "username": "vendedor", "role": "staff"}


def _usuario():
    return USUARIO


@pytest.fixture
def client(tmp_path):
    core.configure(db_path=str(tmp_path / "consultar_cuit.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(build_consultar_cuit_router(usuario_actual=_usuario))
    with TestClient(app) as c:
        yield c
    core._db_path = None


@pytest.fixture
def instancia_en_homologacion(tmp_path):
    """Una instancia con **los dos pares** cargados y el selector en
    homologación. Los dos pares, no uno: con sólo el de homologación, "usó el
    correcto" y "usó el único que había" son indistinguibles."""
    d = tmp_path / "arca_certs"
    d.mkdir()
    for nombre in (CERT_HOMO, CLAVE_HOMO, CERT_PROD, CLAVE_PROD):
        (d / nombre).write_text(nombre)

    db_arca_config.crear_arca_config(
        empresa="default", cuit="20111111119", punto_venta=1,
        clave_path=str(d / CLAVE_PROD), certificado_path=str(d / CERT_PROD),
        ambiente="homologacion",
    )
    db_arca_config.actualizar_arca_config(
        "default",
        certificado_path_homologacion=str(d / CERT_HOMO),
        clave_path_homologacion=str(d / CLAVE_HOMO),
    )
    return d


def test_un_cuit_invalido_no_llega_a_consultar_nada(client):
    r = client.get("/api/consultar-cuit/123")
    assert r.status_code == 400


def test_sin_credenciales_da_503(client):
    r = client.get("/api/consultar-cuit/20111111119")
    assert r.status_code == 503
    assert "Configurá los certificados" in r.json()["error"]


def test_el_padron_autentica_con_el_par_del_ambiente(client, instancia_en_homologacion, monkeypatch):
    """El defecto que este router hereda resuelto de Contalibra: leer
    `arca["certificado_path"]` directo saldría a autenticar con el certificado
    de PRODUCCIÓN contra el WSAA de homologación."""
    from libracore import arca_wsaa, arca_wspadron

    usados = {}

    async def _capturar(cert, clave, ambiente, servicio=""):
        usados.update(cert=cert, clave=clave, ambiente=ambiente)
        return {"token": "t", "sign": "s"}

    async def _padron(*a, **k):
        return {"razon_social": "Alguien SA"}

    monkeypatch.setattr(arca_wsaa, "autenticar", _capturar)
    monkeypatch.setattr(arca_wspadron, "consultar_persona", _padron)

    r = client.get("/api/consultar-cuit/20111111119")
    assert r.status_code == 200, r.text
    assert r.json() == {"razon_social": "Alguien SA"}
    assert usados["ambiente"] == "homologacion"
    assert usados["cert"].endswith(CERT_HOMO), f"autenticó con {usados['cert']} — tiene que ser homologación"
    assert usados["clave"].endswith(CLAVE_HOMO)


def test_el_padron_en_produccion_usa_el_par_real(client, instancia_en_homologacion, monkeypatch):
    """El control del anterior: si el endpoint pidiera siempre el par de
    homologación, el test de arriba pasaría igual."""
    from libracore import arca_wsaa, arca_wspadron

    db_arca_config.actualizar_arca_config("default", ambiente="produccion")
    usados = {}

    async def _capturar(cert, clave, ambiente, servicio=""):
        usados.update(cert=cert, ambiente=ambiente)
        return {"token": "t", "sign": "s"}

    async def _padron(*a, **k):
        return {"razon_social": "Alguien SA"}

    monkeypatch.setattr(arca_wsaa, "autenticar", _capturar)
    monkeypatch.setattr(arca_wspadron, "consultar_persona", _padron)

    r = client.get("/api/consultar-cuit/20111111119")
    assert r.status_code == 200, r.text
    assert usados["ambiente"] == "produccion"
    assert usados["cert"].endswith(CERT_PROD)


def test_un_cuit_inexistente_en_el_padron_da_404(client, instancia_en_homologacion, monkeypatch):
    from libracore import arca_wsaa, arca_wspadron

    async def _capturar(cert, clave, ambiente, servicio=""):
        return {"token": "t", "sign": "s"}

    async def _no_encontrado(*a, **k):
        raise RuntimeError("CUIT inexistente en el padrón")

    monkeypatch.setattr(arca_wsaa, "autenticar", _capturar)
    monkeypatch.setattr(arca_wspadron, "consultar_persona", _no_encontrado)

    r = client.get("/api/consultar-cuit/20111111119")
    assert r.status_code == 404


def test_certificado_sin_acceso_al_padron_da_403_con_el_mensaje_accionable(client, instancia_en_homologacion, monkeypatch):
    from libracore import arca_wsaa, arca_wspadron

    async def _capturar(cert, clave, ambiente, servicio=""):
        return {"token": "t", "sign": "s"}

    async def _sin_acceso(*a, **k):
        raise RuntimeError("El certificado no tiene constraints válidas para este servicio")

    monkeypatch.setattr(arca_wsaa, "autenticar", _capturar)
    monkeypatch.setattr(arca_wspadron, "consultar_persona", _sin_acceso)

    r = client.get("/api/consultar-cuit/20111111119")
    assert r.status_code == 403
    assert "Padrón" in r.json()["error"]


def test_no_esta_en_el_openapi(client):
    """`include_in_schema=False`: es un endpoint interno del formulario, no
    parte del contrato público documentado."""
    schema = client.get("/openapi.json").json()
    assert not any(p.startswith("/api/consultar-cuit") for p in schema["paths"])
