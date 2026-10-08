"""El pedido de certificado: la clave nace en el servidor y sólo sale el `.csr` (ADR-036).

Lo que se prueba, de punta a punta y sin salir a la red:

1. El `.csr` es un PKCS#10 válido con el sujeto que pide ARCA, y su pareja es la clave guardada.
2. Una CA de prueba firma ese `.csr`; al subir el `.crt` la clave pendiente pasa a vigente y la
   instancia queda lista **sin que nadie suba una clave**.
3. 🔑 La clave vigente **no se pisa** mientras el pedido espera: ni al generarlo, ni al subir
   un `.crt` de la clave de siempre.
4. Un `.crt` que no empareja con ninguna de las dos claves es 422, y no cambia nada.
5. 🔴 **Ninguna respuesta ni asiento de auditoría lleva la clave privada.**
6. Lo mismo para un servicio que no es la facturación (`wscpe`), con sus rutas.

Datos ficticios: la CA y los certificados son descartables y el CUIT es `20000000001`, que no
es de nadie y cierra el dígito verificador.
"""
import datetime
import importlib
import json
import os
import re

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore import arca_certificados, arca_pedidos, arca_servicios
from libracore.config_manager import ARCHIVOS_POR_AMBIENTE
from libracore.db import arca_config as db_arca
from libracore.db import core
from libracore.db.schema import init_core_schema

ADMIN = {"x-rol": "admin"}
CUIT = "20000000001"
RAZON = "Empresa Ficticia S.A."
CERT_HOMO, CLAVE_HOMO = ARCHIVOS_POR_AMBIENTE["homologacion"]
CERT_PROD, CLAVE_PROD = ARCHIVOS_POR_AMBIENTE["produccion"]
DATOS = {"cuit": CUIT, "razon_social": RAZON, "alias": "libracargowsfehomo"}
USUARIO = {"id": 7, "username": "marta"}

#: Lo que delata una clave privada en cualquier texto.
CLAVE_EN_TEXTO = re.compile(r"PRIVATE KEY")


# ── La CA de prueba: hace de ARCA ───────────────────────────────────────────


class CaDePrueba:
    """Firma pedidos como lo haría ARCA: toma el sujeto y la clave pública del `.csr`."""

    def __init__(self):
        self.clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.nombre = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "CA de prueba"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "No es ARCA"),
        ])

    def firmar(self, csr_pem: bytes | str, dias: int = 730) -> bytes:
        csr = x509.load_pem_x509_csr(csr_pem.encode() if isinstance(csr_pem, str) else csr_pem)
        assert csr.is_signature_valid, "ARCA rechazaría un pedido con la firma rota"
        ahora = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder().subject_name(csr.subject).issuer_name(self.nombre)
            .public_key(csr.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(ahora - datetime.timedelta(days=1))
            .not_valid_after(ahora + datetime.timedelta(days=dias))
            .sign(self.clave, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def ca():
    return CaDePrueba()


def _par_ajeno(cuit: str = CUIT):
    """Un par `(crt, key)` autofirmado que no tiene nada que ver con ningún pedido."""
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sujeto = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
        x509.NameAttribute(NameOID.COMMON_NAME, "ajeno"),
        x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {cuit}"),
    ])
    ahora = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(sujeto).issuer_name(sujeto)
        .public_key(clave.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(ahora - datetime.timedelta(days=1))
        .not_valid_after(ahora + datetime.timedelta(days=365))
        .sign(clave, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        clave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()),
    )


# ── La función pura ─────────────────────────────────────────────────────────


def test_el_csr_trae_el_sujeto_que_pide_arca_y_empareja_con_la_clave():
    p = arca_certificados.generar_pedido(CUIT, RAZON, "libracargowscpeprod")

    csr = x509.load_pem_x509_csr(p.csr)
    assert csr.is_signature_valid
    atributos = {a.oid: a.value for a in csr.subject}
    assert atributos[NameOID.COUNTRY_NAME] == "AR"
    assert atributos[NameOID.ORGANIZATION_NAME] == RAZON
    assert atributos[NameOID.COMMON_NAME] == "libracargowscpeprod"
    assert atributos[NameOID.SERIAL_NUMBER] == f"CUIT {CUIT}"
    # Y en el orden que armaba `openssl req -subj "/C=AR/O=…/CN=…/serialNumber=CUIT n"`.
    assert [a.oid for a in csr.subject] == [
        NameOID.COUNTRY_NAME, NameOID.ORGANIZATION_NAME,
        NameOID.COMMON_NAME, NameOID.SERIAL_NUMBER]

    clave = arca_certificados.leer_clave(p.clave)  # sin passphrase: no levanta
    assert clave.key_size == 2048
    assert (csr.public_key().public_numbers() == clave.public_key().public_numbers())


def test_cada_pedido_trae_una_clave_distinta():
    a = arca_certificados.generar_pedido(CUIT, RAZON, "alias1")
    b = arca_certificados.generar_pedido(CUIT, RAZON, "alias1")
    assert a.clave != b.clave


def test_la_clave_no_se_imprime():
    """Un `print`, un `logger.debug(pedido)` o el informe de un test que falla no la escriben."""
    p = arca_certificados.generar_pedido(CUIT, RAZON, "alias1")
    assert not CLAVE_EN_TEXTO.search(repr(p)) and not CLAVE_EN_TEXTO.search(str(p))


@pytest.mark.parametrize("cuit", ["20-00000000-1", " 20000000001 ", "20 00000000 1"])
def test_el_cuit_se_acepta_escrito_con_guiones_y_espacios(cuit):
    assert arca_certificados.datos_del_pedido(cuit, RAZON, "alias1")[0] == CUIT


def test_la_razon_social_pierde_tildes_y_eñes():
    _, razon, _ = arca_certificados.datos_del_pedido(
        CUIT, "  Cooperativa   Ñandú de José & Hijos  ", "alias1")
    assert razon == "Cooperativa Nandu de Jose & Hijos"


@pytest.mark.parametrize("campo,valor", [
    ("cuit", "2000000000"), ("cuit", "2000000000A"), ("cuit", ""),
    ("razon_social", ""), ("razon_social", "   "), ("razon_social", "Una/Otra"),
    ("razon_social", "x" * 65), ("razon_social", "Empresa 日本"),
    ("alias", "con-guion"), ("alias", "con espacio"), ("alias", "ab"), ("alias", "a" * 41),
    ("alias", ""),
])
def test_los_datos_que_arca_rechazaria_se_rechazan_antes(campo, valor):
    datos = {"cuit": CUIT, "razon_social": RAZON, "alias": "alias1", campo: valor}
    with pytest.raises(arca_certificados.PedidoInvalido):
        arca_certificados.datos_del_pedido(**datos)


# ── El almacén del pedido pendiente ─────────────────────────────────────────


def test_guardar_leer_y_descartar(tmp_path):
    carpeta = str(tmp_path / "arca_certs")
    gen = arca_certificados.generar_pedido(CUIT, RAZON, "alias1")
    assert arca_pedidos.leer(carpeta, "wsfe", "produccion", "acme") is None

    pedido = arca_pedidos.guardar(carpeta, "wsfe", "produccion", "acme", gen)

    leido = arca_pedidos.leer(carpeta, "wsfe", "produccion", "acme")
    assert leido == pedido
    assert leido.csr == gen.csr and leido.alias == "alias1" and leido.cuit == CUIT
    assert arca_pedidos.clave(carpeta, "wsfe", "produccion", "acme") == gen.clave
    claves = [f for f in os.listdir(carpeta) if f.endswith(".key")]
    assert oct(os.stat(os.path.join(carpeta, claves[0])).st_mode & 0o777) == "0o600"

    assert arca_pedidos.descartar(carpeta, "wsfe", "produccion", "acme") is True
    assert os.listdir(carpeta) == []
    assert arca_pedidos.leer(carpeta, "wsfe", "produccion", "acme") is None
    assert arca_pedidos.descartar(carpeta, "wsfe", "produccion", "acme") is False


def test_un_pedido_es_de_su_servicio_su_ambiente_y_su_empresa(tmp_path):
    carpeta = str(tmp_path)
    gen = arca_certificados.generar_pedido(CUIT, RAZON, "alias1")
    arca_pedidos.guardar(carpeta, "wscpe", "homologacion", "acme", gen)
    for otro in [("wsfe", "homologacion", "acme"), ("wscpe", "produccion", "acme"),
                 ("wscpe", "homologacion", "otra")]:
        assert arca_pedidos.leer(carpeta, *otro) is None, otro
        assert arca_pedidos.clave(carpeta, *otro) is None, otro


def test_sin_el_json_no_hay_pedido_y_un_json_roto_tampoco(tmp_path):
    carpeta = str(tmp_path)
    gen = arca_certificados.generar_pedido(CUIT, RAZON, "alias1")
    arca_pedidos.guardar(carpeta, "wsfe", "produccion", "acme", gen)
    meta = next(f for f in os.listdir(carpeta) if f.endswith(".json"))

    with open(os.path.join(carpeta, meta), "w") as f:
        f.write("{no es json")
    assert arca_pedidos.leer(carpeta, "wsfe", "produccion", "acme") is None

    os.unlink(os.path.join(carpeta, meta))
    assert arca_pedidos.leer(carpeta, "wsfe", "produccion", "acme") is None


def test_el_json_no_lleva_la_clave(tmp_path):
    carpeta = str(tmp_path)
    arca_pedidos.guardar(carpeta, "wsfe", "produccion", "acme",
                         arca_certificados.generar_pedido(CUIT, RAZON, "alias1"))
    meta = next(f for f in os.listdir(carpeta) if f.endswith(".json"))
    texto = open(os.path.join(carpeta, meta), encoding="utf-8").read()
    assert not CLAVE_EN_TEXTO.search(texto)
    assert set(json.loads(texto)) == {"alias", "cuit", "razon_social", "sujeto", "creado", "csr"}


# ── El router ───────────────────────────────────────────────────────────────


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    """`(client, app, registro)`: el router de un producto con `wscpe` y con auditoría."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.arca_credenciales as ac
    import libracore.arca_router as ar
    import libracore.config_manager as cm
    importlib.reload(cm)
    importlib.reload(ac)
    importlib.reload(ar)

    core.configure(db_path=str(tmp_path / "pedido_router.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()

    def gate(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    registro = []
    app = FastAPI()
    app.include_router(
        ar.build_arca_router(
            servicios=("wsfe", "wscpe"), usuario_actual=lambda: USUARIO,
            al_cambiar=lambda accion, detalle, usuario: registro.append((accion, detalle, usuario)),
        ),
        dependencies=[Depends(gate)],
    )
    app.state.certs_dir = cm.CERTS_DIR
    app.state.acciones = ar.ACCIONES
    monkeypatch.setattr(arca_servicios, "dummy", lambda *a, **k: None)
    yield TestClient(app), app, registro
    conn.close()
    core._db_path = None


class Sesion:
    """El cliente, con memoria de todo lo que contestó: para asertar que **ninguna**
    respuesta, de ninguna ruta, llevó la clave privada."""

    def __init__(self, client):
        self.client = client
        self.respuestas = []

    def _va(self, r):
        self.respuestas.append(r)
        return r

    def get(self, ruta, **kw):
        return self._va(self.client.get(ruta, headers=ADMIN, **kw))

    def post(self, ruta, **kw):
        return self._va(self.client.post(ruta, headers=ADMIN, **kw))

    def delete(self, ruta, **kw):
        return self._va(self.client.delete(ruta, headers=ADMIN, **kw))

    def pedir(self, ambiente="homologacion", servicio=None, empresa=None, **datos):
        ruta = "/config/arca/pedido" if servicio is None else f"/config/arca/servicios/{servicio}/pedido"
        params = {"ambiente": ambiente, **({"empresa": empresa} if empresa else {})}
        return self.post(ruta, params=params, json={**DATOS, **datos})

    def subir(self, que, contenido, ambiente="homologacion", servicio=None, empresa=None):
        base = "/config/arca" if servicio is None else f"/config/arca/servicios/{servicio}"
        params = {"ambiente": ambiente, **({"empresa": empresa} if empresa else {})}
        return self.post(f"{base}/{que}", params=params,
                         files={"archivo": (f"a.{'crt' if que == 'certificado' else 'key'}", contenido)})

    def sin_clave_privada(self):
        for r in self.respuestas:
            assert not CLAVE_EN_TEXTO.search(r.text), (r.request.method, r.request.url)


@pytest.fixture
def s(entorno):
    sesion = Sesion(entorno[0])
    yield sesion
    sesion.sin_clave_privada()


def _archivos(app):
    return sorted(os.listdir(app.state.certs_dir)) if os.path.isdir(app.state.certs_dir) else []


def _leer(app, nombre):
    return open(os.path.join(app.state.certs_dir, nombre), "rb").read()


# ── El flujo completo de una empresa nueva ──────────────────────────────────


def test_empresa_nueva_pide_sube_el_crt_y_queda_lista_sin_subir_una_clave(s, entorno, ca):
    _, app, _ = entorno
    assert s.get("/config/arca").json() is None

    r = s.pedir("produccion")
    assert r.status_code == 200, r.text
    pedido = r.json()
    assert pedido["pendiente"] is True and pedido["servicio"] == "wsfe"
    assert pedido["alias"] == "libracargowsfehomo" and pedido["cuit"] == CUIT
    assert pedido["csr"].startswith("-----BEGIN CERTIFICATE REQUEST-----")
    assert re.fullmatch(r"\d\d-\d\d-\d{4}", pedido["creado"])

    # La instancia ganó su fila (la pantalla tiene de dónde leer el pedido) con el CUIT pedido.
    cfg = s.get("/config/arca").json()
    assert cfg["cuit"] == CUIT
    par = cfg["pares"]["produccion"]
    assert par["pedido"]["alias"] == "libracargowsfehomo"
    assert par["tiene_certificado"] is False and par["tiene_clave"] is False
    assert "csr" not in par["pedido"], "el estado no repite el .csr en cada lectura"

    # El .csr se baja como archivo.
    d = s.get("/config/arca/pedido.csr", params={"ambiente": "produccion"})
    assert d.status_code == 200
    assert 'filename="libracargowsfehomo.csr"' in d.headers["content-disposition"]
    assert d.content.decode() == pedido["csr"]

    # ARCA firma, y se sube el .crt: sin campo de clave.
    crt = ca.firmar(pedido["csr"])
    r = s.subir("certificado", crt, "produccion")
    assert r.status_code == 200, r.text

    cfg = s.get("/config/arca").json()
    par = cfg["pares"]["produccion"]
    assert par["completo"] is True and par["tiene_clave"] is True
    assert "pedido" not in par, "el pedido se cumplió"
    assert par["dias_para_vencer"] >= 700

    # Los archivos son el par de producción de siempre, empareja, y la clave es 0600.
    assert arca_certificados.son_pareja(_leer(app, CERT_PROD), _leer(app, CLAVE_PROD))
    assert oct(os.stat(os.path.join(app.state.certs_dir, CLAVE_PROD)).st_mode & 0o777) == "0o600"
    # No quedó nada del pedido en el disco.
    assert _archivos(app) == sorted([CERT_PROD, CLAVE_PROD])
    assert s.get("/config/arca/pedido", params={"ambiente": "produccion"}).json()["pendiente"] is False


def test_un_pedido_en_una_instancia_con_cuit_no_lo_pisa(s):
    s.client.put("/config/arca", headers=ADMIN, json={"empresa": "acme", "cuit": "30714738289"})
    r = s.pedir("homologacion", empresa="acme")
    assert r.status_code == 200
    assert s.get("/config/arca", params={"empresa": "acme"}).json()["cuit"] == "30714738289"


def test_pedir_sin_ambiente_o_con_uno_raro_es_422_y_no_adivina(s, entorno):
    _, app, _ = entorno
    for ambiente in ("", "prod", "Produccion2"):
        r = s.post("/config/arca/pedido", params={"ambiente": ambiente}, json=DATOS)
        assert r.status_code == 422, (ambiente, r.text)
    assert _archivos(app) == []


@pytest.mark.parametrize("cambio,texto", [
    ({"cuit": "20000000002"}, "dígito verificador"),
    ({"cuit": "123"}, "11 dígitos"),
    ({"alias": "con-guion"}, "alias"),
    ({"razon_social": ""}, "razón social"),
])
def test_los_datos_malos_son_422_con_la_causa_y_no_guardan_nada(s, entorno, cambio, texto):
    _, app, _ = entorno
    r = s.pedir("homologacion", **cambio)
    assert r.status_code == 422
    assert texto in r.json()["detail"]
    assert _archivos(app) == []


def test_sin_pedido_el_estado_no_gana_claves_ni_el_csr_existe(s):
    assert s.get("/config/arca/pedido", params={"ambiente": "produccion"}).json() == {
        "pendiente": False, "servicio": "wsfe", "ambiente": "produccion"}
    assert s.get("/config/arca/pedido.csr", params={"ambiente": "produccion"}).status_code == 404
    assert s.delete("/config/arca/pedido", params={"ambiente": "produccion"}).status_code == 404


# ── La clave vigente no se pisa ─────────────────────────────────────────────


def _cargar_vigente(s, ambiente="produccion", servicio=None):
    crt, key = _par_ajeno()
    assert s.subir("certificado", crt, ambiente, servicio).status_code == 200
    assert s.subir("clave", key, ambiente, servicio).status_code == 200
    return crt, key


def test_generar_un_pedido_no_toca_el_par_vigente(s, entorno):
    _, app, _ = entorno
    s.client.put("/config/arca", headers=ADMIN, json={"empresa": "default", "cuit": CUIT})
    crt, key = _cargar_vigente(s)
    antes = {n: _leer(app, n) for n in (CERT_PROD, CLAVE_PROD)}

    assert s.pedir("produccion").status_code == 200

    assert {n: _leer(app, n) for n in (CERT_PROD, CLAVE_PROD)} == antes
    par = s.get("/config/arca").json()["pares"]["produccion"]
    assert par["completo"] is True and par["pedido"]["pendiente"] is True
    assert arca_certificados.son_pareja(_leer(app, CERT_PROD), _leer(app, CLAVE_PROD))


def test_un_crt_de_la_clave_vigente_se_acepta_y_el_pedido_sigue_esperando(s, entorno):
    _, app, _ = entorno
    s.client.put("/config/arca", headers=ADMIN, json={"empresa": "default", "cuit": CUIT})
    crt, _ = _cargar_vigente(s)
    s.pedir("produccion")

    # Renovar el .crt de la clave de siempre mientras el pedido nuevo espera.
    assert s.subir("certificado", crt, "produccion").status_code == 200

    par = s.get("/config/arca").json()["pares"]["produccion"]
    assert par["pedido"]["pendiente"] is True
    assert par["completo"] is True


def test_renovar_el_crt_vigente_con_el_del_pedido_reemplaza_el_par_entero(s, entorno, ca):
    _, app, _ = entorno
    s.client.put("/config/arca", headers=ADMIN, json={"empresa": "default", "cuit": CUIT})
    viejo_crt, viejo_key = _cargar_vigente(s)
    pedido = s.pedir("produccion").json()

    r = s.subir("certificado", ca.firmar(pedido["csr"]), "produccion")

    assert r.status_code == 200, r.text
    assert _leer(app, CERT_PROD) != viejo_crt and _leer(app, CLAVE_PROD) != viejo_key
    assert arca_certificados.son_pareja(_leer(app, CERT_PROD), _leer(app, CLAVE_PROD))
    assert "pedido" not in s.get("/config/arca").json()["pares"]["produccion"]


def test_un_crt_que_no_empareja_con_ninguna_clave_es_422_y_no_cambia_nada(s, entorno):
    _, app, _ = entorno
    s.client.put("/config/arca", headers=ADMIN, json={"empresa": "default", "cuit": CUIT})
    _cargar_vigente(s)
    s.pedir("produccion")
    antes = {n: _leer(app, n) for n in _archivos(app)}
    ajeno, _ = _par_ajeno()

    r = s.subir("certificado", ajeno, "produccion")

    assert r.status_code == 422
    assert "pedido pendiente" in r.json()["detail"] and "libracargowsfehomo" in r.json()["detail"]
    assert "ni a la clave que ya está cargada" in r.json()["detail"]
    assert {n: _leer(app, n) for n in _archivos(app)} == antes
    assert s.get("/config/arca").json()["pares"]["produccion"]["pedido"]["pendiente"] is True


def test_con_un_pedido_y_sin_clave_vigente_un_crt_ajeno_tampoco_pasa(s, entorno):
    """Sin pedido, un `.crt` suelto se acepta (la clave puede venir después). **Con** un pedido
    esperando su respuesta, un certificado que no es esa respuesta es un error de armado."""
    _, app, _ = entorno
    s.pedir("homologacion")
    ajeno, _ = _par_ajeno()
    r = s.subir("certificado", ajeno, "homologacion")
    assert r.status_code == 422
    assert "ni a la clave" not in r.json()["detail"]
    assert CERT_HOMO not in _archivos(app)


def test_sin_pedido_un_crt_suelto_sigue_aceptandose_como_siempre(s):
    ajeno, _ = _par_ajeno()
    assert s.subir("certificado", ajeno, "homologacion").status_code == 200


# ── Reemplazar y descartar ──────────────────────────────────────────────────


def test_un_segundo_pedido_pide_confirmacion_y_no_pierde_el_primero(s, entorno, ca):
    _, app, _ = entorno
    primero = s.pedir("homologacion").json()
    claves_antes = {n: _leer(app, n) for n in _archivos(app)}

    r = s.pedir("homologacion", alias="otroalias")
    assert r.status_code == 409
    assert "libracargowsfehomo" in r.json()["detail"] and "se pierde su clave" in r.json()["detail"]
    assert {n: _leer(app, n) for n in _archivos(app)} == claves_antes

    r = s.pedir("homologacion", alias="otroalias", reemplazar=True)
    assert r.status_code == 200
    segundo = r.json()
    assert segundo["csr"] != primero["csr"] and segundo["alias"] == "otroalias"

    # El .crt del primer pedido ya no tiene con qué emparejar.
    assert s.subir("certificado", ca.firmar(primero["csr"]), "homologacion").status_code == 422
    assert s.subir("certificado", ca.firmar(segundo["csr"]), "homologacion").status_code == 200


def test_descartar_borra_la_clave_y_no_toca_el_par_vigente(s, entorno):
    _, app, _ = entorno
    s.client.put("/config/arca", headers=ADMIN, json={"empresa": "default", "cuit": CUIT})
    _cargar_vigente(s)
    vigentes = {n: _leer(app, n) for n in (CERT_PROD, CLAVE_PROD)}
    s.pedir("produccion")
    assert len(_archivos(app)) == 4

    r = s.delete("/config/arca/pedido", params={"ambiente": "produccion"})

    assert r.status_code == 200 and r.json()["pendiente"] is False
    assert _archivos(app) == sorted([CERT_PROD, CLAVE_PROD])
    assert {n: _leer(app, n) for n in _archivos(app)} == vigentes
    assert "pedido" not in s.get("/config/arca").json()["pares"]["produccion"]


def test_quitar_el_par_no_descarta_el_pedido_que_espera(s):
    s.client.put("/config/arca", headers=ADMIN, json={"empresa": "default", "cuit": CUIT})
    _cargar_vigente(s)
    s.pedir("produccion")
    s.delete("/config/arca/credenciales", params={"ambiente": "produccion"})
    par = s.get("/config/arca").json()["pares"]["produccion"]
    assert par["completo"] is False and par["pedido"]["pendiente"] is True


def test_los_dos_ambientes_tienen_cada_uno_su_pedido(s, ca):
    h = s.pedir("homologacion", alias="aliashomo").json()
    p = s.pedir("produccion", alias="aliasprod").json()
    assert h["csr"] != p["csr"]
    # El .crt de producción no sirve en homologación.
    assert s.subir("certificado", ca.firmar(p["csr"]), "homologacion").status_code == 422
    assert s.subir("certificado", ca.firmar(h["csr"]), "homologacion").status_code == 200
    pares = s.get("/config/arca").json()["pares"]
    assert pares["homologacion"]["completo"] and "pedido" not in pares["homologacion"]
    assert pares["produccion"]["pedido"]["alias"] == "aliasprod"


# ── Un servicio que no es la facturación ────────────────────────────────────


def test_wscpe_pide_sube_el_crt_y_queda_listo(s, entorno, ca):
    _, app, _ = entorno
    r = s.pedir("produccion", servicio="wscpe", alias="libracargowscpeprod")
    assert r.status_code == 200, r.text
    pedido = r.json()
    assert pedido["servicio"] == "wscpe"

    est = s.get("/config/arca/servicios/wscpe/estado").json()
    assert est["pares"]["produccion"]["pedido"]["alias"] == "libracargowscpeprod"
    assert est["configurado"] is False
    lista = s.get("/config/arca/servicios").json()
    assert "pedido" in lista[1]["pares"]["produccion"] and "pedido" not in lista[0]["pares"]["produccion"]

    d = s.get("/config/arca/servicios/wscpe/pedido.csr", params={"ambiente": "produccion"})
    assert d.content.decode() == pedido["csr"]

    assert s.subir("certificado", ca.firmar(pedido["csr"]), "produccion", "wscpe").status_code == 200
    est = s.get("/config/arca/servicios/wscpe/estado").json()
    par = est["pares"]["produccion"]
    assert par["completo"] is True and "pedido" not in par
    assert par["cuit_certificado"] == CUIT
    assert est["configurado"] is True

    claves = [n for n in _archivos(app) if n.endswith(".key")]
    assert len(claves) == 1 and claves[0].startswith("wscpe-produccion-")
    assert oct(os.stat(os.path.join(app.state.certs_dir, claves[0])).st_mode & 0o777) == "0o600"
    assert not [n for n in _archivos(app) if n.startswith("pedido-")]


def test_el_pedido_de_wscpe_y_el_de_la_facturacion_son_independientes(s, ca):
    f = s.pedir("homologacion").json()
    c = s.pedir("homologacion", servicio="wscpe", alias="aliascpe").json()
    assert s.subir("certificado", ca.firmar(c["csr"]), "homologacion").status_code == 422
    assert s.subir("certificado", ca.firmar(f["csr"]), "homologacion").status_code == 200
    assert s.get("/config/arca/servicios/wscpe/estado").json()["pares"]["homologacion"][
        "pedido"]["alias"] == "aliascpe"


def test_wscpe_exige_ambiente_y_no_conoce_otro_servicio(s):
    assert s.post("/config/arca/servicios/wscpe/pedido", json=DATOS).status_code == 422
    assert s.post("/config/arca/servicios/wsinventado/pedido",
                  params={"ambiente": "produccion"}, json=DATOS).status_code == 404
    assert s.get("/config/arca/servicios/wsinventado/pedido.csr",
                 params={"ambiente": "produccion"}).status_code == 404


def test_descartar_el_pedido_de_wscpe(s, entorno):
    _, app, _ = entorno
    s.pedir("produccion", servicio="wscpe")
    assert s.delete("/config/arca/servicios/wscpe/pedido",
                    params={"ambiente": "produccion"}).status_code == 200
    assert _archivos(app) == []
    assert s.delete("/config/arca/servicios/wscpe/pedido",
                    params={"ambiente": "produccion"}).status_code == 404


def test_wscpe_un_crt_ajeno_con_un_pedido_pendiente_es_422(s):
    s.pedir("produccion", servicio="wscpe")
    ajeno, _ = _par_ajeno()
    assert s.subir("certificado", ajeno, "produccion", "wscpe").status_code == 422


def test_wscpe_no_pisa_la_clave_vigente(s, entorno, ca):
    _, app, _ = entorno
    _cargar_vigente(s, "produccion", "wscpe")
    clave_vigente = next(n for n in _archivos(app) if n.endswith(".key"))
    antes = _leer(app, clave_vigente)
    s.pedir("produccion", servicio="wscpe")
    assert _leer(app, clave_vigente) == antes


# ── La clave no sale: ni en respuestas, ni en la auditoría, ni en el log ────


def test_ninguna_respuesta_lleva_la_clave_privada_y_la_clave_del_disco_no_esta_en_ninguna(
        s, entorno, ca):
    _, app, _ = entorno
    s.pedir("produccion")
    clave_pem = _leer(app, next(n for n in _archivos(app) if n.endswith(".key"))).decode()
    cuerpo_de_la_clave = "".join(clave_pem.splitlines()[1:-1])

    s.get("/config/arca")
    s.get("/config/arca/estado")
    s.get("/config/arca/servicios")
    s.get("/config/arca/pedido", params={"ambiente": "produccion"})
    descarga = s.get("/config/arca/pedido.csr", params={"ambiente": "produccion"})
    s.post("/config/arca/pedido", params={"ambiente": "produccion"}, json=DATOS)  # 409
    csr = s.get("/config/arca/pedido", params={"ambiente": "produccion"})
    assert csr.status_code == 200
    s.subir("certificado", ca.firmar(descarga.content), "produccion")

    assert s.respuestas
    for r in s.respuestas:
        assert cuerpo_de_la_clave not in r.text.replace("\n", "").replace("\\n", ""), r.request.url
        assert cuerpo_de_la_clave.encode() not in r.content


def test_la_auditoria_registra_el_pedido_el_descarte_y_la_promocion_sin_secretos(s, entorno, ca):
    _, app, registro = entorno
    pedido = s.pedir("produccion").json()
    s.pedir("produccion", alias="otro", reemplazar=True)
    s.delete("/config/arca/pedido", params={"ambiente": "produccion"})
    nuevo = s.pedir("produccion").json()
    s.subir("certificado", ca.firmar(nuevo["csr"]), "produccion")

    assert [a for a, _, _ in registro] == [
        "pedido", "pedido", "descartar_pedido", "pedido", "certificado", "clave"]
    assert all(u == USUARIO for _, _, u in registro)
    assert {a for a, _, _ in registro} <= set(app.state.acciones)
    assert registro[0][1]["reemplazo"] is False and registro[1][1]["reemplazo"] is True
    assert registro[0][1]["alias"] == pedido["alias"] and registro[0][1]["cuit"] == CUIT
    assert registro[4][1]["desde_pedido"] is True and registro[4][1]["alias"] == nuevo["alias"]
    assert registro[5][1] == {"empresa": "default", "ambiente": "produccion", "desde_pedido": True}
    # Nada del par: ni la clave, ni el .csr, ni el certificado entero.
    volcado = json.dumps(registro, default=str)
    assert not CLAVE_EN_TEXTO.search(volcado)
    assert "BEGIN" not in volcado


def test_un_hook_que_revienta_no_tumba_el_pedido(entorno, monkeypatch, tmp_path):
    client, app, _ = entorno
    import libracore.arca_router as ar

    def hook(*_):
        raise RuntimeError("auditoría caída")

    chico = FastAPI()
    chico.include_router(ar.build_arca_router(al_cambiar=hook))
    r = TestClient(chico).post("/config/arca/pedido", params={"ambiente": "homologacion"}, json=DATOS)
    assert r.status_code == 200
    assert _archivos(app)


def test_no_se_loguea_la_clave(s, entorno, caplog):
    import logging
    _, app, _ = entorno
    with caplog.at_level(logging.DEBUG):
        s.pedir("produccion")
        s.pedir("produccion")  # 409
        s.delete("/config/arca/pedido", params={"ambiente": "produccion"})
    assert not CLAVE_EN_TEXTO.search(caplog.text)


# ── Compatibilidad ──────────────────────────────────────────────────────────


def test_las_acciones_declaradas_incluyen_las_nuevas(entorno):
    _, app, _ = entorno
    assert {"pedido", "descartar_pedido"} <= set(app.state.acciones)
    assert {"configurar", "certificado", "clave", "borrar"} <= set(app.state.acciones)


def test_sin_pedido_la_respuesta_de_siempre_no_gana_ninguna_clave(s):
    s.client.put("/config/arca", headers=ADMIN, json={"empresa": "default", "cuit": CUIT})
    cfg = s.get("/config/arca").json()
    assert all("pedido" not in p for p in cfg["pares"].values())
    assert db_arca.obtener_arca_config("default")["cuit"] == CUIT


def test_el_listado_de_servicios_avisa_que_el_motor_admite_el_pedido(s):
    """La pantalla del kit muestra el botón sólo con esta marca: un motor viejo no la manda."""
    lista = s.get("/config/arca/servicios").json()
    assert [x["admite_pedido"] for x in lista] == [True, True]
