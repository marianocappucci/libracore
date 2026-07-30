"""
Tests de libracore.provisioning.nuevo_cliente. `plans.py` (planes reales de
cada producto) y `npm_api.py` (vive en scripts/ de cada repo, no en
LibraCore) se inyectan como módulos falsos en sys.modules, mismo patrón que
tests/admin/test_services.py. Docker/subprocess se mockean: crear_cliente()
no debe tocar Docker real.
"""
import json
import re
import subprocess
import sys
import types

import pytest

from libracore import provisioning
from libracore.provisioning import nuevo_cliente as nc


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


@pytest.fixture
def fake_plans(monkeypatch):
    mod = types.ModuleType("plans")
    mod.PLANES = ["basico", "pro"]
    mod.aplicar_plan_calls = []
    mod.aplicar_plan_en_db = lambda db_path, plan: mod.aplicar_plan_calls.append((db_path, plan))
    monkeypatch.setitem(sys.modules, "plans", mod)
    return mod


@pytest.fixture
def fake_docker(monkeypatch):
    """Intercepta subprocess.run (docker build/network/compose) y
    _esperar_db_lista (poll de hasta 25s) para que crear_cliente() no
    dependa de Docker real ni sea lento en la suite."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(nc.subprocess, "run", fake_run)
    monkeypatch.setattr(nc, "_esperar_db_lista", lambda *a, **k: False)
    return calls


@pytest.fixture
def cfg(tmp_path, fake_plans, fake_docker):
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo_root, base_port=9000,
    )
    return provisioning.get_config()


def test_slugify_normaliza_acentos_y_espacios():
    assert nc.slugify("Café Cañón S.A.") == "cafe-canon-s-a"


def test_slugify_vacio_devuelve_cliente():
    assert nc.slugify("   ") == "cliente"


def test_crear_cliente_nombre_vacio_lanza_cliente_error(cfg):
    with pytest.raises(nc.ClienteError):
        nc.crear_cliente(nombre="", setup_npm=False)


def test_crear_cliente_plan_invalido_lanza_cliente_error(cfg):
    with pytest.raises(nc.ClienteError):
        nc.crear_cliente(nombre="Cliente Uno", plan="no-existe", setup_npm=False)


def test_crear_cliente_slug_duplicado_lanza_cliente_error(cfg):
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    with pytest.raises(nc.ClienteError):
        nc.crear_cliente(nombre="Cliente Uno Bis", slug="cliente-uno", setup_npm=False)


def test_crear_cliente_genera_compose_con_datos_del_producto(cfg):
    info = nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno",
                            admin_password="secreto123", setup_npm=False)
    assert info["container"] == "testprod-cliente-uno"
    assert info["port"] == 9000  # base_port del producto, sin puertos usados

    compose_text = (cfg.clientes_dir / "cliente-uno" / "docker-compose.yml").read_text()
    assert "container_name: testprod-cliente-uno" in compose_text
    assert "ADMIN_PASSWORD=secreto123" in compose_text

    meta = json.loads((cfg.clientes_dir / "cliente-uno" / "cliente.json").read_text())
    assert meta["container"] == "testprod-cliente-uno"
    assert meta["plan"] == "basico"


def test_crear_cliente_pinea_version_y_nunca_latest(cfg):
    """El compose de un cliente nuevo nace con una versión concreta: si
    naciera en `:latest`, el próximo build de otro cliente lo movería."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)

    compose_text = (cfg.clientes_dir / "cliente-uno" / "docker-compose.yml").read_text()
    assert "testprod:latest" not in compose_text
    assert re.search(r"^\s*image: testprod:v20\d\d\.\d\d\.\d\d-\d{4}$",
                     compose_text, re.MULTILINE)

    meta = json.loads((cfg.clientes_dir / "cliente-uno" / "cliente.json").read_text())
    assert meta["version_desplegada"].startswith("v20")


def test_crear_cliente_reusa_la_version_ya_construida(cfg, monkeypatch):
    """Un cliente nuevo se suma a la versión que ya corre la familia en vez
    de estrenar un artefacto propio por haberse creado más tarde."""
    monkeypatch.setattr(nc, "version_para_cliente_nuevo", lambda rebuild=False: "v2026.01.02-0304")

    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)

    compose_text = (cfg.clientes_dir / "cliente-uno" / "docker-compose.yml").read_text()
    assert "image: testprod:v2026.01.02-0304" in compose_text


def test_version_para_cliente_nuevo_toma_la_mas_reciente_disponible(cfg, monkeypatch):
    from libracore.provisioning import panel_admin as pa

    monkeypatch.setattr(pa, "versiones_disponibles",
                        lambda: ["v2026.05.05-1200", "v2026.01.01-0900"])
    monkeypatch.setattr(nc, "image_exists", lambda ref=None: True)
    llamadas = []
    monkeypatch.setattr(nc, "build_image", lambda *a, **k: llamadas.append(a) or "nueva")

    assert nc.version_para_cliente_nuevo() == "v2026.05.05-1200"
    assert llamadas == []  # no rebuildeó


def test_version_para_cliente_nuevo_buildea_si_no_hay_ninguna(cfg, monkeypatch):
    from libracore.provisioning import panel_admin as pa

    monkeypatch.setattr(pa, "versiones_disponibles", lambda: [])
    monkeypatch.setattr(nc, "build_image", lambda *a, **k: "v2026.09.09-0101")

    assert nc.version_para_cliente_nuevo() == "v2026.09.09-0101"


def test_version_para_cliente_nuevo_con_rebuild_siempre_construye(cfg, monkeypatch):
    from libracore.provisioning import panel_admin as pa

    monkeypatch.setattr(pa, "versiones_disponibles", lambda: ["v2026.05.05-1200"])
    monkeypatch.setattr(nc, "build_image", lambda *a, **k: "v2026.09.09-0101")

    assert nc.version_para_cliente_nuevo(rebuild=True) == "v2026.09.09-0101"


def test_crear_cliente_aplica_plan_cuando_db_lista(cfg, fake_plans, monkeypatch):
    monkeypatch.setattr(nc, "_esperar_db_lista", lambda *a, **k: True)
    nc.crear_cliente(nombre="Cliente Dos", slug="cliente-dos", setup_npm=False)
    assert fake_plans.aplicar_plan_calls[-1][1] == "basico"
    assert fake_plans.aplicar_plan_calls[-1][0].endswith("testprod.db")


def test_crear_cliente_sin_dominio_no_configura_proxy(cfg):
    info = nc.crear_cliente(nombre="Cliente Tres", slug="cliente-tres", setup_npm=True)
    assert info["proxy_ok"] is None


def test_crear_cliente_con_dominio_y_npm_crea_proxy(cfg, monkeypatch):
    created = {}

    class FakeNPMError(Exception):
        pass

    class FakeNPM:
        def get_proxy_host_by_domain(self, domain):
            return None

        def create_proxy_host(self, **kwargs):
            created.update(kwargs)
            return {"id": 1}

    npm_mod = types.ModuleType("npm_api")
    npm_mod.NPMError = FakeNPMError
    npm_mod.client_from_config = lambda: FakeNPM()
    npm_mod.forward_host_from_config = lambda: "10.0.0.1"
    npm_mod.le_email_from_config = lambda: "admin@test.com"
    monkeypatch.setitem(sys.modules, "npm_api", npm_mod)

    info = nc.crear_cliente(nombre="Cliente Cuatro", slug="cliente-cuatro",
                            domain="cliente-cuatro.test", setup_npm=True)
    assert info["proxy_ok"] is True
    assert created["domain"] == "cliente-cuatro.test"
    assert created["forward_host"] == "10.0.0.1"


def _build_cmd(calls):
    """El `docker build` entre las llamadas capturadas. No es la primera:
    el build ahora va precedido por el `git rev-parse` que resuelve el
    label del commit."""
    for cmd in calls:
        if list(cmd[:2]) == ["docker", "build"]:
            return cmd
    raise AssertionError(f"No hubo `docker build` en {calls}")


def test_build_image_sin_libracommerce_no_pasa_ese_ssh_id(cfg, fake_docker):
    (cfg.repo_root / "requirements.txt").write_text("fastapi\nlibracore @ git+ssh://...\n", encoding="utf-8")
    nc.build_image()

    build_cmd = _build_cmd(fake_docker)
    assert "default=" + provisioning.LIBRACORE_SSH_KEY in build_cmd
    assert "libracore=" + provisioning.LIBRACORE_SSH_KEY in build_cmd
    assert not any(a.startswith("libracommerce=") for a in build_cmd)


def test_build_image_con_libracommerce_agrega_su_ssh_id(cfg, fake_docker):
    (cfg.repo_root / "requirements.txt").write_text(
        "fastapi\nlibracore @ git+ssh://...\nlibracommerce @ git+ssh://...\n", encoding="utf-8"
    )
    nc.build_image()

    build_cmd = _build_cmd(fake_docker)
    assert "libracommerce=" + provisioning.LIBRACOMMERCE_SSH_KEY in build_cmd


def test_build_image_etiqueta_version_y_latest(cfg, fake_docker):
    version = nc.build_image("v2026.03.04-0506")

    build_cmd = _build_cmd(fake_docker)
    assert version == "v2026.03.04-0506"
    assert build_cmd[build_cmd.index("-t") + 1] == "testprod:v2026.03.04-0506"
    assert "testprod:latest" in build_cmd  # el puntero móvil se sigue moviendo
    assert "org.libra.version=v2026.03.04-0506" in build_cmd


def test_build_image_falla_corta_el_alta(cfg, monkeypatch):
    # `nc` importó el helper por nombre, así que el doble va sobre `nc`.
    monkeypatch.setattr(nc, "build_image_tagged", lambda *a, **k: False)
    with pytest.raises(SystemExit):
        nc.build_image()


def test_crear_cliente_proxy_existente_no_falla(cfg, monkeypatch):
    class FakeNPMError(Exception):
        pass

    class FakeNPM:
        def get_proxy_host_by_domain(self, domain):
            return {"id": 99}

    npm_mod = types.ModuleType("npm_api")
    npm_mod.NPMError = FakeNPMError
    npm_mod.client_from_config = lambda: FakeNPM()
    npm_mod.forward_host_from_config = lambda: "10.0.0.1"
    npm_mod.le_email_from_config = lambda: "admin@test.com"
    monkeypatch.setitem(sys.modules, "npm_api", npm_mod)

    info = nc.crear_cliente(nombre="Cliente Cinco", slug="cliente-cinco",
                            domain="cliente-cinco.test", setup_npm=True)
    assert info["proxy_ok"] is True
