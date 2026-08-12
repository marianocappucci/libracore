"""Los routers de Configuracion que montan los seis productos.

El mecanismo de backup tiene sus propios tests en `test_respaldo.py`. Lo que se
prueba aca es la capa HTTP: que el `PUT` no pueda escribir mas de lo que dice,
que el logo no filtre por el lado equivocado, y que un backup invalido devuelva
un mensaje que se pueda leer en la pantalla.
"""
import importlib
import io

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracore.respaldo import Instancia, crear_backup


@pytest.fixture
def app(tmp_path, monkeypatch):
    """App de juguete que monta los tres routers como lo haria un producto:
    la lectura abierta, la escritura detras de un gate de admin."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as cm
    importlib.reload(cm)
    import libracore.config_router as cr
    importlib.reload(cr)

    import sqlite3
    base = tmp_path / "producto.db"
    conn = sqlite3.connect(str(base))
    conn.execute("CREATE TABLE cosas (nombre TEXT)")
    conn.execute("INSERT INTO cosas VALUES ('dato-real')")
    conn.commit()
    conn.close()

    instancia = Instancia(nombre="producto", bases=[base], directorios=[tmp_path / "logos"])

    def solo_admin(x_rol: str = ""):
        # Hace de `require_admin` del producto: lo que importa es que el gate
        # lo pone el consumidor, no el paquete.
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    from fastapi import Header

    def gate(x_rol: str = Header(default="")):
        solo_admin(x_rol)

    app = FastAPI()
    app.include_router(cr.build_empresa_router())
    app.include_router(cr.build_empresa_admin_router(), dependencies=[Depends(gate)])
    app.include_router(
        cr.build_backup_router(instancia, tmp_path / "backups"),
        dependencies=[Depends(gate)],
    )
    app.state.instancia = instancia
    app.state.backups_dir = tmp_path / "backups"
    app.state.cm = cm
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


ADMIN = {"x-rol": "admin"}


# ── Datos de empresa ──────────────────────────────────────────────────────

def test_guardar_y_leer_los_datos_de_empresa(client):
    r = client.put("/api/config/empresa", headers=ADMIN, json={
        "empresa_nombre": "Ferreteria Suipacha",
        "empresa_cuit": "20123456789",
        "empresa_iva_condition": "Responsable Inscripto",
    })
    assert r.status_code == 200, r.text
    assert r.json()["empresa_nombre"] == "Ferreteria Suipacha"

    assert client.get("/api/config/empresa").json()["empresa_cuit"] == "20123456789"


def test_la_respuesta_no_trae_los_secretos_de_la_instancia(client, app):
    """Contalibra devuelve `config_manager.load()` **entero**, que incluye
    `mp_access_token` y `email_smtp_password`. Su pantalla los edita todos en
    el mismo lugar; este router es sólo de empresa, así que mandarlos seria
    filtrarlos a una pantalla que no los pide."""
    app.state.cm.save({
        "empresa_nombre": "X",
        "mp_access_token": "TOKEN-SECRETO",
        "email_smtp_password": "CLAVE-SECRETA",
    })

    cuerpo = client.get("/api/config/empresa").json()
    assert "TOKEN-SECRETO" not in str(cuerpo)
    assert "CLAVE-SECRETA" not in str(cuerpo)
    assert set(cuerpo) == {
        "empresa_nombre", "empresa_direccion", "empresa_cuit", "empresa_telefono",
        "empresa_email", "empresa_iibb", "empresa_iva_condition",
        "empresa_inicio_actividades",
    }


def test_un_put_con_una_clave_de_mas_no_escribe_en_la_config(client, app):
    """`config.json` guarda tambien el token de MercadoPago y la contrasena de
    SMTP. Aceptar un dict libre dejaria que este endpoint los pisara."""
    client.put("/api/config/empresa", headers=ADMIN, json={
        "empresa_nombre": "Legitimo", "mp_access_token": "INYECTADO",
    })

    assert app.state.cm.load()["mp_access_token"] != "INYECTADO"


def test_escribir_exige_el_gate_del_producto(client):
    r = client.put("/api/config/empresa", json={"empresa_nombre": "X"})
    assert r.status_code == 403


def test_leer_no_lo_exige(client):
    """LibraDesk ya tiene el `GET` abierto a cualquier usuario logueado, porque
    el generador de PDF lo usa. Por eso son dos routers y no uno."""
    assert client.get("/api/config/empresa").status_code == 200


# ── Logo ──────────────────────────────────────────────────────────────────

def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"0" * 40


def test_subir_y_bajar_el_logo(client):
    r = client.post(
        "/api/config/empresa/logo", headers=ADMIN,
        files={"logo": ("mi-logo.png", _png(), "image/png")},
    )
    assert r.status_code == 200, r.text

    bajado = client.get("/api/config/empresa/logo")
    assert bajado.status_code == 200
    assert bajado.headers["content-type"] == "image/png"
    assert bajado.content == _png()


def test_sin_logo_cargado_devuelve_404_y_no_500(client):
    assert client.get("/api/config/empresa/logo").status_code == 404


def test_un_archivo_que_no_es_imagen_se_rechaza(client):
    r = client.post(
        "/api/config/empresa/logo", headers=ADMIN,
        files={"logo": ("virus.exe", b"MZ", "application/octet-stream")},
    )
    assert r.status_code == 422


def test_el_webp_se_rechaza_aunque_el_resolver_lo_acepte(client):
    """`resolve_logo_path` lo lista como extension valida por historia, pero
    fpdf2 no lo dibuja: dejarlo subir daria un logo que se ve en la pantalla y
    revienta el comprobante."""
    r = client.post(
        "/api/config/empresa/logo", headers=ADMIN,
        files={"logo": ("logo.webp", b"RIFF", "image/webp")},
    )
    assert r.status_code == 422


def test_subir_un_logo_nuevo_borra_el_anterior(client, app):
    """Sin esto quedan `logo.png` y `logo.jpg` conviviendo, y
    `resolve_logo_path` elige **por fecha de modificacion** cuando el
    `logo_path` guardado no existe — o sea que el logo viejo puede volver solo
    despues de una migracion de rutas."""
    client.post("/api/config/empresa/logo", headers=ADMIN,
                files={"logo": ("a.jpg", b"\xff\xd8\xff" + b"0" * 20, "image/jpeg")})
    client.post("/api/config/empresa/logo", headers=ADMIN,
                files={"logo": ("b.png", _png(), "image/png")})

    import os
    quedan = sorted(os.listdir(app.state.cm.LOGO_DIR))
    assert quedan == ["logo.png"]


def test_borrar_el_logo(client):
    client.post("/api/config/empresa/logo", headers=ADMIN,
                files={"logo": ("a.png", _png(), "image/png")})
    assert client.delete("/api/config/empresa/logo", headers=ADMIN).status_code == 200
    assert client.get("/api/config/empresa/logo").status_code == 404


# ── Backup ────────────────────────────────────────────────────────────────

def test_crear_listar_y_descargar_un_backup(client):
    creado = client.post("/api/config/backups", headers=ADMIN)
    assert creado.status_code == 200, creado.text
    nombre = creado.json()["filename"]

    listado = client.get("/api/config/backups", headers=ADMIN).json()
    assert [f["filename"] for f in listado] == [nombre]

    bajado = client.get(f"/api/config/backups/{nombre}", headers=ADMIN)
    assert bajado.status_code == 200
    assert bajado.content[:2] == b"PK"


def test_bajar_uno_al_vuelo_sin_dejarlo_en_el_servidor(client):
    r = client.get("/api/config/backup-ahora", headers=ADMIN)
    assert r.status_code == 200
    assert r.content[:2] == b"PK"


def test_no_se_puede_bajar_algo_de_afuera_de_la_carpeta(client):
    assert client.get("/api/config/backups/..%2F..%2Fetc%2Fpasswd", headers=ADMIN).status_code in (400, 404)


def test_un_backup_que_no_esta_devuelve_404(client):
    assert client.get("/api/config/backups/backup_manual_19990101_000000.zip",
                      headers=ADMIN).status_code == 404


def test_restaurar_un_backup_valido(client, app):
    import sqlite3
    contenido = client.get("/api/config/backup-ahora", headers=ADMIN).content

    base = app.state.instancia.bases[0]
    conn = sqlite3.connect(str(base))
    conn.execute("UPDATE cosas SET nombre = 'ensuciado'")
    conn.commit()
    conn.close()

    r = client.post("/api/config/restore", headers=ADMIN,
                    files={"backup_file": ("b.zip", contenido, "application/zip")})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    conn = sqlite3.connect(str(base))
    assert conn.execute("SELECT nombre FROM cosas").fetchone()[0] == "dato-real"
    conn.close()


def test_el_router_pasa_los_hooks_de_conexion_al_restore(tmp_path, monkeypatch):
    """🔴 Que el router no se los coma. Sin ellos el restore devuelve `ok` y no
    tiene efecto hasta que se reinicie el contenedor — ver
    `test_respaldo.py::test_las_conexiones_se_cierran_antes_y_se_reabren_despues`.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_router as cr
    importlib.reload(cr)

    import sqlite3
    base = tmp_path / "producto.db"
    sqlite3.connect(str(base)).close()
    inst = Instancia(nombre="producto", bases=[base])
    llamados = []

    app = FastAPI()
    app.include_router(cr.build_backup_router(
        inst, tmp_path / "bk",
        cerrar_conexiones=lambda: llamados.append("cerrar"),
        reabrir_conexiones=lambda: llamados.append("reabrir"),
    ))
    c = TestClient(app)
    contenido = c.get("/api/config/backup-ahora").content

    assert c.post("/api/config/restore",
                  files={"backup_file": ("b.zip", contenido, "application/zip")}).status_code == 200
    assert llamados == ["cerrar", "reabrir"]


def test_un_backup_invalido_devuelve_422_con_un_mensaje_legible(client):
    """422 y no 500: el archivo se leyo perfecto, lo que no sirve es su
    contenido. Y el mensaje va tal cual a la pantalla, asi que tiene que decir
    **que** esta mal."""
    r = client.post("/api/config/restore", headers=ADMIN,
                    files={"backup_file": ("x.zip", b"no soy un zip", "application/zip")})
    assert r.status_code == 422
    assert ".zip" in r.json()["detail"]


def test_el_backup_tambien_esta_detras_del_gate(client):
    """Un backup es una copia completa de los datos del cliente: quien lo baje
    se lleva todo, incluidos los usuarios."""
    assert client.get("/api/config/backups").status_code == 403
    assert client.get("/api/config/backup-ahora").status_code == 403
    assert client.post("/api/config/restore",
                       files={"backup_file": ("x.zip", b"x", "application/zip")}).status_code == 403


def test_la_instancia_puede_resolverse_por_callable(tmp_path, monkeypatch):
    """El producto que arma sus rutas recien en `create_app()` — los tests
    montan varias apps en el mismo proceso, con un tmp_path distinto cada una."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_router as cr
    importlib.reload(cr)

    import sqlite3
    base = tmp_path / "tardia.db"
    sqlite3.connect(str(base)).close()

    app = FastAPI()
    app.include_router(
        cr.build_backup_router(lambda: Instancia(nombre="t", bases=[base]), tmp_path / "bk"),
    )
    assert TestClient(app).post("/api/config/backups").status_code == 200


# ── el estado de la copia externa, que la pantalla lee ────────────────────────

def test_resguardo_externo_sin_configurar_no_es_una_alarma(client):
    """Para la pantalla, "no hay archivo" es "no tenés el add-on" — no un
    error. Mostrarle una alarma a quien no lo contrató es ruido."""
    r = client.get('/api/config/resguardo-externo', headers={'x-rol': 'admin'})

    assert r.status_code == 200
    assert r.json() == {
        'contratado': False, 'al_dia': None, 'motivo': None, 'detalle': None,
    }


def test_resguardo_externo_refleja_lo_que_dejo_el_host(client, app):
    """El host escribe el .externo.json; la app sólo lo cuenta. Nunca sube nada
    ni ve la credencial de la nube del cliente."""
    from datetime import datetime
    from libracore.resguardo_estado import escribir_estado

    escribir_estado(app.state.backups_dir, {
        'ok': True,
        'cuando': datetime.now().isoformat(timespec='seconds'),
        'archivo': 'backup_automatico_20260812_040000.zip',
        'destino': 'drive_cliente:libra/cliente',
        'bytes': 3800000,
        'en_destino': 10,
    })

    r = client.get('/api/config/resguardo-externo', headers={'x-rol': 'admin'})

    datos = r.json()
    assert datos['contratado'] is True
    assert datos['al_dia'] is True
    assert datos['detalle']['destino'] == 'drive_cliente:libra/cliente'


def test_resguardo_externo_muestra_la_falla(client, app):
    from datetime import datetime
    from libracore.resguardo_estado import escribir_estado

    escribir_estado(app.state.backups_dir, {
        'ok': False, 'cuando': datetime.now().isoformat(timespec='seconds'),
        'error': 'token expired',
    })

    datos = client.get('/api/config/resguardo-externo',
                       headers={'x-rol': 'admin'}).json()

    assert datos['al_dia'] is False
    assert 'token expired' in datos['motivo']
