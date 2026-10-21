"""Las altas de referencia en un nodo offline: el id provisorio y el gancho que las sube (ADR-077).

Un cliente que se da de alta en un nodo de LibraEdge (ADR-074) hasta hoy se creaba con el siguiente id de la secuencia
**de la base del nodo**. El central, que es la autoridad de los clientes (el nodo los espeja), tiene los suyos con ids que
no coinciden: el cliente del nodo chocaba con uno del central, o lo pisaba el espejo, y la venta subía con un `cliente_id`
que en el central es de **otro cliente**. La decisión del humano (2026-10-21): **las altas de referencia en el nodo se
permiten y suben como operación; el central asigna su id y reescribe las referencias**. Este módulo es la costura del
motor; el resto es de cada capa:

| Quién | Qué hace |
|---|---|
| `libracore` (acá) | En un nodo, `create_client`, `crear_cliente` y `resolver_cliente_externo` guardan al cliente con un **id provisorio negativo** y avisan al gancho `al_crear_cliente`, **dentro de la misma transacción** del alta. Fuera de un nodo no cambian. |
| `libracommerce` (`erp.offline_clientes`) | Arma la `AltasEnNodo` del producto: el gancho encola la operación `cliente.alta` y el central la aplica y traduce los ids de lo que sube después. |
| `libraedge` (ADR-021) | El id provisorio (`siguiente_id_provisorio`), el mapeo `(nodo, tabla, id_nodo) -> id_central` y el reapuntado cuando el id definitivo baja por el espejo. |

El motor no depende de `libraedge` ni de `libracommerce` en runtime (ADR-074): le llegan **inyectados**.

```python
from libracore import altas_en_nodo
altas_en_nodo.configurar(libracommerce.erp.offline_clientes.altas_en_nodo(OUTBOX))   # una vez, al armar la app
```

## Por qué negativo

Un id del central es siempre positivo (la secuencia de PostgreSQL arranca en 1), así que un id provisorio **no se cruza
nunca** con uno que llegue por el espejo ni con uno que el nodo todavía no conoce. Y salta a la vista: `id < 0` es «este
cliente todavía no existe en el central».

## Por qué un gancho global y no uno por llamada

`GanchosDeCaja` (ADR-076) se pasa por llamada a propósito: sólo debe subir lo que decide una persona en la caja, y lo que
nace de una venta lo reproduce el central. Con el alta de un cliente es **al revés**: *todo* alta en un nodo tiene que
llevar su id provisorio y subir, venga de `POST /api/clientes`, del registro de clientes o de lo que agregue un producto.
Un alta que se escapara del gancho sería un cliente que nunca llega al central y una venta que éste rechaza (o, peor,
una que queda a nombre de otro). Una sola costura que cubre todos los caminos de `libracore.db.clients` es la forma de que
no se escape ninguno, sin tocar a los productos.

## Qué pasa fuera de un nodo

Nada. Sin `configurar()`, o con un `nodo()` que devuelve `None` (el central, una instalación común), las tres altas de
`libracore.db.clients` usan la secuencia de la base y no avisan a nadie, igual que antes. Se lee en cada alta (barato: es
una variable de entorno), así que activar el nodo no pide reiniciar nada.

## Qué viaja

`datos_de_cliente(fila)` (JSON, `VERSION = 1`): el id **del nodo** (`cliente_id`, negativo) y las columnas de `clients` que
describen al cliente. El central usa el CUIT/DNI para no duplicar a uno que ya existe (`get_client_by_cuit`) y, si no hay,
lo crea. `id`, `created_at` y `auto_facturar` no viajan: el id lo asigna el central, la fecha la pone su default y el
auto-facturar es de MercadoPago, que no corre sin conexión.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

#: La versión de la forma de los `datos` del gancho. Si cambia, sube: el central rechaza lo que no entiende.
VERSION = 1

#: Las columnas de `clients` que describen al cliente y suben con el alta (las que existan en la fila). Las cuatro últimas
#: son las de la revisión `0002` de Alembic: una instancia que no la corrió no las tiene, y no viajan.
COLUMNAS_DE_CLIENTE = (
    "name", "address", "cuit_dni", "email", "phone", "iva_condition", "external_ref", "activo",
    "empresa", "ciudad", "observaciones", "tipo_facturacion",
)

#: Lo que referencia al id de un cliente **sin clave foránea**, para que `libraedge` lo reapunte cuando el central asigne el
#: definitivo (`libraedge.altas.AltaEnNodo`, que lo declara el `Aprovisionamiento` del producto):
#: `(tabla, columna, {columna_condición: valor})`. Hoy es una sola: el libro de cuentas (`cc_asientos`), cuyo `tercero_id`
#: sirve a clientes y proveedores y por eso no tiene FK; `rol = 'cliente'` deja fuera los asientos de un proveedor que
#: casualmente tenga el mismo número. Todo lo demás (`ventas`, `remitos`, `presupuestos`, `cc_pagos`, `cc_debitos`,
#: `recibos`, `comprobantes_pendientes`, `facturacion_alias`, `cliente_lista_precio`) tiene su FK a `clients`, y
#: `libraedge` las lee del catálogo de la base. `tests/test_altas_en_nodo.py` fija esta lista contra el schema: una columna
#: nueva sin FK que guarde un cliente rompe el test en vez de quedar sin reapuntar.
REFERENCIAS_DE_CLIENTES = (
    ("cc_asientos", "tercero_id", {"rol": "cliente"}),
)


def _nada(conn: Any, datos: dict) -> None:
    return None


@dataclass(frozen=True)
class AltasEnNodo:
    """Lo que el motor necesita saber de LibraEdge para dar de alta en un nodo. Inmutable.

    - `nodo`: el id de este nodo, o `None` si la instancia no es un nodo (el central, una instalación común). Es
      `LIBRAEDGE_NODE_ID` (`libraedge.identidad.nodo_actual`); un producto que además lee su variable vieja inyecta la
      función con su prefijo.
    - `id_provisorio`: `(conn) -> int`, el próximo id negativo del nodo, **sin commitear** (va en la transacción del alta).
      `libraedge.mapeo.siguiente_id_provisorio`.
    - `al_crear_cliente`: `(conn, datos)`, corre después del alta y **adentro de su transacción**: si levanta, el cliente
      no queda (un cliente que se da de alta en el nodo y no sube no es «a medias»: el central no lo ve y la próxima venta
      a su nombre se rechaza). Lo normal es que encole `cliente.alta` en el outbox.
    """

    nodo: Callable[[], str | None]
    id_provisorio: Callable[[Any], int]
    al_crear_cliente: Callable[[Any, dict], None] = _nada


_configuracion: AltasEnNodo | None = None


def configurar(altas: AltasEnNodo | None) -> None:
    """Registra la `AltasEnNodo` del proceso (o la saca, con `None`). La llama el producto al armar la app, una vez.

    Es global y es del proceso a propósito (ver el docstring del módulo). Registrar otra reemplaza a la anterior."""
    global _configuracion
    if altas is not None and not isinstance(altas, AltasEnNodo):
        raise TypeError(f"se esperaba una AltasEnNodo, llegó {type(altas).__name__}")
    _configuracion = altas


def configuracion() -> AltasEnNodo | None:
    """La `AltasEnNodo` registrada, o `None`."""
    return _configuracion


def nodo_actual() -> str | None:
    """El id del nodo si esta instancia es un nodo **y** hay una configuración; si no, `None`. Se lee en cada llamada."""
    if _configuracion is None:
        return None
    return _configuracion.nodo() or None


def en_un_nodo() -> bool:
    """Si las altas de esta instancia son provisorias (hay configuración y la instancia es un nodo)."""
    return nodo_actual() is not None


def id_provisorio_de_cliente(conn: Any) -> int | None:
    """El id provisorio (negativo) para un cliente que se está dando de alta, o `None` fuera de un nodo (la base le
    asigna el suyo). No commitea."""
    if _configuracion is None or not nodo_actual():
        return None
    id_ = int(_configuracion.id_provisorio(conn))
    if id_ >= 0:
        raise ValueError(f"un id provisorio es negativo, no {id_}")
    return id_


def datos_de_cliente(fila: Mapping) -> dict:
    """`{"version", "cliente_id", **columnas}`: el cliente como lo ve el central. `cliente_id` es el **provisorio del
    nodo**. Las columnas son las de `COLUMNAS_DE_CLIENTE` que tenga la fila (`None` queda `None`)."""
    return {
        "version": VERSION,
        "cliente_id": fila["id"],
        **{columna: fila[columna] for columna in COLUMNAS_DE_CLIENTE if columna in fila.keys()},
    }


def avisar_alta_de_cliente(conn: Any, fila: Mapping | None) -> None:
    """Le avisa al gancho que se dio de alta `fila` (la de `clients`, leída con la conexión del alta). No hace nada fuera
    de un nodo ni para un cliente con id del central: sólo suben las altas provisorias."""
    if _configuracion is None or fila is None or fila["id"] >= 0 or not nodo_actual():
        return
    _configuracion.al_crear_cliente(conn, datos_de_cliente(fila))
