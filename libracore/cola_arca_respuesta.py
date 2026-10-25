"""La respuesta «quedó en la cola» de una operación del nodo (ADR-084): **una sola forma** para el 202.

Cuando una persona pide un comprobante en un nodo sin conexión (la factura de una orden de carga, la «conjunta» de una
reserva, la atención de un turno, una carta de porte), el pedido queda en `cola_arca` (ADR-079) y sale solo al volver
la conexión. El producto contesta eso con un cuerpo que libra-ui reconoce (`esRespuestaEnCola`) y muestra con
`AvisoEnCola`: «Se emite al volver la conexión». Hasta ADR-083 cada producto armaba el suyo, con tres formas:

| Producto | Cuerpo |
|---|---|
| LibraCargo (factura y CPE) | `{codigo: "en_cola" o "cpe_en_cola", mensaje, pedido_id, estado, motivo}` |
| LibraClub (factura conjunta) | `{en_cola: true, estado, pedido_id, mensaje}` |
| GestioLibra (atender un turno) | el resultado de la atención más `{en_cola: true, estado, pedido_id, motivo, mensaje}` |

La forma única es la unión de las tres, así que **libra-ui no cambia** (reconoce `en_cola === true` o el `codigo`)::

    {"en_cola": true, "codigo": "en_cola" o "cpe_en_cola", "estado", "pedido_id", "mensaje", "motivo"}

- `en_cola`: siempre `true`. Es lo que distingue este cuerpo del comprobante emitido.
- `codigo`: `cpe_en_cola` si el pedido es una carta de porte (**no hay CPE válida y el camión no puede viajar**: el kit lo
  pone en rojo con el aviso legal), `en_cola` para todo lo demás.
- `estado`: el del pedido (`esperando`, `emitiendo`, `a_revisar`, `incierto`).
- `pedido_id`: el id de la fila de `cola_arca` (se ve en `GET /api/nodo/cola`).
- `mensaje`: el texto para la persona. Default según el tipo; el producto puede pasar el suyo.
- `motivo`: por qué espera o qué dijo ARCA (`ultimo_error` del pedido); `""` si no hay.

Dos usos::

    return respuesta_en_cola(pedido)                         # 202 con ese cuerpo (lo común)
    resultado.update(cuerpo_en_cola(pedido))                 # el cuerpo suelto, para sumarlo a otra respuesta

El segundo es para el endpoint cuya respuesta es **otra cosa** que el comprobante (GestioLibra: el turno ya se atendió, y
eso es lo que se contesta con 200; el pedido en cola es una parte). El criterio del kit es el cuerpo y no el código HTTP.

Sólo para un pedido **abierto**. Uno `emitido` o `descartado` no está en la cola: el producto contesta el comprobante (o
el error) y pasarle uno acá es un defecto que se ve (`ValueError`), no un 202 que miente.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from libracore.db import cola_arca as db

CODIGO_EN_COLA = "en_cola"
CODIGO_CPE_EN_COLA = "cpe_en_cola"

MENSAJE_FACTURA_EN_COLA = (
    "Sin conexión con el central o con ARCA: la factura queda en cola y se emite sola al volver la conexión."
)
MENSAJE_CPE_EN_COLA = (
    "La carta de porte quedó en cola y se emite sola al volver la conexión. Hasta entonces NO hay una CPE válida: "
    "el camión no puede viajar."
)
MENSAJE_PARA_REVISAR = "El pedido quedó en la cola para que una persona lo revise."


def codigo_de_la_cola(pedido: dict) -> str:
    """`cpe_en_cola` para una carta de porte, `en_cola` para lo demás."""
    return CODIGO_CPE_EN_COLA if pedido.get("origen_tipo") == db.ORIGEN_CPE else CODIGO_EN_COLA


def _mensaje_por_defecto(pedido: dict, motivo: str) -> str:
    if pedido["estado"] in db.ESTADOS_DE_REVISION:
        # Lo que importa en una revisión es qué dijo ARCA (el kit muestra `mensaje` antes que `motivo`).
        return motivo or MENSAJE_PARA_REVISAR
    return MENSAJE_CPE_EN_COLA if pedido.get("origen_tipo") == db.ORIGEN_CPE else MENSAJE_FACTURA_EN_COLA


def cuerpo_en_cola(pedido: dict, *, mensaje: str | None = None) -> dict:
    """El cuerpo de «quedó en la cola» para un pedido abierto de `cola_arca` (el dict de `db.get_pedido` o de
    `encolar_y_emitir`). Ver el docstring del módulo. `ValueError` si el pedido ya no está en la cola."""
    estado = pedido.get("estado")
    if estado not in db.ESTADOS_ABIERTOS:
        raise ValueError(f"Un pedido {estado!r} no está en la cola: sólo se contesta «en cola» para {db.ESTADOS_ABIERTOS}.")
    motivo = str(pedido.get("ultimo_error") or "")
    return {
        "en_cola": True,
        "codigo": codigo_de_la_cola(pedido),
        "estado": estado,
        "pedido_id": pedido["id"],
        "mensaje": mensaje if mensaje else _mensaje_por_defecto(pedido, motivo),
        "motivo": motivo,
    }


def respuesta_en_cola(pedido: dict, *, mensaje: str | None = None) -> JSONResponse:
    """`202` con `cuerpo_en_cola(pedido)`: lo que contesta el producto cuando la persona pidió un comprobante y quedó
    en la cola."""
    return JSONResponse(status_code=202, content=cuerpo_en_cola(pedido, mensaje=mensaje))
