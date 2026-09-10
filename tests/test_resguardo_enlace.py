"""El enlace de la copia externa, hecho desde la pantalla del cliente.

Lo que fija, en orden de lo que duele si se rompe:

1. **El token nunca entra al ZIP del backup.** Un backup se baja y se manda por
   mail: si llevara el `rclone.conf`, llevaria el acceso a la nube del cliente.
2. **Un `state` que no es el emitido, o que ya se uso, no canjea nada** — ni
   siquiera llega a hablar con el proveedor.
3. Que no se guarde un enlace que vence solo (sin `refresh_token`), y que lo
   que se guarda sea lo que rclone necesita para refrescarlo.
4. Que desconectar borre aunque no se pueda revocar.
"""
import base64
import hashlib
import json
import sqlite3
import stat
import zipfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore import resguardo_enlace as enl
from libracore.resguardo_estado import ESTADO
from libracore.respaldo import Instancia, crear_backup

ADMIN = {"x-rol": "admin"}
BASE = "/api/config/resguardo-externo/enlace"
DRIVE = enl.PROVEEDORES["drive"]


@pytest.fixture(autouse=True)
def credenciales(monkeypatch):
    """Google habilitado, Dropbox no. Nada del entorno real se filtra al test."""
    monkeypatch.setenv("RESGUARDO_GDRIVE_CLIENT_ID", "id-google")
    monkeypatch.setenv("RESGUARDO_GDRIVE_CLIENT_SECRET", "secreto-google")
    for var in ("RESGUARDO_DROPBOX_APP_KEY", "RESGUARDO_DROPBOX_APP_SECRET", "RESGUARDO_OAUTH_URL_BASE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def backups(tmp_path):
    return tmp_path / "data" / "backups"


@pytest.fixture
def client(backups):
    def gate(x_rol: str = Header(default="")):
        # Hace de `require_admin` + `require_module` del producto.
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    app = FastAPI()
    app.include_router(
        enl.build_resguardo_enlace_router(backups, carpeta="Resguardo Prueba"),
        dependencies=[Depends(gate)],
    )
    return TestClient(app)


@pytest.fixture
def proveedor(monkeypatch):
    """Google falso. Registra cada llamada; `respuesta_token` y `revocar` se
    pueden cambiar desde el test."""
    falso = SimpleNamespace(
        llamadas=[],
        respuesta_token=httpx.Response(200, json={
            "access_token": "ya29.acceso", "refresh_token": "1//refresco",
            "expires_in": 3599, "token_type": "Bearer",
        }),
        revocar=lambda: httpx.Response(200),
    )

    def post(url, **kw):
        falso.llamadas.append((url, kw))
        if url == DRIVE.token_url:
            return falso.respuesta_token
        if url == DRIVE.revoke_url:
            return falso.revocar()
        return httpx.Response(404)

    def get(url, **kw):
        falso.llamadas.append((url, kw))
        return httpx.Response(200, json={"user": {"emailAddress": "cliente@gmail.com"}})

    monkeypatch.setattr(enl.httpx, "post", post)
    monkeypatch.setattr(enl.httpx, "get", get)
    falso.canjes = lambda: [kw for url, kw in falso.llamadas if url == DRIVE.token_url]
    return falso


def _conectar(client, proveedor="drive", **headers):
    r = client.post(f"{BASE}/{proveedor}", headers={**ADMIN, **headers})
    assert r.status_code == 200, r.text
    return {k: v[0] for k, v in parse_qs(urlsplit(r.json()["url"]).query).items()}


def _volver(client, **params):
    r = client.get(f"{BASE}/callback", params=params, headers=ADMIN, follow_redirects=False)
    assert r.status_code == 303, r.text
    destino = urlsplit(r.headers["location"])
    return destino.path, {k: v[0] for k, v in parse_qs(destino.query).items()}


def _enlazar(client):
    q = _conectar(client)
    _, vuelta = _volver(client, state=q["state"], code="4/codigo")
    assert vuelta["resguardo"] == "ok", vuelta
    return q


# ── Que se ofrece ────────────────────────────────────────────────────────────

def test_ofrece_solo_los_proveedores_con_credenciales(client, monkeypatch):
    r = client.get(BASE, headers=ADMIN).json()
    assert r == {"proveedores": [{"clave": "drive", "nombre": "Google Drive"}], "enlace": None}

    monkeypatch.setenv("RESGUARDO_DROPBOX_APP_KEY", "k")
    monkeypatch.setenv("RESGUARDO_DROPBOX_APP_SECRET", "s")
    claves = [p["clave"] for p in client.get(BASE, headers=ADMIN).json()["proveedores"]]
    assert claves == ["drive", "dropbox"]


def test_un_proveedor_sin_credenciales_no_arranca(client):
    r = client.post(f"{BASE}/dropbox", headers=ADMIN)
    assert r.status_code == 422
    assert "no esta habilitado" in r.json()["detail"]
    assert client.post(f"{BASE}/onedrive", headers=ADMIN).status_code == 422


def test_todo_el_enlace_exige_el_gate_del_producto(client):
    """Incluido el callback: la cookie es SameSite=Lax y viaja en la
    redireccion, asi que no hay motivo para dejarlo abierto."""
    assert client.get(BASE).status_code == 403
    assert client.post(f"{BASE}/drive").status_code == 403
    assert client.get(f"{BASE}/callback", params={"state": "x", "code": "y"}).status_code == 403
    assert client.delete(BASE).status_code == 403


# ── La URL de consentimiento ─────────────────────────────────────────────────

def test_la_url_pide_lo_que_hace_falta_para_que_el_enlace_no_venza(client):
    q = _conectar(client)
    assert q["client_id"] == "id-google"
    assert q["redirect_uri"] == f"http://testserver{BASE}/callback"
    assert q["scope"] == "https://www.googleapis.com/auth/drive.file"
    # Sin estos dos Google no devuelve refresh_token desde el segundo enlace.
    assert q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 32


def test_detras_del_proxy_el_redirect_uri_es_el_dominio_del_cliente(client):
    q = _conectar(client, **{"x-forwarded-proto": "https", "host": "compulibra.contalibra.com.ar"})
    assert q["redirect_uri"] == f"https://compulibra.contalibra.com.ar{BASE}/callback"


def test_la_url_base_fija_gana_sobre_los_headers(client, monkeypatch):
    monkeypatch.setenv("RESGUARDO_OAUTH_URL_BASE", "https://fijo.example.com/")
    q = _conectar(client, **{"x-forwarded-proto": "https", "host": "otro.example.com"})
    assert q["redirect_uri"] == f"https://fijo.example.com{BASE}/callback"


# ── El callback ──────────────────────────────────────────────────────────────

def test_enlace_completo(client, backups, proveedor):
    q = _conectar(client)
    ruta, vuelta = _volver(client, state=q["state"], code="4/codigo")
    assert (ruta, vuelta) == ("/configuracion", {"seccion": "datos", "resguardo": "ok"})

    # El canje manda el verificador PKCE que corresponde al desafio de la URL,
    # y el MISMO redirect_uri.
    [canje] = proveedor.canjes()
    datos = canje["data"]
    desafio = base64.urlsafe_b64encode(
        hashlib.sha256(datos["code_verifier"].encode()).digest()
    ).rstrip(b"=").decode()
    assert desafio == q["code_challenge"]
    assert datos["redirect_uri"] == q["redirect_uri"]
    assert datos["code"] == "4/codigo"

    conf = backups / ".resguardo" / "rclone.conf"
    texto = conf.read_text()
    assert texto.startswith("[externo]\ntype = drive\n")
    assert "scope = drive.file" in texto
    # rclone refresca el token con estas dos: sin el secreto el enlace dura una hora.
    assert "client_id = id-google" in texto and "client_secret = secreto-google" in texto
    token = json.loads(texto.split("token = ", 1)[1])
    assert token["refresh_token"] == "1//refresco"
    assert token["expiry"].endswith("Z")
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600

    enlace = client.get(BASE, headers=ADMIN).json()["enlace"]
    assert enlace["proveedor"] == "drive"
    assert enlace["cuenta"] == "cliente@gmail.com"
    assert enlace["carpeta"] == "Resguardo Prueba"
    # Lo que se muestra va en hora de Argentina.
    assert enlace["desde"].endswith("-03:00")


def test_un_state_ajeno_no_llega_a_hablar_con_el_proveedor(client, backups, proveedor):
    _conectar(client)
    _, vuelta = _volver(client, state="uno-inventado", code="4/codigo")
    assert vuelta["resguardo"] == "error"
    assert proveedor.canjes() == []
    assert not (backups / ".resguardo" / "rclone.conf").exists()


def test_el_state_sirve_una_sola_vez(client, proveedor):
    q = _enlazar(client)
    _, vuelta = _volver(client, state=q["state"], code="4/otro")
    assert vuelta["resguardo"] == "error"
    assert len(proveedor.canjes()) == 1


def test_un_enlace_vencido_no_se_completa(client, backups, proveedor, monkeypatch):
    q = _conectar(client)
    vence = json.loads((backups / ".resguardo" / "pendiente.json").read_text())["vence"]
    monkeypatch.setattr(enl, "time", SimpleNamespace(time=lambda: vence + 1))
    _, vuelta = _volver(client, state=q["state"], code="4/codigo")
    assert vuelta["resguardo"] == "error" and "10 minutos" in vuelta["detalle"]
    assert proveedor.canjes() == []


def test_sin_refresh_token_no_se_guarda_un_enlace_que_vence_solo(client, backups, proveedor):
    proveedor.respuesta_token = httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
    q = _conectar(client)
    _, vuelta = _volver(client, state=q["state"], code="4/codigo")
    assert vuelta["resguardo"] == "error" and "permanente" in vuelta["detalle"]
    assert enl.enlace_de(backups) is None
    assert not (backups / ".resguardo" / "rclone.conf").exists()


def test_si_el_proveedor_rechaza_el_codigo_el_cuerpo_no_llega_a_la_url(client, proveedor):
    proveedor.respuesta_token = httpx.Response(400, json={"error": "invalid_grant", "eco": "4/codigo"})
    q = _conectar(client)
    r = client.get(f"{BASE}/callback", params={"state": q["state"], "code": "4/codigo"},
                   headers=ADMIN, follow_redirects=False)
    assert "resguardo=error" in r.headers["location"]
    assert "4%2Fcodigo" not in r.headers["location"] and "invalid_grant" not in r.headers["location"]


def test_si_el_proveedor_no_contesta_no_revienta(client, proveedor, monkeypatch):
    def caido(url, **kw):
        raise httpx.ConnectError("sin red")
    monkeypatch.setattr(enl.httpx, "post", caido)
    q = _conectar(client)
    _, vuelta = _volver(client, state=q["state"], code="4/codigo")
    assert vuelta["resguardo"] == "error" and "No se pudo hablar" in vuelta["detalle"]


def test_si_el_cliente_cancela_se_descarta_el_pendiente(client, backups):
    _conectar(client)
    _, vuelta = _volver(client, error="access_denied")
    assert vuelta["resguardo"] == "error" and "Cancelaste" in vuelta["detalle"]
    assert not (backups / ".resguardo" / "pendiente.json").exists()


def test_el_error_del_proveedor_no_se_refleja_tal_cual(client):
    _, vuelta = _volver(client, error="<b>texto que armo cualquiera</b>")
    assert "texto que armo" not in vuelta["detalle"]


def test_enlazar_borra_el_estado_de_otro_destino(client, backups, proveedor):
    """Si no, la pantalla diria "al dia" para una cuenta a la que no se subio nada."""
    backups.mkdir(parents=True)
    (backups / ESTADO).write_text(json.dumps({"ok": True, "cuando": "2026-09-09T04:00:00"}))
    _enlazar(client)
    assert not (backups / ESTADO).exists()


def test_un_enlace_sin_rclone_conf_no_cuenta_como_conectado(backups):
    d = backups / ".resguardo"
    d.mkdir(parents=True)
    (d / "enlace.json").write_text(json.dumps({"proveedor": "drive"}))
    assert enl.enlace_de(backups) is None
    assert enl.config_rclone(backups) is None


# ── Desconectar ──────────────────────────────────────────────────────────────

def test_desconectar_revoca_y_borra(client, backups, proveedor):
    _enlazar(client)
    (backups / ESTADO).write_text("{}")

    r = client.delete(BASE, headers=ADMIN)
    assert r.json() == {"ok": True, "revocado": True}
    revocaciones = [kw for url, kw in proveedor.llamadas if url == DRIVE.revoke_url]
    assert revocaciones == [{"data": {"token": "1//refresco"}, "timeout": enl._TIMEOUT}]
    assert not any((backups / ".resguardo").iterdir())
    assert not (backups / ESTADO).exists()
    assert client.get(BASE, headers=ADMIN).json()["enlace"] is None


def test_desconectar_borra_aunque_no_se_pueda_revocar(client, backups, proveedor):
    _enlazar(client)

    def caido():
        raise httpx.ConnectError("sin red")
    proveedor.revocar = caido

    assert client.delete(BASE, headers=ADMIN).json() == {"ok": True, "revocado": False}
    assert enl.enlace_de(backups) is None


# ── El ZIP ───────────────────────────────────────────────────────────────────

def test_el_zip_del_backup_nunca_lleva_el_token(tmp_path, client, backups, proveedor):
    """🔴 El caso que motiva `NUNCA_EN_EL_ZIP`: un producto que declara como
    directorio de datos la carpeta que contiene a la de backups."""
    _enlazar(client)
    datos = tmp_path / "data"
    (datos / "logos").mkdir()
    (datos / "logos" / "logo.png").write_bytes(b"png")
    base = tmp_path / "producto.db"
    sqlite3.connect(base).close()

    zip_ = crear_backup(
        Instancia(nombre="producto", bases=[base], directorios=[datos]), tmp_path / "salida",
    )
    nombres = zipfile.ZipFile(zip_).namelist()
    # Control: lo demas del directorio SI entra, asi que la poda no esta
    # vaciando el ZIP entero.
    assert "datos/data/logos/logo.png" in nombres
    assert not [n for n in nombres if ".resguardo" in n], nombres
