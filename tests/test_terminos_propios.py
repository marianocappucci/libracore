"""Los Términos y Condiciones propios de una instancia (ADR-093): carga con gate, una versión es un texto, lectura cacheada sin quedar vieja.

Lo que fijan estos tests, en orden de lo que se rompe sin que se note:

1. 🔴 **Que lo leído nunca quede viejo.** `leer_terminos_propios()` se llama en cada pedido gateado y reusa el objeto si nada cambió; si
   un archivo reemplazado con el mismo largo dejara el texto anterior, la instancia exigiría aceptar un contrato distinto del que el
   revendedor cree haber publicado.
2. 🔴 **Que una versión sea UN texto** (409), también después de borrar los términos.
3. Que sólo el admin del producto o el servicio escriban, y que el texto no salga por ninguna ruta de acá.
4. Que la forma coincida con la de `libraauth` (`Contrato.propio_desde`): versión, vigencia y tope del texto.
5. Que los términos, el logo del membrete y el de la marca no se pisen entre sí (comparten el directorio de la marca).
"""
import hashlib
import importlib
import io
import json
import os

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

ADMIN = {"x-rol": "admin"}
TEXTO = "# Términos de Gestión Sur\n\n1. El servicio lo presta Gestión Sur.\n2. Los datos son del Cliente.\n"
CUERPO = {"version": "2026-1", "vigente_desde": "01-11-2026", "texto": TEXTO}


def sha(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (400, 300), (200, 30, 30, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "datos"))
    (tmp_path / "datos").mkdir()
    import libracore.config_manager as cm
    importlib.reload(cm)
    import libracore.terminos_propios as tp
    importlib.reload(tp)
    import libracore.marca_router as mr
    importlib.reload(mr)

    def gate(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    app = FastAPI()
    app.include_router(mr.build_marca_router())
    app.include_router(mr.build_marca_admin_router(), dependencies=[Depends(gate)])
    app.state.cm, app.state.tp, app.state.mr = cm, tp, mr
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def poner(client, cuerpo=None, **cambios):
    return client.put("/api/marca/terminos", headers=ADMIN, json={**(cuerpo or CUERPO), **cambios})


# ── Cargar y leer ──────────────────────────────────────────────────────────

def test_sin_terminos_no_hay_nada(client, app):
    assert app.state.tp.leer_terminos_propios() is None
    assert client.get("/api/marca/terminos", headers=ADMIN).json() == {
        "cargados": False, "version": "", "vigente_desde": "", "hash": "", "caracteres": 0,
    }
    assert client.get("/api/marca").json()["terminos_propios"] is False


def test_cargar_guarda_el_texto_y_lo_entrega_como_lo_espera_libraauth(client, app):
    r = poner(client)
    assert r.status_code == 200
    assert r.json() == {
        "cargados": True, "version": "2026-1", "vigente_desde": "01-11-2026", "hash": sha(TEXTO), "caracteres": len(TEXTO),
    }
    # Es el dict que `TerminosRepository(contrato_propio=...)` convierte en un `Contrato`: exactamente estas tres claves.
    assert app.state.tp.leer_terminos_propios() == CUERPO
    assert client.get("/api/marca").json()["terminos_propios"] is True


def test_el_texto_no_sale_por_ninguna_ruta_de_este_modulo(client):
    """Lo ve quien entra a la instancia, por la pantalla de aceptación de `libraauth`."""
    poner(client)
    for r in (client.get("/api/marca/terminos", headers=ADMIN), client.get("/api/marca"), poner(client)):
        assert "Gestión Sur" not in r.text and "Cliente" not in r.text


def test_los_finales_de_linea_no_cambian_la_huella(client, app):
    r = poner(client, texto=TEXTO.replace("\n", "\r\n"))
    assert r.json()["hash"] == sha(TEXTO)
    assert app.state.tp.leer_terminos_propios()["texto"] == TEXTO  # y se guarda normalizado
    assert poner(client, texto=TEXTO.replace("\n", "\r")).json()["hash"] == sha(TEXTO)  # idempotente, no 409


def test_el_texto_se_guarda_en_el_directorio_de_la_marca(client, app):
    poner(client)
    ruta = os.path.join(app.state.cm.MARCA_DIR, "terminos.md")
    with open(ruta, encoding="utf-8") as f:
        assert f.read() == TEXTO
    assert not any(n.endswith(".tmp") for n in os.listdir(app.state.cm.MARCA_DIR))


# ── 3. El gate ─────────────────────────────────────────────────────────────

def test_escribir_exige_admin_o_servicio(client):
    assert client.get("/api/marca/terminos").status_code == 403
    assert client.put("/api/marca/terminos", json=CUERPO).status_code == 403
    assert client.put("/api/marca/terminos", json=CUERPO, headers={"x-rol": "cajero"}).status_code == 403
    assert client.delete("/api/marca/terminos").status_code == 403
    assert client.get("/api/marca").status_code == 200  # y la marca sigue pública


# ── 4. La forma, igual que en libraauth ────────────────────────────────────

@pytest.mark.parametrize("version", ["", " ", "a b", "x" * 21, "-v1", "v/1", "v1\n", "ñ1"])
def test_versiones_invalidas(client, version):
    assert poner(client, version=version).status_code == 422


@pytest.mark.parametrize("desde", ["", "2026-11-01", "31-02-2026", "1/11/2026", "mañana"])
def test_fechas_invalidas(client, desde):
    assert poner(client, vigente_desde=desde).status_code == 422


@pytest.mark.parametrize("texto", ["", "   \n ", "\n\n"])
def test_texto_vacio(client, texto):
    assert poner(client, texto=texto).status_code == 422


def test_el_tope_del_texto_es_el_de_libraauth(client, app):
    assert app.state.tp.MAX_TEXTO == 300_000  # `libraauth.terminos.MAX_TEXTO_PROPIO`
    assert poner(client, texto="x" * 300_001).status_code == 422
    assert poner(client, texto="x" * 300_000).status_code == 200


def test_lo_invalido_no_deja_nada_guardado(client, app):
    poner(client, version="a b")
    assert app.state.tp.leer_terminos_propios() is None
    assert not os.path.exists(os.path.join(app.state.cm.MARCA_DIR, "terminos.md"))


# ── 2. Una versión es un texto ─────────────────────────────────────────────

def test_la_misma_version_con_otro_texto_es_409_y_no_cambia_nada(client, app):
    poner(client)
    r = poner(client, texto=TEXTO + "3. Una cláusula nueva.\n")
    assert r.status_code == 409 and "otro número de versión" in r.json()["detail"]
    assert app.state.tp.leer_terminos_propios()["texto"] == TEXTO


def test_publicar_otra_vez_el_mismo_texto_es_idempotente_y_permite_cambiar_la_vigencia(client):
    poner(client)
    r = poner(client, vigente_desde="15-11-2026")
    assert r.status_code == 200 and r.json()["vigente_desde"] == "15-11-2026"


def test_una_version_nueva_reemplaza_a_la_anterior(client, app):
    poner(client)
    r = poner(client, version="2026-2", texto=TEXTO + "3. Una cláusula nueva.\n")
    assert r.status_code == 200 and r.json()["version"] == "2026-2"
    assert app.state.tp.leer_terminos_propios()["version"] == "2026-2"


def test_el_historial_sobrevive_a_borrar_los_terminos(client):
    poner(client)
    assert client.delete("/api/marca/terminos", headers=ADMIN).json()["cargados"] is False
    assert poner(client, texto="Otro texto distinto").status_code == 409  # el número 2026-1 ya fue de otro texto
    assert poner(client).status_code == 200  # y volver a cargar el mismo sí
    assert poner(client, version="2026-3", texto="Otro texto distinto").status_code == 200


# ── 1. Lo leído nunca queda viejo ──────────────────────────────────────────

def test_se_devuelve_el_mismo_objeto_mientras_nada_cambie(client, app):
    poner(client)
    primero = app.state.tp.leer_terminos_propios()
    assert app.state.tp.leer_terminos_propios() is primero  # libraauth no vuelve a armar ni a hashear el contrato


def test_un_archivo_reemplazado_con_el_mismo_largo_no_deja_el_texto_viejo(client, app):
    """El caso que una clave de fecha, largo e inodo no cubre: un respaldo restaurado o una edición a mano que cambia el texto sin
    cambiar la versión ni el largo, varias veces seguidas. La API no puede producirlo (una versión es un texto: 409), el disco sí."""
    poner(client, version="v1", texto="A" * 500)
    ruta = os.path.join(app.state.cm.MARCA_DIR, "terminos.md")
    for i in range(80):
        esperado = ("A" if i % 2 else "B") * 500
        app.state.cm.escribir_atomico(ruta, esperado.encode("utf-8"))
        leido = app.state.tp.leer_terminos_propios()
        assert leido["texto"] == esperado, f"iteración {i}"


def test_varias_escrituras_entre_dos_lecturas_tampoco(client, app):
    poner(client, version="v1", texto="A" * 500)
    app.state.tp.leer_terminos_propios()
    ruta = os.path.join(app.state.cm.MARCA_DIR, "terminos.md")
    for letra in "BCA":  # termina en el mismo texto que se leyó, pasando por otros dos: el inodo puede coincidir con el cacheado
        app.state.cm.escribir_atomico(ruta, (letra * 500).encode("utf-8"))
    assert app.state.tp.leer_terminos_propios()["texto"] == "A" * 500
    app.state.cm.escribir_atomico(ruta, ("Z" * 500).encode("utf-8"))
    assert app.state.tp.leer_terminos_propios()["texto"] == "Z" * 500


def test_publicar_versiones_nuevas_seguido_siempre_devuelve_la_ultima(client, app):
    for i in range(40):
        texto = ("A" if i % 2 == 0 else "B") * 500
        assert poner(client, version=f"v{i}", texto=texto).status_code == 200
        leido = app.state.tp.leer_terminos_propios()
        assert leido["texto"] == texto and leido["version"] == f"v{i}", f"iteración {i}"


def test_si_cambia_solo_la_vigencia_en_el_config_se_entera(client, app):
    poner(client)
    antes = app.state.tp.leer_terminos_propios()
    poner(client, vigente_desde="02-11-2026")
    despues = app.state.tp.leer_terminos_propios()
    assert despues is not antes and despues["vigente_desde"] == "02-11-2026"


# ── Robustez: nunca lanza ──────────────────────────────────────────────────

@pytest.mark.parametrize("basura", ["texto", 5, ["a"], {"version": 1, "vigente_desde": "01-11-2026"}, {"version": "v1"}, {}])
def test_un_config_json_con_basura_es_no_tiene(client, app, basura):
    poner(client)
    cm = app.state.cm
    cm.save({**cm.load(), "terminos_propios": basura})
    assert app.state.tp.leer_terminos_propios() is None
    assert client.get("/api/marca/terminos", headers=ADMIN).json()["cargados"] is False


def test_si_falta_el_archivo_es_no_tiene(client, app):
    poner(client)
    os.unlink(os.path.join(app.state.cm.MARCA_DIR, "terminos.md"))
    assert app.state.tp.leer_terminos_propios() is None


def test_un_archivo_que_no_es_utf8_es_no_tiene(client, app):
    poner(client)
    with open(os.path.join(app.state.cm.MARCA_DIR, "terminos.md"), "wb") as f:
        f.write(b"\xff\xfe\x00 no es utf-8 \xc3\x28")
    assert app.state.tp.leer_terminos_propios() is None


# ── 5. No se pisan con el logo del membrete ni con el de la marca ──────────

def test_borrar_el_logo_de_la_marca_no_se_lleva_los_terminos(client, app):
    poner(client)
    r = client.post("/api/marca/logo", headers=ADMIN, files={"logo": ("l.png", png(), "image/png")})
    assert r.status_code == 200 and r.json()["terminos_propios"] is True
    client.delete("/api/marca/logo", headers=ADMIN)
    assert app.state.tp.leer_terminos_propios() == CUERPO


def test_subir_los_terminos_no_toca_el_logo_ni_el_membrete(client, app):
    cm = app.state.cm
    os.makedirs(cm.LOGO_DIR, exist_ok=True)
    membrete = os.path.join(cm.LOGO_DIR, "logo.png")
    with open(membrete, "wb") as f:
        f.write(png())
    client.post("/api/marca/logo", headers=ADMIN, files={"logo": ("l.png", png(), "image/png")})
    poner(client)
    assert client.get("/api/marca/logo").status_code == 200
    assert os.path.isfile(membrete)


def test_borrar_los_terminos_no_borra_la_marca(client):
    client.put("/api/marca", headers=ADMIN, json={"nombre": "Gestión Sur"})
    poner(client)
    client.delete("/api/marca/terminos", headers=ADMIN)
    assert client.get("/api/marca").json()["nombre"] == "Gestión Sur"


def test_borrar_toda_la_marca_se_lleva_tambien_los_terminos_pero_no_el_historial(client, app):
    """Una instancia que vuelve a ser del producto no puede seguir pidiendo aceptar un contrato que no es el de su producto."""
    client.put("/api/marca", headers=ADMIN, json={"nombre": "Gestión Sur"})
    poner(client)
    r = client.delete("/api/marca", headers=ADMIN)
    assert r.json()["propia"] is False and r.json()["terminos_propios"] is False
    assert app.state.tp.leer_terminos_propios() is None
    assert not os.path.exists(os.path.join(app.state.cm.MARCA_DIR, "terminos.md"))
    assert "2026-1" in app.state.cm.load()["terminos_propios_historial"]


# ── Los dos ganchos de libraauth ───────────────────────────────────────────

def test_es_marca_propia_y_nombre_de_marca(client, app):
    mr = app.state.mr
    assert mr.es_marca_propia() is False and mr.nombre_de_marca("VentaLibra") == "VentaLibra"
    client.put("/api/marca", headers=ADMIN, json={"nombre": "Gestión Sur"})
    assert mr.es_marca_propia() is True and mr.nombre_de_marca("VentaLibra") == "Gestión Sur"


def test_un_color_solo_es_marca_propia_pero_el_nombre_sigue_siendo_el_del_producto(client, app):
    mr = app.state.mr
    client.put("/api/marca", headers=ADMIN, json={"color": "#336699"})
    assert mr.es_marca_propia() is True and mr.nombre_de_marca("VentaLibra") == "VentaLibra"


def test_el_hash_del_logo_con_salto_de_linea_no_vale(client, app):
    """El `$` de Python acepta un salto de línea final (lo encontró un test de libraauth); acá la expresión del hash del logo era igual."""
    cm = app.state.cm
    client.post("/api/marca/logo", headers=ADMIN, files={"logo": ("l.png", png(), "image/png")})
    marca = cm.load()["marca"]
    marca["logo"] = marca["logo"] + "\n"
    cm.save({**cm.load(), "marca": marca})
    assert client.get("/api/marca").json()["logo"] == ""


def test_escribir_atomico_no_deja_basura_ni_pisa_lo_anterior_si_falla(app, monkeypatch):
    cm = app.state.cm
    destino = os.path.join(cm.MARCA_DIR, "x.bin")
    cm.escribir_atomico(destino, b"viejo")

    def falla(*a, **k):
        raise OSError("disco lleno")

    monkeypatch.setattr(os, "replace", falla)
    with pytest.raises(OSError):
        cm.escribir_atomico(destino, b"nuevo")
    monkeypatch.undo()
    with open(destino, "rb") as f:
        assert f.read() == b"viejo"
    assert os.listdir(cm.MARCA_DIR) == ["x.bin"]


def test_el_estado_guardado_en_el_config_es_chico(client, app):
    """Los 300 KB de texto no viajan en `config.json`, que se lee en cada pedido: sólo la versión y la vigencia."""
    poner(client, texto="x" * 200_000)
    assert os.path.getsize(app.state.cm.CONFIG_PATH) < 5_000
    assert json.loads(open(app.state.cm.CONFIG_PATH, encoding="utf-8").read())["terminos_propios"] == {
        "version": "2026-1", "vigente_desde": "01-11-2026",
    }
