"""El pin de la imagen tiene que caer en el servicio de la APP, no en el sidecar.

Existe por un deploy que rompió una instancia el 2026-08-12: `pinear_image`
reemplazaba la **primera** línea `image:` del compose, con el argumento de que
"el compose de un cliente declara un único servicio". Dejó de ser cierto cuando
cada instancia ganó su sidecar de PostgreSQL, y en LibraDesk el sidecar está
declarado **arriba** de la app.

Resultado: el contenedor de PostgreSQL de la demo arrancó **corriendo la imagen
de la aplicación**, la base quedó abajo, y el `up -d` reportó éxito.
"""
import subprocess

import pytest

from libracore import provisioning
from libracore.provisioning import panel_admin as pa

from .test_panel_admin import _reset_config, fake_docker  # noqa: F401


def _compose_sidecar_primero(cfg, slug, imagen_app):
    """El layout de LibraDesk: el sidecar declarado ANTES que la app."""
    return (
        "services:\n"
        f"  {cfg.container_prefix}-{slug}-db:\n"
        "    image: postgres:16-alpine\n"
        f"    container_name: {cfg.container_prefix}-{slug}-db\n"
        "    environment:\n"
        "      POSTGRES_DB: cosas\n"
        "\n"
        f"  {cfg.container_prefix}-{slug}:\n"
        f"    image: {imagen_app}\n"
        f"    container_name: {cfg.container_prefix}-{slug}\n"
        "    ports:\n"
        '      - "9000:8000"\n'
    )


def _compose_app_primero(cfg, slug, imagen_app):
    """El layout de los otros cinco productos."""
    return (
        "services:\n"
        f"  {cfg.container_prefix}-{slug}:\n"
        f"    image: {imagen_app}\n"
        f"    container_name: {cfg.container_prefix}-{slug}\n"
        "\n"
        f"  {cfg.container_prefix}-{slug}-postgres:\n"
        "    image: postgres:16-alpine\n"
        f"    container_name: {cfg.container_prefix}-{slug}-postgres\n"
    )


@pytest.fixture
def cliente(tmp_path, fake_docker):  # noqa: F811
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo_root, base_port=9000,
    )
    cfg = provisioning.get_config()
    import json

    cdir = cfg.clientes_dir / "cliente"
    (cdir / "data").mkdir(parents=True)
    (cdir / "cliente.json").write_text(json.dumps({
        "nombre": "Cliente", "slug": "cliente", "domain": "", "port": 9000,
        "container": "testprod-cliente", "plan": "basico",
    }), encoding="utf-8")
    return cfg, cdir


def test_con_el_sidecar_primero_pinea_la_app(cliente):
    """🔴 El caso que rompió la demo de LibraDesk."""
    cfg, cdir = cliente
    (cdir / "docker-compose.yml").write_text(
        _compose_sidecar_primero(cfg, "cliente", "testprod:v1"), encoding="utf-8")

    anterior = pa.pinear_image("cliente", "testprod:v2")

    texto = (cdir / "docker-compose.yml").read_text(encoding="utf-8")
    assert anterior == "testprod:v1"
    assert "image: postgres:16-alpine" in texto, "el sidecar no se toca"
    assert "image: testprod:v2" in texto
    assert "testprod:v1" not in texto


def test_con_la_app_primero_sigue_andando(cliente):
    """Los otros cinco productos no cambian de comportamiento."""
    cfg, cdir = cliente
    (cdir / "docker-compose.yml").write_text(
        _compose_app_primero(cfg, "cliente", "testprod:v1"), encoding="utf-8")

    anterior = pa.pinear_image("cliente", "testprod:v2")

    texto = (cdir / "docker-compose.yml").read_text(encoding="utf-8")
    assert anterior == "testprod:v1"
    assert "image: postgres:16-alpine" in texto
    assert "image: testprod:v2" in texto


def test_leer_devuelve_la_de_la_app_y_no_la_del_sidecar(cliente):
    cfg, cdir = cliente
    (cdir / "docker-compose.yml").write_text(
        _compose_sidecar_primero(cfg, "cliente", "testprod:v7"), encoding="utf-8")

    assert pa.leer_image_pineada("cliente") == "testprod:v7"


def test_sin_container_name_cae_al_prefijo_del_producto(cliente):
    """Un compose viejo sin `container_name` igual se resuelve bien."""
    cfg, cdir = cliente
    (cdir / "docker-compose.yml").write_text(
        "services:\n"
        "  db:\n"
        "    image: postgres:16-alpine\n"
        "\n"
        "  app:\n"
        "    image: testprod:v1\n",
        encoding="utf-8",
    )

    assert pa.pinear_image("cliente", "testprod:v2") == "testprod:v1"
    texto = (cdir / "docker-compose.yml").read_text(encoding="utf-8")
    assert "image: postgres:16-alpine" in texto
    assert "image: testprod:v2" in texto


def test_si_no_se_puede_identificar_no_pinea_nada(cliente):
    """Preferir no aplicar el deploy antes que apagar la base.

    Sin `container_name` que matchee y sin ninguna imagen del producto, no hay
    forma de saber cuál es la app. Devuelve None y `cmd_actualizar` avisa."""
    cfg, cdir = cliente
    original = (
        "services:\n"
        "  db:\n"
        "    image: postgres:16-alpine\n"
        "\n"
        "  otra-cosa:\n"
        "    image: nginx:alpine\n"
    )
    (cdir / "docker-compose.yml").write_text(original, encoding="utf-8")

    assert pa.pinear_image("cliente", "testprod:v2") is None
    assert (cdir / "docker-compose.yml").read_text(encoding="utf-8") == original
