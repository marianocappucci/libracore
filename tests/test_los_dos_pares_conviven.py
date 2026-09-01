"""Subir el par de homologación no toca el de producción.

🔴 **Medido el 2026-09-01, antes de escribir nada: hasta ese día lo pisaba.** El
paso anterior separó las *columnas* de la base, pero los uploads seguían
escribiendo los dos pares en el **mismo archivo** (`certificado.crt`), y el
rescate de `resolve_cert_paths` caía a ese nombre fijo. O sea que la parte cara
—que el cliente pueda probar sin arriesgar su credencial real— quedaba deshecha
una capa más abajo, en silencio.

Este archivo prueba lo que la pantalla del kit necesita para no ser peligrosa:

1. cada ambiente escribe **su propio archivo**;
2. subir uno **no toca** el otro, ni en disco ni en la base;
3. con el par de homologación vacío, no se cae al de producción;
4. borrar un ambiente deja el otro intacto.
"""

import os

import pytest
from conftest import make_valid_cert_key
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore.arca_router import build_arca_router
from libracore.config_manager import ARCHIVOS_POR_AMBIENTE
from libracore.db import arca_config as db_arca
from libracore.db import core
from libracore.db.schema import init_core_schema

ADMIN = {"x-rol": "admin"}

CERT_HOMO, CLAVE_HOMO = ARCHIVOS_POR_AMBIENTE["homologacion"]
CERT_PROD, CLAVE_PROD = ARCHIVOS_POR_AMBIENTE["produccion"]


@pytest.fixture
def app(tmp_path, monkeypatch):
    from libracore import config_manager

    certs = tmp_path / "arca_certs"
    certs.mkdir()
    monkeypatch.setattr(config_manager, "CERTS_DIR", str(certs))

    core._db_path = None
    core._database_url = None
    core.configure(db_path=str(tmp_path / "pares.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    c.close()

    def gate(x_rol: str = Header("")):
        if x_rol != "admin":
            raise HTTPException(403, "solo admin")

    a = FastAPI()
    a.include_router(build_arca_router(), dependencies=[Depends(gate)])
    a.state.certs_dir = str(certs)
    yield a
    core._db_path = None
    core._database_url = None


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def par_a(tmp_path):
    """Un par válido. Dos pares distintos hacen falta para poder afirmar que
    **no se mezclaron**: con el mismo bytes en los dos, pisar uno con el otro
    sería indistinguible de no haberlo pisado."""
    d = tmp_path / "a"
    d.mkdir()
    return make_valid_cert_key(d)


@pytest.fixture
def par_b(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    return make_valid_cert_key(d)


def _subir(client, cert, clave, ambiente):
    q = f"?ambiente={ambiente}" if ambiente else ""
    with open(cert, "rb") as f:
        r1 = client.post(f"/config/arca/certificado{q}", headers=ADMIN,
                         files={"archivo": ("c.crt", f.read(), "application/x-x509-ca-cert")})
    with open(clave, "rb") as f:
        r2 = client.post(f"/config/arca/clave{q}", headers=ADMIN,
                         files={"archivo": ("c.key", f.read(), "application/x-pem-file")})
    return r1, r2


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


# -- Lo que este paso arregla ------------------------------------------------

def test_subir_el_de_homologacion_no_pisa_el_de_produccion(client, app, par_a, par_b):
    """🔴 **El defecto entero, en un test.** Antes del 2026-09-01 los dos
    uploads escribían `certificado.crt`, así que este test terminaba con el
    certificado de producción reemplazado por el de homologación — la operación
    destructiva que separar las columnas venía a evitar."""
    cert_prod, clave_prod = par_a
    cert_homo, clave_homo = par_b

    _subir(client, cert_prod, clave_prod, "produccion")
    guardado = _bytes(os.path.join(app.state.certs_dir, CERT_PROD))
    assert guardado == _bytes(cert_prod), "control: el de producción se guardó"

    _subir(client, cert_homo, clave_homo, "homologacion")

    assert _bytes(os.path.join(app.state.certs_dir, CERT_PROD)) == _bytes(cert_prod), (
        "🔴 el upload de homologación pisó el certificado de PRODUCCIÓN")
    assert _bytes(os.path.join(app.state.certs_dir, CERT_HOMO)) == _bytes(cert_homo)


def test_cada_ambiente_escribe_su_propio_archivo(client, app, par_a, par_b):
    """El nombre en disco los separa. Sin esto, la separación de columnas de la
    base apunta dos filas al mismo archivo."""
    _subir(client, *par_a, "produccion")
    _subir(client, *par_b, "homologacion")

    hay = sorted(os.listdir(app.state.certs_dir))
    assert hay == sorted([CERT_PROD, CLAVE_PROD, CERT_HOMO, CLAVE_HOMO])
    assert CERT_PROD != CERT_HOMO and CLAVE_PROD != CLAVE_HOMO


def test_cada_par_va_a_la_columna_de_su_ambiente(client, par_a, par_b):
    """Y la base los distingue: `paths_de` de cada ambiente devuelve el suyo."""
    _subir(client, *par_a, "produccion")
    _subir(client, *par_b, "homologacion")

    cfg = db_arca.obtener_todas_arca_configs()[0]
    prod = db_arca.paths_de(cfg, "produccion")
    homo = db_arca.paths_de(cfg, "homologacion")
    assert prod != homo, "los dos ambientes apuntan al mismo par"
    assert prod[0].endswith(CERT_PROD) and homo[0].endswith(CERT_HOMO)


def test_sin_par_de_homologacion_NO_se_cae_al_de_produccion(client, app, par_a):
    """🔴 La segunda colisión, la que no se ve. `paths_de` devuelve ("", "")
    para no entregar las credenciales reales... y `resolve_cert_paths` las
    reponía cayendo al nombre fijo `certificado.crt`.

    Escenario real: una instancia de producción que el operador pasa a
    homologación para probar, antes de subir el par nuevo.
    """
    _subir(client, *par_a, "produccion")
    client.put("/config/arca", headers=ADMIN,
               json={"cuit": "20289933604", "ambiente": "homologacion"})

    estado = client.get("/config/arca/estado", headers=ADMIN).json()
    assert estado["pares"]["homologacion"]["completo"] is False, (
        "🔴 el rescate repuso el par de PRODUCCIÓN para el ambiente de prueba")
    assert estado["pares"]["produccion"]["completo"] is True, (
        "control: el par de producción sigue estando, sólo que no es el de este ambiente")
    assert estado["configurado"] is False


def test_borrar_un_ambiente_deja_el_otro(client, app, par_a, par_b):
    """Lo que hace segura la prueba: terminado el acompañamiento se saca el par
    de homologación y el real sigue donde estaba. Antes borrar era borrar todo."""
    _subir(client, *par_a, "produccion")
    _subir(client, *par_b, "homologacion")

    r = client.delete("/config/arca/credenciales?ambiente=homologacion", headers=ADMIN)
    assert r.status_code == 200

    assert not os.path.exists(os.path.join(app.state.certs_dir, CERT_HOMO))
    assert os.path.exists(os.path.join(app.state.certs_dir, CERT_PROD))
    assert r.json()["pares"]["produccion"]["completo"] is True
    assert r.json()["pares"]["homologacion"]["completo"] is False

    # 🔑 Y las COLUMNAS, no solo lo que reporta la pantalla. Una mutacion que
    # vaciaba las columnas de produccion sobrevivio a las cuatro lineas de
    # arriba: el rescate de `resolve_cert_paths` cae al archivo que sigue en
    # disco y **repara el daño antes de que el test lo vea**. Preguntarle a la
    # capa que arregla el problema es como no preguntar.
    cfg = db_arca.obtener_todas_arca_configs()[0]
    assert cfg["certificado_path"] and cfg["clave_path"], (
        "borrar homologacion vacio las columnas de PRODUCCION")
    assert not cfg["certificado_path_homologacion"]
    assert not cfg["clave_path_homologacion"]


# -- Lo que la pantalla necesita --------------------------------------------

def test_la_pantalla_ve_los_dos_pares_a_la_vez(client, par_a, par_b):
    """🔑 Sin esto, mover la llave a producción es un salto a ciegas: el
    operador está parado en homologación y no tiene cómo saber si el par real
    está cargado ni hasta cuándo dura."""
    _subir(client, *par_a, "produccion")
    _subir(client, *par_b, "homologacion")
    client.put("/config/arca", headers=ADMIN,
               json={"cuit": "20289933604", "ambiente": "homologacion"})

    pares = client.get("/config/arca", headers=ADMIN).json()["pares"]
    assert set(pares) == {"produccion", "homologacion"}
    for amb in ("produccion", "homologacion"):
        assert pares[amb]["completo"] is True
        assert pares[amb]["dias_para_vencer"] > 0, f"falta el vencimiento de {amb}"


def test_un_par_a_medias_no_dice_completo(client, par_a):
    """"Certificado ✓ / clave ✗" se lee como "falta poco", y en realidad no
    factura nada. `completo` es el dato que la pantalla tiene que mostrar."""
    cert, _ = par_a
    with open(cert, "rb") as f:
        client.post("/config/arca/certificado?ambiente=homologacion", headers=ADMIN,
                    files={"archivo": ("c.crt", f.read(), "application/x-x509-ca-cert")})

    par = client.get("/config/arca/estado", headers=ADMIN).json()["pares"]["homologacion"]
    assert par["tiene_certificado"] is True
    assert par["tiene_clave"] is False
    assert par["completo"] is False


def test_sin_ambiente_se_usa_el_selector(client, app, par_a):
    """La pantalla vieja —y cualquier `curl` de antes— no manda `ambiente`. Que
    caiga en el par del selector mantiene andando lo que ya existía."""
    client.put("/config/arca", headers=ADMIN,
               json={"cuit": "20289933604", "ambiente": "produccion"})
    _subir(client, *par_a, "")

    assert os.path.exists(os.path.join(app.state.certs_dir, CERT_PROD))
    assert not os.path.exists(os.path.join(app.state.certs_dir, CERT_HOMO))


def test_un_ambiente_inventado_rebota_y_no_escribe(client, app, par_a):
    """🔴 El destino de un upload es un archivo que se sobrescribe: adivinar el
    ambiente ante un valor raro puede pisar la credencial real. 422, y el disco
    queda como estaba."""
    cert, _ = par_a
    with open(cert, "rb") as f:
        r = client.post("/config/arca/certificado?ambiente=testing", headers=ADMIN,
                        files={"archivo": ("c.crt", f.read(), "application/x-x509-ca-cert")})
    assert r.status_code == 422
    assert "testing" in r.json()["detail"]
    assert os.listdir(app.state.certs_dir) == [], "escribió algo pese al rechazo"


def test_la_pareja_se_chequea_dentro_del_mismo_ambiente(client, par_a, par_b):
    """🔑 El chequeo de pareja tiene que mirar la clave **del mismo ambiente**.
    Si mirara la del otro, subir el certificado de homologación teniendo cargado
    el par de producción daría "no son pareja" — y el operador no tendría forma
    de avanzar."""
    _subir(client, *par_a, "produccion")

    cert_homo, _ = par_b
    with open(cert_homo, "rb") as f:
        r = client.post("/config/arca/certificado?ambiente=homologacion", headers=ADMIN,
                        files={"archivo": ("c.crt", f.read(), "application/x-x509-ca-cert")})
    assert r.status_code == 200, (
        "el chequeo de pareja miró la clave del OTRO ambiente: " + r.text[:200])


def test_la_pareja_SI_se_chequea_cuando_es_del_mismo_ambiente(client, par_a, par_b):
    """El control positivo del anterior: si el chequeo no mirara nada, el test
    de arriba pasaría igual con la validación rota."""
    _subir(client, *par_a, "homologacion")

    cert_ajeno, _ = par_b
    with open(cert_ajeno, "rb") as f:
        r = client.post("/config/arca/certificado?ambiente=homologacion", headers=ADMIN,
                        files={"archivo": ("c.crt", f.read(), "application/x-x509-ca-cert")})
    assert r.status_code == 422
    assert "pareja" in r.json()["detail"]


# -- El rescate, a nivel de `config_manager` --------------------------------

def test_el_rescate_normaliza_el_ambiente(tmp_path, monkeypatch):
    """`resolve_cert_paths` es publica y los dos productos que consultan el
    padron la llaman por su cuenta. Un ambiente con mayusculas o espacios —como
    el que sale de la base sin pasar por `_ambiente_de`— no tiene que quedarse
    sin rescate."""
    from libracore import config_manager

    certs = tmp_path / "certs"
    certs.mkdir()
    monkeypatch.setattr(config_manager, "CERTS_DIR", str(certs))
    (certs / CERT_HOMO).write_bytes(b"homo")
    (certs / CLAVE_HOMO).write_bytes(b"homo")

    for escrito in ("homologacion", "  Homologacion ", "HOMOLOGACION"):
        cert, clave = config_manager.resolve_cert_paths("", "", escrito)
        assert cert.endswith(CERT_HOMO), f"sin rescate para {escrito!r}"
        assert clave.endswith(CLAVE_HOMO)


def test_el_rescate_de_un_ambiente_desconocido_no_inventa_archivo(tmp_path, monkeypatch):
    """🔴 Mismo criterio que `paths_de`: ante un valor raro, nada — y sobre todo
    **no** el par de produccion."""
    from libracore import config_manager

    certs = tmp_path / "certs"
    certs.mkdir()
    monkeypatch.setattr(config_manager, "CERTS_DIR", str(certs))
    (certs / CERT_PROD).write_bytes(b"el real del cliente")
    (certs / CLAVE_PROD).write_bytes(b"el real del cliente")

    assert config_manager.resolve_cert_paths("", "", "testing") == ("", "")
    # Control: con el ambiente bueno SI lo encuentra, asi que el vacio de arriba
    # no es un directorio mal apuntado.
    cert, _ = config_manager.resolve_cert_paths("", "", "produccion")
    assert cert.endswith(CERT_PROD)
