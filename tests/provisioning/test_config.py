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


def test_check_venv_sync_sin_ningun_archivo_no_avisa(tmp_path):
    """Sin requirements.txt NI pyproject.toml no hay pin que leer."""
    assert provisioning.check_venv_sync(tmp_path) is None


def test_check_venv_sync_sin_pin_de_libracore_no_avisa(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    assert provisioning.check_venv_sync(tmp_path) is None


# ── El pin declarado en pyproject.toml (PEP 621) ────────────────────────────
#
# Cuatro de los cinco productos del VPS ya no tienen requirements.txt. Mientras
# `_pin_declarado` leyó sólo ese archivo, `check_venv_sync` devolvía None en
# todos y no avisaba nunca: el 2026-08-04 los cinco `.venv-scripts` estaban en
# 1.5.0, contra pines de 1.2.0 a 1.8.0, y la guarda callada. Ver
# wiki/entities/libracore.md, "Los .venv-scripts al día".

# Recorte fiel del pyproject.toml real de Contalibra: el comentario que nombra
# al motor va JUSTO ARRIBA del pin, y es lo que hace que no alcance con buscar
# la palabra "libracore" en el archivo.
PYPROJECT_REAL = '''\
[project]
name = "contalibra"
dependencies = [
    # libracore provee facturacion/caja/ARCA y la base donde vive `usuarios`.
    # El auth SALIO de aca el 2026-07-30 y ahora lo da libraauth.
    "libracore @ git+https://github.com/marianocappucci/libracore.git@v1.8.0",
    "libraauth @ git+https://github.com/marianocappucci/libraauth.git@v0.7.0",
    "fastapi",
]
'''


def test_pin_se_lee_de_pyproject_cuando_no_hay_requirements(tmp_path):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_REAL, encoding="utf-8")
    assert provisioning._pin_declarado(tmp_path) == ("1.8.0", "pyproject.toml")


def test_check_venv_sync_avisa_con_pin_solo_en_pyproject(tmp_path, monkeypatch):
    """El caso que estuvo roto: producto sin requirements.txt y venv atrasado."""
    import libracore
    monkeypatch.setattr(libracore, "__version__", "1.5.0")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_REAL, encoding="utf-8")

    aviso = provisioning.check_venv_sync(tmp_path)
    assert aviso is not None, "sin requirements.txt la guarda volvio a callarse"
    assert "1.5.0" in aviso
    assert "1.8.0" in aviso


def test_check_venv_sync_no_avisa_con_pyproject_al_dia(tmp_path, monkeypatch):
    import libracore
    monkeypatch.setattr(libracore, "__version__", "1.8.0")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_REAL, encoding="utf-8")
    assert provisioning.check_venv_sync(tmp_path) is None


def test_un_pyproject_que_solo_NOMBRA_libracore_no_cuenta_como_pin(tmp_path):
    """Un comentario, o una mención en la descripción, no es una dependencia.
    Sin esto la guarda inventaría un pin y avisaría de un desfasaje falso."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        'description = "Producto que consume libracore v9.9.9 como motor"\n'
        'dependencies = ["fastapi"]\n',
        encoding="utf-8",
    )
    assert provisioning._pin_declarado(tmp_path) is None
    assert provisioning.check_venv_sync(tmp_path) is None


def test_con_los_dos_archivos_manda_pyproject(tmp_path):
    """El caso REAL de Restolibra, y la razón por la que el orden importa.

    Tiene los dos archivos declarando libracore con pines **distintos**:
    `pyproject.toml` en v1.4.0 y un `requirements.txt` en v1.2.0 que ya no
    consume nadie. Su Dockerfile hace `pip install .`, así que lo que entra
    a la imagen es el de `pyproject` — y su contenedor corre 1.4.0. Con la
    prioridad al revés, la guarda comparaba contra el pin muerto: probado el
    2026-08-04, devolvía 1.2.0 para un producto que corre 1.4.0.
    """
    (tmp_path / "requirements.txt").write_text(
        "# libracore sigue proveyendo facturacion/caja/ARCA -- y la base\n"
        "libracore @ git+ssh://git@github.com/marianocappucci/libracore.git@v1.2.0\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_REAL, encoding="utf-8")
    assert provisioning._pin_declarado(tmp_path) == ("1.8.0", "pyproject.toml")


def test_sin_pyproject_cae_a_requirements(tmp_path):
    """El formato clásico sigue soportado para cualquier repo que no haya
    migrado a PEP 621."""
    (tmp_path / "requirements.txt").write_text(
        "libracore @ git+ssh://git@github.com/marianocappucci/libracore.git@v1.4.0\n",
        encoding="utf-8",
    )
    assert provisioning._pin_declarado(tmp_path) == ("1.4.0", "requirements.txt")


def test_el_aviso_nombra_el_archivo_que_hay_que_editar(tmp_path, monkeypatch):
    """Mandar a corregir `requirements.txt` a un producto que ya no lo tiene
    es mandar a editar un archivo inexistente."""
    import libracore
    monkeypatch.setattr(libracore, "__version__", "1.5.0")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_REAL, encoding="utf-8")

    aviso = provisioning.check_venv_sync(tmp_path)
    assert "pyproject.toml de este producto pinea" in aviso
    assert "requirements.txt" not in aviso


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


def test_requiere_libraauth_sin_pyproject_ni_requirements_es_false(tmp_path):
    assert provisioning._requiere_libraauth(tmp_path) is False


def test_docker_build_ssh_args_agrega_libraauth_via_requirements(tmp_path):
    """El caso real de los 5 productos desde la migracion de auth del
    2026-07-30: `libraauth` en requirements.txt y el Dockerfile con un
    `--mount=type=ssh,id=libraauth`. Sin este `--ssh`, el paso de pip de
    libraauth falla y `nuevo_cliente.build_image` no puede dar de alta una
    instancia nueva."""
    (tmp_path / "requirements.txt").write_text(
        "libracore @ git+ssh://git@github.com/marianocappucci/libracore.git@v1.0.0\n"
        "libraauth @ git+ssh://git@github.com/marianocappucci/libraauth.git@v0.4.0\n"
        "fastapi>=0.111.0\n",
        encoding="utf-8",
    )
    args = provisioning.docker_build_ssh_args(tmp_path)
    assert "libraauth=" + provisioning.LIBRAAUTH_SSH_KEY in args
    assert not any(a.startswith("libragenda=") for a in args)


def test_docker_build_ssh_args_sin_libraauth_no_lo_agrega(tmp_path):
    """Un producto que no lo declara no recibe el `--ssh`: pasar uno con una
    key que no existe en la maquina rompe el build."""
    (tmp_path / "requirements.txt").write_text(
        "libracore @ git+ssh://git@github.com/marianocappucci/libracore.git@v1.0.0\n",
        encoding="utf-8",
    )
    args = provisioning.docker_build_ssh_args(tmp_path)
    assert not any(a.startswith("libraauth=") for a in args)


# ── versionado de imagen ──────────────────────────────────────────────────────

def test_deploy_version_usa_el_esquema_de_farmacia():
    from datetime import datetime
    assert provisioning.deploy_version(datetime(2026, 7, 30, 21, 10, 5)) == "v2026.07.30-211005"


def test_deploy_version_sin_argumento_es_del_momento():
    v = provisioning.deploy_version()
    assert v.startswith("v20") and len(v) == len("v2026.07.30-211005")


def test_image_repo_saca_el_tag(tmp_path):
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=tmp_path,
    )
    cfg = provisioning.get_config()
    assert cfg.image_repo == "testprod"
    assert cfg.image_ref("v2026.07.30-2110") == "testprod:v2026.07.30-2110"


def test_image_repo_sin_tag_declarado(tmp_path):
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=tmp_path,
    )
    assert provisioning.get_config().image_repo == "testprod"


def test_image_repo_no_confunde_el_puerto_de_un_registry_con_un_tag(tmp_path):
    """`registry:5000/testprod` no tiene tag: el `:` es del puerto. Cortar
    ahi dejaria la imagen apuntando a `registry`."""
    provisioning.configure(
        product_name="TESTPROD", image_name="registry:5000/testprod",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=tmp_path,
    )
    cfg = provisioning.get_config()
    assert cfg.image_repo == "registry:5000/testprod"
    assert cfg.image_ref("v1") == "registry:5000/testprod:v1"


def _contexto_fijo(monkeypatch, contexto, commit="abc1234"):
    """Reemplaza `contexto_de_build` por uno fijo.

    Estos tests miran el COMANDO DE DOCKER; de qué ref sale el contexto lo
    cubre `test_contexto_de_build.py` contra un repo git real. Acá hace falta
    porque `subprocess.run` está mockeado entero y git devolvería el doble.
    """
    from contextlib import contextmanager

    @contextmanager
    def _falso(repo_root, ref="main", *, from_checkout=False, log=print):
        yield contexto, commit, f"{ref} (contexto de prueba)"

    monkeypatch.setattr(provisioning, "contexto_de_build", _falso)


def test_build_image_tagged_arma_el_comando_con_los_dos_tags_y_labels(tmp_path, monkeypatch):
    import subprocess as sp
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=tmp_path,
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:2] == ["git", "-C"]:
            return sp.CompletedProcess(cmd, 0, stdout="abc1234\n")
        return sp.CompletedProcess(cmd, 0)

    monkeypatch.setattr(provisioning.subprocess, "run", fake_run)
    _contexto_fijo(monkeypatch, tmp_path)

    assert provisioning.build_image_tagged("v2026.07.30-2110", log=lambda *a: None) is True

    cmd, kwargs = next(c for c in calls if c[0][:2] == ["docker", "build"])
    assert "-t" in cmd and "testprod:v2026.07.30-2110" in cmd
    assert "testprod:latest" in cmd
    assert "org.libra.version=v2026.07.30-2110" in cmd
    assert "org.libra.commit=abc1234" in cmd
    # El contexto ahora es el que rinde `contexto_de_build` (un worktree del
    # ref), no el "." de siempre. `cwd` sigue siendo el repo: de ahí salen las
    # deploy keys que arma `docker_build_ssh_args`.
    assert cmd[-1] == str(tmp_path)
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"]["DOCKER_BUILDKIT"] == "1"


def test_build_image_tagged_aborta_si_el_repo_no_es_git(tmp_path, monkeypatch):
    """🔴 Reemplaza a `..._sin_git_omite_el_label_de_commit`, cuya premisa murió.

    Antes, sin git, se construía igual y sólo se omitía el label del commit.
    Desde que el contexto sale del worktree de un ref, un directorio que no es
    repo no se puede resolver — y **abortar es lo correcto**: seguir sería
    construir el checkout, que es exactamente lo que el guard viene a impedir.
    """
    import subprocess as sp
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=tmp_path,
    )
    monkeypatch.setattr(provisioning.subprocess, "run",
                        lambda cmd, **k: sp.CompletedProcess(cmd, 128, stdout=""))
    with pytest.raises(RuntimeError) as e:
        provisioning.build_image_tagged("v1", log=lambda *a: None)
    assert "no es un repo git" in str(e.value)


def test_build_image_tagged_devuelve_false_si_el_build_falla(tmp_path, monkeypatch):
    import subprocess as sp
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=tmp_path,
    )
    monkeypatch.setattr(provisioning.subprocess, "run",
                        lambda cmd, **k: sp.CompletedProcess(cmd, 1, stdout=""))
    _contexto_fijo(monkeypatch, tmp_path)
    assert provisioning.build_image_tagged("v1", log=lambda *a: None) is False
