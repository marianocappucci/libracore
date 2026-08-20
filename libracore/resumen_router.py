"""`GET /api/resumen`: lo que una instancia le contesta al panel del cliente.

Una sucursal contesta por si misma; el panel le pregunta a las N y suma. Ese
reparto es lo que evita que el panel tenga credenciales de N bases: le alcanza
con hablarles por HTTP.

Es una **factory** y no un router suelto, con la misma forma que
`libraauth.session_auth.build_demo_codigos_router`: el producto lo monta y le
pasa lo que solo el sabe contestar.

Ver wiki/analyses/panel-del-dueno-multisucursal.md.
"""
import datetime
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from libracore.db.resumen import get_resumen_core


def build_resumen_router(
    *,
    identidad: Callable[[], dict],
    guard: Callable,
    bloques: dict[str, Callable[[str, str], dict]] | None = None,
    prefix: str = "/api",
) -> APIRouter:
    """Arma el endpoint de resumen de un producto.

    `guard` es la dependencia que decide quien entra —en la familia,
    `libraauth.session_auth.json_api_require_panel_o_admin`— y viene **inyectada
    a proposito**: LibraCore y LibraAuth son motores peers y este paquete no
    depende de aquel. Importarlo aca para ahorrarse un parametro acoplaria los
    dos motores y ataria sus releases.

    `identidad()` devuelve quien es esta sucursal: al menos `nombre` y `cuit`.
    El CUIT es lo que le permite al panel el consolidado **por razon social**,
    que es el unico que cierra contra los libros — sumar entre CUITs da un
    numero de gestion, no uno declarable.

    `bloques` son los agregados que este producto puede contestar ademas del
    nucleo, cada uno una funcion `(desde, hasta) -> dict`. Por ejemplo
    `{"comercio": ...}` en los que montan LibraCommerce.

    🔴 **Un bloque que el producto no tiene se OMITE, no se manda en cero.**
    Una sucursal de MedLibra no tiene ventas de buffet: mandar `0` ahi dice "no
    vendieron nada" cuando lo cierto es "esto no se mide aca" — y consolidando,
    un cero se suma y un ausente no. Por eso `bloques` es lo que el producto
    pasa, y lo que no pasa simplemente no aparece en la respuesta.
    """
    bloques = bloques or {}
    router = APIRouter(prefix=prefix, tags=["resumen"])

    @router.get("/resumen")
    def resumen(
        desde: str = Query(default=""),
        hasta: str = Query(default=""),
        _: dict = Depends(guard),
    ):
        """Totales del periodo. Sin `desde`/`hasta`, el mes en curso.

        Las fechas van en ISO y no en el formato de pantalla: es una API entre
        maquinas, y el estandar de la familia es que las URLs lleven ISO aunque
        la interfaz muestre dd-mm-aaaa.
        """
        hoy = datetime.date.today()
        desde_ = desde or hoy.replace(day=1).isoformat()
        hasta_ = hasta or hoy.isoformat()

        for etiqueta, valor in (("desde", desde_), ("hasta", hasta_)):
            try:
                datetime.date.fromisoformat(valor)
            except ValueError:
                raise HTTPException(
                    422, f"`{etiqueta}` tiene que ser una fecha ISO (aaaa-mm-dd)"
                )
        if desde_ > hasta_:
            raise HTTPException(422, "`desde` no puede ser posterior a `hasta`")

        salida = {
            "instancia": identidad(),
            "periodo": {"desde": desde_, "hasta": hasta_},
            "nucleo": get_resumen_core(desde_, hasta_),
        }
        for nombre, calcular in bloques.items():
            salida[nombre] = calcular(desde_, hasta_)
        return salida

    return router
