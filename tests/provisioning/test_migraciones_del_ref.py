"""`actualizar` corre las migraciones del commit que construye, no las del checkout.

🔴 **El defecto (2026-09-16).** El panel corre desde el checkout del VPS, que está
en `develop`, y construye la imagen desde un clon limpio de `main`. Las
migraciones salían de `get_config()` —o sea del checkout— y se corrían sobre la
imagen de `main`. En LibraDesk, el deploy a producción de un hotfix corrió
`libraauth-migrar` en las tres instancias antes de que esa adopción se
promoviera.

Lo que se fija acá:

1. que corran las del **ref**, aunque el checkout declare otras (de más o de
   menos), y que se avise;
2. que se lean **del mismo árbol** que se construye (el callback del build);
3. que si no se pueden leer, **no se construye ni se despliega** —fallar
   cerrado, porque caer a las del checkout es el defecto—;
4. y cómo se leen: literal, sin ejecutar el script.
"""
import json
import subprocess

import pytest

from libracore import provisioning
from libracore.provisioning import panel_admin as pa

from ._dobles import build_falso, escribir_panel

ADOPCION = ("libraauth-migrar", "upgrade", "--prefijo", "testprod", "--base", "dominio")
CORE = ("libracore-migrar", "upgrade", "--prefijo", "testprod")
ALEMBIC = ("alembic", "upgrade", "head")


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


@pytest.fixture(autouse=True)
def _contexto_falso(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _falso(repo_root, ref="main", *, from_checkout=False, log=print):
        yield repo_root, "abc1234", f"{ref} (prueba)"

    monkeypatch.setattr(provisioning, "contexto_de_build", _falso)
    monkeypatch.setattr(pa, "contexto_de_build", _falso)


def _armar(tmp_path, monkeypatch, *, del_checkout, del_ref, arbol_del_ref=None):
    """El checkout declara `del_checkout` (lo que ve `get_config()`); el árbol que
    se construye declara `del_ref`. Son dos directorios distintos, como en el
    VPS: el checkout en `develop` y el clon limpio de `main`."""
    checkout = tmp_path / "checkout"
    (checkout / "clientes" / "demo" / "data").mkdir(parents=True)
    (checkout / "clientes" / "demo" / "cliente.json").write_text(json.dumps(
        {"nombre": "Demo", "slug": "demo", "port": 9000, "container": "testprod-demo"}))
    (checkout / "clientes" / "demo" / "docker-compose.yml").write_text(
        "services:\n  testprod-demo:\n    image: testprod:v1\n"
        "    container_name: testprod-demo\n")
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=checkout, base_port=9000, migraciones=del_checkout,
    )
    arbol = arbol_del_ref or (tmp_path / "clon-de-main")
    if del_ref is not None:
        escribir_panel(arbol, del_ref)

    eventos: list = []

    def _build(version, *a, al_materializar=None, **k):
        eventos.append("build-llamado")
        if al_materializar is not None:
            al_materializar(arbol, "def5678")
        eventos.append("docker-build")
        return True

    def _compose(slug, *args):
        eventos.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(pa, "build_image_tagged", _build)
    monkeypatch.setattr(pa, "container_status", lambda c: {"status": "running"})
    monkeypatch.setattr(pa, "compose", _compose)
    monkeypatch.setattr(pa, "pinear_image", lambda slug, ref: "testprod:v1")
    monkeypatch.setattr(pa, "podar_imagenes_viejas", lambda *a, **k: ([], []))
    monkeypatch.setattr(pa, "_guardar_meta", lambda *a, **k: None)
    monkeypatch.setattr(pa, "check_venv_sync", lambda *a, **k: None)
    return eventos


def _corridas(eventos):
    return [e[3:] for e in eventos if isinstance(e, tuple) and e[:2] == ("run", "--rm")]


# ── 1. Corren las del ref ────────────────────────────────────────────────────


def test_una_migracion_que_solo_esta_en_el_checkout_NO_corre(tmp_path, monkeypatch, capsys):
    """🔴 El caso de LibraDesk: `develop` adoptó una cadena, `main` todavía no."""
    eventos = _armar(tmp_path, monkeypatch,
                     del_checkout=(CORE, ADOPCION, ALEMBIC), del_ref=(CORE, ALEMBIC))
    assert pa.cmd_actualizar(["demo"]) is True
    assert _corridas(eventos) == [CORE, ALEMBIC]
    salida = capsys.readouterr().out
    assert "[AVISO]" in salida and "NO se corre: libraauth-migrar" in salida


def test_una_migracion_que_solo_esta_en_el_ref_SI_corre(tmp_path, monkeypatch, capsys):
    """El otro lado: un checkout atrasado no puede dejar sin correr lo que la
    imagen necesita."""
    eventos = _armar(tmp_path, monkeypatch,
                     del_checkout=(CORE,), del_ref=(CORE, ADOPCION))
    assert pa.cmd_actualizar(["demo"]) is True
    assert _corridas(eventos) == [CORE, ADOPCION]
    assert "sólo en el commit, se corre" in capsys.readouterr().out


def test_manda_el_orden_del_ref(tmp_path, monkeypatch):
    eventos = _armar(tmp_path, monkeypatch,
                     del_checkout=(ALEMBIC, CORE), del_ref=(CORE, ALEMBIC))
    assert pa.cmd_actualizar(["demo"]) is True
    assert _corridas(eventos) == [CORE, ALEMBIC]


def test_control_iguales_no_avisa(tmp_path, monkeypatch, capsys):
    """Control: con las dos listas iguales corre lo mismo que antes y no hay aviso."""
    eventos = _armar(tmp_path, monkeypatch,
                     del_checkout=(CORE, ALEMBIC), del_ref=(CORE, ALEMBIC))
    assert pa.cmd_actualizar(["demo"]) is True
    assert _corridas(eventos) == [CORE, ALEMBIC]
    assert "[AVISO] Las migraciones" not in capsys.readouterr().out


def test_el_ref_sin_migraciones_no_corre_ninguna(tmp_path, monkeypatch):
    eventos = _armar(tmp_path, monkeypatch, del_checkout=(ADOPCION,), del_ref=())
    assert pa.cmd_actualizar(["demo"]) is True
    assert _corridas(eventos) == []
    assert ("up", "-d") in eventos


# ── 2 y 3. Del mismo árbol, y si no se puede, nada ───────────────────────────


def test_se_leen_antes_del_docker_build(tmp_path, monkeypatch):
    """Si no se pueden leer, que no se gaste un build: la lectura va primero."""
    orden = []
    monkeypatch.setattr(pa, "migraciones_declaradas",
                        lambda ctx: orden.append("leer") or ())
    eventos = _armar(tmp_path, monkeypatch, del_checkout=(), del_ref=())

    pa.cmd_actualizar(["demo"])
    assert eventos.index("docker-build") > eventos.index("build-llamado")
    assert orden == ["leer"]


def test_sin_script_del_panel_en_el_ref_no_despliega_nada(tmp_path, monkeypatch, capsys):
    eventos = _armar(tmp_path, monkeypatch, del_checkout=(CORE,), del_ref=None)
    assert pa.cmd_actualizar(["demo"]) is False
    assert "docker-build" not in eventos, "no se construyó"
    assert _corridas(eventos) == [] and ("up", "-d") not in eventos
    assert "No se construyó ni se desplegó nada" in capsys.readouterr().out


def test_un_build_que_no_informa_las_migraciones_falla_cerrado(tmp_path, monkeypatch, capsys):
    """🔴 Sin la lista del ref, usar la del checkout es exactamente el defecto."""
    eventos = _armar(tmp_path, monkeypatch, del_checkout=(CORE,), del_ref=(CORE,))
    monkeypatch.setattr(pa, "build_image_tagged", lambda *a, **k: True)
    assert pa.cmd_actualizar(["demo"]) is False
    assert _corridas(eventos) == [] and ("up", "-d") not in eventos
    assert "no se despliega con las del checkout" in capsys.readouterr().out


def test_dry_run_muestra_las_del_ref(tmp_path, monkeypatch, capsys):
    arbol = tmp_path / "checkout"
    _armar(tmp_path, monkeypatch, del_checkout=(CORE, ADOPCION), del_ref=None)
    escribir_panel(arbol, (CORE,))  # el contexto falso rinde el checkout
    assert pa.cmd_actualizar(["demo"], dry_run=True) is True
    salida = capsys.readouterr().out
    assert "migraciones del ref: libracore-migrar upgrade --prefijo testprod" in salida
    assert "NO se corre: libraauth-migrar" in salida


def test_build_image_tagged_llama_al_callback_antes_del_docker_build(tmp_path, monkeypatch):
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db", repo_root=tmp_path)
    orden = []
    monkeypatch.setattr(provisioning.subprocess, "run",
                        lambda cmd, **k: orden.append(cmd[0]) or subprocess.CompletedProcess(cmd, 0))
    assert provisioning.build_image_tagged(
        "v1", log=lambda *a: None,
        al_materializar=lambda ctx, commit: orden.append(("callback", ctx, commit))) is True
    assert orden[0] == ("callback", tmp_path, "abc1234")
    assert "docker" in orden[1:]


def test_una_excepcion_en_el_callback_corta_sin_construir(tmp_path, monkeypatch):
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db", repo_root=tmp_path)
    corridos = []
    monkeypatch.setattr(provisioning.subprocess, "run",
                        lambda cmd, **k: corridos.append(cmd) or subprocess.CompletedProcess(cmd, 0))

    def _revienta(ctx, commit):
        raise provisioning.MigracionesIlegibles("x")

    with pytest.raises(provisioning.MigracionesIlegibles):
        provisioning.build_image_tagged("v1", log=lambda *a: None, al_materializar=_revienta)
    assert corridos == []


# ── 4. Cómo se leen ──────────────────────────────────────────────────────────


def _panel(tmp_path, fuente):
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "panel_admin.py").write_text(fuente, encoding="utf-8")
    return tmp_path


def test_lee_la_forma_real_de_un_producto(tmp_path):
    """La forma de los ocho: `configure(...)` con comentarios adentro de la tupla."""
    _panel(tmp_path, """
from pathlib import Path
from libracore.provisioning import configure
configure(
    product_name="LibraCargo",
    repo_root=Path(__file__).resolve().parent.parent,
    migraciones=(
        ("libracore-migrar", "upgrade", "--prefijo", "libracargo"),
        # libraauth: sus seis tablas viven en la base del dominio
        ("libraauth-migrar", "upgrade", "--prefijo", "libracargo", "--base", "dominio"),
        ("alembic", "upgrade", "head"),
    ),
)
""")
    assert provisioning.migraciones_declaradas(tmp_path) == (
        ("libracore-migrar", "upgrade", "--prefijo", "libracargo"),
        ("libraauth-migrar", "upgrade", "--prefijo", "libracargo", "--base", "dominio"),
        ("alembic", "upgrade", "head"),
    )


def test_sin_la_keyword_son_ninguna(tmp_path):
    _panel(tmp_path, "from libracore.provisioning import configure\nconfigure(product_name='X')\n")
    assert provisioning.migraciones_declaradas(tmp_path) == ()


def test_llamada_calificada_tambien_cuenta(tmp_path):
    _panel(tmp_path, "import libracore.provisioning as p\np.configure(migraciones=(('a', 'b'),))\n")
    assert provisioning.migraciones_declaradas(tmp_path) == (("a", "b"),)


def test_no_ejecuta_el_script(tmp_path):
    """Importarlo pisaría la configuración global y correría código de otra rama."""
    marca = tmp_path / "ejecutado"
    _panel(tmp_path, f"open({str(marca)!r}, 'w').write('x')\nconfigure(migraciones=())\n")
    provisioning.migraciones_declaradas(tmp_path)
    assert not marca.exists()


@pytest.mark.parametrize("fuente,motivo", [
    ("MIGR = (('a',),)\nconfigure(migraciones=MIGR)\n", "no es un literal"),
    ("configure(migraciones=())\nconfigure(migraciones=())\n", "2 llamadas"),
    ("x = 1\n", "0 llamadas"),
    ("configure(migraciones=('alembic', 'upgrade', 'head'))\n", "secuencia de COMANDOS"),
    ("configure(migraciones=(\n", "no se puede parsear"),
])
def test_lo_que_no_se_puede_leer_falla(tmp_path, fuente, motivo):
    _panel(tmp_path, fuente)
    with pytest.raises(provisioning.MigracionesIlegibles, match=motivo):
        provisioning.migraciones_declaradas(tmp_path)


def test_sin_archivo_falla(tmp_path):
    with pytest.raises(provisioning.MigracionesIlegibles, match="No existe"):
        provisioning.migraciones_declaradas(tmp_path)
