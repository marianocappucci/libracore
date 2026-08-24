#!/usr/bin/env python3
"""Regenera `libracore/datos/feriados.json` desde la API de ArgentinaDatos.

    python scripts/generar_feriados.py

Fuente: **ArgentinaDatos** (https://api.argentinadatos.com/v1/feriados/{año}),
pública, sin API key y con licencia MIT. **No es oficial** — el proyecto declara
a La Nación como fuente del calendario. Es la mejor disponible: el relevamiento
del 2026-08-24 está en el wiki (`feriados-y-horario-de-negocio-familia-libra`) y
descartó a las otras con motivos medidos.

## Por qué ésta y no las otras

- `nolaborables.com.ar`, la que más aparece al buscar, **ya no resuelve DNS** y
  su repo anuncia baja de servicio. Está muerta.
- `date.nager.at` responde bien pero devuelve **16 feriados para 2026 contra los
  19 de ArgentinaDatos**: le faltan exactamente los tres `puente`. Para una
  agenda eso es lo peor que puede faltar, porque el puente es justo el día que
  el negocio cierra y nadie recuerda.

## Por qué un archivo empaquetado y no una consulta en runtime

Mismo criterio que `generar_geografia.py`, y por las mismas tres razones: no hay
migración que correr en seis productos, el catálogo no puede divergir entre
instancias, y **LibraCore no importa SQLAlchemy en runtime**. Se suma una cuarta
que la geografía no tiene: una instancia sin salida a internet —o con la API
caída— quedaría sin feriados **y sin forma de notarlo**, que es peor que tenerlos
viejos.

La contracara es real y hay que decirla: los feriados **cambian todos los años**.
Por eso el módulo publica qué años cubre y falla fuerte fuera de esa ventana, en
vez de contestar "no es feriado" para todo 2029.

## 🔴 El año que viene está incompleto a propósito de la realidad

Los **puentes turísticos los decreta el Ejecutivo año por año**. Medido el
2026-08-24: 2026 traía 19 feriados (12 inamovibles, 4 trasladables, 3 puentes) y
2027 traía 16 **con cero puentes**. No es un bug de la API ni de este script: esos
días todavía no existen. Entonces regenerar una vez y olvidarse deja al negocio
abierto tres días que va a estar cerrado — **hay que regenerar el año en curso**,
no sólo sembrar el siguiente.

Y no son sólo los puentes: `tipo: "trasladable"` es la **clase** del feriado, no
su estado. La fecha que devuelve la API es la efectiva del momento, y para los
años futuros los trasladables siguen en su fecha original hasta que el traslado
se decreta. En la misma medición, los cuatro trasladables de 2026 caían lunes y
los cuatro de 2027 seguían en su fecha original.

## La ventana de años se descubre, no se declara

La API devuelve 404 con `{"error": "Not found"}` fuera de su cobertura, no una
lista vacía. El script sondea año por año alrededor del actual y estampa los que
contestaron. Al 2026-08-24 la ventana era **2021–2027**.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.request

API = "https://api.argentinadatos.com/v1/feriados/"
DESTINO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "libracore", "datos", "feriados.json")

#: Cuántos años sondear para cada lado del actual. Con 8 sobra: la ventana
#: medida era de 7 años y el sondeo es barato (un GET por año).
MARGEN = 8

#: Piso de cordura por año. El conjunto fijo de feriados nacionales no baja de
#: 15 en ningún año medido; si un año contestara con menos, la API cambió de
#: forma y hay que mirarlo **antes** de estampar el archivo. Está fijo a
#: propósito, igual que las 24 provincias del generador de geografía.
MINIMO_POR_ANIO = 15

#: Los tipos que la API usa hoy. Uno nuevo no es un error —el script lo estampa
#: igual— pero se avisa, porque el consumidor puede querer tratarlo distinto.
TIPOS_CONOCIDOS = {"inamovible", "trasladable", "puente"}


#: 🔴 **Sin User-Agent propio la API devuelve 403.** El de `urllib`
#: (`Python-urllib/3.x`) está bloqueado; con `curl` la misma URL contesta 200,
#: que es lo que hace que el endpoint parezca sano cuando se lo prueba a mano y
#: falle desde acá. No es autenticación: no hay API key en ningún lado.
CABECERAS = {"User-Agent": "libracore-generar-feriados/1.0 (+libra)"}


def _pedir(anio: int) -> list[dict] | None:
    """Los feriados de un año, o `None` si la API no lo cubre."""
    pedido = urllib.request.Request(f"{API}{anio}", headers=CABECERAS)
    try:
        with urllib.request.urlopen(pedido, timeout=30) as respuesta:
            return json.loads(respuesta.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code == 403:
            raise SystemExit(
                f"403 en {API}{anio}: la API rechazo el User-Agent. Ver CABECERAS."
            ) from exc
        raise


def main() -> None:
    hoy = dt.date.today()
    por_anio: dict[str, list[list[str]]] = {}
    tipos_nuevos: set[str] = set()

    for anio in range(hoy.year - MARGEN, hoy.year + MARGEN + 1):
        crudo = _pedir(anio)
        if crudo is None:
            continue
        if len(crudo) < MINIMO_POR_ANIO:
            raise SystemExit(
                f"{anio} devolvio {len(crudo)} feriados, menos que el piso de "
                f"{MINIMO_POR_ANIO}. Mirar la API antes de estampar el archivo."
            )
        tipos_nuevos |= {f["tipo"] for f in crudo} - TIPOS_CONOCIDOS
        por_anio[str(anio)] = sorted(
            [f["fecha"], f["tipo"], f["nombre"]] for f in crudo
        )
        print(f"  {anio}: {len(crudo)} feriados")

    if str(hoy.year) not in por_anio:
        raise SystemExit(
            f"la API no devolvio el anio en curso ({hoy.year}). Sin eso el "
            f"archivo no sirve para nada y no se estampa."
        )
    if tipos_nuevos:
        print(f"  AVISO: tipos que este script no conocia: {sorted(tipos_nuevos)}")

    anios = sorted(int(a) for a in por_anio)
    salida = {
        "fuente": "ArgentinaDatos (https://api.argentinadatos.com/v1/feriados/)",
        "generado": hoy.isoformat(),
        "anios": anios,
        "por_anio": por_anio,
    }
    destino = os.path.abspath(DESTINO)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "w", encoding="utf-8") as archivo:
        json.dump(salida, archivo, ensure_ascii=False, indent=1, sort_keys=True)
        archivo.write("\n")
    total = sum(len(v) for v in por_anio.values())
    print(f"{destino}: {total} feriados, anios {anios[0]}-{anios[-1]}")


if __name__ == "__main__":
    main()
