"""El subidor del host, con un enlace hecho desde la pantalla del cliente.

Lo que fija: que el host encuentre el `rclone.conf` que dejo la app **sin que
nadie toque `cliente.json`**, que lo use en **todas** las llamadas a rclone
—una sola sin `--config` iria a la config global, donde ese remoto no existe—,
y que el alta a mano en `cliente.json` siga ganando.
"""
import json
from datetime import datetime

import pytest

from libracore.provisioning import resguardo_externo as rx

AHORA = datetime(2026, 9, 10, 4, 30)
ZIP = "backup_automatico_20260910_040000.zip"


@pytest.fixture
def backups(tmp_path):
    d = tmp_path / "data" / "backups"
    (d / ".resguardo").mkdir(parents=True)
    (d / ".resguardo" / "rclone.conf").write_text("[externo]\ntype = drive\n")
    (d / ".resguardo" / "enlace.json").write_text(json.dumps({"proveedor": "drive", "carpeta": "Resguardo Prueba"}))
    (d / ZIP).write_bytes(b"x" * 64)
    return d


@pytest.fixture
def rclone(monkeypatch):
    registro = []

    def fake(*args, binario="rclone", timeout=1800):
        registro.append(args)
        if args[0] == "lsjson":
            return json.dumps([{"Name": ZIP, "Size": 64, "IsDir": False}])
        return ""
    monkeypatch.setattr(rx, "_rclone", fake)
    return registro


def test_el_destino_sale_del_enlace(backups):
    assert rx.destino_de({"slug": "x"}, backups) == {
        "remoto": "externo:",
        "ruta": "Resguardo Prueba",
        "config": str(backups / ".resguardo" / "rclone.conf"),
    }


def test_sin_backups_dir_no_mira_el_enlace(backups):
    """Quien llama sin la ruta sigue viendo exactamente lo de antes."""
    assert rx.destino_de({"slug": "x"}) is None


def test_el_alta_a_mano_gana_sobre_el_enlace(backups):
    cfg = rx.destino_de({"slug": "x", "resguardo_externo": {"remoto": "drive_x:", "ruta": "a/b"}}, backups)
    assert cfg == {"remoto": "drive_x:", "ruta": "a/b", "config": None}


def test_subir_usa_el_rclone_conf_de_la_instancia_en_cada_llamada(backups, rclone):
    estado = rx.subir({"slug": "x"}, backups, ahora=AHORA, log=lambda *_: None)
    assert estado["ok"] is True, estado
    assert estado["destino"] == "externo:Resguardo Prueba"
    assert [a[0] for a in rclone] == ["copy", "lsjson"]
    conf = str(backups / ".resguardo" / "rclone.conf")
    for args in rclone:
        assert args[-2:] == ("--config", conf), args


def test_con_el_alta_a_mano_no_se_pasa_config(backups, rclone):
    rx.subir({"slug": "x", "resguardo_externo": {"remoto": "drive_x:"}}, backups, ahora=AHORA, log=lambda *_: None)
    assert rclone and not [a for a in rclone if "--config" in a]
