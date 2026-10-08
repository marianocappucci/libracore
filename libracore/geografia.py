"""Provincias y localidades de Argentina, para que se elijan y no se tipeen.

Nace del pedido del cliente de [[libracargo]] (2026-08-20): *"que no se carguen
mal y sólo se seleccionen"*. Vive acá y no en el producto porque **los seis
manejan direcciones y ninguno tenía catálogo**: el maestro de localidades de
LibraCargo tenía 121 filas cargadas a mano, con `Gral Paz` y `Gral. Paz` como
dos localidades distintas, `Pto San Martín` y `Pto. San Martín` también, y
entradas como `Campo`, `Shap` o `(sin nombre)`.

## Es un archivo, no una tabla

El catálogo se **empaqueta con la librería** (`datos/argentina.json`, 204 KB) y
se lee en memoria. No es una tabla:

- **No hay migración que correr en seis productos.** Actualizar el catálogo es
  subir la versión de LibraCore, no coordinar seis `alembic upgrade`.
- **No puede divergir entre instancias.** Con una tabla por base, dos clientes
  del mismo producto terminan con catálogos distintos según cuándo se dieron de
  alta.
- LibraCore **no importa SQLAlchemy en runtime** —sólo la cadena de
  migraciones—, y una tabla lo obligaría.

La contracara: el catálogo es de **sólo lectura**. El maestro editable sigue
siendo del producto, y tiene que seguir siéndolo — hay lugares reales que no
están en ningún recurso oficial (Tomás Jofré, sin ir más lejos) y un desplegable
cerrado dejaría al operador sin poder cargar un viaje.

## Cómo se usa

    from libracore.geografia import build_geo_router, buscar, provincias

    app.include_router(build_geo_router(), dependencies=[Depends(require_staff)])

El gate lo pone el producto, igual que con `build_empresa_router`: el vocabulario
de roles no es el mismo en los seis y meterlo acá obligaría a este paquete a
conocerlos todos.

Para regenerar el archivo: `python scripts/generar_geografia.py`, que documenta
de dónde sale y por qué es `localidades-censales`.
"""
from __future__ import annotations

import json
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

_ARCHIVO = Path(__file__).resolve().parent / "datos" / "argentina.json"
#: El resto del Mercosur (Brasil, Chile, Paraguay, Bolivia, Uruguay), de GeoNames (CC-BY 4.0): ver
#: `scripts/generar_mercosur.py`.
_MERCOSUR = Path(__file__).resolve().parent / "datos" / "mercosur.json"

#: Tope de filas que devuelve el endpoint de localidades. El desplegable del
#: producto busca por teclado; traer las 4.027 de una es transferir 200 KB para
#: mostrar diez.
LIMITE_POR_OMISION = 50
LIMITE_MAXIMO = 5000


def normalizar(texto: str) -> str:
    """Minúsculas, sin acentos y sin puntuación de abreviatura.

    Es lo que hace comparables `Gral. Paz` y `Gral Paz`, que en el maestro de
    LibraCargo eran **dos localidades distintas**. No expande abreviaturas:
    `Gral. Paz` y `General Paz` siguen siendo dos cosas para esta función, y
    resolver eso es decisión de una persona, no de un `replace`.
    """
    plano = unicodedata.normalize("NFKD", texto)
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return " ".join(plano.lower().replace(".", " ").replace("-", " ").split())


@lru_cache(maxsize=1)
def _catalogo() -> dict[str, Any]:
    """Argentina (Georef, códigos censales) y el resto del Mercosur (GeoNames), con el país en cada fila."""
    crudo = json.loads(_ARCHIVO.read_text(encoding="utf-8"))
    nombres = {p["id"]: p["nombre"] for p in crudo["provincias"]}
    provincias_ = [{**p, "pais": "AR"} for p in crudo["provincias"]]
    localidades_ = [
        {"id": id_, "nombre": nombre, "provincia_id": prov, "provincia": nombres[prov], "pais": "AR"}
        for id_, nombre, prov in crudo["localidades"]
    ]
    paises_ = [{"id": "AR", "nombre": "Argentina"}]
    if _MERCOSUR.exists():
        mer = json.loads(_MERCOSUR.read_text(encoding="utf-8"))
        paises_ += mer["paises"]
        provincias_ += mer["provincias"]
        nombres.update({p["id"]: p["nombre"] for p in mer["provincias"]})
        localidades_ += [
            {"id": id_, "nombre": nombre, "provincia_id": prov, "provincia": nombres[prov], "pais": id_[:2]}
            for id_, nombre, prov in mer["localidades"]
        ]
    indice: dict[str, list[dict[str, Any]]] = {}
    for loc in localidades_:
        indice.setdefault(normalizar(loc["nombre"]), []).append(loc)
    return {"paises": paises_, "provincias": provincias_, "localidades": localidades_, "indice": indice,
            "por_id": {loc["id"]: loc for loc in localidades_}}


def _del_pais(filas: list[dict[str, Any]], pais: str | None) -> list[dict[str, Any]]:
    return filas if pais is None else [f for f in filas if f["pais"] == pais.upper()]


def paises() -> list[dict[str, str]]:
    """Argentina primero y después el resto del Mercosur: Brasil, Chile, Paraguay, Bolivia y Uruguay."""
    return list(_catalogo()["paises"])


def provincias(pais: str | None = "AR") -> list[dict[str, str]]:
    """Las provincias (o estados, regiones, departamentos) de `pais`, ordenadas por nombre. `None`: todas.

    Por omisión **Argentina**, que es lo que devolvía antes de sumarse el Mercosur: las 24, CABA incluida.
    """
    # El orden de los archivos: Argentina primero y, en cada país, por nombre (es como se generaron).
    return _del_pais(_catalogo()["provincias"], pais)


def localidades(provincia_id: str | None = None, q: str | None = None,
                limite: int | None = None, pais: str | None = "AR") -> list[dict[str, Any]]:
    """Filtradas por país (por omisión **Argentina**; `None` es todo el Mercosur, Argentina primero), provincia y
    texto, en ese orden.

    `q` matchea por **prefijo normalizado** y, si no encuentra nada, cae a
    "contiene". El prefijo primero porque quien escribe "mer" en un buscador
    espera Mercedes antes que Villa Ballester del Mercado: sin esa preferencia,
    el orden alfabético manda y lo que se busca queda abajo.
    """
    filas = _catalogo()["localidades"]
    if provincia_id:
        filas = [f for f in filas if f["provincia_id"] == provincia_id]
    else:
        filas = _del_pais(filas, pais)
    if q and q.strip():
        aguja = normalizar(q)
        empiezan = [f for f in filas if normalizar(f["nombre"]).startswith(aguja)]
        filas = empiezan or [f for f in filas if aguja in normalizar(f["nombre"])]
        filas = sorted(filas, key=lambda f: f["pais"] != "AR")
    return filas[: (limite if limite is not None else len(filas))]


def localidad(id_: str) -> dict[str, Any] | None:
    """La localidad del catálogo con ese id, de cualquier país, o `None`.

    El código censal de 8 dígitos en Argentina; `{PAÍS}-{geonameid}` en el resto del Mercosur. Es lo que guarda un
    producto para **vincular** su maestro editable con el catálogo: el nombre se puede escribir de muchas maneras,
    el id no.
    """
    return _catalogo()["por_id"].get((id_ or "").strip())


def buscar(nombre: str, provincia_id: str | None = None, pais: str | None = "AR") -> list[dict[str, Any]]:
    """Coincidencias **exactas** por nombre normalizado, en `pais` (por omisión Argentina; `None`: todos).

    Devuelve una lista y no un resultado porque un nombre puede estar en varias
    provincias: `San Pedro` está en ocho. Quien la llama decide qué hacer con
    eso — y "una sola coincidencia" es el criterio para completar la provincia
    de un dato viejo sin preguntarle a nadie.
    """
    encontradas = _catalogo()["indice"].get(normalizar(nombre), [])
    if provincia_id:
        return [f for f in encontradas if f["provincia_id"] == provincia_id]
    return _del_pais(list(encontradas), pais)


def build_geo_router(prefijo: str = "/api/geo") -> APIRouter:
    """El router de consulta del catálogo. Sólo lectura: no hay `POST`."""
    router = APIRouter(prefix=prefijo, tags=["geografia"])

    def _pais(pais: str) -> str | None:
        return None if pais.strip().lower() in ("todos", "*", "") else pais

    @router.get("/paises")
    def _paises() -> list[dict[str, str]]:
        return paises()

    @router.get("/provincias")
    def _provincias(pais: str = Query(default="AR", description="AR, BR, CL, PY, BO, UY o «todos»")
                    ) -> list[dict[str, str]]:
        return provincias(_pais(pais))

    @router.get("/localidades")
    def _localidades(
        provincia_id: str | None = None,
        q: str | None = Query(default=None, description="busca por nombre"),
        limite: int = Query(default=LIMITE_POR_OMISION, ge=1, le=LIMITE_MAXIMO),
        pais: str = Query(default="AR", description="AR, BR, CL, PY, BO, UY o «todos»"),
    ) -> list[dict[str, Any]]:
        return localidades(provincia_id, q, limite, _pais(pais))

    @router.get("/localidades/{id_}")
    def _localidad(id_: str) -> dict[str, Any]:
        encontrada = localidad(id_)
        if encontrada is None:
            raise HTTPException(404, f"no hay una localidad con id {id_!r} en el catálogo")
        return encontrada

    return router
