"""Las cabeceras de caché del frontend, que son lo que hace que un deploy se vea.

**Por qué existe este archivo.** Hasta el 2026-08-20 esta aplicación servía el
`index.html` **sin `Cache-Control`** — medido contra el dominio, no leído del
compose. Sin esa cabecera el navegador aplica caché heurística y puede servir la
copia guardada sin preguntar. Como Vite le pone un hash en el nombre a cada
bundle, el bundle viejo **sigue existiendo**: el `index.html` viejo lo pide y lo
recibe, con 200. No hay error en ninguna capa — ni un 404 que delate nada. Le
pasó a LibraCargo el 2026-08-19 con la pantalla de Backup.

Que la app de cada producto llame a `montar_spa` lo prueba cada producto (lee su `app/asgi.py`).

Los dos tests principales son las dos mitades del mismo arreglo, y ninguna sirve
sola:

- el archivo cuyo nombre NO cambia (`index.html`) revalida siempre;
- los que llevan el hash en el nombre se cachean para siempre, que es seguro
  **porque** el index revalida.

🔴 **Se prueba `montar_spa`, no se importa la app.** La primera versión de este
archivo reimportaba `app.asgi` con un `dist` presente, que era la única
forma de tener las rutas reales mientras el cableado vivía ahí. Eso reconstruye
la app entera en medio de la suite: vuelve a sembrar el usuario admin y deja en
`sys.modules` una instancia distinta de la que usan los demás tests. **106
errores ajenos en un producto y 176 en otro**, todos `invalid credentials`, con
este archivo pasando en verde aislado. Por eso el cableado se movió a
`app/spa.py` como función: la que llama producción es la que llama el test.
Desde el 2026-10-05 la función es del motor (`libracore.spa`, ADR-020) y este
archivo vino con ella desde VentaLibra.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore.spa import montar_spa

ASSET = "index-DELTEST123.js"


@pytest.fixture
def cliente(tmp_path):
    """Una `FastAPI` limpia con el cableado real y un `dist` de mentira.

    Sin efectos sobre el resto de la suite: no importa la app del producto, no
    toca la base y no siembra usuarios.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>ventalibra</title>", encoding="utf-8")
    (dist / "assets" / ASSET).write_text("console.log(1)", encoding="utf-8")
    (dist / "prueba-de-cache.txt").write_text("hola", encoding="utf-8")

    app = FastAPI()
    montar_spa(app, dist)
    return TestClient(app)


def test_el_index_revalida_siempre(cliente):
    """El archivo cuyo nombre no cambia no se puede cachear a ciegas.

    Es el único que dice cuál es el bundle de ahora. Si el navegador se queda
    con el viejo, pide el bundle viejo —que existe, porque el nombre lleva
    hash— y lo recibe con 200: el deploy no se ve y nada falla.
    """
    r = cliente.get("/")
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", ""), dict(r.headers)


def test_una_ruta_del_ruteo_del_cliente_tambien(cliente):
    """Cae en el mismo `index.html` por el catch-all, y trae lo mismo."""
    r = cliente.get("/ventas")
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", ""), dict(r.headers)


def test_los_assets_se_cachean_para_siempre(cliente):
    """La otra mitad: el nombre lleva el hash del contenido, así que el mismo
    nombre nunca cambia de contenido."""
    r = cliente.get(f"/assets/{ASSET}")
    assert r.status_code == 200
    cache = r.headers.get("cache-control", "")
    assert "immutable" in cache and "max-age=31536000" in cache, cache


def test_los_archivos_sueltos_del_dist_no_se_cachean(cliente):
    """Un favicon, un manifest o el `sw.js` no llevan hash: mismo criterio que
    el index."""
    r = cliente.get("/prueba-de-cache.txt")
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", ""), dict(r.headers)


def test_las_dos_politicas_son_distintas(cliente):
    """El control de los dos de arriba, juntos.

    Si algún día las dos rutas devolvieran la misma cabecera, uno de los dos
    tests seguiría pasando por el motivo equivocado y nadie se enteraría.
    """
    index = cliente.get("/").headers.get("cache-control", "")
    asset = cliente.get(f"/assets/{ASSET}").headers.get("cache-control", "")
    assert index and asset and index != asset, f"index={index!r} asset={asset!r}"

