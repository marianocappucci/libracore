"""Bajar los índices de ajuste de la fuente oficial y guardarlos en `indices_ajuste` (ADR-085).

Dos fuentes, medidas el 2026-10-26 (detalle en `docs/indices-de-ajuste.md`):

- **BCRA, API de estadísticas v4** (`/estadisticas/v4.0/monetarias/{idVariable}`): ICL (40), CER (30), UVA (31),
  dólar A 3500 (5) y la inflación mensual (27). 🔴 **La v3 está dada de baja**: contesta 410 «Método correspondiente
  a la v3 ha sido deprecado». Los valores vienen como número JSON con once decimales: se leen con
  `parse_float=Decimal`, nunca como `float`. Los mensuales vienen fechados el último día del mes.
- **datos.gob.ar, API de series de tiempo** (`/series/api/series/`): el nivel del IPC del INDEC. Llega con más
  demora que la variación del BCRA (el 2026-10-26 tenía hasta agosto). Fechado el primer día del mes.

🔑 **Una descarga no pisa lo cargado a mano** (`guardar(..., pisar_manual=False)`): si difiere, lo informa en
`manuales_distintos` para que una persona decida. Y **una fuente caída no frena a las otras**: `sincronizar_todos`
junta los errores por índice.

`obtener` es inyectable (`(url, params) -> dict`) para que los tests no salgan a la red. Lo que corre esto todos los
días (el cron de la instancia, un botón) es del producto: el motor da la función y el comando
`python -m libracore.indices_descarga`.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

from libracore import indices_ajuste as ia
from libracore.db import indices_ajuste as db
from libracore.fechas import hoy as _hoy

logger = logging.getLogger(__name__)

URL_BCRA = "https://api.bcra.gob.ar/estadisticas/v4.0/monetarias/{serie}"
URL_DATOS_GOB_AR = "https://apis.datos.gob.ar/series/api/series/"

#: Cuántos valores pide cada página al BCRA. Medido: con `limit=3000` devolvió los 1.030 del ICL desde 2024 de una vez.
_PAGINA_BCRA = 3000

#: Sin `desde`, se piden los últimos 90 días: alcanza para cubrir una semana sin correr y un IPC con dos meses de
#: demora, y no baja cinco años de ICL todos los días.
_DIAS_POR_DEFECTO = 90

Obtener = Callable[[str, dict], dict]


class FuenteNoDisponible(RuntimeError):
    """La fuente no contestó o contestó algo que no se entiende. El mensaje dice qué."""


def _obtener_http(url: str, params: dict) -> dict:
    import httpx

    try:
        r = httpx.get(url, params=params, timeout=30)
    except httpx.HTTPError as exc:
        raise FuenteNoDisponible(f"No se pudo consultar {url}: {exc.__class__.__name__}") from exc
    if r.status_code != 200:
        raise FuenteNoDisponible(f"{url} contestó {r.status_code}: {r.text[:200]}")
    try:
        return json.loads(r.text, parse_float=Decimal)
    except ValueError as exc:
        raise FuenteNoDisponible(f"{url} no devolvió JSON.") from exc


def _leer_bcra(serie: int, desde: str, hasta: str, obtener: Obtener) -> list[tuple[str, Decimal]]:
    valores, offset = [], 0
    while True:
        d = obtener(URL_BCRA.format(serie=serie),
                    {"desde": desde, "hasta": hasta, "limit": _PAGINA_BCRA, "offset": offset})
        try:
            resultados = d.get("results") or []
            detalle = resultados[0]["detalle"] if resultados else []
            valores += [(str(x["fecha"]), Decimal(str(x["valor"]))) for x in detalle]
            total = int(d.get("metadata", {}).get("resultset", {}).get("count", len(valores)))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise FuenteNoDisponible(f"La respuesta del BCRA para la serie {serie} no tiene la forma esperada.") from exc
        offset += len(detalle)
        if not detalle or offset >= total:
            return valores


def _leer_datos_gob_ar(serie: str, desde: str, hasta: str, obtener: Obtener) -> list[tuple[str, Decimal]]:
    d = obtener(URL_DATOS_GOB_AR, {"ids": serie, "start_date": desde, "end_date": hasta, "format": "json",
                                   "limit": 1000})
    try:
        return [(str(f), Decimal(str(v))) for f, v in d["data"] if v is not None]
    except (KeyError, TypeError, ValueError) as exc:
        raise FuenteNoDisponible(f"La respuesta de datos.gob.ar para {serie} no tiene la forma esperada.") from exc


_LECTORES = {ia.FUENTE_BCRA: _leer_bcra, ia.FUENTE_DATOS_GOB_AR: _leer_datos_gob_ar}

#: Los índices que se bajan solos. `casa_propia` no está: se carga a mano.
DESCARGABLES = tuple(c for c, i in ia.INDICES.items() if i.fuente in _LECTORES)


def sincronizar(clave: str, *, desde: date | str | None = None, hasta: date | str | None = None,
                obtener: Obtener | None = None, conn=None) -> dict:
    """Baja `clave` entre `desde` y `hasta` y la guarda. Devuelve qué pasó con cada valor.

    `{indice, fuente, desde, hasta, leidos, insertados, actualizados, iguales, manuales_distintos}`, donde
    `manuales_distintos` es la lista de `{fecha, manual, publicado}` que se dejaron como estaban."""
    ind = ia.indice(clave)
    if ind.fuente not in _LECTORES:
        raise ValueError(f"{ind.nombre} no se baja de ninguna fuente: se carga a mano.")
    hasta_d = date.fromisoformat(str(hasta)[:10]) if hasta else _hoy()
    desde_d = date.fromisoformat(str(desde)[:10]) if desde else hasta_d - timedelta(days=_DIAS_POR_DEFECTO)
    leidos = _LECTORES[ind.fuente](ind.serie, desde_d.isoformat(), hasta_d.isoformat(), obtener or _obtener_http)

    cuenta = {"insertado": 0, "actualizado": 0, "igual": 0}
    manuales = []
    for fecha, valor in leidos:
        f = ia.fecha_del_indice(clave, fecha)
        r = db.guardar(clave, f, valor, ind.fuente, pisar_manual=False, conn=conn)
        if r == "manual_distinto":
            manuales.append({"fecha": f, "manual": db.obtener(clave, f, conn=conn)["valor"], "publicado": valor})
        else:
            cuenta[r] += 1
    return {"indice": clave, "fuente": ind.fuente, "desde": desde_d.isoformat(), "hasta": hasta_d.isoformat(),
            "leidos": len(leidos), "insertados": cuenta["insertado"], "actualizados": cuenta["actualizado"],
            "iguales": cuenta["igual"], "manuales_distintos": manuales}


def sincronizar_todos(*, desde: date | str | None = None, hasta: date | str | None = None,
                      obtener: Obtener | None = None, conn=None) -> dict:
    """`sincronizar` de cada índice descargable. Una fuente que falla queda en `errores` y no corta a las demás."""
    resultados, errores = {}, {}
    for clave in DESCARGABLES:
        try:
            resultados[clave] = sincronizar(clave, desde=desde, hasta=hasta, obtener=obtener, conn=conn)
        except FuenteNoDisponible as exc:
            logger.warning("No se pudo bajar %s: %s", clave, exc)
            errores[clave] = str(exc)
    return {"resultados": resultados, "errores": errores}


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m libracore.indices_descarga",
                                description="Baja los índices de ajuste de la fuente oficial.")
    p.add_argument("indices", nargs="*", help=f"Cuáles (por defecto, todos): {', '.join(DESCARGABLES)}")
    p.add_argument("--desde", help="AAAA-MM-DD (por defecto, 90 días antes de --hasta)")
    p.add_argument("--hasta", help="AAAA-MM-DD (por defecto, hoy)")
    a = p.parse_args(argv)
    if a.indices:
        salida = {"resultados": {}, "errores": {}}
        for clave in a.indices:
            try:
                salida["resultados"][clave] = sincronizar(clave, desde=a.desde, hasta=a.hasta)
            except FuenteNoDisponible as exc:
                salida["errores"][clave] = str(exc)
    else:
        salida = sincronizar_todos(desde=a.desde, hasta=a.hasta)
    print(json.dumps(salida, ensure_ascii=False, indent=2, default=str))
    return 1 if salida["errores"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
