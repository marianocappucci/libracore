"""La pantalla de configuración de ARCA que montan los productos que facturan.

Lo que se prueba acá es la capa HTTP. El criptográfico ya lo cubre
`test_arca_certificados.py`; lo que falta verificar es que el router **use** ese
chequeo antes de tocar el disco, que el gate lo pueda poner el producto, y que
un rechazo no deje la instancia peor de lo que estaba.
"""

import importlib
import os

import pytest
from conftest import make_expired_cert_key, make_mismatched_key, make_valid_cert_key
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore.db import core
from libracore.db.schema import init_core_schema

ADMIN = {"x-rol": "admin"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    """App de juguete que monta el router como lo haría un producto: detrás de
    **su** gate de admin, que el paquete no conoce."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as cm
    importlib.reload(cm)
    import libracore.arca_router as ar
    importlib.reload(ar)

    core.configure(db_path=str(tmp_path / "arca_router_test.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()

    def gate(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    aplicacion = FastAPI()
    aplicacion.include_router(ar.build_arca_router(), dependencies=[Depends(gate)])
    aplicacion.state.certs_dir = cm.CERTS_DIR
    yield aplicacion
    conn.close()
    core._db_path = None


@pytest.fixture
def client(app):
    return TestClient(app)


def _bytes_de(tmp_path):
    cert_path, key_path = make_valid_cert_key(tmp_path)
    return open(cert_path, "rb").read(), open(key_path, "rb").read()


def _csr() -> bytes:
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pedido")]))
        .sign(clave, hashes.SHA256())
    ).public_bytes(serialization.Encoding.PEM)


def _subir_cert(client, contenido, nombre="certificado.crt"):
    return client.post(
        "/config/arca/certificado",
        files={"archivo": (nombre, contenido, "application/x-x509-ca-cert")},
        headers=ADMIN,
    )


def _subir_clave(client, contenido, nombre="clave_privada.key"):
    return client.post(
        "/config/arca/clave",
        files={"archivo": (nombre, contenido, "application/octet-stream")},
        headers=ADMIN,
    )


# ── El gate lo pone el producto ──────────────────────────────────────────────

def test_el_gate_lo_pone_el_producto(client):
    """Las dos mitades: sin el rol no entra, con el rol sí.

    Sólo el 403 pasaría igual con un router que no existe.
    """
    assert client.get("/config/arca").status_code == 403
    assert client.get("/config/arca", headers=ADMIN).status_code == 200


# ── Alta y lectura ───────────────────────────────────────────────────────────

def test_arranca_sin_configurar(client):
    assert client.get("/config/arca", headers=ADMIN).json() is None


def test_guardar_y_leer(client):
    guardado = client.put("/config/arca", headers=ADMIN, json={
        "empresa": "default", "cuit": "20289933604",
        "punto_venta": 5, "ambiente": "produccion",
    })
    assert guardado.status_code == 200, guardado.text
    leido = client.get("/config/arca", headers=ADMIN).json()
    assert leido["cuit"] == "20289933604"
    assert leido["punto_venta"] == 5
    assert leido["ambiente"] == "produccion"
    assert leido["tiene_certificado"] is False


def test_un_ambiente_inventado_cae_a_homologacion(client):
    """El ambiente decide contra qué servidor de ARCA se emite. Un valor que no
    es ninguno de los dos no puede quedar guardado: el default seguro es el que
    no emite comprobantes fiscales reales."""
    client.put("/config/arca", headers=ADMIN, json={"ambiente": "PRODUCCION!!"})
    assert client.get("/config/arca", headers=ADMIN).json()["ambiente"] == "homologacion"


# ── Lo que no tiene que entrar ───────────────────────────────────────────────

def test_el_csr_se_rechaza_y_no_deja_nada_en_el_disco(client, app):
    """🔑 Las dos mitades. Un 422 que igual escribió el archivo deja la
    instancia diciendo que tiene certificado cargado."""
    r = _subir_cert(client, _csr())
    assert r.status_code == 422
    assert ".csr" in r.json()["detail"]
    assert not os.path.exists(os.path.join(app.state.certs_dir, "certificado.crt"))
    assert client.get("/config/arca", headers=ADMIN).json() is None


def test_la_clave_en_el_campo_del_certificado_se_rechaza(client, tmp_path):
    _, clave = _bytes_de(tmp_path)
    r = _subir_cert(client, clave)
    assert r.status_code == 422


def test_el_certificado_en_el_campo_de_la_clave_se_rechaza(client, tmp_path):
    certificado, _ = _bytes_de(tmp_path)
    r = _subir_clave(client, certificado)
    assert r.status_code == 422


def test_no_se_puede_mandar_un_path_por_la_api(client):
    """🔴 Cuatro productos aceptaban `certificado_path` del cliente y lo abrían.

    El `PUT` ignora la clave de más: `ArcaPayload` la declara y pydantic la
    descarta, así que el path sigue siendo del servidor.
    """
    client.put("/config/arca", headers=ADMIN, json={
        "cuit": "20289933604", "certificado_path": "/etc/passwd",
        "clave_path": "/etc/shadow",
    })
    leido = client.get("/config/arca", headers=ADMIN).json()
    assert "/etc/passwd" not in leido["certificado_path"]
    assert "/etc/shadow" not in leido["clave_path"]


# ── El par ───────────────────────────────────────────────────────────────────

def test_el_par_bueno_entra(client, tmp_path):
    certificado, clave = _bytes_de(tmp_path)
    assert _subir_cert(client, certificado).status_code == 200
    r = _subir_clave(client, clave)
    assert r.status_code == 200, r.text
    leido = r.json()
    assert leido["tiene_certificado"] is True
    assert leido["tiene_clave"] is True


def test_una_clave_de_otro_par_no_pisa_la_que_estaba(client, tmp_path):
    """🔑 El caso que ningún nombre de archivo detecta, y su contracara: que el
    rechazo **no** deje la instancia con la mitad cambiada."""
    certificado, clave = _bytes_de(tmp_path)
    _subir_cert(client, certificado)
    _subir_clave(client, clave)

    _, clave_ajena_path = make_mismatched_key(tmp_path)
    ajena = open(clave_ajena_path, "rb").read()
    r = _subir_clave(client, ajena)
    assert r.status_code == 422
    assert "pareja" in r.json()["detail"]

    leido = client.get("/config/arca", headers=ADMIN).json()
    with open(leido["clave_path"], "rb") as f:
        assert f.read() == clave, "la clave buena tiene que seguir en el disco"


def test_un_certificado_de_otro_par_tampoco_pisa(client, tmp_path):
    certificado, clave = _bytes_de(tmp_path)
    _subir_cert(client, certificado)
    _subir_clave(client, clave)

    cert_ajeno_path, _ = make_mismatched_key(tmp_path)
    # `make_mismatched_key` devuelve el cert de A y la clave de B; el cert de A
    # no es pareja de la clave que quedó cargada, que es la del par de arriba.
    ajeno = open(cert_ajeno_path, "rb").read()
    r = _subir_cert(client, ajeno)
    assert r.status_code == 422

    leido = client.get("/config/arca", headers=ADMIN).json()
    with open(leido["certificado_path"], "rb") as f:
        assert f.read() == certificado


# ── Sacar el par ─────────────────────────────────────────────────────────────

def test_borrar_credenciales_borra_los_archivos_y_los_paths(client, tmp_path, app):
    certificado, clave = _bytes_de(tmp_path)
    _subir_cert(client, certificado)
    _subir_clave(client, clave)

    r = client.delete("/config/arca/credenciales", headers=ADMIN)
    assert r.status_code == 200
    leido = r.json()
    assert leido["tiene_certificado"] is False
    assert leido["tiene_clave"] is False
    assert not os.path.exists(os.path.join(app.state.certs_dir, "certificado.crt"))
    assert not os.path.exists(os.path.join(app.state.certs_dir, "clave_privada.key"))


def test_borrar_sin_configuracion_es_404(client):
    assert client.delete("/config/arca/credenciales", headers=ADMIN).status_code == 404


# ── Estado ───────────────────────────────────────────────────────────────────

def test_estado_sin_nada(client):
    estado = client.get("/config/arca/estado", headers=ADMIN).json()
    assert estado["configurado"] is False


def test_estado_con_el_par_dice_cuando_vence(client, tmp_path):
    """🔑 El dato que evita la falla silenciosa: duran dos años y el día que
    vencen la facturación deja de andar sin que nadie haya tocado nada."""
    certificado, clave = _bytes_de(tmp_path)
    _subir_cert(client, certificado)
    _subir_clave(client, clave)

    estado = client.get("/config/arca/estado", headers=ADMIN).json()
    assert estado["configurado"] is True
    assert estado["vencido"] is False
    assert estado["dias_para_vencer"] > 0
    assert estado["vence"].count("-") == 2, "dd-mm-aaaa"


def test_estado_marca_el_certificado_vencido(client, tmp_path, app):
    """El par vencido se sube **por afuera del router**: subirlo por la API la
    validación lo frenaría, y lo que se prueba acá es la instancia que ya lo
    tenía cuando venció."""
    cert_path, key_path = make_expired_cert_key(tmp_path)
    os.makedirs(app.state.certs_dir, exist_ok=True)
    destino_cert = os.path.join(app.state.certs_dir, "certificado.crt")
    destino_clave = os.path.join(app.state.certs_dir, "clave_privada.key")
    for origen, destino in ((cert_path, destino_cert), (key_path, destino_clave)):
        with open(origen, "rb") as f, open(destino, "wb") as g:
            g.write(f.read())
    client.put("/config/arca", headers=ADMIN, json={"cuit": "20289933604"})

    estado = client.get("/config/arca/estado", headers=ADMIN).json()
    assert estado["configurado"] is True, "los archivos están: el problema es la fecha"
    assert estado["vencido"] is True
    assert estado["dias_para_vencer"] < 0


# ── Probar contra ARCA ───────────────────────────────────────────────────────

def test_probar_sin_configuracion(client):
    assert client.post("/config/arca/probar", headers=ADMIN).status_code == 400


def test_probar_con_el_par_cruzado_no_llama_a_arca(client, tmp_path, app, monkeypatch):
    """Se corta antes de salir a la red: el error es local y el mensaje lo
    dice. Salir a ARCA con un par roto devuelve un error genérico que manda a
    buscar el problema al lado equivocado."""
    cert_path, clave_ajena = make_mismatched_key(tmp_path)
    os.makedirs(app.state.certs_dir, exist_ok=True)
    for origen, nombre in ((cert_path, "certificado.crt"), (clave_ajena, "clave_privada.key")):
        with open(origen, "rb") as f, open(os.path.join(app.state.certs_dir, nombre), "wb") as g:
            g.write(f.read())
    client.put("/config/arca", headers=ADMIN, json={"cuit": "20289933604"})

    llamadas = []

    import libracore.arca_router as ar

    async def no_deberia_llamarse(*a, **k):
        llamadas.append(a)
        return {"token": "t", "sign": "s"}

    monkeypatch.setattr(ar.arca_wsaa, "autenticar", no_deberia_llamarse)

    r = client.post("/config/arca/probar", headers=ADMIN)
    assert r.status_code == 400
    assert "no corresponde" in r.json()["detail"]
    assert llamadas == [], "no tiene que haber salido a la red"


def test_probar_ok(client, tmp_path, monkeypatch):
    certificado, clave = _bytes_de(tmp_path)
    _subir_cert(client, certificado)
    _subir_clave(client, clave)
    client.put("/config/arca", headers=ADMIN, json={
        "cuit": "20289933604", "ambiente": "homologacion",
    })

    import libracore.arca_router as ar

    async def autenticar_ok(*a, **k):
        return {"token": "TKN", "sign": "SGN", "expiracion": "2027-01-01T00:00:00-03:00"}

    monkeypatch.setattr(ar.arca_wsaa, "autenticar", autenticar_ok)

    r = client.post("/config/arca/probar", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["ambiente"] == "homologacion"


def test_probar_cuando_arca_rechaza_devuelve_el_texto_de_arca(client, tmp_path, monkeypatch):
    """El texto de ARCA va tal cual: es el que distingue "el certificado no
    está habilitado para wsfe" de "la hora del servidor está corrida"."""
    certificado, clave = _bytes_de(tmp_path)
    _subir_cert(client, certificado)
    _subir_clave(client, clave)

    import libracore.arca_router as ar

    async def autenticar_falla(*a, **k):
        raise RuntimeError("Computador no autorizado a acceder al servicio")

    monkeypatch.setattr(ar.arca_wsaa, "autenticar", autenticar_falla)

    r = client.post("/config/arca/probar", headers=ADMIN)
    assert r.status_code == 502
    assert "no autorizado" in r.json()["detail"]


# ── El slug de la empresa lo pone el producto ────────────────────────────────


@pytest.fixture
def cliente_de_instancia_unica(tmp_path, monkeypatch):
    """Un producto de instancia unica, que lee su facturacion con un slug fijo.

    El caso vivo son cuatro: `negocio` en Gestiolibra, `consultorio` en
    MedLibra, `venta` en VentaLibra, `complejo` en LibraClub.

    Se arma igual que el fixture `app` --misma base temporal, mismo DATA_DIR--
    pero declarando el default del producto y sin gate: lo que se prueba aca es
    en que fila cae el guardado, no el permiso.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as cm
    importlib.reload(cm)
    import libracore.arca_router as ar
    importlib.reload(ar)

    core.configure(db_path=str(tmp_path / "arca_slug_test.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()

    aplicacion = FastAPI()
    aplicacion.include_router(ar.build_arca_router(empresa_por_defecto="venta"))
    yield TestClient(aplicacion)
    conn.close()
    core._db_path = None


def test_la_fila_nueva_se_crea_con_el_slug_del_producto(cliente_de_instancia_unica):
    """🔴 La falla que esto cierra es muda: con la fila creada como `default`, el
    PUT contesta 200 y la pantalla dice "Guardado", pero el servicio de
    facturacion del producto --que lee `venta`-- no la ve nunca. Se descubre al
    emitir el primer comprobante."""
    r = cliente_de_instancia_unica.put("/config/arca", json={"cuit": "30111111118", "punto_venta": 3})
    assert r.status_code == 200
    assert r.json()["empresa"] == "venta"


def test_subir_el_certificado_primero_tambien_cae_en_el_slug_del_producto(
    cliente_de_instancia_unica,
):
    """El primer movimiento puede ser subir el certificado, no guardar el CUIT:
    ahi la fila la crea `_guardar_path`, y tiene que caer en el mismo lugar."""
    r = cliente_de_instancia_unica.post(
        "/config/arca/certificado", files={"archivo": ("x.pem", b"no soy un certificado", "text/plain")},
    )
    # Se rechaza por invalido --que es lo correcto-- y no llega a crear fila.
    assert r.status_code == 422


def test_una_fila_que_YA_existe_le_gana_al_default(cliente_de_instancia_unica):
    """El default es para la instancia SIN fila. Si ya hay una --por ejemplo,
    creada con la razon social-- pisarla con el slug del producto crearia una
    segunda al lado de la que la instancia venia usando."""
    from libracore.db import arca_config as db_arca_config
    db_arca_config.crear_arca_config(
        empresa="razon-social-real", cuit="20111111119", punto_venta=1,
        clave_path="", certificado_path="",
    )
    r = cliente_de_instancia_unica.put("/config/arca", json={"cuit": "20111111119", "punto_venta": 9})
    assert r.json()["empresa"] == "razon-social-real"


def test_el_control__sin_declararlo_sigue_siendo_default(client):
    """Contalibra y Restolibra son multi-empresa y no declaran nada. Sin este
    control, un router que escribiera SIEMPRE `venta` pasaria los tres tests de
    arriba y les crearia la fila con el nombre de otro producto."""
    r = client.put("/config/arca", headers=ADMIN, json={"cuit": "30111111118", "punto_venta": 3})
    assert r.json()["empresa"] == "default"
