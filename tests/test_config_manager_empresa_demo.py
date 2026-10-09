"""La empresa de una demo pública es siempre ficticia (ADR-038).

El caso que lo motivó: la demo de LibraDesk tenía en `config.json` los datos
fiscales reales de un cliente y un logo con el nombre de otra empresa. Estos
tests fijan que, con `DEMO_MODE=1`, lo guardado en disco no llega nunca a la
pantalla ni al PDF.
"""
import importlib
import json
import os

import pytest

from libracore.provisioning.nuevo_cliente import cuit_valido

REAL = {
    "empresa_nombre": "Cliente Real SRL",
    "empresa_cuit": "30-11111111-8",
    "empresa_direccion": "Calle Verdadera 1",
    "empresa_iibb": "123-456",
}


def _cm(tmp_path, monkeypatch, demo: bool):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    if demo:
        monkeypatch.setenv("DEMO_MODE", "1")
    else:
        monkeypatch.delenv("DEMO_MODE", raising=False)
    import libracore.config_manager as cm
    importlib.reload(cm)
    return cm


def _guardar_real_con_logo(tmp_path):
    logos = tmp_path / "logos"
    logos.mkdir()
    logo = logos / "logo.png"
    logo.write_bytes(b"\x89PNG real")
    (tmp_path / "config.json").write_text(
        json.dumps({**REAL, "logo_path": str(logo)}), encoding="utf-8")
    return str(logo)


def test_fuera_de_una_demo_manda_lo_guardado(tmp_path, monkeypatch):
    cm = _cm(tmp_path, monkeypatch, demo=False)
    logo = _guardar_real_con_logo(tmp_path)
    cfg = cm.load()
    assert cfg["empresa_nombre"] == "Cliente Real SRL"
    assert cm.resolve_logo_path(cfg) == logo


def test_en_una_demo_lo_guardado_no_sale_nunca(tmp_path, monkeypatch):
    cm = _cm(tmp_path, monkeypatch, demo=True)
    _guardar_real_con_logo(tmp_path)
    cfg = cm.load()
    for campo, valor in REAL.items():
        assert cfg[campo] != valor, campo
    assert cfg["empresa_nombre"] == cm.EMPRESA_DEMO_POR_DEFECTO["empresa_nombre"]
    assert cfg["logo_path"] == ""
    # Ni el guardado ni el «más reciente» de LOGO_DIR: ahí estaba el del cliente.
    assert cm.resolve_logo_path() == ""
    assert cm.resolve_logo_path({"logo_path": os.path.join(str(tmp_path), "logos", "logo.png")}) == ""


def test_guardar_en_una_demo_no_deja_ver_el_dato_real(tmp_path, monkeypatch):
    cm = _cm(tmp_path, monkeypatch, demo=True)
    cm.save({**REAL})
    assert cm.load()["empresa_cuit"] == cm.EMPRESA_DEMO_POR_DEFECTO["empresa_cuit"]


def test_el_producto_registra_su_empresa_y_su_logo(tmp_path, monkeypatch):
    cm = _cm(tmp_path, monkeypatch, demo=True)
    logo = tmp_path / "logo-demo.png"
    logo.write_bytes(b"\x89PNG demo")
    cm.usar_empresa_demo({"empresa_nombre": "Nexo Soporte IT SRL",
                          "empresa_cuit": "30-99999902-2"}, logo_path=str(logo))
    cfg = cm.load()
    assert cfg["empresa_nombre"] == "Nexo Soporte IT SRL"
    assert cfg["empresa_cuit"] == "30-99999902-2"
    # Lo que no registró sale del genérico, no de disco.
    assert cfg["empresa_direccion"] == cm.EMPRESA_DEMO_POR_DEFECTO["empresa_direccion"]
    assert cfg["logo_path"] == str(logo)
    assert cm.resolve_logo_path() == str(logo)


def test_un_logo_registrado_que_no_existe_no_cae_al_de_disco(tmp_path, monkeypatch):
    cm = _cm(tmp_path, monkeypatch, demo=True)
    _guardar_real_con_logo(tmp_path)
    cm.usar_empresa_demo(logo_path=str(tmp_path / "no-existe.png"))
    assert cm.resolve_logo_path() == ""
    assert cm.load()["logo_path"] == ""


def test_un_campo_ajeno_a_la_empresa_se_rechaza(tmp_path, monkeypatch):
    cm = _cm(tmp_path, monkeypatch, demo=True)
    with pytest.raises(ValueError, match="empresa_nombr"):
        cm.usar_empresa_demo({"empresa_nombr": "Typo SRL"})


def test_demo_mode_se_lee_en_cada_llamada(tmp_path, monkeypatch):
    cm = _cm(tmp_path, monkeypatch, demo=False)
    _guardar_real_con_logo(tmp_path)
    assert cm.load()["empresa_nombre"] == "Cliente Real SRL"
    monkeypatch.setenv("DEMO_MODE", "1")
    assert cm.load()["empresa_nombre"] != "Cliente Real SRL"


def test_la_empresa_por_defecto_es_ficticia_y_completa(tmp_path, monkeypatch):
    cm = _cm(tmp_path, monkeypatch, demo=True)
    emp = cm.EMPRESA_DEMO_POR_DEFECTO
    assert set(emp) == set(cm.CAMPOS_EMPRESA)
    assert all(emp.values())
    assert cuit_valido(emp["empresa_cuit"])
    assert emp["empresa_cuit"].startswith("30-999")
    assert emp["empresa_email"].endswith(".example")
