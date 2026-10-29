"""La marca de una instancia (ADR-092): lectura pública, escritura detrás del gate, logo normalizado, íconos y manifest."""
import importlib
import io
import json

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from PIL import Image

ADMIN = {"x-rol": "admin"}
MANIFEST_DEL_BUILD = {
    "name": "ProductoX",
    "short_name": "ProductoX",
    "start_url": "/",
    "display": "standalone",
    "theme_color": "#111111",
    "icons": [{"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"}],
}


def png(ancho=400, alto=300, color=(200, 30, 30, 255), formato="PNG") -> bytes:
    buf = io.BytesIO()
    imagen = Image.new("RGBA" if formato != "JPEG" else "RGB", (ancho, alto), color if formato != "JPEG" else color[:3])
    imagen.save(buf, format=formato)
    return buf.getvalue()


@pytest.fixture
def dist(tmp_path):
    carpeta = tmp_path / "dist"
    carpeta.mkdir()
    (carpeta / "manifest.webmanifest").write_text(json.dumps(MANIFEST_DEL_BUILD), encoding="utf-8")
    (carpeta / "index.html").write_text("<html>SPA</html>", encoding="utf-8")
    return carpeta


@pytest.fixture
def app(tmp_path, monkeypatch, dist):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "datos"))
    (tmp_path / "datos").mkdir()
    import libracore.config_manager as cm
    importlib.reload(cm)
    import libracore.marca_router as mr
    importlib.reload(mr)

    def gate(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    app = FastAPI()
    app.include_router(mr.build_marca_router(dist=dist))
    app.include_router(mr.build_marca_admin_router(), dependencies=[Depends(gate)])

    # El catch-all de la SPA, montado DESPUÉS: contesta HTML con 200 a cualquier cosa. Es el riesgo que el ADR-092 nombra.
    @app.get("/{full_path:path}")
    def spa(full_path: str):
        return HTMLResponse("<html>SPA</html>")

    app.state.cm = cm
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def subir(client, datos, nombre="logo.png", tipo="image/png"):
    return client.post("/api/marca/logo", headers=ADMIN, files={"logo": (nombre, datos, tipo)})


# ── Lectura y gate ─────────────────────────────────────────────────────────

def test_sin_marca_devuelve_vacio_y_no_se_cachea(client):
    r = client.get("/api/marca")
    assert r.status_code == 200
    assert r.json() == {"propia": False, "nombre": "", "nombre_corto": "", "color": "", "logo": ""}
    assert r.headers["cache-control"] == "no-cache"


def test_la_lectura_es_publica_y_la_escritura_no(client):
    assert client.get("/api/marca").status_code == 200
    assert client.put("/api/marca", json={"nombre": "X"}).status_code == 403
    assert client.put("/api/marca", json={"nombre": "X"}, headers={"x-rol": "cajero"}).status_code == 403
    assert client.post("/api/marca/logo", files={"logo": ("l.png", png(), "image/png")}).status_code == 403
    assert client.delete("/api/marca").status_code == 403
    assert client.delete("/api/marca/logo").status_code == 403


# ── Texto y color ──────────────────────────────────────────────────────────

def test_guardar_normaliza_y_se_lee_de_vuelta(client):
    r = client.put("/api/marca", headers=ADMIN, json={"nombre": "  Mi Sistema  ", "nombre_corto": "MiSis", "color": "#ECFDF5"})
    assert r.status_code == 200
    esperado = {"propia": True, "nombre": "Mi Sistema", "nombre_corto": "MiSis", "color": "#ecfdf5", "logo": ""}
    assert r.json() == esperado
    assert client.get("/api/marca").json() == esperado


def test_el_nombre_corto_por_defecto_sale_del_nombre(client):
    r = client.put("/api/marca", headers=ADMIN, json={"nombre": "Gestión Integral Sur"})
    assert r.json()["nombre_corto"] == "Gestión Inte"


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"nombre": "x" * 41},
        {"nombre": "a", "nombre_corto": "y" * 13},
        {"nombre": "a", "color": "rojo"},
        {"nombre": "a\x00b"},
        {"nombre": "a‮b"},  # inversor bidireccional: invisible y sirve para engañar
        {"nombre": "a​b"},  # espacio de ancho cero
    ],
)
def test_lo_invalido_se_rechaza_y_no_deja_nada_guardado(client, cuerpo):
    assert client.put("/api/marca", headers=ADMIN, json=cuerpo).status_code == 422
    assert client.get("/api/marca").json()["propia"] is False


def test_un_put_conserva_el_logo(client):
    subir(client, png())
    client.put("/api/marca", headers=ADMIN, json={"nombre": "Otro"})
    assert client.get("/api/marca").json()["logo"].startswith("/api/marca/logo?v=")


# ── Logo ───────────────────────────────────────────────────────────────────

def test_subir_un_logo_lo_guarda_como_png_y_lo_sirve(client, app):
    r = subir(client, png(800, 400))
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["propia"] is True
    assert cuerpo["logo"].startswith("/api/marca/logo?v=")
    servido = client.get(cuerpo["logo"])
    assert servido.status_code == 200
    assert servido.headers["content-type"] == "image/png"
    imagen = Image.open(io.BytesIO(servido.content))
    assert imagen.format == "PNG" and imagen.size == (800, 400)


def test_un_jpg_se_convierte_a_png_y_un_logo_grande_se_achica(client):
    subir(client, png(2000, 1000, formato="JPEG"), "l.jpg", "image/jpeg")
    imagen = Image.open(io.BytesIO(client.get("/api/marca/logo").content))
    assert imagen.format == "PNG" and imagen.size == (1024, 512)


def test_el_logo_se_cachea_para_siempre_solo_con_su_version(client):
    ruta = subir(client, png()).json()["logo"]
    assert "immutable" in client.get(ruta).headers["cache-control"]
    assert client.get("/api/marca/logo").headers["cache-control"] == "no-cache"
    assert client.get("/api/marca/logo?v=000000000000").headers["cache-control"] == "no-cache"


def test_un_logo_nuevo_cambia_la_version(client):
    a = subir(client, png(color=(255, 0, 0, 255))).json()["logo"]
    b = subir(client, png(color=(0, 0, 255, 255))).json()["logo"]
    assert a != b


@pytest.mark.parametrize(
    "datos, nombre, motivo",
    [
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "l.svg", "PNG, JPG o WEBP|no es una imagen"),
        (b"no soy una imagen", "l.png", "no es una imagen"),
        (png(100, 100), "l.png", "demasiado chico"),
        (png(5000, 300), "l.png", "demasiado grande"),
    ],
)
def test_un_logo_invalido_se_rechaza(client, datos, nombre, motivo):
    r = subir(client, datos, nombre)
    assert r.status_code == 422
    assert any(parte in r.json()["detail"] for parte in motivo.split("|"))
    assert client.get("/api/marca").json()["logo"] == ""


def test_un_logo_de_mas_de_un_mega_es_413(client):
    import libracore.marca_router as mr

    assert subir(client, b"\x00" * (mr.MAX_BYTES_LOGO + 10)).status_code == 413


def test_la_marca_no_toca_el_logo_del_membrete(client, app):
    cm = app.state.cm
    import os

    os.makedirs(cm.LOGO_DIR, exist_ok=True)
    membrete = os.path.join(cm.LOGO_DIR, "logo.png")
    original = png(color=(10, 20, 30, 255))
    with open(membrete, "wb") as f:
        f.write(original)
    cfg = cm.load()
    cfg["logo_path"] = membrete
    cm.save(cfg)

    assert subir(client, png(color=(0, 255, 0, 255))).status_code == 200
    with open(membrete, "rb") as f:
        assert f.read() == original  # subir la marca no pisó el logo del membrete
    assert client.delete("/api/marca", headers=ADMIN).status_code == 200  # y borrarla tampoco lo borra

    assert os.path.isfile(membrete)
    with open(membrete, "rb") as f:
        assert f.read() == original
    assert cm.load()["logo_path"] == membrete


def test_si_falta_el_archivo_del_logo_no_cuenta(client, app):
    import os

    subir(client, png())
    os.unlink(os.path.join(app.state.cm.MARCA_DIR, "logo.png"))
    assert client.get("/api/marca").json() == {"propia": False, "nombre": "", "nombre_corto": "", "color": "", "logo": ""}
    assert client.get("/api/marca/logo").status_code == 404


# ── Íconos ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tipo, lado", [("192", 192), ("512", 512), ("maskable", 512), ("apple", 180)])
def test_los_iconos_salen_cuadrados_del_lado_correcto(client, tipo, lado):
    subir(client, png(800, 300))
    r = client.get(f"/api/marca/icono/{tipo}.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).size == (lado, lado)


def test_el_icono_comun_es_transparente_y_los_de_fondo_son_opacos(client):
    client.put("/api/marca", headers=ADMIN, json={"color": "#336699"})
    subir(client, png(800, 300, color=(255, 0, 0, 255)))
    comun = Image.open(io.BytesIO(client.get("/api/marca/icono/512.png").content))
    assert comun.mode == "RGBA" and comun.getpixel((0, 0))[3] == 0  # la esquina es del lienzo, no del logo

    for tipo in ("maskable", "apple"):
        icono = Image.open(io.BytesIO(client.get(f"/api/marca/icono/{tipo}.png").content))
        assert icono.getpixel((0, 0))[:3] == (0x33, 0x66, 0x99)  # el fondo es el color de la marca
        assert icono.mode in ("RGB", "RGBA") and (icono.mode == "RGB" or icono.getpixel((0, 0))[3] == 255)
    assert Image.open(io.BytesIO(client.get("/api/marca/icono/apple.png").content)).mode == "RGB"  # iOS: sin canal alfa


def test_el_maskable_deja_el_logo_dentro_de_la_zona_segura(client):
    subir(client, png(400, 400, color=(255, 0, 0, 255)))
    icono = Image.open(io.BytesIO(client.get("/api/marca/icono/maskable.png").content)).convert("RGB")
    fondo = (255, 255, 255)  # sin color de marca: blanco
    # El rojo (el logo) tiene que quedar adentro del 74 % central: 512 * 0,13 = 66 px de margen.
    for x, y in [(60, 256), (451, 256), (256, 60), (256, 451)]:
        assert icono.getpixel((x, y)) == fondo
    assert icono.getpixel((256, 256)) == (255, 0, 0)


def test_los_iconos_con_su_version_se_cachean_para_siempre(client):
    subir(client, png())
    manifest = client.get("/manifest.webmanifest").json()
    ruta = manifest["icons"][0]["src"]
    assert "?v=" in ruta and "immutable" in client.get(ruta).headers["cache-control"]
    assert client.get("/api/marca/icono/192.png").headers["cache-control"] == "no-cache"


def test_la_version_cambia_si_cambia_el_color(client):
    subir(client, png())
    antes = client.get("/manifest.webmanifest").json()["icons"][0]["src"]
    client.put("/api/marca", headers=ADMIN, json={"color": "#112233"})
    assert client.get("/manifest.webmanifest").json()["icons"][0]["src"] != antes


def test_sin_logo_o_con_un_tipo_desconocido_no_hay_icono(client):
    assert client.get("/api/marca/icono/192.png").status_code == 404
    subir(client, png())
    assert client.get("/api/marca/icono/64.png").status_code == 404


# ── Manifest ───────────────────────────────────────────────────────────────

def test_sin_marca_el_manifest_es_el_del_build_y_le_gana_al_catch_all(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    assert r.json() == MANIFEST_DEL_BUILD  # y no el HTML de la SPA


def test_con_marca_el_manifest_la_lleva_y_conserva_lo_del_producto(client):
    client.put("/api/marca", headers=ADMIN, json={"nombre": "Mi Sistema", "color": "#336699"})
    subir(client, png())
    m = client.get("/manifest.webmanifest").json()
    assert m["name"] == "Mi Sistema" and m["short_name"] == "Mi Sistema"
    assert m["theme_color"] == "#336699"
    assert m["start_url"] == "/" and m["display"] == "standalone"  # lo suyo, del producto
    assert [i["purpose"] for i in m["icons"]] == ["any", "any", "maskable"]
    assert all(i["src"].startswith("/api/marca/icono/") for i in m["icons"])


def test_con_marca_sin_logo_el_manifest_conserva_los_iconos_del_producto(client):
    client.put("/api/marca", headers=ADMIN, json={"nombre": "Mi Sistema"})
    m = client.get("/manifest.webmanifest").json()
    assert m["name"] == "Mi Sistema"
    assert m["icons"] == MANIFEST_DEL_BUILD["icons"]
    assert m["theme_color"] == "#111111"


def test_borrar_la_marca_devuelve_el_manifest_del_producto(client, app):
    import os

    client.put("/api/marca", headers=ADMIN, json={"nombre": "Mi Sistema"})
    subir(client, png())
    r = client.delete("/api/marca", headers=ADMIN)
    assert r.status_code == 200 and r.json()["propia"] is False
    assert client.get("/manifest.webmanifest").json() == MANIFEST_DEL_BUILD
    assert not os.path.exists(os.path.join(app.state.cm.MARCA_DIR, "logo.png"))
    assert "marca" not in app.state.cm.load()


def test_borrar_solo_el_logo_conserva_el_nombre(client):
    client.put("/api/marca", headers=ADMIN, json={"nombre": "Mi Sistema"})
    subir(client, png())
    r = client.delete("/api/marca/logo", headers=ADMIN)
    assert r.json()["nombre"] == "Mi Sistema" and r.json()["logo"] == ""
    assert client.get("/api/marca/logo").status_code == 404


def test_sin_dist_y_sin_marca_el_manifest_es_404(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as cm
    importlib.reload(cm)
    import libracore.marca_router as mr
    importlib.reload(mr)
    app = FastAPI()
    app.include_router(mr.build_marca_router())
    assert TestClient(app).get("/manifest.webmanifest").status_code == 404


def test_sin_dist_y_con_marca_el_manifest_es_uno_minimo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as cm
    importlib.reload(cm)
    import libracore.marca_router as mr
    importlib.reload(mr)
    cm.save({**cm.load(), "marca": {"nombre": "Solo Nombre"}})
    app = FastAPI()
    app.include_router(mr.build_marca_router())
    m = TestClient(app).get("/manifest.webmanifest").json()
    assert m["name"] == "Solo Nombre" and m["display"] == "standalone" and m["start_url"] == "/"


# ── Robustez ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("basura", ["texto", 5, ["a"], {"nombre": 5, "color": "rojo", "logo": "../../etc/passwd"}])
def test_un_config_json_con_basura_no_rompe_la_lectura(client, app, basura):
    cm = app.state.cm
    cm.save({**cm.load(), "marca": basura})
    r = client.get("/api/marca")
    assert r.status_code == 200
    assert r.json()["propia"] is False and r.json()["logo"] == ""
