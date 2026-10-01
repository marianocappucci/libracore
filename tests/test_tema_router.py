"""El tema de la suite de una instancia: lectura pública, escritura detrás del gate del producto, sólo la FORMA validada."""
import importlib
import json

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as cm
    importlib.reload(cm)
    import libracore.tema_router as tr
    importlib.reload(tr)

    def gate(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    app = FastAPI()
    app.include_router(tr.build_tema_router())
    app.include_router(tr.build_tema_admin_router(), dependencies=[Depends(gate)])
    app.state.cm = cm
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


ADMIN = {"x-rol": "admin"}


def test_sin_tema_guardado_devuelve_vacio_y_no_se_cachea(client):
    r = client.get("/api/tema")
    assert r.status_code == 200
    assert r.json() == {"tema": {}}
    assert r.headers["cache-control"] == "no-cache"


def test_la_lectura_es_publica_y_la_escritura_no(client):
    assert client.get("/api/tema").status_code == 200
    assert client.put("/api/tema", json={"tema": {"menuActivoFondo": "#ffffff"}}).status_code == 403
    assert client.put("/api/tema", json={"tema": {"menuActivoFondo": "#ffffff"}}, headers={"x-rol": "cajero"}).status_code == 403


def test_guardar_normaliza_y_se_lee_de_vuelta(client):
    r = client.put("/api/tema", headers=ADMIN, json={"tema": {"menuActivoFondo": "#ECFDF5", "menuActivoBorde": "#5E9"}})
    assert r.status_code == 200
    esperado = {"menuActivoFondo": "#ecfdf5", "menuActivoBorde": "#55ee99"}
    assert r.json() == {"tema": esperado}
    assert client.get("/api/tema").json() == {"tema": esperado}


def test_un_tema_vacio_borra_todo(client, app):
    client.put("/api/tema", headers=ADMIN, json={"tema": {"menuActivoFondo": "#123456"}})
    assert client.put("/api/tema", headers=ADMIN, json={"tema": {}}).status_code == 200
    assert client.get("/api/tema").json() == {"tema": {}}
    with open(app.state.cm.CONFIG_PATH, encoding="utf-8") as f:
        assert "tema" not in json.load(f)


@pytest.mark.parametrize("malo", [
    {"menuActivoFondo": "verde"}, {"menuActivoFondo": "#12345"}, {"menuActivoFondo": "rgb(0,0,0)"}, {"menuActivoFondo": ""},
    {"1clave": "#ffffff"}, {"con espacio": "#ffffff"}, {"x" * 41: "#ffffff"}, {"a": 5},
])
def test_rechaza_lo_que_no_tiene_la_forma(client, malo):
    assert client.put("/api/tema", headers=ADMIN, json={"tema": malo}).status_code == 422
    assert client.get("/api/tema").json() == {"tema": {}}


def test_rechaza_mas_colores_que_el_tope(client):
    demasiados = {f"c{i}": "#ffffff" for i in range(33)}
    assert client.put("/api/tema", headers=ADMIN, json={"tema": demasiados}).status_code == 422


def test_una_clave_que_el_kit_no_conoce_se_guarda_igual(client):
    # La lista de colores vive en libra-ui; acá sólo se valida la forma.
    assert client.put("/api/tema", headers=ADMIN, json={"tema": {"colorFuturo": "#000000"}}).status_code == 200
    assert client.get("/api/tema").json() == {"tema": {"colorFuturo": "#000000"}}


def test_un_config_json_roto_o_de_otra_version_no_rompe_la_lectura(client, app):
    cm = app.state.cm
    with open(cm.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"tema": {"ok": "#ABCDEF", "mal": "xx", "1x": "#ffffff", "lista": ["#fff"]}}, f)
    assert client.get("/api/tema").json() == {"tema": {"ok": "#abcdef"}}
    with open(cm.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"tema": "no soy un dict"}, f)
    assert client.get("/api/tema").json() == {"tema": {}}


def test_guardar_el_tema_no_toca_el_resto_de_la_config(client, app):
    cm = app.state.cm
    cfg = cm.load()
    cfg["empresa_nombre"] = "Kiosco Ana"
    cm.save(cfg)
    client.put("/api/tema", headers=ADMIN, json={"tema": {"menuActivoFondo": "#123456"}})
    assert cm.load()["empresa_nombre"] == "Kiosco Ana"
