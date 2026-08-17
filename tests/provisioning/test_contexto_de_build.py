"""
De qué código se construye la imagen de un cliente.

Hasta acá `build_image_tagged` construía el **checkout**, y eso ataba el deploy
del cliente a una variable global compartida: el mismo directorio alimenta el
build de `<producto>-dev`, así que la rama que necesitaba dev decidía de rebote
qué se le desplegaba al cliente. Le pasó a LibraDesk el 2026-08-03 y de ahí
salió el guard que estos tests fijan.

Se usa un repo git **de verdad** (temporal) en vez de mockear git: lo que se
está probando es justamente la resolución de refs y el worktree, o sea el
comportamiento de git. Con un doble, el test pasaría aunque el comando estuviera
mal escrito.
"""
import subprocess
from pathlib import Path

import pytest

from libracore.provisioning import contexto_de_build


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True, **kw)


@pytest.fixture
def repo(tmp_path):
    """Repo con dos ramas y contenido distinto en cada una.

    `main` tiene `marca.txt` con "main"; `develop` lo tiene con "develop". Eso
    es lo que permite afirmar de QUÉ rama salió el contexto mirando el archivo,
    en vez de creerle al nombre.
    """
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "test@test")
    git(r, "config", "user.name", "test")
    (r / "marca.txt").write_text("main\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "main")

    git(r, "checkout", "-q", "-b", "develop")
    (r / "marca.txt").write_text("develop\n")
    (r / "solo-en-develop.txt").write_text("x\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "develop")

    # El checkout queda EN DEVELOP a propósito: es el estado peligroso real,
    # el que hizo falta el guard.
    return r


def test_construye_main_aunque_el_checkout_este_en_develop(repo):
    """🔴 El caso que motivó todo."""
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "develop"

    with contexto_de_build(repo, "main") as (ctx, commit, origen):
        assert (ctx / "marca.txt").read_text().strip() == "main"
        # Y no arrastra lo que sólo existe en develop.
        assert not (ctx / "solo-en-develop.txt").exists()
        assert commit

    # El control de la otra mitad: el checkout NO se movió.
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "develop"


def test_el_control_pidiendo_develop_da_develop(repo):
    """Sin esto, el test de arriba pasaría igual si la función devolviera
    siempre el mismo árbol pasara lo que pasara."""
    with contexto_de_build(repo, "develop") as (ctx, _commit, _origen):
        assert (ctx / "marca.txt").read_text().strip() == "develop"
        assert (ctx / "solo-en-develop.txt").exists()


def test_el_contexto_es_un_repo_AUTOCONTENIDO(repo):
    """🔴 El defecto que se comió 3 de 6 deploys el 2026-08-17.

    Con `git worktree add`, `.git` es un ARCHIVO que apunta a
    `<repo>/.git/worktrees/<nombre>`. Ese archivo entra al contexto de Docker y
    adentro del contenedor la ruta no existe, así que cualquier cosa que llame
    a git muere con `fatal: not a git repository`. Los productos cuyo
    Dockerfile hace `pip install .` con versión derivada de git fallaban; los
    que no llaman a git pasaban igual — **el defecto sólo se veía en la mitad
    de los casos**, que es por qué los tests anteriores no lo agarraron: medían
    que el contexto tuviera el CONTENIDO correcto, no que fuera usable.
    """
    with contexto_de_build(repo, "main") as (ctx, commit, _origen):
        dotgit = ctx / ".git"
        assert dotgit.is_dir(), f".git tiene que ser un directorio, no {dotgit}"
        assert git(ctx, "rev-parse", "HEAD").stdout.strip().startswith(commit)
        # No apunta afuera: ni gitdir de worktree, ni alternates al padre.
        assert "worktrees" not in git(ctx, "rev-parse", "--git-dir").stdout
        alternates = dotgit / "objects" / "info" / "alternates"
        assert not alternates.exists(), \
            f"usa alternates al repo padre: {alternates.read_text()!r}"


def test_el_contexto_sobrevive_a_que_el_padre_desaparezca(repo, tmp_path):
    """La prueba dura de "autocontenido": si el contexto se copia a otro lado y
    el repo padre ya no está —que es exactamente lo que hace Docker al meterlo
    en la imagen— git tiene que seguir funcionando."""
    import shutil as sh
    with contexto_de_build(repo, "main") as (ctx, commit, _o):
        copia = tmp_path / "copiado"
        sh.copytree(ctx, copia)
    repo.rename(repo.parent / "padre-mudado")
    assert git(copia, "rev-parse", "HEAD").stdout.strip().startswith(commit)
    assert (copia / "marca.txt").read_text().strip() == "main"


def test_el_contexto_se_limpia_al_salir(repo):
    with contexto_de_build(repo, "main") as (ctx, _c, _o):
        assert ctx.exists()
        creado = ctx
    assert not creado.exists()
    # Y el repo padre no queda con worktrees colgados de intentos anteriores.
    assert "src" not in git(repo, "worktree", "list").stdout


def test_se_limpia_tambien_si_el_build_revienta(repo):
    """El worktree vive en /tmp y se registra en el repo: si no se limpiara
    ante una excepción, cada deploy fallido dejaría basura y una entrada
    colgada en `git worktree list`."""
    with pytest.raises(ValueError):
        with contexto_de_build(repo, "main") as (ctx, _c, _o):
            creado = ctx
            raise ValueError("el build exploto")
    assert not creado.exists()
    assert "src" not in git(repo, "worktree", "list").stdout


def test_un_ref_que_no_existe_falla_nombrando_lo_disponible(repo):
    with pytest.raises(RuntimeError) as e:
        with contexto_de_build(repo, "no-existe-esta-rama"):
            pass
    msg = str(e.value)
    assert "no-existe-esta-rama" in msg
    # 🔴 Que además diga qué SÍ hay: un "no existe" a secas manda a adivinar.
    assert "develop" in msg or "main" in msg


def test_from_checkout_construye_el_working_tree_con_lo_no_commiteado(repo):
    """La salida de emergencia. Es explícita a propósito: incluye lo que esté
    sin commitear, que es justo lo que no se le quiere mandar a un cliente."""
    (repo / "sin-commitear.txt").write_text("y\n")
    with contexto_de_build(repo, "main", from_checkout=True) as (ctx, _c, origen):
        assert Path(ctx) == repo
        assert (ctx / "sin-commitear.txt").exists()
        assert "checkout" in origen


def test_no_es_un_repo_git(tmp_path):
    vacio = tmp_path / "no-repo"
    vacio.mkdir()
    with pytest.raises(RuntimeError) as e:
        with contexto_de_build(vacio, "main"):
            pass
    assert "no es un repo git" in str(e.value)
