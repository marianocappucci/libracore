"""Feriados nacionales de Argentina, para que la agenda sepa qué días cierra.

Nace del relevamiento del 2026-08-24 (wiki:
`feriados-y-horario-de-negocio-familia-libra`). Vive acá y no en cada producto
por el mismo motivo que la geografía: **los seis tienen calendario y ninguno
tenía feriados**. [[libragenda]] modela el feriado por sucursal desde hace
meses, pero la tabla se llena a mano, feriado por feriado y sucursal por
sucursal.

## Es un archivo, no una tabla ni una llamada en vivo

El catálogo se **empaqueta con la librería** (`datos/feriados.json`) y se lee en
memoria, igual que `geografia.py` y por las mismas tres razones: no hay
migración que correr en seis productos, no puede divergir entre instancias, y
LibraCore no importa SQLAlchemy en runtime.

Y hay una cuarta, propia de este dato: **consultarlo en vivo lo haría fallar
justo cuando importa**. Una instancia sin salida a internet, o un día en que la
API no contesta, se quedaría sin feriados **y sin nada que lo indique** — la
agenda abriría el 25 de diciembre y nadie vería un error. Un archivo viejo es un
problema visible; una lista vacía silenciosa, no.

## 🔴 La ventana de años, y por qué esto no devuelve `None` fuera de ella

Los feriados **cambian todos los años**, y encima el año siguiente está
incompleto: los **puentes turísticos los decreta el Ejecutivo año por año**. Al
generar este archivo el 2026-08-24, 2026 tenía 19 feriados y 2027 tenía 16, con
**cero puentes** — los tres puentes de 2026 no existían todavía en 2027.

Por eso preguntar por un año fuera de la ventana **levanta `FueraDeCobertura`** en
vez de contestar que no es feriado. Un `False` silencioso para todo 2029 es
indistinguible de "ese año no tiene feriados", y el consumidor —una agenda— no
tiene forma de notar la diferencia hasta que abre un 1° de mayo.

    if cubre(dia.year):
        feriado = es_feriado(dia)

## Lo que este catálogo NO trae

Sólo **nacionales**. Los provinciales y municipales no están en ninguna API
pública, y los días no laborables religiosos (Pesaj, Rosh Hashaná, Yom Kipur,
Año Nuevo Islámico) tampoco. Tampoco el cierre propio del negocio: vacaciones,
inventario, mudanza.

**El feed propone, no dispone.** Lo que se importe desde acá tiene que quedar
editable en el producto, y la excepción puntual del recurso le sigue ganando al
feriado — que es exactamente lo que [[libragenda]] ya decide.

## Cómo se usa

    from libracore.feriados import build_feriados_router, cubre, es_feriado

    app.include_router(build_feriados_router(), dependencies=[Depends(require_staff)])

El gate lo pone el producto, igual que con `build_geo_router`.

Para regenerar el archivo: `python scripts/generar_feriados.py`, que documenta
de dónde sale y por qué esa fuente y no las otras.
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

_ARCHIVO = Path(__file__).with_name("datos") / "feriados.json"


class FueraDeCobertura(LookupError):
    """Se preguntó por un año que el archivo empaquetado no tiene.

    No es un error del que pregunta: es que este catálogo se estampa con una
    ventana de años y hay que regenerarlo. Se levanta en vez de devolver "no es
    feriado" porque esa respuesta sería indistinguible de la verdad.
    """


@lru_cache(maxsize=1)
def _catalogo() -> dict[str, Any]:
    crudo = json.loads(_ARCHIVO.read_text(encoding="utf-8"))
    por_anio = {
        int(anio): [
            {"fecha": fecha, "tipo": tipo, "nombre": nombre}
            for fecha, tipo, nombre in filas
        ]
        for anio, filas in crudo["por_anio"].items()
    }
    por_dia = {
        feriado["fecha"]: feriado
        for filas in por_anio.values()
        for feriado in filas
    }
    return {
        "fuente": crudo["fuente"],
        "generado": crudo["generado"],
        "anios": tuple(sorted(por_anio)),
        "por_anio": por_anio,
        "por_dia": por_dia,
    }


def anios_cubiertos() -> tuple[int, ...]:
    """Los años que trae el archivo empaquetado, de menor a mayor."""
    return _catalogo()["anios"]


def generado() -> str:
    """La fecha en que se estampó el archivo, en ISO. Sirve para saber si el
    año en curso puede haber sumado puentes después de esta regeneración."""
    return _catalogo()["generado"]


def cubre(anio: int) -> bool:
    """Si se puede preguntar por ese año sin que levante `FueraDeCobertura`."""
    return anio in _catalogo()["por_anio"]


def feriados_de(anio: int) -> list[dict[str, str]]:
    """Los feriados nacionales de un año, ordenados por fecha.

    Cada uno es `{"fecha": "2026-01-01", "tipo": "inamovible", "nombre": ...}`,
    con la fecha en ISO.

    🔴 **`tipo: "trasladable"` NO quiere decir "ya movido al lunes".** Es la
    clase del feriado, no su estado: la fecha es la **efectiva que publica la
    fuente**, y para los años futuros los trasladables siguen en su fecha
    original hasta que el traslado se decreta. Medido sobre este mismo archivo
    el 2026-08-24: en 2026 los cuatro trasladables caían lunes, y en 2027 los
    cuatro seguían en su fecha original (17/6, 17/8, 12/10 y 20/11, ninguno
    lunes). En los años ya pasados hay de las dos: 2021-11-20 quedó sábado.

    O sea que **regenerar el año que viene no alcanza**: además de los puentes
    que todavía no existen, los traslados pueden moverse después.
    """
    catalogo = _catalogo()
    if anio not in catalogo["por_anio"]:
        raise FueraDeCobertura(
            f"{anio} no esta en el catalogo empaquetado "
            f"(cubre {catalogo['anios'][0]}-{catalogo['anios'][-1]}). "
            f"Regenerar con scripts/generar_feriados.py."
        )
    return list(catalogo["por_anio"][anio])


def es_feriado(dia: date) -> dict[str, str] | None:
    """El feriado nacional de ese día, o `None` si es un día común.

    Levanta `FueraDeCobertura` si el año no está en el archivo — ver el
    docstring del módulo: contestar `None` ahí sería mentir con la misma cara
    que dice la verdad.
    """
    if not cubre(dia.year):
        raise FueraDeCobertura(
            f"{dia.year} no esta en el catalogo empaquetado. "
            f"Guardar la consulta con cubre({dia.year}) o regenerar el archivo."
        )
    return _catalogo()["por_dia"].get(dia.isoformat())


def build_feriados_router(prefijo: str = "/api/feriados") -> APIRouter:
    """El router de consulta del catálogo. Sólo lectura: no hay `POST`."""
    router = APIRouter(prefix=prefijo, tags=["feriados"])

    @router.get("/cobertura")
    def _cobertura() -> dict[str, Any]:
        """Qué años hay y cuándo se estampó el archivo.

        Va primero y con ruta fija para que no se la coma `/{anio}`: FastAPI
        resuelve por orden de declaración, y con la dinámica arriba
        `/cobertura` entraría como un año no numérico.
        """
        return {
            "anios": list(anios_cubiertos()),
            "generado": generado(),
            "fuente": _catalogo()["fuente"],
        }

    @router.get("/{anio}")
    def _del_anio(anio: int) -> list[dict[str, str]]:
        try:
            return feriados_de(anio)
        except FueraDeCobertura as exc:
            raise HTTPException(404, str(exc))

    return router
