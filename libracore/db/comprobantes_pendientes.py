"""
La bandeja de comprobantes que otro producto de la familia dejó para facturar:
alta idempotente, listados por estado y resolución (facturado / descartado).

Un comprobante pendiente **no es una factura**. No tiene número, no tiene CAE y
no vale como comprobante de nada: es lo que un producto devengó y todavía
espera a que una persona lo mire. La razón de que exista está en el DDL
(`libracore.db.schema`) y en
`wiki/analyses/libradesk-contalibra-puente-facturacion.md`.

Este módulo sólo escribe y lee filas. Armar el prefill de la factura a partir
de uno o varios pendientes —que es la parte con reglas— vive en
`libracore.comprobantes_pendientes`, igual que la separación entre
`libracore.db.recibos` y `libracore.recibos`.
"""
import json
import sqlite3

from libracore.db.core import Conexion, get_connection
from libracore.db.facturas import _con

ESTADO_PENDIENTE = "pendiente"
ESTADO_ENVIADO = "enviado"
ESTADO_ACEPTADO = "aceptado"
ESTADO_FACTURADO = "facturado"
ESTADO_DESCARTADO = "descartado"

ESTADOS = (ESTADO_PENDIENTE, ESTADO_ENVIADO, ESTADO_ACEPTADO, ESTADO_FACTURADO,
           ESTADO_DESCARTADO)

# `enviado` y `aceptado` son del ciclo de la pre factura (ADR-030,
# `libracore.pre_facturas`): sólo los alcanza quien la manda y la acepta, así que a
# los productores de siempre (Contalibra, LibraDesk) no les cambia nada.
#
# **Abierto** es lo que todavía se puede facturar o descartar; **final** no se mueve
# más. `facturado` y `descartado` son finales: sigue valiendo que una resolución la
# tomó una persona y nadie la revierte.
ESTADOS_ABIERTOS = (ESTADO_PENDIENTE, ESTADO_ENVIADO, ESTADO_ACEPTADO)
ESTADOS_FINALES = (ESTADO_FACTURADO, ESTADO_DESCARTADO)

# Lo que un producto puede depositar. La lista es cerrada a propósito: un
# `origen_tipo` libre convierte la bandeja en un buzón donde nadie sabe qué
# esperar, y la pantalla no puede agrupar ni etiquetar lo que no conoce.
ORIGEN_CUOTA_CONTRATO = "cuota_contrato"
ORIGEN_INCIDENCIA = "incidencia"
ORIGEN_REMITO = "remito"
ORIGEN_PRESUPUESTO = "presupuesto"
# Una pre factura que arma un producto juntando varias cosas suyas (órdenes de carga,
# tickets): no es una sola fila de origen. `libracore.pre_facturas` le pone de
# `origen_id` su número interno si el producto no pasa uno propio.
ORIGEN_PRE_FACTURA = "pre_factura"

ORIGENES = (
    ORIGEN_CUOTA_CONTRATO,
    ORIGEN_INCIDENCIA,
    ORIGEN_REMITO,
    ORIGEN_PRESUPUESTO,
    ORIGEN_PRE_FACTURA,
)


class ComprobanteYaResuelto(Exception):
    """El productor reenvió algo que acá ya se facturó, se descartó o una persona ya movió.

    No es un error del productor: reintentar es lo correcto cuando se corta la
    red. Lo que no puede pasar es que el reintento pise una resolución que ya
    tomó una persona, así que el alta lo informa en vez de escribir. Vale también
    para una fila `enviada` o `aceptada` (una pre factura que el cliente ya vio):
    cambiarle los datos por debajo es lo que `editar` hace, con la persona delante.
    """


def _row_a_dict(row) -> dict:
    d = dict(row)
    d["items"] = json.loads(d["items"] or "[]")
    return d


def calcular_total(items: list) -> float:
    """El total con IVA de una lista de ítems.

    **Es la única forma en que `total` se escribe.** No se acepta del productor:
    si lo mandara él, dos sistemas tendrían opinión sobre cuánto sale lo mismo y
    la bandeja mostraría un número que no es la suma de lo que muestra abajo.
    """
    total = 0.0
    for i in items:
        qty = float(i.get("qty") or 0)
        precio = float(i.get("unit_price") or 0)
        iva = float(i.get("iva_rate") or 0)
        total += qty * precio * (1 + iva)
    return round(total, 2)


def upsert_comprobante(origen_producto, origen_tipo, origen_id, cliente_razon,
                       items, origen_instancia="", cliente_id=None,
                       cliente_cuit="", cliente_domicilio="",
                       fecha_sugerida="", periodo_desde="", periodo_hasta="",
                       concepto="", condicion_venta="",
                       observaciones="") -> tuple[int, bool]:
    """Deja un comprobante en la bandeja. Devuelve `(id, creado)`.

    **Idempotente por origen**: la cuádrupla
    `(origen_producto, origen_instancia, origen_tipo, origen_id)` tiene un
    UNIQUE, así que reenviar lo mismo no duplica.

    Qué hace ante un reenvío:

    - Si el pendiente sigue `pendiente`, **actualiza los datos** y devuelve
      `creado=False`. El origen puede haber corregido el importe o el período
      entre un intento y el siguiente, y lo último que mandó es lo que vale.
    - Si ya está `facturado` o `descartado` (o `enviado` o `aceptado`: una persona
      ya la mandó o la aceptó), **no toca nada** y levanta `ComprobanteYaResuelto`.
      Una resolución la tomó una persona; un reintento automático no la revierte.
    """
    if origen_tipo not in ORIGENES:
        raise ValueError(
            f"origen_tipo invalido: {origen_tipo!r} (esperado uno de {ORIGENES})"
        )
    if not str(origen_producto or "").strip():
        raise ValueError("origen_producto es obligatorio: sin el, la bandeja no "
                         "puede decir de donde vino la fila")
    if not str(origen_id or "").strip():
        raise ValueError("origen_id es obligatorio: es lo que hace idempotente "
                         "el reenvio")

    items = list(items or [])
    total = calcular_total(items)

    with get_connection() as conn:
        existente = conn.execute(
            "SELECT id, estado FROM comprobantes_pendientes "
            "WHERE origen_producto=? AND origen_instancia=? AND origen_tipo=? "
            "AND origen_id=?",
            (origen_producto, origen_instancia, origen_tipo, str(origen_id)),
        ).fetchone()

        if existente is not None:
            fila = dict(existente)
            if fila["estado"] != ESTADO_PENDIENTE:
                raise ComprobanteYaResuelto(
                    f"El comprobante {origen_tipo}:{origen_id} de "
                    f"{origen_producto} ya esta {fila['estado']}"
                )
            conn.execute(
                "UPDATE comprobantes_pendientes SET cliente_id=?, cliente_cuit=?, "
                "cliente_razon=?, cliente_domicilio=?, fecha_sugerida=?, "
                "periodo_desde=?, periodo_hasta=?, concepto=?, condicion_venta=?, "
                "observaciones=?, items=?, total=? WHERE id=?",
                (cliente_id, cliente_cuit, cliente_razon, cliente_domicilio,
                 fecha_sugerida, periodo_desde, periodo_hasta, concepto,
                 condicion_venta, observaciones, json.dumps(items), total,
                 fila["id"]),
            )
            conn.commit()
            return fila["id"], False

        try:
            cur = conn.execute(
                "INSERT INTO comprobantes_pendientes "
                "(origen_producto, origen_instancia, origen_tipo, origen_id, "
                " cliente_id, cliente_cuit, cliente_razon, cliente_domicilio, "
                " fecha_sugerida, periodo_desde, periodo_hasta, concepto, "
                " condicion_venta, observaciones, items, total, estado) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (origen_producto, origen_instancia, origen_tipo, str(origen_id),
                 cliente_id, cliente_cuit, cliente_razon, cliente_domicilio,
                 fecha_sugerida, periodo_desde, periodo_hasta, concepto,
                 condicion_venta, observaciones, json.dumps(items), total,
                 ESTADO_PENDIENTE),
            )
            conn.commit()
            return cur.lastrowid, True
        except sqlite3.IntegrityError:
            # Dos altas simultáneas del mismo origen: la otra ganó la carrera.
            # Es exactamente el caso que el UNIQUE viene a cubrir, y la
            # respuesta correcta es devolver la fila que quedó, no fallar.
            #
            # 🔴 **El `rollback()` no es de higiene: sin él este camino no
            # existe contra PostgreSQL.** Ahí un error ABORTA la transacción, y
            # cualquier consulta posterior sobre la misma conexión muere con
            # *"current transaction is aborted, commands ignored until end of
            # transaction block"*. O sea que el `SELECT` de abajo —el que va a
            # buscar la fila que ganó— fallaba justo cuando la carrera ocurría,
            # que es el único momento en que este bloque corre. En SQLite no
            # pasa: la conexión sigue usable y por eso nunca se vio.
            #
            # Lo anticipa el docstring de `_errores_como_sqlite3` en
            # `_postgres.py`: el adaptador traduce los NOMBRES de las
            # excepciones, y avisa que esta diferencia de comportamiento queda
            # afuera y hay que mirarla caso por caso. Ésta es uno de esos casos.
            #
            # Los otros reintentos de la familia (`facturas.py`, `recibos.py`)
            # no lo necesitan porque abren una conexión nueva por intento,
            # dentro del bucle; acá la conexión es una sola para toda la
            # función. `ventas.py` sí hace `rollback()`, por lo mismo.
            conn.rollback()
            fila = conn.execute(
                "SELECT id FROM comprobantes_pendientes "
                "WHERE origen_producto=? AND origen_instancia=? AND "
                "origen_tipo=? AND origen_id=?",
                (origen_producto, origen_instancia, origen_tipo, str(origen_id)),
            ).fetchone()
            return dict(fila)["id"], False


def get_comprobante(comprobante_id: int, *, conn: Conexion | None = None) -> dict | None:
    """Con `conn`, lee dentro de la transacción de quien llama (ADR-025)."""
    with _con(conn) as c:
        row = c.execute(
            "SELECT * FROM comprobantes_pendientes WHERE id=?", (comprobante_id,)
        ).fetchone()
        return _row_a_dict(row) if row else None


def get_comprobantes(ids: list, *, conn: Conexion | None = None) -> list[dict]:
    """Varios por id, en el orden en que están en la base.

    Es lo que necesita el armado del prefill cuando se facturan juntos varios
    pendientes del mismo cliente.
    """
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return []
    marcas = ",".join("?" * len(ids))
    with _con(conn) as c:
        rows = c.execute(
            f"SELECT * FROM comprobantes_pendientes WHERE id IN ({marcas}) "
            "ORDER BY id",
            tuple(ids),
        ).fetchall()
        return [_row_a_dict(r) for r in rows]


def list_por_estado(estado: str, limit: int | None = None, *,
                    conn: Conexion | None = None) -> list[dict]:
    if estado not in ESTADOS:
        raise ValueError(f"estado invalido: {estado!r} (esperado uno de {ESTADOS})")
    sql = ("SELECT * FROM comprobantes_pendientes WHERE estado=? "
           "ORDER BY created_at DESC, id DESC")
    params: tuple = (estado,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (estado, int(limit))
    with _con(conn) as c:
        return [_row_a_dict(r) for r in c.execute(sql, params).fetchall()]


def contar_pendientes() -> int:
    """Para el badge del menú, que es lo que hace que alguien entre a la
    bandeja. Sin esto la pantalla existe y nadie la abre."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM comprobantes_pendientes WHERE estado=?",
            (ESTADO_PENDIENTE,),
        ).fetchone()
        return row[0] or 0


def _resolver(comprobante_id, estado, usuario="", factura_id=None, motivo="",
              conn: Conexion | None = None):
    """El único escritor de `estado` hacia un final. Sólo mueve lo que está abierto.

    El `WHERE estado IN (abiertos)` no es defensivo de más: es lo que hace que
    marcar dos veces no pise el `factura_id` de la primera, que es el dato con
    el que después se rastrea qué factura cubrió qué. Abierto es `pendiente`,
    `enviado` o `aceptado`: una pre factura se factura o se anula en cualquiera de
    los tres.

    Con `conn`, en la transacción de quien llama y sin confirmar (ADR-025): es lo
    que deja a un producto marcar la pre factura facturada en la misma transacción
    en que emite el comprobante.
    """
    marcas = ",".join("?" * len(ESTADOS_ABIERTOS))
    with _con(conn) as c:
        cur = c.execute(
            "UPDATE comprobantes_pendientes SET estado=?, factura_id=?, "
            "motivo_descarte=?, resuelto_por=?, "
            "resuelto_at=datetime('now','-3 hours') "
            f"WHERE id=? AND estado IN ({marcas})",
            (estado, factura_id, motivo, usuario, comprobante_id,
             *ESTADOS_ABIERTOS),
        )
        return cur.rowcount > 0


def marcar_facturado(comprobante_id: int, factura_id: int, usuario: str = "", *,
                     conn: Conexion | None = None) -> bool:
    """Devuelve `False` si ya estaba resuelto (facturado o descartado) o no existe."""
    return _resolver(comprobante_id, ESTADO_FACTURADO, usuario=usuario,
                     factura_id=factura_id, conn=conn)


def descartar(comprobante_id: int, motivo: str = "", usuario: str = "", *,
              conn: Conexion | None = None) -> bool:
    return _resolver(comprobante_id, ESTADO_DESCARTADO, usuario=usuario,
                     motivo=motivo, conn=conn)
