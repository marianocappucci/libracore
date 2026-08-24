"""Que el deploy corra las migraciones, y en el orden correcto.

🔴 **Tener el mecanismo no es tenerlo invocado.** LibraClub llegó a `main` el
2026-08-24 con la revisión `0008` adentro de la imagen y **ningún paso del
deploy la aplicaba**: los únicos `alembic upgrade` del repo estaban en
`semilla_dev.py`, `reset_demo.sh` y la suite. La instancia se habría
reconstruido con código que espera una columna que su base no tiene.

Lo que se fija acá es lo que no se ve leyendo la función:

1. que el comando se corra **antes** del `up -d` y no después —el orden es la
   mitad del arreglo—;
2. que una migración fallida **aborte** el deploy y repinee el compose;
3. y que un producto **sin** migraciones declaradas se comporte exactamente
   como antes, que es el control de que esto no cambió a los otros cinco.
"""
import json
import subprocess

import pytest

from libracore import provisioning
from libracore.provisioning import panel_admin as pa


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


@pytest.fixture(autouse=True)
def _contexto_falso(monkeypatch):
    """Igual que en `test_exit_code_del_deploy`: hay que parchear **los dos**
    nombres, porque `panel_admin` importó `contexto_de_build` con `from . import`
    y tiene su propia referencia."""
    from contextlib import contextmanager

    @contextmanager
    def _falso(repo_root, ref="main", *, from_checkout=False, log=print):
        yield repo_root, "abc1234", f"{ref} (prueba)"

    monkeypatch.setattr(provisioning, "contexto_de_build", _falso)
    monkeypatch.setattr(pa, "contexto_de_build", _falso)


def _armar(tmp_path, monkeypatch, *, migraciones=()):
    """Un producto con un cliente `demo` corriendo y docker/compose bajo control.

    Devuelve `(llamadas, estado)`: `llamadas` es la secuencia de invocaciones a
    `compose`, que es lo que permite afirmar sobre el ORDEN.
    """
    repo = tmp_path / "repo"
    (repo / "clientes" / "demo" / "data").mkdir(parents=True)
    (repo / "clientes" / "demo" / "cliente.json").write_text(json.dumps(
        {"nombre": "Demo", "slug": "demo", "port": 9000, "container": "testprod-demo"}))
    (repo / "clientes" / "demo" / "docker-compose.yml").write_text(
        "services:\n  testprod-demo:\n    image: testprod:v1\n"
        "    container_name: testprod-demo\n")
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo, base_port=9000, migraciones=migraciones,
    )

    llamadas: list[tuple] = []
    estado = {"migracion_ok": True, "up_ok": True}

    def _compose(slug, *args):
        llamadas.append(args)
        if args[:2] == ("run", "--rm"):
            return subprocess.CompletedProcess(args, 0 if estado["migracion_ok"] else 1)
        return subprocess.CompletedProcess(args, 0 if estado["up_ok"] else 1)

    pineos: list[str] = []
    monkeypatch.setattr(pa, "build_image_tagged", lambda *a, **k: True)
    monkeypatch.setattr(pa, "container_status", lambda c: {"status": "running"})
    monkeypatch.setattr(pa, "compose", _compose)
    monkeypatch.setattr(pa, "podar_imagenes_viejas", lambda *a, **k: ([], []))
    monkeypatch.setattr(pa, "_guardar_meta", lambda *a, **k: None)
    monkeypatch.setattr(pa, "check_venv_sync", lambda *a, **k: None)

    def _pinear(slug, ref):
        pineos.append(ref)
        return "testprod:v1"

    monkeypatch.setattr(pa, "pinear_image", _pinear)
    return llamadas, estado, pineos


# ── El control: sin migraciones declaradas, nada cambia ──────────────────────


def test_un_producto_sin_migraciones_no_corre_nada(tmp_path, monkeypatch):
    """El control de no-regresión para los otros cinco productos.

    VentaLibra crea su esquema al conectar y Contalibra/Restolibra con
    `init_core_schema()`: no tienen nada que correr acá. Si este test se pusiera
    rojo, el cambio les habría agregado un paso que no pidieron.
    """
    llamadas, _, _ = _armar(tmp_path, monkeypatch)
    assert pa.cmd_actualizar(["demo"]) is True
    assert llamadas == [("up", "-d")], "sólo el arranque, sin `run`"


# ── Con migraciones: que corran, y ANTES ─────────────────────────────────────


def test_las_migraciones_corren_antes_del_up(tmp_path, monkeypatch):
    """🔑 **El orden es la mitad del arreglo.**

    Migrar después del `up -d` deja una ventana en la que el código nuevo
    consulta un esquema viejo. Con el compose ya pineado, el `run` levanta un
    contenedor efímero con el código NUEVO mientras la instancia sigue sirviendo
    el viejo.
    """
    llamadas, _, _ = _armar(
        tmp_path, monkeypatch, migraciones=("alembic", "upgrade", "head"))
    assert pa.cmd_actualizar(["demo"]) is True

    assert llamadas == [
        ("run", "--rm", "testprod-demo", "alembic", "upgrade", "head"),
        ("up", "-d"),
    ]
    # Explícito además del `==`: si mañana se agrega otra llamada al medio, el
    # `==` se cae por el motivo equivocado y esto dice cuál era la afirmación.
    assert llamadas.index(("up", "-d")) > 0, "el `up` va DESPUÉS de la migración"


def test_el_comando_se_pasa_como_argumentos_sueltos(tmp_path, monkeypatch):
    """Sin shell de por medio: la tupla viaja a `subprocess` argumento por
    argumento. Un string con espacios se ejecutaría como un binario inexistente
    llamado «alembic upgrade head»."""
    llamadas, _, _ = _armar(
        tmp_path, monkeypatch, migraciones=("python", "-m", "app.migrar"))
    pa.cmd_actualizar(["demo"])
    assert llamadas[0] == ("run", "--rm", "testprod-demo", "python", "-m", "app.migrar")


# ── Una migración que falla aborta el deploy ─────────────────────────────────


def test_una_migracion_fallida_no_mueve_la_instancia(tmp_path, monkeypatch):
    """🔴 Seguir sería mover la instancia a código que su base no soporta — el
    peor de los dos estados posibles."""
    llamadas, estado, _ = _armar(
        tmp_path, monkeypatch, migraciones=("alembic", "upgrade", "head"))
    estado["migracion_ok"] = False

    assert pa.cmd_actualizar(["demo"]) is False
    assert ("up", "-d") not in llamadas, "no se arrancó la imagen nueva"


def test_una_migracion_fallida_repinea_el_compose(tmp_path, monkeypatch):
    """Dejar el compose pineado a la imagen nueva con la base sin migrar es una
    bomba: el próximo `up -d` de cualquiera —un reinicio del host— la aplica."""
    _, estado, pineos = _armar(
        tmp_path, monkeypatch, migraciones=("alembic", "upgrade", "head"))
    estado["migracion_ok"] = False

    pa.cmd_actualizar(["demo"])
    assert pineos[-1] == "testprod:v1", "volvió a la versión anterior"


def test_el_control_de_que_el_fallo_viene_de_la_migracion(tmp_path, monkeypatch):
    """Control positivo de los dos de arriba: con la migración en verde y el
    `up` en rojo, el deploy también falla — pero por el otro motivo, y **sí**
    llegó a intentar el arranque. Sin esto, un `False` constante los pasaría
    todos."""
    llamadas, estado, _ = _armar(
        tmp_path, monkeypatch, migraciones=("alembic", "upgrade", "head"))
    estado["up_ok"] = False

    assert pa.cmd_actualizar(["demo"]) is False
    assert ("up", "-d") in llamadas, "acá sí se intentó arrancar"
