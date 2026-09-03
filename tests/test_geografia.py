"""El catálogo de provincias y localidades.

Lo que más se prueba acá es que el archivo **esté adentro del paquete instalado**
y que las localidades que el negocio usa de verdad estén en él: un catálogo que
se ve completo y no tiene el pueblo al que va el camión es peor que no tenerlo,
porque el operador no puede cargar el viaje y no hay nada que explique por qué.
"""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore.geografia import (
    _ARCHIVO,
    LIMITE_POR_OMISION,
    build_geo_router,
    buscar,
    localidades,
    normalizar,
    provincias,
)

#: Localidades reales de la operación de Suitrans, que es de donde salió el
#: pedido. `Capilla del Señor` y `Morse` son las que **no están** en el recurso
#: `localidades` de Georef y sí en `localidades-censales`: si alguien cambiara
#: el recurso del generador, estas dos lo ponen en rojo.
DEL_NEGOCIO = ["Suipacha", "Chivilcoy", "Capilla del Señor", "Morse", "Mercedes"]


def test_el_archivo_viaja_con_el_paquete():
    """🔴 El modo de falla que esto cierra es de empaquetado, no de código.

    `hatchling` incluye lo que está adentro del paquete, pero un `.json` no es
    un `.py` y nadie se entera de que falta hasta que una instancia levanta y el
    desplegable sale vacío. Acá se lee la ruta real del módulo instalado.
    """
    assert _ARCHIVO.exists(), f"no esta el catalogo en {_ARCHIVO}"
    crudo = json.loads(_ARCHIVO.read_text(encoding="utf-8"))
    assert crudo["fuente"].startswith("Georef")


def test_las_24_provincias():
    filas = provincias()
    assert len(filas) == 24
    nombres = [p["nombre"] for p in filas]
    assert nombres == sorted(nombres)
    assert "Ciudad Autónoma de Buenos Aires" in nombres
    assert "Tierra del Fuego, Antártida e Islas del Atlántico Sur" in nombres


def test_estan_las_localidades_que_el_negocio_usa():
    for nombre in DEL_NEGOCIO:
        assert buscar(nombre), f"falta {nombre} en el catalogo"


def test_normalizar_hace_comparables_las_abreviaturas_con_punto():
    """`Gral Paz` y `Gral. Paz` eran dos localidades distintas en el maestro."""
    assert normalizar("Gral. Paz") == normalizar("Gral Paz")
    assert normalizar("  José  C.  Paz ") == normalizar("Jose C Paz")
    # Control: no expande abreviaturas. Resolver eso es decision de una persona.
    assert normalizar("Gral. Paz") != normalizar("General Paz")


def test_un_nombre_puede_estar_en_varias_provincias():
    """Es la razon por la que `buscar` devuelve una lista y no un resultado."""
    provincias_de_san_pedro = {f["provincia"] for f in buscar("San Pedro")}
    assert len(provincias_de_san_pedro) > 1
    # Y con la provincia, una sola.
    assert len(buscar("San Pedro", provincia_id="06")) == 1


def test_el_filtro_por_provincia_no_deja_pasar_otras():
    filas = localidades(provincia_id="06")
    assert filas, "Buenos Aires no puede estar vacia"
    assert {f["provincia_id"] for f in filas} == {"06"}


def test_la_busqueda_prefiere_el_prefijo_al_contiene():
    """Quien escribe "mer" espera Mercedes primero."""
    filas = localidades(q="mer", limite=5)
    assert filas
    assert all(normalizar(f["nombre"]).startswith("mer") for f in filas)
    # Y si nada empieza asi, cae a "contiene" en vez de devolver vacio.
    assert localidades(q="uipach"), "el fallback a contiene no anda"


def test_la_busqueda_ignora_acentos_y_mayusculas():
    assert buscar("CAPILLA DEL SENOR")
    assert buscar("capilla del señor")


def test_una_busqueda_que_no_existe_da_vacio():
    """Control negativo: sin esto, los tests de arriba pasarian aunque la
    busqueda devolviera siempre todo."""
    assert localidades(q="zzqqxx") == []
    assert buscar("Localidad Que No Existe") == []


def test_el_router_es_de_solo_lectura():
    app = FastAPI()
    app.include_router(build_geo_router())
    cliente = TestClient(app)

    r = cliente.get("/api/geo/provincias")
    assert r.status_code == 200
    assert len(r.json()) == 24

    r = cliente.get("/api/geo/localidades?provincia_id=06&q=suipacha")
    assert r.status_code == 200
    assert r.json()[0]["nombre"] == "Suipacha"
    assert r.json()[0]["provincia"] == "Buenos Aires"

    # El tope existe: son 4.027 y traerlas todas es transferir 200 KB para
    # mostrar diez.
    assert len(cliente.get("/api/geo/localidades").json()) == LIMITE_POR_OMISION

    # No hay forma de escribirle al catalogo.
    assert cliente.post("/api/geo/localidades", json={"nombre": "X"}).status_code == 405
