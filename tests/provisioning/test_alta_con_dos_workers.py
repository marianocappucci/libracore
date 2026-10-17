"""Una instancia nueva nace con 2 workers y 2 CPU (decisión del humano, 2026-10-17; `docs/varios-procesos.md`).

La imagen arranca `uvicorn` sin `--workers`, así que lo decide `WEB_CONCURRENCY` del compose. El alta es el único lugar
que escribe el compose entero (`pinear_image` sólo cambia la imagen): si la plantilla no lo trae, una instancia nueva
queda en un proceso aunque el resto de la suite corra con dos.
"""
import yaml

from libracore.provisioning import nuevo_cliente as nc

from .test_nuevo_cliente import CUIT, cfg, fake_docker, fake_plans  # noqa: F401


def test_el_alta_nace_con_dos_workers_y_dos_cpu(cfg):  # noqa: F811
    nc.crear_cliente(empresa_cuit=CUIT, nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)

    servicio = yaml.safe_load((cfg.clientes_dir / "cliente-uno" / "docker-compose.yml").read_text())["services"][
        "testprod-cliente-uno"]
    env = dict(i.partition("=")[::2] for i in servicio["environment"])
    assert env["WEB_CONCURRENCY"] == "2"
    assert float(servicio["cpus"]) == 2.0
