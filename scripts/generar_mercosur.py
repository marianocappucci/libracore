#!/usr/bin/env python3
"""Genera `libracore/datos/mercosur.json` desde GeoNames: los lugares poblados de Brasil, Chile, Paraguay, Bolivia y
Uruguay.

    python scripts/generar_mercosur.py RUTA/cities1000.txt RUTA/admin1CodesASCII.txt

Fuente: **GeoNames** (https://www.geonames.org), volcados `cities1000` (lugares de más de 1.000 habitantes) y
`admin1CodesASCII` (estados, regiones y departamentos), descargables de https://download.geonames.org/export/dump/.
**Licencia CC-BY 4.0: hay que citar la fuente**, y el archivo generado la lleva en `fuente` y `licencia`.

## Por qué GeoNames y `cities1000`

No hay un equivalente de Georef (el catálogo oficial argentino) que cubra los cinco países con un mismo formato.
GeoNames sí, y es la referencia abierta más usada. `cities1000` deja afuera los caseríos (medido el 2026-10-08:
6.621 lugares; Brasil 5.882, Chile 305, Paraguay 151, Bolivia 148, Uruguay 135). Para un lugar que no esté —un
puerto chico, una planta— el producto tiene su maestro editable y los parajes, igual que en Argentina.

## Ids

`{PAÍS}-{geonameid}` para las localidades y `{PAÍS}-{código admin1}` para la división de primer nivel, para que
nunca choquen con los códigos censales argentinos (8 dígitos).
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date

PAISES = {"BR": "Brasil", "CL": "Chile", "PY": "Paraguay", "BO": "Bolivia", "UY": "Uruguay"}
DESTINO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "libracore", "datos", "mercosur.json")


#: GeoNames nombra las divisiones en inglés («Beni Department», «Biobío Region»): el sufijo sobra en castellano.
_SUFIJOS = (" Department", " Region", " Province", " State")


def _sin_sufijo(nombre: str) -> str:
    for sufijo in _SUFIJOS:
        if nombre.endswith(sufijo):
            return nombre[: -len(sufijo)]
    return nombre


def main(ciudades: str, admin1: str) -> None:
    divisiones: dict[str, str] = {}
    with open(admin1, encoding="utf-8") as f:
        for fila in csv.reader(f, delimiter="\t"):
            pais, _, codigo = fila[0].partition(".")
            if pais in PAISES:
                divisiones[f"{pais}-{codigo}"] = _sin_sufijo(fila[1])
    localidades, usadas = [], set()
    with open(ciudades, encoding="utf-8") as f:
        for fila in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            geonameid, nombre, pais, admin = fila[0], fila[1], fila[8], fila[10]
            if pais not in PAISES:
                continue
            division = f"{pais}-{admin}"
            if division not in divisiones:
                continue
            localidades.append([f"{pais}-{geonameid}", nombre, division])
            usadas.add(division)
    localidades.sort(key=lambda x: (x[0][:2], x[1]))
    provincias = sorted(({"id": k, "nombre": v, "pais": k[:2]} for k, v in divisiones.items() if k in usadas),
                        key=lambda p: (p["pais"], p["nombre"]))
    datos = {
        "fuente": "GeoNames (cities1000 y admin1CodesASCII), https://www.geonames.org",
        "licencia": "CC-BY 4.0 (https://creativecommons.org/licenses/by/4.0/)",
        "generado": date.today().isoformat(),
        "paises": [{"id": k, "nombre": v} for k, v in PAISES.items()],
        "provincias": provincias,
        "localidades": localidades,
    }
    with open(DESTINO, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, separators=(",", ":"))
    por_pais = {p: sum(1 for x in localidades if x[0].startswith(p)) for p in PAISES}
    print(f"{len(localidades)} localidades, {len(provincias)} divisiones: {por_pais}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
