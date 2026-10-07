"""La pantalla de ARCA con más de un servicio: `build_arca_router(servicios=...)` (ADR-032).

La facturación (`wsfe`) no cambia: lo que se prueba acá es (1) que por omisión el
router es el de siempre, (2) que al optar por `wscpe` aparecen sus rutas con las
mismas validaciones y la misma auditoría que las de la facturación, (3) que
«Probar» se autentica **para ese servicio** y explica en castellano lo que ARCA
rechaza, y (4) que nada de eso toca el par de la facturación.

Datos ficticios: los certificados son autofirmados y descartables, y el CUIT es
`20000000001`, que no es de nadie.
"""
import datetime
import importlib
import os

import pytest
from conftest import make_mismatched_key
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore import arca_servicios
from libracore.config_manager import ARCHIVOS_POR_AMBIENTE
from libracore.db import arca_config as db_arca
from libracore.db import core
from libracore.db.schema import init_core_schema

#: El `dummy` de verdad, guardado antes de que el autouse lo reemplace.
_DUMMY_REAL = arca_servicios.dummy

ADMIN = {"x-rol": "admin"}
CUIT_FICTICIO = "20000000001"
CERT_HOMO, CLAVE_HOMO = ARCHIVOS_POR_AMBIENTE["homologacion"]


def _par(cuit: str = CUIT_FICTICIO, dias: int = 365, alias: str = "alias-de-prueba"):
    """`(crt, key)` en bytes, con el sujeto como los emite ARCA: `serialNumber=CUIT n`."""
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sujeto = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PERSONA FICTICIA"),
        x509.NameAttribute(NameOID.COMMON_NAME, alias),
        x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {cuit}"),
    ])
    ahora = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(sujeto).issuer_name(sujeto)
        .public_key(clave.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(ahora - datetime.timedelta(days=1))
        .not_valid_after(ahora + datetime.timedelta(days=dias))
        .sign(clave, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        clave.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()),
    )


@pytest.fixture
def montar(tmp_path, monkeypatch):
    """Fábrica de apps: `montar(servicios=..., **kwargs)` -> (client, app)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.arca_credenciales as ac
    import libracore.arca_router as ar
    import libracore.config_manager as cm
    importlib.reload(cm)
    importlib.reload(ac)
    importlib.reload(ar)

    core.configure(db_path=str(tmp_path / "servicios_router.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()

    def gate(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    def _montar(**kwargs):
        app = FastAPI()
        app.include_router(ar.build_arca_router(**kwargs), dependencies=[Depends(gate)])
        app.state.certs_dir = cm.CERTS_DIR
        return TestClient(app), app

    yield _montar
    conn.close()
    core._db_path = None


@pytest.fixture
def cpe(montar):
    """Un producto que optó por `wscpe`."""
    return montar(servicios=("wsfe", "wscpe"))[0]


@pytest.fixture(autouse=True)
def _sin_red_para_el_dummy(monkeypatch):
    """Ningún test sale a ARCA: el `dummy` se reemplaza por uno que contesta OK."""
    monkeypatch.setattr(arca_servicios, "dummy", lambda *a, **k: {
        "appserver": "OK", "authserver": "OK", "dbserver": "OK"})


def _subir(client, que, contenido, ambiente="homologacion", servicio="wscpe", **extra):
    return client.post(
        f"/config/arca/servicios/{servicio}/{que}",
        params={"ambiente": ambiente, **extra}, headers=ADMIN,
        files={"archivo": (f"a.{'crt' if que == 'certificado' else 'key'}", contenido)},
    )


def _cargar_par(client, ambiente="homologacion", empresa=None, **kw):
    crt, key = _par(**kw)
    extra = {"empresa": empresa} if empresa else {}
    assert _subir(client, "certificado", crt, ambiente, **extra).status_code == 200
    assert _subir(client, "clave", key, ambiente, **extra).status_code == 200
    return crt, key


# ── Por omisión, el router es el de siempre ─────────────────────────────────


def test_por_omision_lista_solo_la_facturacion(montar):
    client, _ = montar()
    r = client.get("/config/arca/servicios", headers=ADMIN)
    assert r.status_code == 200
    assert [s["servicio"] for s in r.json()] == ["wsfe"]
    assert r.json()[0]["etiqueta"] == "Facturación electrónica"


def test_por_omision_no_hay_rutas_de_otros_servicios(montar):
    client, _ = montar()
    assert client.get("/config/arca/servicios/wscpe/estado", headers=ADMIN).status_code == 404
    assert _subir(client, "certificado", b"x").status_code in (404, 405)


def test_las_rutas_de_siempre_siguen_con_su_misma_forma(montar):
    """El `/estado` y el `GET` de la facturación no ganan claves: sus consumidores
    (el kit y los productos) no las esperan."""
    client, _ = montar(servicios=("wsfe", "wscpe"))
    client.put("/config/arca", headers=ADMIN, json={"cuit": CUIT_FICTICIO})
    crt, key = _par()
    client.post("/config/arca/certificado", params={"ambiente": "homologacion"}, headers=ADMIN,
                files={"archivo": ("c.crt", crt)})
    client.post("/config/arca/clave", params={"ambiente": "homologacion"}, headers=ADMIN,
                files={"archivo": ("c.key", key)})
    estado = client.get("/config/arca/estado", headers=ADMIN).json()
    assert "cuit_certificado" not in estado["pares"]["homologacion"]
    assert set(estado["pares"]["homologacion"]) == {
        "ambiente", "tiene_certificado", "tiene_clave", "completo",
        "vence", "dias_para_vencer", "vencido", "sujeto"}


def test_un_servicio_que_el_motor_no_conoce_falla_al_montar(montar):
    with pytest.raises(ValueError, match="desconocido"):
        montar(servicios=("wsfe", "wsinventado"))


# ── GET /servicios ───────────────────────────────────────────────────────────


def test_con_opt_in_lista_los_dos(cpe):
    lista = cpe.get("/config/arca/servicios", headers=ADMIN).json()
    assert [s["servicio"] for s in lista] == ["wsfe", "wscpe"]
    cpe_ = lista[1]
    assert cpe_["etiqueta"] == "CTG y Carta de Porte"
    assert "persona que representa a la empresa" in cpe_["ayuda"]
    assert cpe_["configurado"] is False
    assert set(cpe_["pares"]) == {"homologacion", "produccion"}
    assert cpe_["pares"]["homologacion"] == {
        "ambiente": "homologacion", "tiene_certificado": False,
        "tiene_clave": False, "completo": False}


def test_la_facturacion_aparece_con_su_estado_real(cpe):
    cpe.put("/config/arca", headers=ADMIN, json={"cuit": CUIT_FICTICIO, "empresa": "acme"})
    crt, key = _par()
    cpe.post("/config/arca/certificado", params={"ambiente": "produccion", "empresa": "acme"},
             headers=ADMIN, files={"archivo": ("c.crt", crt)})
    cpe.post("/config/arca/clave", params={"ambiente": "produccion", "empresa": "acme"},
             headers=ADMIN, files={"archivo": ("c.key", key)})
    wsfe = cpe.get("/config/arca/servicios", headers=ADMIN).json()[0]
    assert wsfe["empresa"] == "acme"
    assert wsfe["configurado"] is True
    assert wsfe["pares"]["produccion"]["completo"] is True
    assert wsfe["pares"]["produccion"]["cuit_certificado"] == CUIT_FICTICIO
    assert wsfe["pares"]["homologacion"]["completo"] is False


# ── Subir, estado y borrar ──────────────────────────────────────────────────


def test_subir_el_par_de_wscpe_y_ver_su_estado(cpe):
    _cargar_par(cpe)
    r = cpe.get("/config/arca/servicios/wscpe/estado", headers=ADMIN).json()
    h = r["pares"]["homologacion"]
    assert h["completo"] and h["tiene_certificado"] and h["tiene_clave"]
    assert h["cuit_certificado"] == CUIT_FICTICIO
    assert CUIT_FICTICIO in h["sujeto"]
    assert h["vencido"] is False and h["dias_para_vencer"] >= 360
    assert h["vence"]
    assert r["pares"]["produccion"]["completo"] is False
    assert r["configurado"] is True


def test_el_estado_no_expone_rutas_ni_contenido(montar):
    client, app = montar(servicios=("wsfe", "wscpe"))
    crt, key = _par()
    _subir(client, "certificado", crt)
    r = _subir(client, "clave", key)
    assert "PRIVATE KEY" not in r.text and "BEGIN" not in r.text
    assert "_path" not in r.text and app.state.certs_dir not in r.text


def test_los_archivos_van_al_volumen_con_la_clave_en_0600(montar):
    client, app = montar(servicios=("wsfe", "wscpe"))
    _cargar_par(client)
    archivos = sorted(os.listdir(app.state.certs_dir))
    assert len(archivos) == 2
    assert all(a.startswith("wscpe-homologacion-") for a in archivos)
    clave = next(a for a in archivos if a.endswith(".key"))
    assert oct(os.stat(os.path.join(app.state.certs_dir, clave)).st_mode & 0o777) == "0o600"
    # y NO son los de la facturación
    assert CERT_HOMO not in archivos and CLAVE_HOMO not in archivos


def test_subir_wscpe_no_toca_el_par_de_la_facturacion(montar):
    client, app = montar(servicios=("wsfe", "wscpe"))
    client.put("/config/arca", headers=ADMIN, json={"cuit": CUIT_FICTICIO, "empresa": "default"})
    crt, key = _par(alias="facturacion")
    client.post("/config/arca/certificado", params={"ambiente": "homologacion"}, headers=ADMIN,
                files={"archivo": ("c.crt", crt)})
    client.post("/config/arca/clave", params={"ambiente": "homologacion"}, headers=ADMIN,
                files={"archivo": ("c.key", key)})
    antes = db_arca.obtener_arca_config("default")
    con = open(os.path.join(app.state.certs_dir, CERT_HOMO), "rb").read()

    _cargar_par(client, alias="cpe")
    client.delete("/config/arca/servicios/wscpe/credenciales", params={"ambiente": "homologacion"},
                  headers=ADMIN)

    assert db_arca.obtener_arca_config("default") == antes
    assert open(os.path.join(app.state.certs_dir, CERT_HOMO), "rb").read() == con
    assert client.get("/config/arca/estado", headers=ADMIN).json()["pares"]["homologacion"][
        "completo"] is True


def test_cada_ambiente_tiene_su_archivo(montar):
    client, app = montar(servicios=("wsfe", "wscpe"))
    _cargar_par(client, "homologacion", alias="h")
    _cargar_par(client, "produccion", alias="p")
    assert len(os.listdir(app.state.certs_dir)) == 4
    e = client.get("/config/arca/servicios/wscpe/estado", headers=ADMIN).json()["pares"]
    assert e["homologacion"]["completo"] and e["produccion"]["completo"]


def test_dos_empresas_no_se_pisan(montar):
    client, app = montar(servicios=("wsfe", "wscpe"))
    _cargar_par(client, empresa="uno")
    _cargar_par(client, empresa="dos")
    assert len(os.listdir(app.state.certs_dir)) == 4
    uno = client.get("/config/arca/servicios/wscpe/estado", params={"empresa": "uno"},
                     headers=ADMIN).json()
    tres = client.get("/config/arca/servicios/wscpe/estado", params={"empresa": "tres"},
                      headers=ADMIN).json()
    assert uno["pares"]["homologacion"]["completo"] is True
    assert tres["pares"]["homologacion"]["completo"] is False


def test_sin_empresa_usa_la_del_producto_aunque_no_haya_fila_de_facturacion(montar):
    client, _ = montar(servicios=("wsfe", "wscpe"), empresa_por_defecto="transportes-demo")
    _cargar_par(client)
    e = client.get("/config/arca/servicios/wscpe/estado", headers=ADMIN).json()
    assert e["empresa"] == "transportes-demo" and e["configurado"] is True


def test_el_certificado_vencido_se_marca(cpe):
    # Un certificado ya vencido: se arma a mano porque `_par` sólo mira hacia adelante.
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sujeto = x509.Name([x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {CUIT_FICTICIO}"),
                        x509.NameAttribute(NameOID.COMMON_NAME, "vencido")])
    ahora = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(sujeto).issuer_name(sujeto)
            .public_key(clave.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(ahora - datetime.timedelta(days=30))
            .not_valid_after(ahora - datetime.timedelta(days=1)).sign(clave, hashes.SHA256()))
    assert _subir(cpe, "certificado", cert.public_bytes(serialization.Encoding.PEM)).status_code == 200
    h = cpe.get("/config/arca/servicios/wscpe/estado", headers=ADMIN).json()["pares"]["homologacion"]
    assert h["vencido"] is True


def test_un_csr_en_vez_del_certificado_se_rechaza_y_no_deja_nada(cpe, montar):
    client, app = montar(servicios=("wsfe", "wscpe"))
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pedido")]))
           .sign(rsa.generate_private_key(public_exponent=65537, key_size=2048), hashes.SHA256())
           ).public_bytes(serialization.Encoding.PEM)
    r = _subir(client, "certificado", csr)
    assert r.status_code == 422 and "no parece un certificado" in r.json()["detail"]
    assert not os.path.isdir(app.state.certs_dir) or os.listdir(app.state.certs_dir) == []
    assert client.get("/config/arca/servicios/wscpe/estado", headers=ADMIN).json()["configurado"] is False


def test_cruzar_certificado_y_clave_se_rechaza(cpe):
    crt, _ = _par()
    assert _subir(cpe, "clave", crt).status_code == 422          # el .crt en el campo de la clave
    _, key = _par()
    assert _subir(cpe, "certificado", key).status_code == 422    # la clave en el del certificado


def test_una_clave_de_otro_par_no_pisa_la_que_estaba(cpe):
    crt, key = _cargar_par(cpe)
    _, otra = _par()
    r = _subir(cpe, "clave", otra)
    assert r.status_code == 422 and "no es pareja" in r.json()["detail"]
    r = _subir(cpe, "certificado", _par()[0])
    assert r.status_code == 422 and "no es pareja" in r.json()["detail"]
    assert cpe.get("/config/arca/servicios/wscpe/estado", headers=ADMIN).json()[
        "pares"]["homologacion"]["completo"] is True


def test_el_ambiente_es_obligatorio_y_uno_raro_no_cae_a_produccion(cpe):
    crt, _ = _par()
    sin = cpe.post("/config/arca/servicios/wscpe/certificado", headers=ADMIN,
                   files={"archivo": ("c.crt", crt)})
    assert sin.status_code == 422 and "ambiente" in sin.json()["detail"]
    raro = _subir(cpe, "certificado", crt, ambiente="testing")
    assert raro.status_code == 422
    assert cpe.delete("/config/arca/servicios/wscpe/credenciales", headers=ADMIN).status_code == 422
    assert cpe.post("/config/arca/servicios/wscpe/probar", headers=ADMIN).status_code == 422


def test_un_servicio_que_el_producto_no_pidio_es_404(cpe):
    assert cpe.get("/config/arca/servicios/wsinventado/estado", headers=ADMIN).status_code == 404
    assert _subir(cpe, "certificado", b"x", servicio="wsinventado").status_code == 404
    # y la facturación tampoco vive bajo /servicios/{id}: sigue en sus rutas de siempre
    assert cpe.get("/config/arca/servicios/wsfe/estado", headers=ADMIN).status_code == 404


def test_borrar_saca_un_ambiente_y_no_el_otro(montar):
    client, app = montar(servicios=("wsfe", "wscpe"))
    _cargar_par(client, "homologacion")
    _cargar_par(client, "produccion")
    r = client.delete("/config/arca/servicios/wscpe/credenciales",
                      params={"ambiente": "homologacion"}, headers=ADMIN)
    assert r.status_code == 200
    p = r.json()["pares"]
    assert p["homologacion"]["tiene_certificado"] is False and p["homologacion"]["tiene_clave"] is False
    assert p["produccion"]["completo"] is True
    assert len(os.listdir(app.state.certs_dir)) == 2


# ── Probar ──────────────────────────────────────────────────────────────────


def _autenticar_con(monkeypatch, resultado):
    """Reemplaza `arca_wsaa.autenticar`: devuelve el ticket o levanta. Anota las llamadas."""
    import libracore.arca_router as ar
    llamadas = []

    async def falso(cert, clave, ambiente="homologacion", servicio="wsfe"):
        llamadas.append({"ambiente": ambiente, "servicio": servicio})
        if isinstance(resultado, Exception):
            raise resultado
        return resultado

    monkeypatch.setattr(ar.arca_wsaa, "autenticar", falso)
    return llamadas


def test_probar_ok_autentica_para_el_servicio_de_wscpe(cpe, monkeypatch):
    _cargar_par(cpe)
    llamadas = _autenticar_con(monkeypatch, {
        "token": "TKN", "sign": "SGN", "expiracion": "2027-01-01T00:00:00-03:00"})
    r = cpe.post("/config/arca/servicios/wscpe/probar", params={"ambiente": "homologacion"},
                 headers=ADMIN)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["servicio"] == "wscpe" and j["ambiente"] == "homologacion"
    assert j["cuit_certificado"] == CUIT_FICTICIO
    assert j["ticket_vence"] == "2027-01-01T00:00:00-03:00"
    assert j["servicio_en_linea"] == {"appserver": "OK", "authserver": "OK", "dbserver": "OK"}
    assert "CTG y Carta de Porte" in j["mensaje"]
    assert llamadas == [{"ambiente": "homologacion", "servicio": "wscpe"}]
    # El ticket (token y sign) es una credencial: no sale en la respuesta.
    assert "TKN" not in r.text and "SGN" not in r.text


def test_probar_con_el_dummy_caido_sigue_siendo_ok(cpe, monkeypatch):
    _cargar_par(cpe)
    _autenticar_con(monkeypatch, {"token": "t", "sign": "s", "expiracion": ""})

    def roto(*a, **k):
        raise RuntimeError("sin red")
    monkeypatch.setattr(arca_servicios, "dummy", roto)
    r = cpe.post("/config/arca/servicios/wscpe/probar", params={"ambiente": "homologacion"},
                 headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["servicio_en_linea"] is None and "sin red" in r.json()["aviso_en_linea"]


def test_probar_no_autorizado_explica_la_relacion_y_trae_el_texto_de_arca(cpe, monkeypatch):
    _cargar_par(cpe)
    _autenticar_con(monkeypatch, RuntimeError(
        "WSAA error [ns1:coe.notAuthorized]: Computador no autorizado a acceder al servicio"))
    r = cpe.post("/config/arca/servicios/wscpe/probar", params={"ambiente": "homologacion"},
                 headers=ADMIN)
    assert r.status_code == 502
    d = r.json()["detail"]
    assert "no está autorizado para este servicio" in d
    assert "Administrador de Relaciones" in d and "alias del certificado" in d
    assert "Computador no autorizado a acceder al servicio" in d  # el texto de ARCA, tal cual


@pytest.mark.parametrize("codigo, dice", [
    ("cms.cert.untrusted", "homologación no sirve en producción"),
    ("cms.cert.expired", "está vencido"),
])
def test_probar_certificado_no_valido_para_el_ambiente(cpe, monkeypatch, codigo, dice):
    _cargar_par(cpe, "produccion")
    _autenticar_con(monkeypatch, RuntimeError(f"WSAA error [ns1:{codigo}]: Certificado no emitido por AC"))
    r = cpe.post("/config/arca/servicios/wscpe/probar", params={"ambiente": "produccion"},
                 headers=ADMIN)
    assert r.status_code == 502
    assert dice in r.json()["detail"] and "Certificado no emitido por AC" in r.json()["detail"]


def test_probar_con_un_error_que_no_conocemos_deja_el_texto_de_arca(cpe, monkeypatch):
    _cargar_par(cpe)
    _autenticar_con(monkeypatch, RuntimeError("algo raro de ARCA"))
    r = cpe.post("/config/arca/servicios/wscpe/probar", params={"ambiente": "homologacion"},
                 headers=ADMIN)
    assert r.status_code == 502 and r.json()["detail"].endswith("algo raro de ARCA")


def test_probar_sin_par_dice_cual_falta_y_no_sale_a_la_red(cpe, monkeypatch):
    llamadas = _autenticar_con(monkeypatch, {"token": "t", "sign": "s"})
    r = cpe.post("/config/arca/servicios/wscpe/probar", params={"ambiente": "homologacion"},
                 headers=ADMIN)
    assert r.status_code == 400 and "el certificado y la clave privada" in r.json()["detail"]
    _subir(cpe, "certificado", _par()[0])
    r = cpe.post("/config/arca/servicios/wscpe/probar", params={"ambiente": "homologacion"},
                 headers=ADMIN)
    assert r.status_code == 400 and "la clave privada" in r.json()["detail"]
    assert llamadas == []


def test_probar_con_el_par_roto_no_llama_a_arca(montar, tmp_path, monkeypatch):
    client, app = montar(servicios=("wsfe", "wscpe"))
    cert_path, clave_ajena = make_mismatched_key(tmp_path)
    # Se arma el par cruzado directo en el volumen y en la tabla (la API no lo deja subir).
    from libracore.db import arca_credenciales_servicio as db
    os.makedirs(app.state.certs_dir, exist_ok=True)
    destino_c = os.path.join(app.state.certs_dir, "x.crt")
    destino_k = os.path.join(app.state.certs_dir, "x.key")
    open(destino_c, "wb").write(open(cert_path, "rb").read())
    open(destino_k, "wb").write(open(clave_ajena, "rb").read())
    db.guardar_paths_de_servicio("default", "wscpe", "homologacion",
                                 certificado_path=destino_c, clave_path=destino_k)
    llamadas = _autenticar_con(monkeypatch, {"token": "t", "sign": "s"})
    r = client.post("/config/arca/servicios/wscpe/probar", params={"ambiente": "homologacion"},
                    headers=ADMIN)
    assert r.status_code == 400 and "no corresponde" in r.json()["detail"]
    assert llamadas == []


def test_el_ticket_se_reusa_y_no_se_pide_otro(cpe, monkeypatch):
    """Dos «Probar» seguidos son UN login contra WSAA: el segundo sale de la caché.

    Sin eso el segundo habría recibido `coe.alreadyAuthenticated` de ARCA.
    """
    from libracore import arca_wsaa
    _cargar_par(cpe)
    pedidos = []
    vence = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=11)).isoformat()

    async def pedir(cert, clave, ambiente="homologacion", servicio="wsfe"):
        pedidos.append((ambiente, servicio))
        return {"token": "T", "sign": "S", "expiracion": vence}

    monkeypatch.setattr(arca_wsaa, "_pedir_ticket", pedir)
    for _ in range(2):
        r = cpe.post("/config/arca/servicios/wscpe/probar", params={"ambiente": "homologacion"},
                     headers=ADMIN)
        assert r.status_code == 200, r.text
    assert pedidos == [("homologacion", "wscpe")]


# ── Gate y auditoría ────────────────────────────────────────────────────────


@pytest.mark.parametrize("metodo, ruta", [
    ("get", "/config/arca/servicios"),
    ("get", "/config/arca/servicios/wscpe/estado"),
    ("post", "/config/arca/servicios/wscpe/certificado?ambiente=homologacion"),
    ("post", "/config/arca/servicios/wscpe/clave?ambiente=homologacion"),
    ("delete", "/config/arca/servicios/wscpe/credenciales?ambiente=homologacion"),
    ("post", "/config/arca/servicios/wscpe/probar?ambiente=homologacion"),
])
def test_el_gate_del_producto_cubre_las_rutas_nuevas(cpe, metodo, ruta):
    r = getattr(cpe, metodo)(ruta)  # sin el header de admin
    assert r.status_code == 403


def test_la_auditoria_registra_servicio_y_ambiente_y_nunca_la_clave(montar):
    eventos = []
    client, _ = montar(servicios=("wsfe", "wscpe"), usuario_actual=lambda: "marta",
                       al_cambiar=lambda accion, detalle, usuario: eventos.append(
                           (accion, detalle, usuario)))
    crt, key = _par()
    _subir(client, "certificado", crt)
    _subir(client, "clave", key)
    client.delete("/config/arca/servicios/wscpe/credenciales",
                  params={"ambiente": "homologacion"}, headers=ADMIN)

    assert [e[0] for e in eventos] == ["certificado", "clave", "borrar"]
    for _, detalle, _u in eventos:
        assert detalle["servicio"] == "wscpe" and detalle["ambiente"] == "homologacion"
    assert CUIT_FICTICIO in eventos[0][1]["sujeto"]
    assert "PRIVATE KEY" not in repr(eventos) and key.decode()[30:60] not in repr(eventos)


def test_un_hook_de_auditoria_que_falla_no_tumba_la_subida(montar):
    def roto(*a):
        raise RuntimeError("log caído")
    client, _ = montar(servicios=("wsfe", "wscpe"), al_cambiar=roto)
    crt, _ = _par()
    assert _subir(client, "certificado", crt).status_code == 200


# ── El catálogo y la traducción de errores ──────────────────────────────────


def test_el_catalogo_tiene_los_dos_servicios_y_sus_endpoints():
    assert arca_servicios.servicio("wsfe").etiqueta == "Facturación electrónica"
    c = arca_servicios.servicio("wscpe")
    assert c.wsaa == "wscpe" and c.etiqueta == "CTG y Carta de Porte"
    assert c.endpoints == {
        "homologacion": "https://cpea-ws-qaext.afip.gob.ar/wscpe/services/soap",
        "produccion": "https://cpea-ws.afip.gob.ar/wscpe/services/soap",
    }
    with pytest.raises(arca_servicios.ServicioDesconocido):
        arca_servicios.servicio("nada")


def test_traducir_error_deja_el_texto_de_arca_y_no_inventa(monkeypatch):
    t = arca_servicios.traducir_error_wsaa
    assert t("WSAA error [ns1:coe.notAuthorized]: Computador no autorizado").startswith(
        "El certificado no está autorizado para este servicio")
    assert t("cms.cert.untrusted").startswith("ARCA no reconoce este certificado")
    assert t("WSAA: ARCA ya emitió coe.alreadyAuthenticated").startswith("ARCA ya emitió un ticket")
    assert t("xml.bad.generationTime") .startswith("La hora de este servidor")
    assert t("Tiempo de espera agotado al conectar con WSAA") == "Tiempo de espera agotado al conectar con WSAA"
    assert t("") == ""


def test_dummy_lee_los_tres_servidores(monkeypatch):
    """El parseo del `dummy` real, con un doble de `httpx.Client` (no se sale a la red)."""
    import httpx

    xml = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
           '<ns:dummyResponse xmlns:ns="https://serviciosjava.afip.gob.ar/wscpe/"><ns:respuesta>'
           '<ns:appserver>Ok</ns:appserver><ns:authserver>Ok</ns:authserver>'
           '<ns:dbserver>Ok</ns:dbserver></ns:respuesta></ns:dummyResponse></soap:Body></soap:Envelope>')
    pedidos = []

    class Falso:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, content, headers):
            pedidos.append((url, headers["SOAPAction"]))
            return httpx.Response(200, text=xml)

    monkeypatch.setattr(arca_servicios, "dummy", _DUMMY_REAL)
    monkeypatch.setattr(httpx, "Client", Falso)
    estado = arca_servicios.dummy("wscpe", "homologacion")
    assert estado == {"appserver": "Ok", "authserver": "Ok", "dbserver": "Ok"}
    assert pedidos == [("https://cpea-ws-qaext.afip.gob.ar/wscpe/services/soap",
                        '"https://serviciosjava.afip.gob.ar/wscpe/dummy"')]
    assert arca_servicios.dummy("wsfe", "produccion") is None


def test_dummy_que_no_contesta_un_dummy_levanta(monkeypatch):
    import httpx

    class Falso:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return httpx.Response(500, text="<html><body>caido</body></html>")

    monkeypatch.setattr(arca_servicios, "dummy", _DUMMY_REAL)
    monkeypatch.setattr(httpx, "Client", Falso)
    with pytest.raises(RuntimeError, match="no contestó un dummy"):
        arca_servicios.dummy("wscpe", "produccion")
