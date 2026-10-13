"""`LIBRA_INSTANCIA`, `LIBRA_ENTORNO` y `LIBRA_VERSION` en el compose de cada instancia (ADR-045).

El alta las escribe y `pinear_image` —el único punto por el que pasan `actualizar`, `rollback` y el
repineo de un deploy fallido— las mantiene. Tienen que ser idempotentes: un `actualizar` repetido no
puede apilar líneas, y el compose de una instancia vieja (sin las variables) tiene que ganarlas.
"""
import json
import re

import pytest
import yaml

from libracore import provisioning
from libracore.provisioning import errores_env as ee
from libracore.provisioning import nuevo_cliente as nc
from libracore.provisioning import panel_admin as pa

from .test_nuevo_cliente import CUIT, cfg, fake_docker, fake_plans  # noqa: F401
from .test_panel_admin import _reset_config  # noqa: F401


def _env_del_compose(texto, servicio):
    return {k: v for k, _, v in (i.partition("=") for i in yaml.safe_load(texto)["services"][servicio]["environment"])}


# ── El alta ───────────────────────────────────────────────────────────────────

def test_el_alta_escribe_las_tres_variables(cfg):  # noqa: F811
    nc.crear_cliente(empresa_cuit=CUIT, nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)

    texto = (cfg.clientes_dir / "cliente-uno" / "docker-compose.yml").read_text()
    env = _env_del_compose(texto, "testprod-cliente-uno")
    meta = json.loads((cfg.clientes_dir / "cliente-uno" / "cliente.json").read_text())
    assert env["LIBRA_INSTANCIA"] == "testprod-cliente-uno"
    assert env["LIBRA_ENTORNO"] == "cliente"
    assert env["LIBRA_VERSION"] == meta["version_desplegada"], "la variable dice lo mismo que el tag pineado"
    assert re.fullmatch(r"v20\d\d\.\d\d\.\d\d-\d{4}(\d{2})?", env["LIBRA_VERSION"])
    assert texto.count("LIBRA_VERSION=") == 1
    # y no rompió las que ya estaban
    assert env["DATA_DIR"] == "/app/data"


def test_la_instancia_demo_es_entorno_demo(cfg):  # noqa: F811
    nc.crear_cliente(empresa_cuit=CUIT, nombre="Demo", slug="demo", setup_npm=False)
    texto = (cfg.clientes_dir / "demo" / "docker-compose.yml").read_text()
    assert _env_del_compose(texto, "testprod-demo")["LIBRA_ENTORNO"] == "demo"


def test_entorno_de():
    assert ee.entorno_de("demo") == "demo"
    assert ee.entorno_de("lagrace") == "cliente"
    assert ee.entorno_de("demo-2") == "cliente"


def test_version_de_una_referencia():
    assert ee.version_de("contalibra:v2026.10.12-103000") == "v2026.10.12-103000"
    assert ee.version_de("registro:5000/contalibra:v1") == "v1"
    assert ee.version_de("contalibra") == "contalibra"


# ── Mantenerlas con el pin ────────────────────────────────────────────────────

APP_PRIMERO = """\
name: testprod-cliente

services:
  testprod-cliente:
    image: testprod:v1
    container_name: testprod-cliente
    restart: unless-stopped
    volumes:
      - ./data:/app/data
    environment:
      - DATA_DIR=/app/data
      - TZ=America/Argentina/Buenos_Aires
      - "LIBRAAUTH_SMTP_FROM_NAME=Casa # 5"
    networks:
      - stack-net

  testprod-cliente-postgres:
    image: postgres:16-alpine
    container_name: testprod-cliente-postgres
    environment:
      - POSTGRES_DB=testprod

networks:
  stack-net:
    external: true
"""

SIDECAR_PRIMERO = """\
services:
  testprod-cliente-db:
    image: postgres:16-alpine
    container_name: testprod-cliente-db
    environment:
      - POSTGRES_DB=cosas

  testprod-cliente:
    image: testprod:v1
    container_name: testprod-cliente
    environment:
      - DATA_DIR=/app/data
"""


@pytest.fixture
def instancia(tmp_path, fake_docker, request):  # noqa: F811
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest", container_prefix="testprod",
        db_filename="testprod.db", repo_root=repo_root, base_port=9000)
    carpeta = provisioning.get_config().clientes_dir / "cliente"
    (carpeta / "data").mkdir(parents=True)
    (carpeta / "cliente.json").write_text(json.dumps({
        "nombre": "Cliente", "slug": "cliente", "domain": "", "port": 9000, "container": "testprod-cliente",
        "plan": "basico"}), encoding="utf-8")
    compose = carpeta / "docker-compose.yml"
    compose.write_text(getattr(request, "param", APP_PRIMERO), encoding="utf-8")
    return compose


def test_pinear_agrega_las_variables_a_una_instancia_vieja(instancia):
    pa.pinear_image("cliente", "testprod:v2026.10.12-103000")

    env = _env_del_compose(instancia.read_text(), "testprod-cliente")
    assert env["LIBRA_VERSION"] == "v2026.10.12-103000"
    assert env["LIBRA_INSTANCIA"] == "testprod-cliente"
    assert env["LIBRA_ENTORNO"] == "cliente"
    assert env["LIBRAAUTH_SMTP_FROM_NAME"] == "Casa # 5", "lo que ya estaba queda igual"


def test_pinear_es_idempotente(instancia):
    pa.pinear_image("cliente", "testprod:v2")
    una = instancia.read_text()
    pa.pinear_image("cliente", "testprod:v2")
    pa.pinear_image("cliente", "testprod:v2")
    assert instancia.read_text() == una
    for v in ee.VARIABLES:
        assert una.count(f"{v}=") == 1, v


def test_un_nuevo_pin_actualiza_la_version_y_un_rollback_la_devuelve(instancia):
    pa.pinear_image("cliente", "testprod:v2")
    pa.pinear_image("cliente", "testprod:v3")
    assert _env_del_compose(instancia.read_text(), "testprod-cliente")["LIBRA_VERSION"] == "v3"
    pa.pinear_image("cliente", "testprod:v2")
    assert _env_del_compose(instancia.read_text(), "testprod-cliente")["LIBRA_VERSION"] == "v2"
    assert instancia.read_text().count("LIBRA_VERSION=") == 1


def test_un_entorno_fijado_a_mano_se_respeta(instancia):
    instancia.write_text(APP_PRIMERO.replace("      - DATA_DIR=/app/data\n",
                                             "      - DATA_DIR=/app/data\n      - LIBRA_ENTORNO=demo\n"))
    pa.pinear_image("cliente", "testprod:v2")
    assert _env_del_compose(instancia.read_text(), "testprod-cliente")["LIBRA_ENTORNO"] == "demo"


def test_la_instancia_y_la_version_si_se_corrigen(instancia):
    instancia.write_text(APP_PRIMERO.replace(
        "      - DATA_DIR=/app/data\n",
        "      - DATA_DIR=/app/data\n      - LIBRA_INSTANCIA=viejo\n      - LIBRA_VERSION=v0\n"))
    pa.pinear_image("cliente", "testprod:v2")
    env = _env_del_compose(instancia.read_text(), "testprod-cliente")
    assert env["LIBRA_INSTANCIA"] == "testprod-cliente" and env["LIBRA_VERSION"] == "v2"


def test_no_toca_el_sidecar_ni_el_resto_del_archivo(instancia):
    antes = instancia.read_text()
    pa.pinear_image("cliente", "testprod:v2")
    despues = instancia.read_text()
    assert _env_del_compose(despues, "testprod-cliente-postgres") == {"POSTGRES_DB": "testprod"}
    assert "image: postgres:16-alpine" in despues
    quitadas = [ln for ln in despues.splitlines() if ln not in antes.splitlines()]
    assert sorted(quitadas) == sorted(["    image: testprod:v2", "      - LIBRA_ENTORNO=cliente",
                                       "      - LIBRA_INSTANCIA=testprod-cliente", "      - LIBRA_VERSION=v2"])


@pytest.mark.parametrize("instancia", [SIDECAR_PRIMERO], indirect=True)
def test_con_el_sidecar_declarado_primero_va_a_la_app(instancia):
    pa.pinear_image("cliente", "testprod:v2")
    texto = instancia.read_text()
    assert "LIBRA_VERSION" in _env_del_compose(texto, "testprod-cliente")
    assert _env_del_compose(texto, "testprod-cliente-db") == {"POSTGRES_DB": "cosas"}


@pytest.mark.parametrize("instancia", [
    "services:\n  testprod-cliente:\n    image: testprod:v1\n    container_name: testprod-cliente\n",
    "services:\n  testprod-cliente:\n    image: testprod:v1\n    container_name: testprod-cliente\n"
    "    environment:\n      DATA_DIR: /app/data\n",
], indirect=True, ids=["sin environment", "environment en formato de mapa"])
def test_un_compose_que_no_se_reconoce_solo_cambia_la_imagen(instancia):
    pa.pinear_image("cliente", "testprod:v2")
    texto = instancia.read_text()
    assert "image: testprod:v2" in texto
    assert "LIBRA_" not in texto
    yaml.safe_load(texto)


def test_el_resultado_siempre_es_yaml_valido(instancia):
    pa.pinear_image("cliente", "testprod:v2")
    pa.pinear_image("cliente", "testprod:v3")
    yaml.safe_load(instancia.read_text())


def test_con_variables_directo_sobre_el_texto():
    """La función pura: `4` es la línea (base 0) de la `image:` de la app."""
    texto = ee.con_variables(APP_PRIMERO, 4, {"LIBRA_VERSION": "v1", "LIBRA_ENTORNO": "cliente"})
    assert texto is not None
    env = _env_del_compose(texto, "testprod-cliente")
    assert env["LIBRA_VERSION"] == "v1" and env["LIBRA_ENTORNO"] == "cliente"
    assert "LIBRA_INSTANCIA" not in env, "sólo escribe las que se le pasan"
