"""
Tests de libracore.provisioning.panel_admin. Docker/subprocess se mockean
(no debe tocar Docker real); `npm_api.py` se inyecta como módulo falso en
sys.modules, mismo patrón que tests/admin/test_services.py.
"""
import json
import subprocess
import sys
import types

import pytest

from libracore import provisioning
from libracore.provisioning import panel_admin as pa


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


@pytest.fixture
def fake_docker(monkeypatch):
    calls = []

    def fake_run(*args, capture=False, cwd=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="running|2026-07-14T10:00:00Z", stderr="")

    monkeypatch.setattr(pa, "docker", fake_run)

    compose_calls = []
    monkeypatch.setattr(
        pa, "compose",
        lambda slug, *args: compose_calls.append((slug, args)) or subprocess.CompletedProcess([], 0),
    )
    return {"docker_calls": calls, "compose_calls": compose_calls}


@pytest.fixture
def cfg(tmp_path, fake_docker):
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo_root, base_port=9000,
    )
    return provisioning.get_config()


def _mkclient(cfg, slug, nombre="Cliente", domain="", port=9000, plan="basico", db_content=b""):
    cdir = cfg.clientes_dir / slug
    (cdir / "data").mkdir(parents=True)
    (cdir / "data" / cfg.db_filename).write_bytes(db_content)
    meta = {"nombre": nombre, "slug": slug, "domain": domain, "port": port,
            "container": f"{cfg.container_prefix}-{slug}", "admin_user": "admin",
            "admin_password": "pass", "plan": plan}
    (cdir / "cliente.json").write_text(json.dumps(meta), encoding="utf-8")
    return cdir


def test_load_clients_vacio(cfg):
    assert pa.load_clients() == []


def test_load_clients_y_find_client(cfg):
    _mkclient(cfg, "cliente-uno", nombre="Cliente Uno")
    clientes = pa.load_clients()
    assert len(clientes) == 1
    assert clientes[0]["slug"] == "cliente-uno"
    assert clientes[0]["container"] == "testprod-cliente-uno"

    encontrado = pa.find_client("cliente-uno")
    assert encontrado["nombre"] == "Cliente Uno"
    assert pa.find_client("no-existe") is None


def test_cmd_listar_sin_clientes(cfg, capsys):
    pa.cmd_listar()
    out = capsys.readouterr().out
    assert "No hay clientes" in out


def test_cmd_info_cliente_inexistente(cfg, capsys):
    pa.cmd_info("no-existe")
    assert "no encontrado" in capsys.readouterr().out


def test_cmd_start_stop_restart_delegan_a_compose(cfg, fake_docker):
    _mkclient(cfg, "cliente-uno")
    pa.cmd_start("cliente-uno")
    pa.cmd_stop("cliente-uno")
    pa.cmd_restart("cliente-uno")
    calls = fake_docker["compose_calls"]
    assert ("cliente-uno", ("up", "-d")) in calls
    assert ("cliente-uno", ("stop",)) in calls
    assert ("cliente-uno", ("restart",)) in calls


def test_cmd_backup_crea_tar_y_copia_db(cfg):
    import sqlite3
    cdir = _mkclient(cfg, "cliente-uno")
    db_path = cdir / "data" / cfg.db_filename
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.commit()
    con.close()

    pa.cmd_backup("cliente-uno", quiet=True)

    tars = list(cfg.clientes_dir.glob("cliente-uno_backup_*.tar.gz"))
    assert len(tars) == 1
    dbs = list((cdir / "backups").glob("testprod_*.db"))
    assert len(dbs) == 1


def test_set_servicio_estado_persiste_config(cfg):
    cdir = _mkclient(cfg, "cliente-uno")
    (cdir / "data" / "config.json").write_text("{}", encoding="utf-8")

    ok = pa._set_servicio_estado("cliente-uno", "pausado", "en mantenimiento")
    assert ok is True
    cfg_json = json.loads((cdir / "data" / "config.json").read_text())
    assert cfg_json["servicio_estado"] == "pausado"
    assert cfg_json["servicio_mensaje"] == "en mantenimiento"


def test_cmd_eliminar_borra_directorio(cfg, monkeypatch):
    cdir = _mkclient(cfg, "cliente-uno")
    monkeypatch.setattr("builtins.input", lambda *_: "cliente-uno")
    pa.cmd_eliminar("cliente-uno")
    assert not cdir.exists()


def test_npm_indisponible_muestra_error(cfg, capsys, monkeypatch):
    sys.modules.pop("npm_api", None)
    pa.cmd_npm_listar()
    assert "no disponible" in capsys.readouterr().out


def test_cmd_npm_crear_usa_dominio_del_cliente(cfg, monkeypatch):
    _mkclient(cfg, "cliente-uno", domain="cliente-uno.test")
    created = {}

    class FakeNPMError(Exception):
        pass

    class FakeNPM:
        def get_proxy_host_by_domain(self, domain):
            return None

        def create_proxy_host(self, **kwargs):
            created.update(kwargs)
            return {"id": 5}

    npm_mod = types.ModuleType("npm_api")
    npm_mod.NPMError = FakeNPMError
    npm_mod.client_from_config = lambda: FakeNPM()
    npm_mod.forward_host_from_config = lambda: "10.0.0.1"
    npm_mod.le_email_from_config = lambda: "admin@test.com"
    monkeypatch.setitem(sys.modules, "npm_api", npm_mod)

    pa.cmd_npm_crear("cliente-uno")
    assert created["domain"] == "cliente-uno.test"


def test_menu_incluye_nombre_del_producto(cfg):
    assert "TESTPROD" in pa._menu()


def test_cli_actualizar_sin_slug_actualiza_todos(cfg, monkeypatch):
    _mkclient(cfg, "cliente-uno")
    monkeypatch.setattr(sys, "argv", ["panel_admin.py", "actualizar"])
    monkeypatch.setattr(pa.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 0))
    pa.cli()  # no debe lanzar


def test_cmd_actualizar_avisa_si_venv_desincronizado(cfg, monkeypatch, capsys):
    (cfg.repo_root / "requirements.txt").write_text(
        "libracore @ git+ssh://git@github.com/marianocappucci/libracore.git@v0.23.0\n",
        encoding="utf-8",
    )
    import libracore
    monkeypatch.setattr(libracore, "__version__", "0.15.0")
    monkeypatch.setattr(pa.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0))

    pa.cmd_actualizar()

    salida = capsys.readouterr().out
    assert "ADVERTENCIA" in salida
    assert "0.15.0" in salida
    assert "0.23.0" in salida


def test_cmd_actualizar_sin_libracommerce_no_pasa_ese_ssh_id(cfg, monkeypatch):
    (cfg.repo_root / "requirements.txt").write_text("fastapi\nlibracore @ git+ssh://...\n", encoding="utf-8")
    build_calls = []

    def fake_run(cmd, **kwargs):
        build_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(pa.subprocess, "run", fake_run)
    pa.cmd_actualizar()

    build_cmd = build_calls[0]
    assert "default=" + pa.LIBRACORE_SSH_KEY in build_cmd
    assert "libracore=" + pa.LIBRACORE_SSH_KEY in build_cmd
    assert not any(a.startswith("libracommerce=") for a in build_cmd)


def test_cmd_actualizar_con_libracommerce_agrega_su_ssh_id(cfg, monkeypatch):
    (cfg.repo_root / "requirements.txt").write_text(
        "fastapi\nlibracore @ git+ssh://...\nlibracommerce @ git+ssh://...\n", encoding="utf-8"
    )
    build_calls = []

    def fake_run(cmd, **kwargs):
        build_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(pa.subprocess, "run", fake_run)
    pa.cmd_actualizar()

    build_cmd = build_calls[0]
    assert "libracommerce=" + pa.LIBRACOMMERCE_SSH_KEY in build_cmd


def test_cmd_actualizar_sin_libra_ui_no_pasa_ese_ssh_id(cfg, monkeypatch):
    (cfg.repo_root / "requirements.txt").write_text("fastapi\nlibracore @ git+ssh://...\n", encoding="utf-8")
    build_calls = []

    def fake_run(cmd, **kwargs):
        build_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(pa.subprocess, "run", fake_run)
    pa.cmd_actualizar()

    build_cmd = build_calls[0]
    assert not any(a.startswith("libra-ui=") for a in build_cmd)


def test_cmd_actualizar_con_libra_ui_agrega_su_ssh_id(cfg, monkeypatch):
    (cfg.repo_root / "requirements.txt").write_text("fastapi\nlibracore @ git+ssh://...\n", encoding="utf-8")
    frontend_dir = cfg.repo_root / "frontend"
    frontend_dir.mkdir(parents=True, exist_ok=True)
    (frontend_dir / "package.json").write_text(
        '{"dependencies": {"libra-ui": "git+https://github.com/marianocappucci/libra-ui.git#v0.2.0"}}',
        encoding="utf-8",
    )
    build_calls = []

    def fake_run(cmd, **kwargs):
        build_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(pa.subprocess, "run", fake_run)
    pa.cmd_actualizar()

    build_cmd = build_calls[0]
    assert "libra-ui=" + pa.LIBRA_UI_SSH_KEY in build_cmd
