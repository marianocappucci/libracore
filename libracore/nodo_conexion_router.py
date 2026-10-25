"""`GET /api/nodo/conexion` sin `PuertoDeVentas` (ADR-084): lo que el cartel «sin conexión» de libra-ui necesita saber de
un nodo de LibraEdge, para el producto que no vende en un punto de venta.

Hasta ADR-083 la única ruta era la de `ventas_cobro_router.build_facturacion_del_nodo_router`, que pide un
`PuertoDeVentas` (de `libracommerce.erp`) sólo para leer dos datos de él: `ventas.conexion` y la bandeja de ventas a
facturar. LibraCargo, LibraClub y GestioLibra no tienen puerto de ventas y preguntaban la conexión a `GET /api/nodo/cola?limite=1`
(la cola de comprobantes la trae); MedLibra escribía la ruta a mano. Este constructor es la misma ruta con la misma
respuesta, y el que ya monta la de ventas no cambia.

```python
app.include_router(
    build_conexion_del_nodo_router(conexion=COLA.conexion, usuario_actual=get_current_user_json),
)
```

La respuesta es `{"es_nodo", "en_linea", "node_id", "a_facturar"}` (`ConexionDelNodo.estado`). Fuera de un nodo
`es_nodo: false` y `en_linea: true`: el cartel deja de consultar. `a_facturar` es `0` salvo que el producto pase un
`a_facturar() -> int` (es lo que la barra de estado del kit cuenta como «ventas por facturar»; los productos con cola de
comprobantes lo dejan en 0 y muestran la cola con `BandejaDeLaCola`).

**Una sola ruta por prefijo:** un producto monta o este router o `build_facturacion_del_nodo_router` (que ya incluye
`GET /conexion`), no los dos con el mismo prefijo.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends

from libracore.nodo_offline import ConexionDelNodo


def _nada_a_facturar() -> int:
    return 0


def build_conexion_del_nodo_router(
    *,
    conexion: ConexionDelNodo,
    usuario_actual: Callable[..., Any],
    a_facturar: Callable[[], int] = _nada_a_facturar,
    prefix: str = "/api/nodo",
) -> APIRouter:
    """`GET {prefix}/conexion`. `usuario_actual` es la dependencia de sesión del producto (la misma que usa para el resto
    del nodo): cualquier usuario de la instancia, porque la pantalla lo lee en cuanto carga la sesión.

    El `ConexionDelNodo` consulta sus funciones (`nodo`, `en_linea`) **en cada pedido**, no al montar: un nodo que
    pierde o recupera la conexión se ve en la próxima consulta."""
    router = APIRouter(prefix=prefix, tags=["nodo"])

    @router.get("/conexion")
    def conexion_del_nodo(user: dict = Depends(usuario_actual)):
        """`{"es_nodo", "en_linea", "node_id", "a_facturar"}`. La pantalla lo consulta para avisar «sin conexión»."""
        return conexion.estado(a_facturar())

    return router
