"""El central aplica la caja que subió de un nodo, con un gancho para lo propio del producto (ADR-080).

`libracore.ganchos_de_caja` es la mitad del nodo: los ganchos que el producto engancha para encolar la operación.
Esta es la del central, y es la que **no** depende de `libracommerce` ni de `libraedge`: reproduce un `caja.movimiento`
y un `caja.movimiento_anulado` con las funciones del motor, idempotente por `(origen, origen_id)`, y deja un punto de
extensión para que el producto rehaga **lo suyo** —su fila hija con el mismo id, el asiento de su cuenta corriente—
dentro de la misma transacción (el mismo patrón que `al_materializar` de `libracommerce` ADR-072, que hace lo mismo con
las ventas)::

    def crear_la_hija(conn, mov_id, datos):                    # el gancho del central
        extra = datos.get("extra") or {}
        conn.execute("INSERT INTO movimientos_caja_cargo (movimiento_id, tercero_id, ...) VALUES (?, ?, ...)",
                     (mov_id, extra.get("tercero_id"), ...))

    def aplicar_caja_movimiento(conn, operacion):              # lo registra el producto en su Despachador
        aplicar_movimiento_del_nodo(conn, operacion.node_id, operacion.payload, al_aplicar=crear_la_hija)

Del lado del nodo, el producto pasa lo propio en `create_caja_movimiento(..., extra={...}, ganchos=)`: viaja en
`datos["extra"]`, y como el gancho del nodo corre **antes** de que el producto cree su fila hija, es lo único que
puede llevar (lo que se lee de la fila hija todavía no existe).

🔴 **Todo en la transacción de `conn`, y no confirma.** Si `al_aplicar` levanta, no queda nada de la operación: ni el
movimiento, ni lo que el gancho haya escrito. Un `ValueError` (toda la jerarquía de `CajaDelNodoError` lo es) va a revisión
manual en el receptor de LibraEdge sin trabar la cola; cualquier otra excepción es un fallo de transporte y corta la
tanda (ADR-020 de libraedge), así que lo que **falta o choca** se dice con `ValueError`.

🔑 **Idempotente, y el gancho corre una sola vez.** Aplicar dos veces el mismo movimiento deja una fila y no vuelve a llamar
a `al_aplicar`: el gancho se escribe sin preocuparse por el reintento.

Lo que **no** hace (es del aplicador del producto o de `libracommerce.erp.offline_caja`): el `Despachador`, el
`operation_type`, los turnos (`turno.abierto` / `turno.cerrado`) y el cierre del día.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from libracore.db.caja import anular_caja_movimiento, create_caja_movimiento, get_movimiento_de_origen
from libracore.db.core import Conexion
from libracore.db.turnos import get_turno_de_origen
from libracore.ganchos_de_caja import VERSION

#: El gancho del central: `(conn, mov_id, datos)`. `conn` es la conexión del receptor (no confirma ni cierra nada),
#: `mov_id` el id **del central** de la fila del motor y `datos` el payload del nodo tal cual (con los ids del nodo y,
#: si el producto llevó algo, `datos["extra"]`).
AlAplicar = Callable[[Any, int, dict], None]


class CajaDelNodoError(ValueError):
    """Una operación de caja del nodo que el central no puede aplicar. Es un `ValueError` a propósito: el receptor de
    LibraEdge la manda a revisión manual y el nodo no traba la cola detrás de ella."""


class TurnoNoSincronizado(CajaDelNodoError):
    """El turno del nodo no está en el central (su `turno.abierto` no se aplicó)."""


class MovimientoNoSincronizado(CajaDelNodoError):
    """El movimiento del nodo no está en el central (su `caja.movimiento` no se aplicó)."""


class CajaNoSincronizada(CajaDelNodoError):
    """La caja del nodo no existe en el central: las cajas se espejan con la misma clave, y ésta no llegó."""


class CajaRechazadaPorElCentral(CajaDelNodoError):
    """El motor de caja del central no acepta la operación (una referencia que ya tiene otro movimiento)."""


def _datos(datos: Mapping, tipo: str) -> Mapping:
    if datos.get("version") != VERSION:
        raise CajaDelNodoError(
            f"versión {datos.get('version')!r} de los datos de {tipo} desconocida (se entiende {VERSION})")
    return datos


def aplicar_movimiento_del_nodo(conn: Conexion, node_id: str, datos: Mapping, *,
                                al_aplicar: AlAplicar | None = None) -> int:
    """Anota en el central el ingreso o egreso que anotó el nodo `node_id` y devuelve el id de la fila del central.

    `datos` es el de `ganchos_de_caja.datos_de_movimiento`. La fila lleva `origen = node_id` y `origen_id` = el id del
    nodo, en el turno del central que subió con ese `turno_id` (`TurnoNoSincronizado` si no subió; sin `turno_id`, sin
    turno) y en la caja con el mismo id (`CajaNoSincronizada` si el central no la tiene). `factura_id` y `cc_pago_id`
    **no se copian** —son ids de otra base—: quedan en `NULL`.

    Si ya estaba aplicado (por `(origen, origen_id)`), devuelve su id **sin tocar nada y sin llamar a `al_aplicar`**.
    Si no, después de escribir la fila llama `al_aplicar(conn, mov_id, datos)`. Una referencia que el central ya tiene
    en otro movimiento no hace fila nueva (la idempotencia de `create_caja_movimiento`): se rechaza con
    `CajaRechazadaPorElCentral` en vez de dar por aplicado un movimiento que no está."""
    datos = _datos(datos, "caja.movimiento")
    existente = get_movimiento_de_origen(node_id, datos["movimiento_id"], conn=conn)
    if existente is not None:
        return existente["id"]
    turno_id = None
    if datos.get("turno_id") is not None:
        turno = get_turno_de_origen(node_id, datos["turno_id"], conn=conn)
        if turno is None:
            raise TurnoNoSincronizado(
                f"el turno {datos['turno_id']} del nodo {node_id} no está en el central: falta aplicar su "
                "turno.abierto")
        turno_id = turno["id"]
    caja_id = datos.get("caja_id")
    if caja_id is not None and conn.execute("SELECT 1 FROM cajas WHERE id=?", (caja_id,)).fetchone() is None:
        raise CajaNoSincronizada(
            f"la caja {caja_id} del nodo {node_id} no existe en el central: las cajas se espejan con la misma clave "
            "y ésta no llegó")
    mov_id = create_caja_movimiento(
        datos["fecha"], datos["tipo"], datos["concepto"], datos["monto"], datos.get("referencia") or "",
        usuario_id=datos.get("usuario_id"), caja_id=caja_id, medio_pago=datos.get("medio_pago") or "",
        turno_id=turno_id, conn=conn, origen=node_id, origen_id=datos["movimiento_id"])
    propio = get_movimiento_de_origen(node_id, datos["movimiento_id"], conn=conn)
    if propio is None or propio["id"] != mov_id:
        raise CajaRechazadaPorElCentral(
            f"el movimiento {datos['movimiento_id']} del nodo {node_id} tiene la referencia "
            f"{datos.get('referencia')!r}, que el central ya tiene en otro movimiento: no se duplica")
    if al_aplicar is not None:
        al_aplicar(conn, mov_id, dict(datos))
    return mov_id


def aplicar_anulacion_del_nodo(conn: Conexion, node_id: str, datos: Mapping, *,
                               al_aplicar: AlAplicar | None = None) -> bool:
    """Anula en el central el movimiento que anuló el nodo `node_id`. Devuelve si cambió algo.

    Sin el movimiento (su `caja.movimiento` no se aplicó), `MovimientoNoSincronizado`. Uno que ya estaba anulado no se
    toca y **no llama a `al_aplicar`** (devuelve `False`). Si no, anula, y recién entonces llama
    `al_aplicar(conn, mov_id, datos)` —con `datos["extra"]` si el nodo llevó algo en `anular_caja_movimiento(extra=)`—
    para que el producto rehaga lo suyo (revertir su asiento)."""
    datos = _datos(datos, "caja.movimiento_anulado")
    mov = get_movimiento_de_origen(node_id, datos["movimiento_id"], conn=conn)
    if mov is None:
        raise MovimientoNoSincronizado(
            f"el movimiento {datos['movimiento_id']} del nodo {node_id} no está en el central: falta aplicar su "
            "caja.movimiento")
    if mov["anulado"]:
        return False
    anular_caja_movimiento(mov["id"], conn=conn)
    if al_aplicar is not None:
        al_aplicar(conn, mov["id"], dict(datos))
    return True
