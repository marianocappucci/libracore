"""
Que un deploy fallido se REPORTE como fallido.

🔴 Hasta el 2026-08-17 `cmd_actualizar` devolvía `None` en todos los caminos y
`cli()` descartaba el resultado, así que `panel_admin.py actualizar` salía con
código 0 aunque el build se hubiera caído. Se descubrió desplegando los seis
productos: tres builds fallaron y los tres "terminaron bien". Lo que lo delató
fue comparar la imagen del contenedor antes y después.

Estos tests cubren las dos mitades, porque arreglar una sola no alcanza: que la
función devuelva `False`, y que el CLI lo traduzca en un exit distinto de cero.
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
    """El contexto de build sale de git; acá se fija con un doble.

    🔴 Hay que parchear **los dos nombres**: `panel_admin` importó
    `contexto_de_build` con `from . import ...`, así que tiene su propia
    referencia y parchear sólo `provisioning.contexto_de_build` no lo alcanza.
    Con uno solo, el dry-run corría el real y fallaba por "no es un repo git" —
    o sea que un test que esperaba `False` pasaba **por el motivo equivocado**.

    De qué ref sale el contexto lo cubre `test_contexto_de_build.py` contra un
    repo git real."""
    from contextlib import contextmanager

    @contextmanager
    def _falso(repo_root, ref="main", *, from_checkout=False, log=print):
        yield repo_root, "abc1234", f"{ref} (prueba)"

    monkeypatch.setattr(provisioning, "contexto_de_build", _falso)
    monkeypatch.setattr(pa, "contexto_de_build", _falso)


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    """Un producto con un cliente `demo` corriendo, y docker/compose bajo control.

    `estado` permite decidir si el build y el `compose up` andan, que es lo único
    que estos tests necesitan mover.
    """
    repo = tmp_path / "repo"
    (repo / "clientes" / "demo" / "data").mkdir(parents=True)
    (repo / "clientes" / "demo" / "cliente.json").write_text(json.dumps(
        {"nombre": "Demo", "slug": "demo", "port": 9000, "container": "testprod-demo"}))
    (repo / "clientes" / "demo" / "docker-compose.yml").write_text(
        "services:\n  testprod:\n    image: testprod:v1\n"
        "    container_name: testprod-demo\n")
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo, base_port=9000,
    )

    estado = {"build_ok": True, "up_ok": True}
    monkeypatch.setattr(pa, "build_image_tagged",
                        lambda *a, **k: estado["build_ok"])
    monkeypatch.setattr(pa, "container_status", lambda c: {"status": "running"})
    monkeypatch.setattr(pa, "compose", lambda slug, *args: subprocess.CompletedProcess(
        args, 0 if estado["up_ok"] else 1))
    monkeypatch.setattr(pa, "podar_imagenes_viejas", lambda *a, **k: ([], []))
    monkeypatch.setattr(pa, "_guardar_meta", lambda *a, **k: None)
    monkeypatch.setattr(pa, "check_venv_sync", lambda *a, **k: None)
    return estado


def test_el_control_todo_bien_devuelve_True(entorno):
    """Sin esto, un `False` constante pasaría los tests de abajo."""
    assert pa.cmd_actualizar(["demo"]) is True


def test_si_el_build_falla_devuelve_False(entorno):
    entorno["build_ok"] = False
    assert pa.cmd_actualizar(["demo"]) is False


def test_si_una_instancia_no_arranca_devuelve_False(entorno):
    entorno["up_ok"] = False
    assert pa.cmd_actualizar(["demo"]) is False


def test_un_slug_que_no_existe_es_un_fallo(entorno):
    """Nombrar un slug inexistente antes caía en el `[INFO] Sin contenedores` y
    devolvía éxito: un typo en el nombre del cliente se leía como deploy hecho."""
    assert pa.cmd_actualizar(["no-existe"]) is False


def test_sin_slugs_y_sin_clientes_no_es_un_fallo(tmp_path, monkeypatch):
    """El caso legítimo: construir la imagen sin instancias que mover."""
    repo = tmp_path / "vacio"
    (repo / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db", repo_root=repo)
    monkeypatch.setattr(pa, "build_image_tagged", lambda *a, **k: True)
    monkeypatch.setattr(pa, "podar_imagenes_viejas", lambda *a, **k: ([], []))
    monkeypatch.setattr(pa, "check_venv_sync", lambda *a, **k: None)
    assert pa.cmd_actualizar() is True


def test_el_dry_run_que_no_resuelve_el_ref_es_un_fallo(entorno, monkeypatch, capsys):
    from contextlib import contextmanager

    @contextmanager
    def _revienta(*a, **k):
        raise RuntimeError("el ref 'inventado' no existe")
        yield  # pragma: no cover

    monkeypatch.setattr(pa, "contexto_de_build", _revienta)
    assert pa.cmd_actualizar(["demo"], git_ref="inventado", dry_run=True) is False
    # Que el False venga de ESTE motivo y no de otro: la primera version de este
    # test parcheaba el nombre equivocado y pasaba porque el contexto real
    # fallaba por "no es un repo git".
    assert "el ref 'inventado' no existe" in capsys.readouterr().out


def test_el_dry_run_que_resuelve_no_es_un_fallo(entorno):
    assert pa.cmd_actualizar(["demo"], dry_run=True) is True


# ── la otra mitad: que el CLI lo traduzca a un exit code ──────────────────────

def test_el_cli_sale_1_cuando_el_deploy_falla(entorno, monkeypatch):
    """Arreglar sólo `cmd_actualizar` no alcanzaba: `cli()` llamaba a la función
    y descartaba el resultado, así que el fallo seguía saliendo con código 0."""
    entorno["build_ok"] = False
    monkeypatch.setattr(pa.sys, "argv", ["panel_admin.py", "actualizar", "demo"])
    with pytest.raises(SystemExit) as e:
        pa.cli()
    assert e.value.code == 1


def test_el_cli_sale_0_cuando_el_deploy_anda(entorno, monkeypatch):
    monkeypatch.setattr(pa.sys, "argv", ["panel_admin.py", "actualizar", "demo"])
    pa.cli()  # no levanta SystemExit


def test_un_comando_que_devuelve_None_no_hace_salir_1(entorno, monkeypatch):
    """La mayoría de los comandos no informa éxito ni fracaso. La comparación es
    `is False` justamente para no convertirlos a todos en exit 1."""
    monkeypatch.setattr(pa, "cmd_listar", lambda: None)
    monkeypatch.setattr(pa.sys, "argv", ["panel_admin.py", "listar"])
    pa.cli()
