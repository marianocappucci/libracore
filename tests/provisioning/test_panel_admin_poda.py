"""
Tests de la poda de imágenes de deploy (`podar_imagenes_viejas`).

Docker se mockea con un doble que responde distinto por subcomando —el
`fake_docker` de test_panel_admin.py devuelve siempre la misma línea, que
no sirve para `docker images` ni para `docker image inspect`.
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


@pytest.fixture
def docker_falso(monkeypatch):
    """Doble de `pa.docker` configurable. `estado` lleva:
      - `tags`:      los tags que `docker images` reporta;
      - `en_uso`:    refs que `docker ps -a` dice que usan contenedores;
      - `rmi_falla`: refs para las que `docker rmi` devuelve != 0;
      - `rmi`:       las refs que efectivamente se intentaron borrar.
    El ID de una imagen se deriva del ref (`sha256:<ref>`), así que dos refs
    distintos son dos imágenes distintas salvo que se diga lo contrario."""
    estado = {"tags": [], "en_uso": [], "rmi_falla": set(), "rmi": [],
              "alias": {}}

    def fake(*args, capture=False, cwd=None):
        argv = list(args)
        def ok(out=""):
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        if argv[:1] == ["images"]:
            return ok("\n".join(estado["tags"]) + "\n")
        if argv[:2] == ["ps", "-a"]:
            return ok("\n".join(estado["en_uso"]) + "\n")
        if argv[:2] == ["image", "inspect"]:
            ref = argv[2]
            return ok("sha256:" + estado["alias"].get(ref, ref) + "\n")
        if argv[:1] == ["rmi"]:
            ref = argv[1]
            estado["rmi"].append(ref)
            if ref in estado["rmi_falla"]:
                return subprocess.CompletedProcess(argv, 1, stdout="",
                                                   stderr="image is being used")
            return ok()
        return ok()

    monkeypatch.setattr(pa, "docker", fake)
    return estado


@pytest.fixture
def cfg(tmp_path, docker_falso):
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo_root, base_port=9000,
    )
    return provisioning.get_config()


def _cliente(cfg, slug, image):
    cdir = cfg.clientes_dir / slug
    (cdir / "data").mkdir(parents=True)
    (cdir / "cliente.json").write_text(json.dumps(
        {"nombre": slug, "slug": slug, "domain": "", "port": 9000,
         "container": f"testprod-{slug}", "admin_user": "a",
         "admin_password": "b", "plan": "basico"}), encoding="utf-8")
    (cdir / "docker-compose.yml").write_text(
        f"services:\n  testprod:\n    image: {image}\n", encoding="utf-8")


# ── qué se conserva ───────────────────────────────────────────────────────────

def test_conserva_los_n_mas_nuevos_y_borra_el_resto(cfg, docker_falso):
    docker_falso["tags"] = [
        "v2026.08.07-0900", "v2026.08.06-0800", "v2026.08.05-0700",
        "v2026.08.04-0600", "v2026.08.03-0500",
    ]
    borrados, _ = pa.podar_imagenes_viejas(keep=3)
    assert borrados == ["testprod:v2026.08.04-0600", "testprod:v2026.08.03-0500"]


def test_no_toca_los_tags_puestos_a_mano(cfg, docker_falso):
    """`p7`, `pre-p8-cutover-rollback` y compañía son hitos de migración: no
    los acuñó `deploy_version()` y nadie los va a volver a generar."""
    docker_falso["tags"] = [
        "v2026.08.07-0900", "v2026.08.06-0800", "v2026.08.05-0700",
        "v2026.08.04-0600",
        "p7", "p8-cutover", "pre-p7-rollback", "pre-recibos-20260805-074659",
    ]
    borrados, conservados = pa.podar_imagenes_viejas(keep=3)
    assert borrados == ["testprod:v2026.08.04-0600"]
    for hito in ("p7", "p8-cutover", "pre-p7-rollback",
                 "pre-recibos-20260805-074659"):
        assert any(f"testprod:{hito} " in c for c in conservados)


def test_no_borra_la_imagen_pineada_por_un_cliente_parado(cfg, docker_falso):
    """El caso que hace falta acertar: el cliente no corre, así que no aparece
    en `docker ps`, y su pin es lo único que dice con qué arrancar."""
    docker_falso["tags"] = [
        "v2026.08.07-0900", "v2026.08.06-0800", "v2026.08.05-0700",
        "v2026.08.04-0600", "v2026.07.01-0100",
    ]
    docker_falso["en_uso"] = []            # ningún contenedor corriendo ni parado
    _cliente(cfg, "pausado", "testprod:v2026.07.01-0100")

    borrados, conservados = pa.podar_imagenes_viejas(keep=3)
    assert "testprod:v2026.07.01-0100" not in borrados
    assert any("pineado en el compose" in c for c in conservados)
    assert borrados == ["testprod:v2026.08.04-0600"]


def test_no_borra_la_imagen_que_referencia_un_contenedor(cfg, docker_falso):
    docker_falso["tags"] = [
        "v2026.08.07-0900", "v2026.08.06-0800", "v2026.08.05-0700",
        "v2026.08.04-0600",
    ]
    docker_falso["en_uso"] = ["testprod:v2026.08.04-0600"]
    borrados, conservados = pa.podar_imagenes_viejas(keep=3)
    assert borrados == []
    assert any("en uso por un contenedor" in c for c in conservados)


def test_latest_nunca_es_candidato(cfg, docker_falso):
    docker_falso["tags"] = ["latest", "v2026.08.07-0900", "v2026.08.01-0100"]
    borrados, _ = pa.podar_imagenes_viejas(keep=1)
    assert borrados == ["testprod:v2026.08.01-0100"]
    assert "testprod:latest" not in docker_falso["rmi"]


# ── cómo se comporta ante fallas ──────────────────────────────────────────────

def test_si_docker_se_niega_se_reporta_conservada_y_no_rompe(cfg, docker_falso):
    docker_falso["tags"] = ["v2026.08.07-0900", "v2026.08.02-0200",
                            "v2026.08.01-0100"]
    docker_falso["rmi_falla"] = {"testprod:v2026.08.02-0200"}
    borrados, conservados = pa.podar_imagenes_viejas(keep=1)
    assert borrados == ["testprod:v2026.08.01-0100"]
    assert any("se negó" in c for c in conservados)


def test_nunca_usa_force(cfg, docker_falso):
    """`-f` convertiría una imagen retenida en un contenedor sin imagen."""
    docker_falso["tags"] = ["v2026.08.07-0900", "v2026.08.01-0100"]
    llamadas = []
    original = pa.docker
    def espia(*args, **kw):
        llamadas.append(list(args))
        return original(*args, **kw)
    pa.docker = espia
    try:
        pa.podar_imagenes_viejas(keep=1)
    finally:
        pa.docker = original
    rmi = [c for c in llamadas if c[:1] == ["rmi"]]
    assert rmi, "no hubo ningún rmi"
    assert all("-f" not in c and "--force" not in c for c in rmi)


def test_dry_run_no_borra_nada(cfg, docker_falso):
    docker_falso["tags"] = ["v2026.08.07-0900", "v2026.08.01-0100"]
    candidatos, _ = pa.podar_imagenes_viejas(keep=1, dry_run=True)
    assert candidatos == ["testprod:v2026.08.01-0100"]
    assert docker_falso["rmi"] == []


# ── integración con el deploy ─────────────────────────────────────────────────

def test_cmd_actualizar_poda_despues_de_desplegar(cfg, docker_falso, monkeypatch):
    """El orden importa: si se podara antes del build, un deploy fallido se
    quedaría sin la versión a la que volver."""
    orden = []
    monkeypatch.setattr(pa, "build_image_tagged",
                        lambda v, **kw: orden.append("build") or True)
    monkeypatch.setattr(pa, "load_clients", lambda: [])
    monkeypatch.setattr(pa, "podar_imagenes_viejas",
                        lambda *a, **kw: orden.append("poda") or ([], []))
    monkeypatch.setattr(pa, "check_venv_sync", lambda *_: None)

    pa.cmd_actualizar()
    # Sin clientes, `cmd_actualizar` corta temprano: la poda no debe correr
    # antes del build en ningún camino.
    assert orden[0] == "build"


def test_cmd_actualizar_poda_al_final_con_un_cliente(cfg, docker_falso, monkeypatch):
    orden = []
    monkeypatch.setattr(pa, "build_image_tagged",
                        lambda v, **kw: orden.append("build") or True)
    monkeypatch.setattr(pa, "check_venv_sync", lambda *_: None)
    monkeypatch.setattr(pa, "container_status", lambda c: {"status": "running"})
    monkeypatch.setattr(pa, "compose",
                        lambda slug, *a: orden.append("up") or
                        subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(pa, "podar_imagenes_viejas",
                        lambda *a, **kw: orden.append("poda") or ([], []))
    _cliente(cfg, "uno", "testprod:v2026.08.01-0100")

    pa.cmd_actualizar()
    assert orden == ["build", "up", "poda"]
