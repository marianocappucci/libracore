"""El boton de la pantalla tiene que armar (y restaurar) el mismo ZIP que el
cron del host y el restore por CLI: los tres caminos usan
`respaldo.directorios_de_datos(data_dir)` para decidir que subcarpetas de
`data/` entran, no la lista `directorios=[...]` que cada producto escribe a
mano. Ver `config_router.build_backup_router._resolver`.

Corren sobre SQLite en `tmp_path`, sin PostgreSQL: el motor lo soporta en
`bases` y es lo mismo que hace `test_config_router.py`.
"""
import io
import sqlite3
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import libracore.config_router as cr
from libracore.respaldo import Instancia, crear_backup, directorios_de_datos


def _base_sqlite(path: Path) -> Path:
    """Una base SQLite valida y no vacia, como la de `test_config_router.py`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE cosas (nombre TEXT)")
    conn.execute("INSERT INTO cosas VALUES ('dato-real')")
    conn.commit()
    conn.close()
    return path


def _carpeta_con_archivo(carpeta: Path, nombre_archivo: str, contenido: bytes) -> Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / nombre_archivo).write_bytes(contenido)
    return carpeta


def _entradas_datos(zip_bytes: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        return {n for n in z.namelist() if n.startswith("datos/")}


def _app(instancia, backups_dir) -> TestClient:
    app = FastAPI()
    app.include_router(cr.build_backup_router(instancia, backups_dir))
    app.state.instancia_original = instancia
    return TestClient(app)


# ── 1. El boton arma el mismo ZIP que el cron ────────────────────────────────

def test_el_boton_arma_el_mismo_zip_que_el_cron(tmp_path):
    data = tmp_path / "data"
    base = _base_sqlite(data / "producto.db")
    _carpeta_con_archivo(data / "logos", "logo.png", b"PNG")
    _carpeta_con_archivo(data / "arca_certs", "clave_privada.key", b"CLAVE-SECRETA")
    _carpeta_con_archivo(data / "facturas_pdf", "f1.pdf", b"PDF")
    backups_dir = data / "backups"

    # Como declaran VentaLibra y Gestiolibra hoy: solo el logo.
    instancia_producto = Instancia(nombre="producto", bases=[base], directorios=[data / "logos"])
    client = _app(instancia_producto, backups_dir)

    del_boton = client.get("/api/config/backup-ahora")
    assert del_boton.status_code == 200, del_boton.text
    entradas_boton = _entradas_datos(del_boton.content)

    # La regla del cron, armada aparte con el mismo contenido de `data/`.
    instancia_cron = Instancia(
        nombre="producto", bases=[base], directorios=directorios_de_datos(data),
    )
    destino_cron = crear_backup(instancia_cron, tmp_path / "cron_out", motivo="cron")
    entradas_cron = _entradas_datos(destino_cron.read_bytes())

    assert entradas_boton == entradas_cron
    assert "datos/arca_certs/clave_privada.key" in entradas_boton


# ── 2. Una carpeta creada despues de armar el router entra ──────────────────

def test_una_carpeta_creada_despues_de_armar_el_router_entra(tmp_path):
    """No alcanza con que la union se evalue una vez, con tal de que sea
    despues de armar el router: tiene que evaluarse en CADA request, porque
    `arca_certs/` puede aparecer entre dos backups del mismo proceso vivo."""
    data = tmp_path / "data"
    base = _base_sqlite(data / "producto.db")
    _carpeta_con_archivo(data / "logos", "logo.png", b"PNG")
    backups_dir = data / "backups"

    instancia_producto = Instancia(nombre="producto", bases=[base], directorios=[data / "logos"])
    client = _app(instancia_producto, backups_dir)

    # Primer backup: todavia no hay ningun certificado ARCA.
    primero = client.get("/api/config/backup-ahora")
    assert primero.status_code == 200, primero.text
    assert "datos/arca_certs/clave_privada.key" not in _entradas_datos(primero.content)

    # El primer certificado ARCA se sube recien ahora, con la app ya arriba.
    _carpeta_con_archivo(data / "arca_certs", "clave_privada.key", b"CLAVE-SECRETA")

    segundo = client.get("/api/config/backup-ahora")
    assert segundo.status_code == 200, segundo.text
    assert "datos/arca_certs/clave_privada.key" in _entradas_datos(segundo.content)

    # Y una TERCERA carpeta nueva, para descartar un cacheo que solo se activa
    # despues de la primera vez que hubo algo para sumar (el segundo llamado ya
    # habia encontrado una novedad; si ahi se guardara un resultado fijo, esta
    # tercera carpeta no aparecería).
    _carpeta_con_archivo(data / "facturas_pdf", "f1.pdf", b"PDF")

    tercero = client.get("/api/config/backup-ahora")
    assert tercero.status_code == 200, tercero.text
    assert "datos/facturas_pdf/f1.pdf" in _entradas_datos(tercero.content)


# ── 3. Sin duplicados, incluso con otra forma de la misma ruta ──────────────

def test_sin_duplicados_con_otra_forma_de_la_misma_ruta(tmp_path):
    data = tmp_path / "data"
    base = _base_sqlite(data / "producto.db")
    _carpeta_con_archivo(data / "logos", "logo.png", b"PNG")
    backups_dir = data / "backups"

    # Misma carpeta que `directorios_de_datos` encontraria, pero escrita con
    # un ".." en el medio: es una forma distinta de la MISMA ruta resuelta.
    # `otro/` tiene que existir de verdad: el sistema de archivos necesita
    # poder "entrar" ahi para volver con el "..", aunque no tenga nada adentro.
    (data / "otro").mkdir(parents=True, exist_ok=True)
    forma_distinta = data / "otro" / ".." / "logos"
    assert forma_distinta.resolve() == (data / "logos").resolve()

    instancia_producto = Instancia(nombre="producto", bases=[base], directorios=[forma_distinta])
    client = _app(instancia_producto, backups_dir)

    r = client.get("/api/config/backup-ahora")
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        nombres = z.namelist()
    assert len(nombres) == len(set(nombres)), f"hay nombres repetidos: {nombres}"
    # Y el archivo de logos aparece una sola vez, no una por cada forma.
    assert nombres.count("datos/logos/logo.png") == 1


# ── 4. Con un backups_dir que no se llama "backups" ─────────────────────────

def test_backups_dir_con_otro_nombre_no_suma_nada_y_no_se_anida(tmp_path):
    data = tmp_path / "data"
    base = _base_sqlite(data / "producto.db")
    _carpeta_con_archivo(data / "logos", "logo.png", b"PNG")
    _carpeta_con_archivo(data / "arca_certs", "clave_privada.key", b"CLAVE-SECRETA")
    # La carpeta de backups NO se llama "backups": su padre no es realmente
    # `data/`, asi que sumar `directorios_de_datos(padre)` se llevaria adentro
    # a la propia carpeta de respaldos (con los ZIP anteriores).
    backups_dir = data / "respaldo"

    instancia_producto = Instancia(nombre="producto", bases=[base], directorios=[data / "logos"])
    client = _app(instancia_producto, backups_dir)

    # Un backup ya viejo, para probar que el segundo no se lo lleva adentro.
    primero = client.get("/api/config/backup-ahora")
    assert primero.status_code == 200, primero.text

    segundo = client.post("/api/config/backups")
    assert segundo.status_code == 200, segundo.text
    entradas = _entradas_datos(
        (backups_dir / segundo.json()["filename"]).read_bytes()
    )

    # Solo lo que el producto declaro. Ni `arca_certs/` (la union no aplico)
    # ni ningun ".zip" de la carpeta de respaldo colandose adentro.
    assert entradas == {"datos/logos/logo.png"}
    assert not any(n.endswith(".zip") for n in entradas)


# ── 5. Restaurar desde el boton repone lo que el producto no declara ───────

def test_restaurar_desde_el_boton_repone_arca_certs(tmp_path):
    data = tmp_path / "data"
    base = _base_sqlite(data / "producto.db")
    _carpeta_con_archivo(data / "logos", "logo.png", b"PNG")
    _carpeta_con_archivo(data / "arca_certs", "clave_privada.key", b"ORIGINAL")
    backups_dir = data / "backups"

    instancia_producto = Instancia(nombre="producto", bases=[base], directorios=[data / "logos"])
    client = _app(instancia_producto, backups_dir)

    # Backup "del cron": trae logos Y arca_certs, aunque el producto solo
    # declare logos.
    zip_del_cron = client.get("/api/config/backup-ahora").content
    assert "datos/arca_certs/clave_privada.key" in _entradas_datos(zip_del_cron)

    # Se ensucia arca_certs para comprobar que el restore la repone.
    (data / "arca_certs" / "clave_privada.key").write_bytes(b"ENSUCIADO")

    r = client.post("/api/config/restore",
                    files={"backup_file": ("b.zip", zip_del_cron, "application/zip")})
    assert r.status_code == 200, r.text
    assert (data / "arca_certs" / "clave_privada.key").read_bytes() == b"ORIGINAL"


def test_restaurar_un_zip_viejo_sin_arca_certs_no_la_borra(tmp_path):
    """Control del caso anterior: un ZIP viejo que solo trae `logos/` no tiene
    que dejar sin `arca_certs/` a una instancia que ya la tenia viva."""
    data = tmp_path / "data"
    base = _base_sqlite(data / "producto.db")
    _carpeta_con_archivo(data / "logos", "logo.png", b"PNG")
    backups_dir = data / "backups"

    instancia_producto = Instancia(nombre="producto", bases=[base], directorios=[data / "logos"])
    client = _app(instancia_producto, backups_dir)

    # ZIP viejo: se arma ANTES de que exista arca_certs, asi que solo trae logos.
    zip_viejo = client.get("/api/config/backup-ahora").content
    assert "datos/arca_certs" not in "".join(_entradas_datos(zip_viejo))

    # Ahora arca_certs si existe y esta viva.
    _carpeta_con_archivo(data / "arca_certs", "clave_privada.key", b"VIVA")

    r = client.post("/api/config/restore",
                    files={"backup_file": ("viejo.zip", zip_viejo, "application/zip")})
    assert r.status_code == 200, r.text
    assert (data / "arca_certs" / "clave_privada.key").read_bytes() == b"VIVA"


# ── 6. La instancia del producto no se muta entre requests ─────────────────

def test_la_instancia_del_producto_no_se_muta_entre_requests(tmp_path):
    data = tmp_path / "data"
    base = _base_sqlite(data / "producto.db")
    _carpeta_con_archivo(data / "logos", "logo.png", b"PNG")
    backups_dir = data / "backups"

    instancia_producto = Instancia(nombre="producto", bases=[base], directorios=[data / "logos"])
    directorios_originales = list(instancia_producto.directorios)
    client = _app(instancia_producto, backups_dir)

    client.get("/api/config/backup-ahora")
    _carpeta_con_archivo(data / "arca_certs", "clave_privada.key", b"CLAVE-SECRETA")
    r = client.get("/api/config/backup-ahora")
    assert "datos/arca_certs/clave_privada.key" in _entradas_datos(r.content)

    # El objeto que el producto le paso al router sigue igual que al principio.
    assert instancia_producto.directorios == directorios_originales
