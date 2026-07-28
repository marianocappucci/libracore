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


# El comando sugerido por el aviso tiene que ser uno que REALMENTE funcione
# donde se lo lee. En el VPS, github.com plano falla con "Repository not
# found": la deploy key del repo esta declarada como alias en ~/.ssh/config
# (incidente real 2026-07-28, ver wiki/entities/libracore.md v0.26.1).


def _ssh_config(tmp_path, monkeypatch, contenido):
    """Apunta Path.home() a un HOME descartable con el ~/.ssh/config dado."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    if contenido is not None:
        (home / ".ssh" / "config").write_text(contenido, encoding="utf-8")
    monkeypatch.setattr(provisioning.Path, "home", staticmethod(lambda: home))


def test_url_usa_el_alias_ssh_cuando_esta_declarado(tmp_path, monkeypatch):
    _ssh_config(tmp_path, monkeypatch, "Host github-libracore\n    IdentityFile ~/.ssh/k\n")
    url = provisioning.url_instalacion_libracore("0.26.0")
    assert "git@github-libracore/" in url
    assert "git@github.com/" not in url
    assert url.endswith("@v0.26.0")


def test_url_cae_a_github_plano_sin_alias(tmp_path, monkeypatch):
    _ssh_config(tmp_path, monkeypatch, "Host otro-host\n    IdentityFile ~/.ssh/k\n")
    assert "git@github.com/" in provisioning.url_instalacion_libracore("0.26.0")


def test_url_sin_ssh_config_no_rompe(tmp_path, monkeypatch):
    _ssh_config(tmp_path, monkeypatch, None)
    assert "git@github.com/" in provisioning.url_instalacion_libracore("0.26.0")


def test_alias_declarado_junto_a_otros_en_la_misma_linea(tmp_path, monkeypatch):
    # `Host a b c` es sintaxis valida de ssh_config
    _ssh_config(tmp_path, monkeypatch, "Host github-otro github-libracore\n")
    assert "git@github-libracore/" in provisioning.url_instalacion_libracore("0.26.0")


def test_no_confunde_un_alias_que_lo_contiene_como_prefijo(tmp_path, monkeypatch):
    _ssh_config(tmp_path, monkeypatch, "Host github-libracore-viejo\n")
    assert "git@github.com/" in provisioning.url_instalacion_libracore("0.26.0")


def test_el_aviso_sugiere_el_alias_en_un_entorno_con_deploy_key(tmp_path, monkeypatch):
    import libracore
    monkeypatch.setattr(libracore, "__version__", "0.24.0")
    (tmp_path / "requirements.txt").write_text(
        "libracore @ git+ssh://git@github.com/marianocappucci/libracore.git@v0.26.0\n",
        encoding="utf-8",
    )
    _ssh_config(tmp_path, monkeypatch, "Host github-libracore\n")
    aviso = provisioning.check_venv_sync(tmp_path)
    assert aviso is not None
    # el pin del requirements viene con el host plano; el aviso NO debe
    # copiarlo tal cual, porque es justamente el que falla en el VPS
    assert "git@github-libracore/" in aviso
    assert "git@github.com/" not in aviso


def test_requiere_libracommerce_via_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "fastapi\nlibracommerce @ git+ssh://...\n", encoding="utf-8"
    )
    assert provisioning._requiere_libracommerce(tmp_path) is True


def test_requiere_libracommerce_via_pyproject_toml(tmp_path):
    # VentaLibra no tiene requirements.txt -- declara sus dependencias
    # privadas en pyproject.toml (formato PEP 621). Antes de este fix,
    # _requiere_libracommerce solo miraba requirements.txt y nunca
    # detectaba esta dependencia real.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = [\n'
        '    "libracommerce @ git+https://github.com/marianocappucci/libracommerce.git@v0.1.5",\n'
        ']\n',
        encoding="utf-8",
    )
    assert provisioning._requiere_libracommerce(tmp_path) is True


def test_requiere_libracommerce_sin_dependencia_es_false(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["libracore @ git+https://..."]\n', encoding="utf-8"
    )
    assert provisioning._requiere_libracommerce(tmp_path) is False


def test_requiere_libragenda_via_pyproject_toml(tmp_path):
    # Gestiolibra/MedLibra: mismo caso que libracommerce/VentaLibra, pero
    # con libragenda.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = [\n'
        '    "libragenda @ git+https://github.com/marianocappucci/libragenda.git@v0.9.0",\n'
        ']\n',
        encoding="utf-8",
    )
    assert provisioning._requiere_libragenda(tmp_path) is True


def test_requiere_libragenda_sin_pyproject_ni_requirements_es_false(tmp_path):
    assert provisioning._requiere_libragenda(tmp_path) is False


def test_docker_build_ssh_args_agrega_libragenda_si_corresponde(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = [\n'
        '    "libragenda @ git+https://github.com/marianocappucci/libragenda.git@v0.9.0",\n'
        '    "libracore @ git+https://github.com/marianocappucci/libracore.git@v0.18.0",\n'
        ']\n',
        encoding="utf-8",
    )
    args = provisioning.docker_build_ssh_args(tmp_path)
    assert "libragenda=" + provisioning.LIBRAGENDA_SSH_KEY in args
    assert not any(a.startswith("libracommerce=") for a in args)


def test_docker_build_ssh_args_agrega_libracommerce_via_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = [\n'
        '    "libracommerce @ git+https://github.com/marianocappucci/libracommerce.git@v0.1.5",\n'
        '    "libracore @ git+https://github.com/marianocappucci/libracore.git@v0.18.0",\n'
        ']\n',
        encoding="utf-8",
    )
    args = provisioning.docker_build_ssh_args(tmp_path)
    assert "libracommerce=" + provisioning.LIBRACOMMERCE_SSH_KEY in args
    assert not any(a.startswith("libragenda=") for a in args)
