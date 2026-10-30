"""El `index.html` sale con la marca de la instancia (marca blanca, PR 15 del piloto; libracore ADR-095).

Sin esto, la pestaña dice el nombre del producto hasta que corre el JavaScript, y un enlace compartido (WhatsApp, redes) —que lee este
HTML sin ejecutarlo— muestra el del producto. Se mide por el catch-all real de `montar_spa`, sobre un `dist` de mentira.
"""
import importlib
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from libracore.spa import SIN_CACHE

INDEX = """<!doctype html>
<html lang="es">
  <head>
    <meta charset="UTF-8" />
    <link rel="icon" type="image/png" href="/icons/icon-192.png?v=abc" />
    <link rel="apple-touch-icon" sizes="180x180" href="/icons/icon-apple-180.png?v=def" />
    <link rel="manifest" href="/manifest.webmanifest" />
    <meta name="theme-color" content="#f39200" />
    <title>VentaLibra</title>
  </head>
  <body><div id="root"></div></body>
</html>
"""
ADMIN = {"x-rol": "admin"}


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
    import libracore.marca_router as mr

    importlib.reload(mr)
    import libracore.spa as spa

    importlib.reload(spa)  # toma el `marca_router` recargado
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX, encoding="utf-8")

    from fastapi import Depends, Header, HTTPException

    def gate(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    app = FastAPI()
    app.include_router(mr.build_marca_router(dist=dist))
    app.include_router(mr.build_marca_admin_router(), dependencies=[Depends(gate)])
    spa.montar_spa(app, dist)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _marca(client, **cuerpo):
    r = client.put("/api/marca", json=cuerpo, headers=ADMIN)
    assert r.status_code == 200, r.text


def _logo(client):
    r = client.post("/api/marca/logo", files={"logo": ("logo.png", png(), "image/png")}, headers=ADMIN)
    assert r.status_code == 200, r.text


def test_sin_marca_propia_es_el_archivo_tal_cual(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.text == INDEX
    assert r.headers["cache-control"] == SIN_CACHE


def test_con_nombre_y_color_cambia_el_titulo_y_el_theme_color(client):
    _marca(client, nombre="Clínica Modelo", color="#2563eb")
    html = client.get("/").text
    assert "<title>Clínica Modelo</title>" in html
    assert "VentaLibra" not in html
    assert '<meta name="theme-color" content="#2563eb" />' in html
    assert "#f39200" not in html
    # Sin logo, el favicon y el ícono de iOS quedan los del producto.
    assert 'href="/icons/icon-192.png?v=abc"' in html


def test_con_logo_el_favicon_y_el_apple_touch_icon_son_los_de_la_marca(client):
    _marca(client, nombre="Clínica Modelo")
    _logo(client)
    html = client.get("/").text
    assert 'rel="icon" type="image/png" href="/api/marca/icono/192.png?v=' in html
    assert 'rel="apple-touch-icon" sizes="180x180" href="/api/marca/icono/apple.png?v=' in html
    assert "/icons/icon-192.png" not in html and "/icons/icon-apple-180.png" not in html
    # Una sola etiqueta de cada una, y el manifest sigue ahí.
    assert html.count('rel="icon"') == 1 and html.count('rel="apple-touch-icon"') == 1
    assert 'rel="manifest"' in html
    # El ícono al que apunta existe.
    import re

    href = re.search(r'rel="icon"[^>]*href="([^"]+)"', html).group(1)
    assert client.get(href).status_code == 200


def test_el_nombre_se_escapa(client):
    _marca(client, nombre="A </title><script>x</script>")
    html = client.get("/").text
    assert "<script>x</script>" not in html
    assert "&lt;/title&gt;" in html


def test_una_barra_invertida_en_el_nombre_no_se_interpreta_como_plantilla(client):
    _marca(client, nombre=r"Sur \1 \g<0>")
    assert r"<title>Sur \1 \g&lt;0&gt;</title>" in client.get("/").text


def test_un_index_sin_las_etiquetas_las_recibe_al_final_del_head(tmp_path, app, client):
    (tmp_path / "dist" / "index.html").write_text("<html><head><title>X</title></head><body></body></html>", encoding="utf-8")
    _marca(client, nombre="Clínica Modelo", color="#2563eb")
    _logo(client)
    html = client.get("/").text
    assert '<meta name="theme-color" content="#2563eb" />' in html
    assert 'rel="icon"' in html and 'rel="apple-touch-icon"' in html
    assert html.index('rel="icon"') < html.index("</head>")


def test_head_y_las_rutas_de_la_spa_llevan_la_marca_y_la_api_no_cambia(client):
    _marca(client, nombre="Clínica Modelo")
    assert "<title>Clínica Modelo</title>" in client.get("/pantalla/3").text
    cabecera = client.head("/pantalla/3")
    assert cabecera.status_code == 200 and cabecera.content == b""
    assert cabecera.headers["cache-control"] == SIN_CACHE
    assert client.get("/api/inexistente").status_code == 404


def test_al_borrar_la_marca_el_index_vuelve_a_ser_el_del_producto(client):
    _marca(client, nombre="Clínica Modelo")
    assert client.delete("/api/marca", headers=ADMIN).status_code == 200
    assert client.get("/").text == INDEX


def test_el_camino_caliente_no_toca_el_almacen_de_secretos(app, client, tmp_path):
    """🔴 `config_manager.load()` con almacén enchufado hace una consulta a la base por clave secreta. El `index.html` y el gate de Términos
    se piden en cada navegación y en cada pedido gateado: no pueden depender de la base (el 2026-10-30 dos productos cayeron en el CI por
    esto: `relation "secretos_instancia" does not exist`)."""
    from libracore import config_manager, marca_router, terminos_propios

    class Almacen:
        consultas = 0

        def get(self, clave):
            Almacen.consultas += 1
            raise RuntimeError("la base no está")

        def set(self, clave, valor):  # pragma: no cover
            raise RuntimeError("la base no está")

    _marca(client, nombre="Clínica Modelo")
    config_manager.usar_almacen_de_secretos(Almacen())
    try:
        assert "<title>Clínica Modelo</title>" in client.get("/").text
        assert client.get("/api/marca").json()["nombre"] == "Clínica Modelo"
        assert marca_router.es_marca_propia() is True
        assert terminos_propios.leer_terminos_propios() is None
        assert Almacen.consultas == 0
    finally:
        config_manager.usar_almacen_de_secretos(None)
