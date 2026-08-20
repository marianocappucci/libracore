#!/usr/bin/env python3
"""Regenera `libracore/datos/argentina.json` desde la API oficial de Georef.

    python scripts/generar_geografia.py

Fuente: **Georef**, del Ministerio del Interior, publicada en datos.gob.ar
(https://apis.datos.gob.ar/georef/api/). Es el servicio oficial de
normalización de datos geográficos de Argentina.

## Por qué `localidades-censales` y no `localidades`

Los dos rondan las 4.030 filas y **no son el mismo conjunto**. Medido el
2026-08-20 contra las 121 localidades cargadas a mano en la instancia de
Suitrans: el recurso `localidades` **no tiene Capilla del Señor ni Morse**, que
son pueblos reales a los que esa agencia lleva carga. `localidades-censales`
—la unidad del censo del INDEC— sí los tiene.

`asentamientos` (14.673) es un superconjunto que llega hasta el paraje, pero
mete entradas como "Km 12" que ensucian un desplegable. La decisión es
`localidades-censales` **con el maestro del producto editable**, para las
excepciones: hay lugares reales que no están en ningún recurso —Tomás Jofré,
por ejemplo— y un catálogo cerrado dejaría al operador sin poder cargarlos.

## Por qué se pagina por provincia

La API corta con `max + inicio <= 10000`, así que una sola pasada global no
alcanza para los recursos grandes. Pidiendo por provincia, ninguna llega al
tope.
"""
from __future__ import annotations

import json
import os
import urllib.request

API = "https://apis.datos.gob.ar/georef/api/"
DESTINO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "libracore", "datos", "argentina.json")
#: 24: 23 provincias + CABA. Está fijo a propósito — si la API devolviera otra
#: cantidad, algo cambió y hay que mirarlo antes de estampar el archivo.
PROVINCIAS_ESPERADAS = 24


def traer(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=90) as respuesta:
        return json.load(respuesta)


def main() -> None:
    provincias = traer(f"{API}provincias?campos=id,nombre&max=30")["provincias"]
    if len(provincias) != PROVINCIAS_ESPERADAS:
        raise SystemExit(f"la API devolvio {len(provincias)} provincias, esperaba "
                         f"{PROVINCIAS_ESPERADAS}")
    provincias.sort(key=lambda p: p["nombre"])

    localidades = []
    for provincia in provincias:
        inicio = 0
        while True:
            pagina = traer(f"{API}localidades-censales?provincia={provincia['id']}"
                           f"&campos=id,nombre&max=5000&inicio={inicio}")
            localidades.extend(
                [fila["id"], fila["nombre"], provincia["id"]]
                for fila in pagina["localidades_censales"]
            )
            inicio += len(pagina["localidades_censales"])
            if inicio >= pagina["total"] or not pagina["localidades_censales"]:
                break

    total = traer(f"{API}localidades-censales?max=1")["total"]
    if len(localidades) != total:
        raise SystemExit(f"baje {len(localidades)} localidades y la API dice {total}")

    localidades.sort(key=lambda fila: (fila[2], fila[1]))
    contenido = {
        "fuente": "Georef (datos.gob.ar) — recurso localidades-censales",
        "url": API,
        "provincias": provincias,
        # Tripla `[id, nombre, provincia_id]` y no un objeto por fila: son 4.027
        # y repetir tres claves en cada una triplica el archivo sin agregar nada.
        "localidades": localidades,
    }
    with open(DESTINO, "w", encoding="utf-8") as archivo:
        json.dump(contenido, archivo, ensure_ascii=False, indent=1)
        archivo.write("\n")
    print(f"{len(provincias)} provincias y {len(localidades)} localidades -> {DESTINO}")


if __name__ == "__main__":
    main()
