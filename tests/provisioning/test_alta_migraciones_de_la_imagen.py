"""El alta corre las migraciones del commit de la imagen, no las del checkout.

🔴 Gemelo de `test_migraciones_del_ref.py` (2026-09-16). `crear_cliente` corría
`get_config().migraciones` —las del `scripts/nuevo_cliente.py` del checkout del
VPS, en `develop`— sobre una imagen de `main`. Y el alta **no siempre construye**:
`version_para_cliente_nuevo` reusa la última imagen. Lo que ata las migraciones
al código que va a correr es la imagen, que dice de qué commit salió
(`org.libra.commit`).

Se fija:

1. que el alta corra las de la imagen aunque el checkout declare otras, y avise;
2. que si no se pueden saber **no se cree la instancia** (y haya rollback);
3. cómo se resuelven: label → commit → `git show` → parseo literal, con un
   `fetch` si el commit no está, y falla cerrado en cada paso.
"""
import subprocess

import pytest

from libracore import provisioning
from libracore.provisioning import nuevo_cliente as nc

# `fake_plans` es el fixture de la suite (se importa para que pytest lo vea).
from .test_nuevo_cliente import CUIT, fake_plans  # noqa: F401

CORE = ("libracore-migrar", "upgrade", "--prefijo", "testprod")
ADOPCION = ("libraauth-migrar", "upgrade", "--prefijo", "testprod", "--base", "dominio")
ALEMBIC = ("alembic", "upgrade", "head")


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


@pytest.fixture
def alta(tmp_path, monkeypatch, fake_plans):  # noqa: F811 - el fixture importado
    """Un producto que declara (CORE, ADOPCION, ALEMBIC) en el checkout, con
    docker/compose bajo control. Devuelve las llamadas a `subprocess.run`."""
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo_root, base_port=9000, migraciones=(CORE, ADOPCION, ALEMBIC),
    )
    calls = []
    monkeypatch.setattr(nc.subprocess, "run",
                        lambda args, **k: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(nc, "_esperar_db_lista", lambda *a, **k: True)
    monkeypatch.setattr(nc, "_esperar_tabla_en_sidecar", lambda *a, **k: True)
    monkeypatch.setattr(nc, "_aplicar_plan_en_contenedor", lambda *a, **k: True)
    monkeypatch.setattr(nc, "version_para_cliente_nuevo", lambda rebuild=False: "v2026.09.16-1200")
    return calls


def _corridas(calls):
    return [tuple(c[5:]) for c in calls
            if list(c[:5])[:4] == ["docker", "compose", "run", "--rm"]]


# ── 1. Corren las de la imagen ───────────────────────────────────────────────


def test_el_alta_corre_las_de_la_imagen_y_no_las_del_checkout(alta, monkeypatch):
    """🔴 El caso: `develop` adoptó una cadena que la imagen de `main` no trae."""
    monkeypatch.setattr(nc, "migraciones_de_la_imagen",
                        lambda repo_root, image_ref, script=None: ((CORE, ALEMBIC), "def5678"))
    logs = []
    nc.crear_cliente(empresa_cuit=CUIT, nombre="Demo", slug="demo", setup_npm=False,
                     log=logs.append)
    assert _corridas(alta) == [CORE, ALEMBIC]
    texto = "\n".join(logs)
    assert "[AVISO]" in texto and "NO se corre: libraauth-migrar" in texto


def test_pregunta_por_la_imagen_que_se_pinea(alta, monkeypatch):
    pedidas = []
    monkeypatch.setattr(nc, "migraciones_de_la_imagen",
                        lambda repo_root, image_ref, script=None: pedidas.append(image_ref) or ((), "x"))
    nc.crear_cliente(empresa_cuit=CUIT, nombre="Demo", slug="demo", setup_npm=False)
    assert pedidas == ["testprod:v2026.09.16-1200"]


def test_control_iguales_no_avisa(alta, monkeypatch):
    monkeypatch.setattr(nc, "migraciones_de_la_imagen",
                        lambda repo_root, image_ref, script=None: ((CORE, ADOPCION, ALEMBIC), "def5678"))
    logs = []
    nc.crear_cliente(empresa_cuit=CUIT, nombre="Demo", slug="demo", setup_npm=False,
                     log=logs.append)
    assert _corridas(alta) == [CORE, ADOPCION, ALEMBIC]
    assert not any("[AVISO] Las migraciones" in l for l in logs)


# ── 2. Si no se pueden saber, no hay instancia ───────────────────────────────


def test_migraciones_ilegibles_no_crean_la_instancia(alta, monkeypatch):
    def _revienta(repo_root, image_ref, script=None):
        raise provisioning.MigracionesIlegibles("la imagen no dice de qué commit salió")
    monkeypatch.setattr(nc, "migraciones_de_la_imagen", _revienta)

    with pytest.raises(nc.ClienteError, match="No se creó la instancia"):
        nc.crear_cliente(empresa_cuit=CUIT, nombre="Demo", slug="demo", setup_npm=False)
    assert _corridas(alta) == []
    assert not any(list(c[:3]) == ["docker", "compose", "up"] for c in alta)
    assert not (provisioning.get_config().clientes_dir / "demo").exists(), "hubo rollback"


# ── 3. Cómo se resuelven ─────────────────────────────────────────────────────


FUENTE = (
    "from libracore.provisioning import configure\n"
    "configure(product_name='X', migraciones=(\n"
    "    ('libracore-migrar', 'upgrade', '--prefijo', 'x'),\n"
    "    # un comentario adentro, como en los productos\n"
    "    ('alembic', 'upgrade', 'head'),\n"
    "))\n"
)


def _subprocess_falso(monkeypatch, *, label="abc1234", existe=(True,), show_ok=True,
                      fuente=FUENTE):
    """`existe` es la secuencia de respuestas de `git cat-file -e` (la segunda,
    después del fetch)."""
    llamadas = []
    respuestas = list(existe)

    def _run(args, **k):
        llamadas.append(list(args))
        if args[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, stdout=label + "\n", stderr="")
        if "cat-file" in args:
            ok = respuestas.pop(0) if respuestas else False
            return subprocess.CompletedProcess(args, 0 if ok else 1)
        if "fetch" in args:
            return subprocess.CompletedProcess(args, 0)
        if "show" in args:
            return subprocess.CompletedProcess(args, 0 if show_ok else 128,
                                               stdout=fuente if show_ok else "", stderr="")
        raise AssertionError(f"llamada inesperada: {args}")

    monkeypatch.setattr(provisioning.subprocess, "run", _run)
    return llamadas


def test_resuelve_label_commit_y_script(tmp_path, monkeypatch):
    llamadas = _subprocess_falso(monkeypatch, label="abc1234")
    migraciones, commit = provisioning.migraciones_de_la_imagen(tmp_path, "testprod:v1")
    assert commit == "abc1234"
    assert migraciones == (("libracore-migrar", "upgrade", "--prefijo", "x"),
                           ("alembic", "upgrade", "head"))
    show = next(c for c in llamadas if "show" in c)
    assert show[-1] == "abc1234:scripts/nuevo_cliente.py"


@pytest.mark.parametrize("label", ["", "<no value>"])
def test_sin_label_falla(tmp_path, monkeypatch, label):
    _subprocess_falso(monkeypatch, label=label)
    with pytest.raises(provisioning.MigracionesIlegibles, match="org.libra.commit"):
        provisioning.migraciones_de_la_imagen(tmp_path, "testprod:v1")


def test_commit_ausente_hace_fetch_y_sigue(tmp_path, monkeypatch):
    llamadas = _subprocess_falso(monkeypatch, existe=(False, True))
    migraciones, _ = provisioning.migraciones_de_la_imagen(tmp_path, "testprod:v1")
    assert any("fetch" in c for c in llamadas)
    assert len(migraciones) == 2


def test_commit_ausente_despues_del_fetch_falla(tmp_path, monkeypatch):
    _subprocess_falso(monkeypatch, existe=(False, False))
    with pytest.raises(provisioning.MigracionesIlegibles, match="ni después de un fetch"):
        provisioning.migraciones_de_la_imagen(tmp_path, "testprod:v1")


def test_script_ausente_en_el_commit_falla(tmp_path, monkeypatch):
    _subprocess_falso(monkeypatch, show_ok=False)
    with pytest.raises(provisioning.MigracionesIlegibles, match="No existe"):
        provisioning.migraciones_de_la_imagen(tmp_path, "testprod:v1")


def test_fuente_no_literal_falla(tmp_path, monkeypatch):
    _subprocess_falso(monkeypatch, fuente="M = ()\nconfigure(migraciones=M)\n")
    with pytest.raises(provisioning.MigracionesIlegibles, match="no es un literal"):
        provisioning.migraciones_de_la_imagen(tmp_path, "testprod:v1")


def test_contra_un_repo_git_real(tmp_path, monkeypatch):
    """Sin doble de git: un commit real con el script, y sólo docker falseado.
    Después se cambia el script en el working tree —como el checkout en
    `develop`— y tiene que seguir leyendo el del commit."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "nuevo_cliente.py").write_text(FUENTE, encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
    commit = subprocess.run([*git, "rev-parse", "--short=7", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    (repo / "scripts" / "nuevo_cliente.py").write_text(
        "configure(migraciones=(('libraauth-migrar', 'upgrade'),))\n", encoding="utf-8")

    real = subprocess.run

    def _run(args, **k):
        if list(args[:3]) == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, stdout=commit + "\n", stderr="")
        return real(args, **k)

    monkeypatch.setattr(provisioning.subprocess, "run", _run)
    migraciones, c = provisioning.migraciones_de_la_imagen(repo, "testprod:v1")
    assert c == commit
    assert migraciones == (("libracore-migrar", "upgrade", "--prefijo", "x"),
                           ("alembic", "upgrade", "head"))
