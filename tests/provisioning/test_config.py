"""
Tests de libracore.provisioning (configure()/get_config()/ProductConfig y
los resolvers diferidos de plans/npm_api).
"""
import sys
import types

import pytest

from libracore import provisioning


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


def test_get_config_sin_configurar_lanza_runtime_error():
    with pytest.raises(RuntimeError):
        provisioning.get_config()


def test_configure_expone_clientes_dir(tmp_path):
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=tmp_path,
    )
    cfg = provisioning.get_config()
    assert cfg.product_name == "TESTPROD"
    assert cfg.clientes_dir == tmp_path / "clientes"
    assert cfg.base_port == 8071  # default


def test_configure_inserta_repo_root_y_scripts_en_sys_path(tmp_path):
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=tmp_path,
    )
    assert str(tmp_path) in sys.path
    assert str(tmp_path / "scripts") in sys.path


def test_npm_available_false_sin_npm_api(tmp_path):
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=tmp_path,
    )
    sys.modules.pop("npm_api", None)
    assert provisioning.npm_available() is False
    assert provisioning.client_from_config() is None


def test_npm_available_true_delega_al_modulo_inyectado(tmp_path, monkeypatch):
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=tmp_path,
    )
    npm_mod = types.ModuleType("npm_api")
    npm_mod.client_from_config = lambda: "cliente-npm"
    npm_mod.forward_host_from_config = lambda: "10.0.0.1"
    npm_mod.le_email_from_config = lambda: "admin@test.com"
    monkeypatch.setitem(sys.modules, "npm_api", npm_mod)

    assert provisioning.npm_available() is True
    assert provisioning.client_from_config() == "cliente-npm"
    assert provisioning.forward_host_from_config() == "10.0.0.1"
    assert provisioning.le_email_from_config() == "admin@test.com"


def test_plans_requiere_configuracion_previa():
    with pytest.raises(RuntimeError):
        provisioning._plans()


def test_check_venv_sync_sin_requirements_txt_no_avisa(tmp_path):
    assert provisioning.check_venv_sync(tmp_path) is None


def test_check_venv_sync_sin_pin_de_libracore_no_avisa(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    assert provisioning.check_venv_sync(tmp_path) is None


def test_check_venv_sync_version_coincide_no_avisa(tmp_path, monkeypatch):
    import libracore
    monkeypatch.setattr(libracore, "__version__", "0.23.0")
    (tmp_path / "requirements.txt").write_text(
        "libracore @ git+ssh://git@github.com/marianocappucci/libracore.git@v0.23.0\n",
        encoding="utf-8",
    )
    assert provisioning.check_venv_sync(tmp_path) is None


def test_check_venv_sync_version_distinta_avisa(tmp_path, monkeypatch):
    import libracore
    monkeypatch.setattr(libracore, "__version__", "0.15.0")
    (tmp_path / "requirements.txt").write_text(
        "libracore @ git+ssh://git@github.com/marianocappucci/libracore.git@v0.23.0\n",
        encoding="utf-8",
    )
    aviso = provisioning.check_venv_sync(tmp_path)
    assert aviso is not None
    assert "0.15.0" in aviso
    assert "0.23.0" in aviso
    assert "pip install --upgrade" in aviso
