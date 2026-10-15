"""El tope de sucursales contratadas de una instancia (ADR-041): dato en el `config.json`, router con guarda inyectada, y la verificación del alta."""
import importlib
import json

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

TOKEN = "token-de-servicio-de-prueba"
SERVICIO = {"x-internal-auth": TOKEN}
ADMIN = {"x-rol": "admin"}


@pytest.fixture
def limites(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as cm
    importlib.reload(cm)
    import libracore.limites as lim
    importlib.reload(lim)
    lim.cm = cm
    return lim


def _guarda_lectura(x_internal_auth: str = Header(default=""), x_rol: str = Header(default="")):
    """Lo que el producto inyecta para leer: el token de servicio o un admin; sin ninguno de los dos, 401; con otro rol, 403."""
    if x_internal_auth == TOKEN or x_rol == "admin":
        return
    raise HTTPException(403 if x_rol else 401, "no autorizado")


def _guarda_escritura(x_internal_auth: str = Header(default=""), x_rol: str = Header(default="")):
    """Lo que el producto inyecta para escribir: SÓLO el token de servicio. Un admin de la instancia es 403."""
    if x_internal_auth == TOKEN:
        return
    raise HTTPException(403 if x_rol else 401, "no autorizado")


@pytest.fixture
def activas():
    return {"n": 1}


@pytest.fixture
def client(limites, activas):
    app = FastAPI()
    app.include_router(limites.build_limites_router(
        guard_lectura=_guarda_lectura, guard_escritura=_guarda_escritura, medir_activas=lambda: activas["n"],
    ))
    return TestClient(app)


# ── router ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("metodo, cuerpo", [("get", None), ("put", {"contratadas": 2})])
def test_sin_credenciales_no_entra(client, metodo, cuerpo):
    r = getattr(client, metodo)("/api/limites/sucursales", **({"json": cuerpo} if cuerpo else {}))
    assert r.status_code == 401


def test_un_rol_que_no_es_admin_no_lee_ni_escribe(client):
    assert client.get("/api/limites/sucursales", headers={"x-rol": "cajero"}).status_code == 403
    assert client.put("/api/limites/sucursales", headers={"x-rol": "cajero"}, json={"contratadas": 2}).status_code == 403


def test_con_el_token_de_servicio_lee_y_escribe(client):
    assert client.get("/api/limites/sucursales", headers=SERVICIO).status_code == 200
    assert client.put("/api/limites/sucursales", headers=SERVICIO, json={"contratadas": 2}).status_code == 200


def test_el_admin_de_la_instancia_lee_pero_no_puede_subirse_el_tope(client):
    """El tope es un límite comercial: si el admin del cliente pudiera cargar su número, el bloqueo no bloquearía nada."""
    assert client.get("/api/limites/sucursales", headers=ADMIN).status_code == 200
    r = client.put("/api/limites/sucursales", headers=ADMIN, json={"contratadas": 99})
    assert r.status_code == 403
    assert client.get("/api/limites/sucursales", headers=ADMIN).json()["contratadas"] is None


def test_sin_dato_cargado_dice_null_y_cuenta_las_activas(client, activas):
    activas["n"] = 3
    assert client.get("/api/limites/sucursales", headers=SERVICIO).json() == {"contratadas": None, "activas": 3, "mensaje_de_limite": None}


def test_put_guarda_y_get_lo_devuelve(client, activas, limites):
    r = client.put("/api/limites/sucursales", headers=SERVICIO, json={"contratadas": 2})
    assert r.status_code == 200
    MENSAJE = "Tu plan incluye 2 sucursales. Para sumar otra, comunicate con nosotros."
    assert r.json() == {"contratadas": 2, "activas": 1, "mensaje_de_limite": MENSAJE}
    assert client.get("/api/limites/sucursales", headers=SERVICIO).json() == {"contratadas": 2, "activas": 1, "mensaje_de_limite": MENSAJE}
    with open(limites.config_manager.CONFIG_PATH, encoding="utf-8") as f:
        assert json.load(f)["limite_sucursales"] == 2


def test_la_lectura_trae_el_mismo_texto_que_el_409_para_que_la_pantalla_no_lo_copie(client, limites):
    """ADR-052: la pantalla de Sucursales del kit muestra este texto al llegar al tope. Es `mensaje_de_limite` (el de `LimiteExcedido`), no una frase aparte."""
    for n, plural in ((1, "sucursal"), (3, "sucursales")):
        cuerpo = client.put("/api/limites/sucursales", headers=SERVICIO, json={"contratadas": n}).json()
        assert cuerpo["mensaje_de_limite"] == limites.mensaje_de_limite(n) == f"Tu plan incluye {n} {plural}. Para sumar otra, comunicate con nosotros."
        assert cuerpo["mensaje_de_limite"] == str(limites.LimiteExcedido(n, n))
        assert client.get("/api/limites/sucursales", headers=ADMIN).json()["mensaje_de_limite"] == cuerpo["mensaje_de_limite"]


def test_put_null_quita_el_dato(client, limites):
    client.put("/api/limites/sucursales", headers=SERVICIO, json={"contratadas": 2})
    r = client.put("/api/limites/sucursales", headers=SERVICIO, json={"contratadas": None})
    assert r.status_code == 200
    assert r.json()["contratadas"] is None and r.json()["mensaje_de_limite"] is None
    with open(limites.config_manager.CONFIG_PATH, encoding="utf-8") as f:
        assert "limite_sucursales" not in json.load(f)


def test_put_por_debajo_de_las_activas_se_acepta_y_el_get_lo_muestra(client, activas):
    """El cliente achicó el contrato con 3 locales abiertos: no se da de baja nada, sólo se frena el alta (y el backoffice lo marca)."""
    activas["n"] = 3
    assert client.put("/api/limites/sucursales", headers=SERVICIO, json={"contratadas": 1}).json() == {
        "contratadas": 1, "activas": 3, "mensaje_de_limite": "Tu plan incluye 1 sucursal. Para sumar otra, comunicate con nosotros."}


@pytest.mark.parametrize("malo", [0, -1, 1001, "mucho", 1.5, True, False, [], {}])
def test_put_rechaza_lo_que_no_es_un_entero_valido(client, malo):
    assert client.put("/api/limites/sucursales", headers=SERVICIO, json={"contratadas": malo}).status_code == 422
    assert client.get("/api/limites/sucursales", headers=SERVICIO).json()["contratadas"] is None


def test_put_sin_el_campo_es_422_y_no_borra_nada(client):
    client.put("/api/limites/sucursales", headers=SERVICIO, json={"contratadas": 2})
    assert client.put("/api/limites/sucursales", headers=SERVICIO, json={}).status_code == 422
    assert client.get("/api/limites/sucursales", headers=SERVICIO).json()["contratadas"] == 2


def test_guardar_el_limite_no_pisa_el_resto_de_la_config(client, limites):
    cfg = limites.config_manager.load()
    cfg["empresa_nombre"] = "Almacén de prueba"
    limites.config_manager.save(cfg)
    client.put("/api/limites/sucursales", headers=SERVICIO, json={"contratadas": 2})
    assert limites.config_manager.load()["empresa_nombre"] == "Almacén de prueba"


# ── la verificación del alta ─────────────────────────────────────────────────

def test_sin_dato_no_limita(limites):
    limites.verificar_alta_de_sucursal(0)
    limites.verificar_alta_de_sucursal(50)


def test_justo_en_el_limite_pasa_y_uno_mas_falla(limites):
    limites.fijar_sucursales_contratadas(2)
    limites.verificar_alta_de_sucursal(0)
    limites.verificar_alta_de_sucursal(1)  # la 2ª, que es la última contratada
    with pytest.raises(limites.LimiteExcedido) as e:
        limites.verificar_alta_de_sucursal(2)  # la 3ª
    assert e.value.contratadas == 2 and e.value.activas == 2


def test_el_mensaje_es_para_el_cliente_y_concuerda_en_numero(limites):
    assert limites.mensaje_de_limite(1) == "Tu plan incluye 1 sucursal. Para sumar otra, comunicate con nosotros."
    assert limites.mensaje_de_limite(3) == "Tu plan incluye 3 sucursales. Para sumar otra, comunicate con nosotros."
    limites.fijar_sucursales_contratadas(1)
    with pytest.raises(limites.LimiteExcedido, match=r"Tu plan incluye 1 sucursal\. Para sumar otra, comunicate con nosotros\."):
        limites.verificar_alta_de_sucursal(1)


def test_la_verificacion_acepta_el_tope_ya_leido(limites):
    with pytest.raises(limites.LimiteExcedido):
        limites.verificar_alta_de_sucursal(1, contratadas=1)
    limites.verificar_alta_de_sucursal(1, contratadas=2)


def test_quitar_el_dato_levanta_el_tope(limites):
    limites.fijar_sucursales_contratadas(1)
    with pytest.raises(limites.LimiteExcedido):
        limites.verificar_alta_de_sucursal(1)
    limites.fijar_sucursales_contratadas(None)
    limites.verificar_alta_de_sucursal(1)


@pytest.mark.parametrize("basura", [0, -3, "2", 2.0, True, None, [], "", {"n": 2}])
def test_un_valor_ilegible_en_el_config_se_lee_como_sin_dato(limites, basura):
    """Un `config.json` editado a mano no puede bloquear a una instancia ni romper la lectura."""
    cfg = limites.config_manager.load()
    cfg["limite_sucursales"] = basura
    limites.config_manager.save(cfg)
    assert limites.sucursales_contratadas() is None
    limites.verificar_alta_de_sucursal(10)


@pytest.mark.parametrize("malo", [0, -1, 1001, True, "2", 2.5])
def test_fijar_rechaza_lo_que_no_es_un_entero_valido(limites, malo):
    with pytest.raises(ValueError):
        limites.fijar_sucursales_contratadas(malo)
    assert limites.sucursales_contratadas() is None
