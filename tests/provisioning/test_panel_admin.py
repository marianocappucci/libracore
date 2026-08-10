"""
Tests de libracore.provisioning.panel_admin. Docker/subprocess se mockean
(no debe tocar Docker real); `npm_api.py` se inyecta como módulo falso en
sys.modules, mismo patrón que tests/admin/test_services.py.
"""
import json
from pathlib import Path
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


def _mkclient(cfg, slug, nombre="Cliente", domain="", port=9000, plan="basico",
              db_content=b"", image="testprod:latest", **extra_meta):
    cdir = cfg.clientes_dir / slug
    (cdir / "data").mkdir(parents=True)
    (cdir / "data" / cfg.db_filename).write_bytes(db_content)
    meta = {"nombre": nombre, "slug": slug, "domain": domain, "port": port,
            "container": f"{cfg.container_prefix}-{slug}", "admin_user": "admin",
            "admin_password": "pass", "plan": plan}
    meta.update(extra_meta)
    (cdir / "cliente.json").write_text(json.dumps(meta), encoding="utf-8")
    (cdir / "docker-compose.yml").write_text(
        "services:\n"
        f"  {cfg.container_prefix}:\n"
        f"    image: {image}\n"
        f"    container_name: {cfg.container_prefix}-{slug}\n"
        "    ports:\n"
        f'      - "{port}:8000"\n',
        encoding="utf-8",
    )
    return cdir


def _build_cmd(calls):
    """El `docker build` entre las llamadas capturadas — no es la primera:
    lo precede el `git rev-parse` del label de commit."""
    for cmd in calls:
        if list(cmd[:2]) == ["docker", "build"]:
            return cmd
    raise AssertionError(f"No hubo `docker build` en {calls}")


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

    build_cmd = _build_cmd(build_calls)
    assert "default=" + provisioning.LIBRACORE_SSH_KEY in build_cmd
    assert "libracore=" + provisioning.LIBRACORE_SSH_KEY in build_cmd
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

    build_cmd = _build_cmd(build_calls)
    assert "libracommerce=" + provisioning.LIBRACOMMERCE_SSH_KEY in build_cmd


def test_cmd_actualizar_sin_libra_ui_no_pasa_ese_ssh_id(cfg, monkeypatch):
    (cfg.repo_root / "requirements.txt").write_text("fastapi\nlibracore @ git+ssh://...\n", encoding="utf-8")
    build_calls = []

    def fake_run(cmd, **kwargs):
        build_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(pa.subprocess, "run", fake_run)
    pa.cmd_actualizar()

    build_cmd = _build_cmd(build_calls)
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

    build_cmd = _build_cmd(build_calls)
    assert "libra-ui=" + provisioning.LIBRA_UI_SSH_KEY in build_cmd


# ── versionado de imagen ──────────────────────────────────────────────────────

def test_leer_y_pinear_image_del_compose(cfg):
    _mkclient(cfg, "cliente-uno", image="testprod:latest")
    assert pa.leer_image_pineada("cliente-uno") == "testprod:latest"

    anterior = pa.pinear_image("cliente-uno", "testprod:v2026.03.04-0506")
    assert anterior == "testprod:latest"
    assert pa.leer_image_pineada("cliente-uno") == "testprod:v2026.03.04-0506"

    # el resto del compose queda intacto
    texto = (cfg.clientes_dir / "cliente-uno" / "docker-compose.yml").read_text()
    assert "container_name: testprod-cliente-uno" in texto
    assert '- "9000:8000"' in texto


def test_pinear_image_sin_compose_devuelve_none(cfg):
    cdir = cfg.clientes_dir / "cliente-uno"
    (cdir / "data").mkdir(parents=True)
    (cdir / "cliente.json").write_text('{"slug": "cliente-uno"}', encoding="utf-8")
    assert pa.pinear_image("cliente-uno", "testprod:v1") is None
    assert pa.leer_image_pineada("cliente-uno") is None


def test_pinear_image_solo_toca_la_primera_ocurrencia(cfg):
    cdir = _mkclient(cfg, "cliente-uno", image="testprod:latest")
    compose_file = cdir / "docker-compose.yml"
    compose_file.write_text(
        compose_file.read_text() + "  sidecar:\n    image: otra-cosa:9.9\n", encoding="utf-8"
    )

    pa.pinear_image("cliente-uno", "testprod:v2026.03.04-0506")

    texto = compose_file.read_text()
    assert "image: testprod:v2026.03.04-0506" in texto
    assert "image: otra-cosa:9.9" in texto


def test_cmd_actualizar_pinea_la_version_nueva_en_el_compose(cfg, monkeypatch):
    _mkclient(cfg, "cliente-uno", image="testprod:latest")
    monkeypatch.setattr(pa.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))

    pa.cmd_actualizar(["cliente-uno"], version="v2026.03.04-0506")

    assert pa.leer_image_pineada("cliente-uno") == "testprod:v2026.03.04-0506"
    meta = json.loads((cfg.clientes_dir / "cliente-uno" / "cliente.json").read_text())
    assert meta["version_desplegada"] == "v2026.03.04-0506"
    assert meta["version_anterior"] == "testprod:latest"


def test_cmd_actualizar_no_toca_a_los_clientes_no_nombrados(cfg, monkeypatch):
    """El punto de todo el cambio: mover a un cliente no puede arrastrar al
    otro, que es justo lo que hacía `:latest` compartido."""
    _mkclient(cfg, "cliente-uno", image="testprod:v2026.01.01-0000")
    _mkclient(cfg, "cliente-dos", image="testprod:v2026.01.01-0000")
    monkeypatch.setattr(pa.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))

    pa.cmd_actualizar(["cliente-uno"], version="v2026.03.04-0506")

    assert pa.leer_image_pineada("cliente-uno") == "testprod:v2026.03.04-0506"
    assert pa.leer_image_pineada("cliente-dos") == "testprod:v2026.01.01-0000"


def test_cmd_actualizar_no_repinea_a_un_cliente_detenido(cfg, monkeypatch):
    _mkclient(cfg, "cliente-uno", image="testprod:v2026.01.01-0000")
    monkeypatch.setattr(pa.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(pa, "container_status", lambda c: {"status": "exited", "started": ""})

    pa.cmd_actualizar(["cliente-uno"], version="v2026.03.04-0506")

    assert pa.leer_image_pineada("cliente-uno") == "testprod:v2026.01.01-0000"


def test_cmd_actualizar_revierte_el_pin_si_falla_el_arranque(cfg, monkeypatch, capsys):
    _mkclient(cfg, "cliente-uno", image="testprod:v2026.01.01-0000")
    monkeypatch.setattr(pa.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(pa, "compose", lambda slug, *a: subprocess.CompletedProcess([], 1))

    pa.cmd_actualizar(["cliente-uno"], version="v2026.03.04-0506")

    assert pa.leer_image_pineada("cliente-uno") == "testprod:v2026.01.01-0000"
    assert "repineado" in capsys.readouterr().out
    meta = json.loads((cfg.clientes_dir / "cliente-uno" / "cliente.json").read_text())
    assert "version_desplegada" not in meta


def test_cmd_actualizar_construye_con_tag_de_version(cfg, monkeypatch):
    build_calls = []
    monkeypatch.setattr(pa.subprocess, "run",
                        lambda cmd, **k: build_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))

    pa.cmd_actualizar(version="v2026.03.04-0506")

    build_cmd = _build_cmd(build_calls)
    assert "testprod:v2026.03.04-0506" in build_cmd
    assert "testprod:latest" in build_cmd


def test_versiones_disponibles_excluye_latest_y_ordena(cfg, monkeypatch):
    monkeypatch.setattr(
        pa, "docker",
        lambda *a, capture=False, cwd=None: subprocess.CompletedProcess(
            a, 0, stdout="latest\nv2026.01.01-0900\nv2026.05.05-1200\n<none>\n"),
    )
    assert pa.versiones_disponibles() == ["v2026.05.05-1200", "v2026.01.01-0900"]


def test_cmd_versiones_marca_al_que_sigue_en_latest(cfg, monkeypatch, capsys):
    _mkclient(cfg, "cliente-uno", image="testprod:latest")
    _mkclient(cfg, "cliente-dos", image="testprod:v2026.03.04-0506")
    monkeypatch.setattr(pa, "container_image",
                        lambda c: "testprod:v2026.03.04-0506" if c.endswith("dos") else "testprod:latest")
    monkeypatch.setattr(pa, "image_id", lambda ref: "sha256:aaa")
    monkeypatch.setattr(pa, "container_image_id", lambda c: "sha256:aaa")

    pa.cmd_versiones()

    out = capsys.readouterr().out
    assert "sin pin" in out
    assert "cliente-uno" in out


def test_cmd_versiones_marca_desfasaje_por_id_y_no_por_nombre(cfg, monkeypatch, capsys):
    """El compose es la intención y el contenedor el hecho. La comparación
    va por ID a propósito: dos contenedores pueden decir el mismo
    `producto:latest` y correr imágenes distintas, así que comparar strings
    daría un falso 'todo en orden'."""
    _mkclient(cfg, "cliente-uno", image="testprod:v2026.05.05-1200")
    monkeypatch.setattr(pa, "container_image", lambda c: "testprod:v2026.05.05-1200")
    monkeypatch.setattr(pa, "image_id", lambda ref: "sha256:nueva")
    monkeypatch.setattr(pa, "container_image_id", lambda c: "sha256:vieja")

    pa.cmd_versiones()

    assert "desfasado" in capsys.readouterr().out


def test_cmd_versiones_sin_marcas_cuando_el_pin_y_el_contenedor_coinciden(cfg, monkeypatch, capsys):
    _mkclient(cfg, "cliente-uno", image="testprod:v2026.05.05-1200")
    monkeypatch.setattr(pa, "container_image", lambda c: "testprod:v2026.05.05-1200")
    monkeypatch.setattr(pa, "image_id", lambda ref: "sha256:misma")
    monkeypatch.setattr(pa, "container_image_id", lambda c: "sha256:misma")

    pa.cmd_versiones()

    out = capsys.readouterr().out
    assert "⚠" not in out


def test_cmd_rollback_sin_version_usa_la_anterior_del_cliente(cfg, fake_docker, monkeypatch):
    _mkclient(cfg, "cliente-uno", image="testprod:v2026.05.05-1200",
              version_anterior="testprod:v2026.01.01-0900")
    monkeypatch.setattr(pa, "versiones_disponibles",
                        lambda: ["v2026.05.05-1200", "v2026.01.01-0900"])
    monkeypatch.setattr("builtins.input", lambda *_: "s")

    pa.cmd_rollback("cliente-uno")

    assert pa.leer_image_pineada("cliente-uno") == "testprod:v2026.01.01-0900"
    assert ("cliente-uno", ("up", "-d")) in fake_docker["compose_calls"]


def test_cmd_rollback_rechaza_una_version_inexistente(cfg, monkeypatch, capsys):
    _mkclient(cfg, "cliente-uno", image="testprod:v2026.05.05-1200")
    monkeypatch.setattr(pa, "versiones_disponibles", lambda: ["v2026.05.05-1200"])

    pa.cmd_rollback("cliente-uno", "v2020.01.01-0000")

    assert "no está construida" in capsys.readouterr().out
    assert pa.leer_image_pineada("cliente-uno") == "testprod:v2026.05.05-1200"


def test_cmd_rollback_cancelado_no_toca_el_compose(cfg, monkeypatch):
    _mkclient(cfg, "cliente-uno", image="testprod:v2026.05.05-1200")
    monkeypatch.setattr(pa, "versiones_disponibles",
                        lambda: ["v2026.05.05-1200", "v2026.01.01-0900"])
    monkeypatch.setattr("builtins.input", lambda *_: "n")

    pa.cmd_rollback("cliente-uno", "v2026.01.01-0900")

    assert pa.leer_image_pineada("cliente-uno") == "testprod:v2026.05.05-1200"


def test_cmd_rollback_revierte_el_pin_si_falla_el_arranque(cfg, monkeypatch, capsys):
    _mkclient(cfg, "cliente-uno", image="testprod:v2026.05.05-1200")
    monkeypatch.setattr(pa, "versiones_disponibles",
                        lambda: ["v2026.05.05-1200", "v2026.01.01-0900"])
    monkeypatch.setattr("builtins.input", lambda *_: "s")
    monkeypatch.setattr(pa, "compose", lambda slug, *a: subprocess.CompletedProcess([], 1))

    pa.cmd_rollback("cliente-uno", "v2026.01.01-0900")

    assert pa.leer_image_pineada("cliente-uno") == "testprod:v2026.05.05-1200"
    assert "repineado" in capsys.readouterr().out


# ── El backup del cron cuando la instancia esta en PostgreSQL ─────────────
#
# 🔴 El defecto: `cmd_backup` hacia `if db_src.exists()` y nada mas. Con la
# instancia migrada ese archivo no existe, asi que la base se saltaba **en
# silencio** -- el cron nocturno dejaba un tar.gz con los logos y los adjuntos,
# sin datos, y escribia `[OK]`. Un backup que miente es peor que uno que falta,
# y este corre todas las noches sobre instancias de clientes.

def test_sin_base_y_sin_url_avisa_en_vez_de_callarse(cfg, capsys, monkeypatch):
    """El caso que estaba mudo. Se prueba lo que el defecto NO hacia: decirlo."""
    cdir = _mkclient(cfg, "vacio")
    (cdir / "data" / cfg.db_filename).unlink()
    monkeypatch.setattr(pa, "_url_postgres_del_contenedor", lambda c: None)

    pa.cmd_backup("vacio")

    salida = capsys.readouterr().out
    assert "ERROR" in salida, salida
    assert "no tiene la base" in salida, salida


def test_con_url_de_postgres_hace_el_dump(cfg, capsys, monkeypatch):
    """Con la instancia en PostgreSQL se llama a `pg_dump`, no se saltea."""
    cdir = _mkclient(cfg, "migrado")
    (cdir / "data" / cfg.db_filename).unlink()
    monkeypatch.setattr(
        pa, "_url_postgres_del_contenedor",
        lambda c: "postgresql://u:p@host:5432/base",
    )

    dumps = []

    def falso_dump(url, destino):
        dumps.append((url, Path(destino).name))
        Path(destino).write_bytes(b"PGDMP" + b"0" * 100)

    monkeypatch.setattr("libracore.respaldo._dump_postgres", falso_dump)

    pa.cmd_backup("migrado")

    assert len(dumps) == 1, "no se llamo a pg_dump"
    url, nombre = dumps[0]
    assert url == "postgresql://u:p@host:5432/base"
    assert nombre.endswith(".dump"), nombre
    assert "Dump PostgreSQL" in capsys.readouterr().out


def test_con_archivo_sqlite_sigue_haciendo_la_copia_wal_safe(cfg, capsys, monkeypatch):
    """El contrapeso: el camino de siempre no se rompe. Sin esto, borrar la
    rama SQLite entera tambien pasaria los dos tests de arriba."""
    # Sin `db_content`: un archivo vacio es una base SQLite valida y vacia.
    # Con bytes inventados, `Connection.backup()` tira "file is not a
    # database" -- fallaba el test, no el codigo.
    _mkclient(cfg, "clasico")
    monkeypatch.setattr(pa, "_url_postgres_del_contenedor", lambda c: None)

    pa.cmd_backup("clasico")

    salida = capsys.readouterr().out
    assert "WAL-safe" in salida, salida
    copias = list((cfg.clientes_dir / "clasico" / "backups").glob("*.db"))
    assert len(copias) == 1, copias


def test_la_url_se_busca_por_el_valor_y_no_por_el_nombre(cfg, monkeypatch):
    """Cada producto nombra su variable distinto (`DATABASE_URL`,
    `<PRODUCTO>_DB_PATH`...), asi que se busca por el esquema del valor. Y el
    `postgresql+psycopg://` de SQLAlchemy se normaliza: `pg_dump` no lo
    entiende."""
    import subprocess as sp

    _mkclient(cfg, "raro")
    monkeypatch.setattr(
        pa.subprocess if hasattr(pa, "subprocess") else sp, "run",
        lambda *a, **k: sp.CompletedProcess(
            a, 0,
            stdout="PATH=/usr/bin\nRAROLIBRA_DB_PATH=postgresql+psycopg://u:p@h:5432/b\n",
            stderr="",
        ),
    )
    url = pa._url_postgres_del_contenedor({"container": "testprod-raro"})
    assert url == "postgresql://u:p@h:5432/b", url
