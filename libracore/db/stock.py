"""
Movimientos de stock (entradas, salidas, ajustes, descuento por venta con
recetas y modificadores opcionales) y consulta de stock actual. Migrado a
libracore.db (Fase 3 de LibraCore, migración real, Tier 2 — el más
delicado de la migración: `descontar_stock_venta` receta-aware dependía
en Restolibra de un import directo a `db_recetas` (módulo que no existe
en Contalibra); acá se resuelve vía el hook `configure_resolver_receta`/
`get_resolver_receta`, mismo patrón callback que `libracore.auth`.
Restolibra configura el hook con `db_recetas.get_receta` al arrancar;
Contalibra no configura nada y preserva el comportamiento simple original
— ver wiki/entities/libracore.md).
"""
import json
import contextlib

from libracore.db.core import Conexion, get_connection, get_resolver_receta
from libracore.db.productos import get_default_deposito_id


def add_movimiento_stock(producto_id: int, tipo: str, cantidad: float,
                         referencia: str = "", fecha: str = "",
                         venta_id: int | None = None,
                         usuario_id: int | None = None,
                         deposito_id: int | None = None,
                         conn: Conexion | None = None):
    """Agrega un movimiento de stock. cantidad positiva=entrada, negativa=salida."""
    from datetime import date as _date
    _fecha = fecha or _date.today().isoformat()
    _deposito = deposito_id or get_default_deposito_id()
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        c.execute(
            """INSERT INTO movimientos_stock
               (producto_id, tipo, cantidad, referencia, venta_id, usuario_id, fecha, deposito_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (producto_id, tipo, cantidad, referencia, venta_id, usuario_id, _fecha, _deposito),
        )


def get_stock_actual(producto_id: int) -> float:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cantidad),0) FROM movimientos_stock WHERE producto_id=?",
            (producto_id,),
        ).fetchone()
    return float(row[0])


def get_stock_todos() -> list[dict]:
    """Devuelve todos los productos con su stock actual."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT p.id, p.codigo, p.nombre, p.unidad, p.categoria,
                   p.stock_minimo, p.activo,
                   COALESCE(SUM(m.cantidad), 0) AS stock_actual
            FROM productos p
            LEFT JOIN movimientos_stock m ON m.producto_id = p.id
            WHERE p.activo = 1
            GROUP BY p.id
            ORDER BY p.nombre
        """).fetchall()
    return [dict(r) for r in rows]


def get_movimientos_stock(producto_id: int | None = None,
                          desde: str = "", hasta: str = "",
                          limit: int = 200) -> list[dict]:
    with get_connection() as conn:
        where, params = [], []
        if producto_id:
            where.append("m.producto_id = ?"); params.append(producto_id)
        if desde:
            where.append("m.fecha >= ?"); params.append(desde)
        if hasta:
            where.append("m.fecha <= ?"); params.append(hasta)
        sql = """SELECT m.*, p.nombre AS producto_nombre, p.unidad
                 FROM movimientos_stock m
                 JOIN productos p ON p.id = m.producto_id"""
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY m.fecha DESC, m.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def ajustar_stock(producto_id: int, stock_nuevo: float, referencia: str,
                  usuario_id: int | None = None, fecha: str = ""):
    """Crea un movimiento de ajuste para llevar el stock al valor indicado."""
    actual = get_stock_actual(producto_id)
    delta  = round(stock_nuevo - actual, 4)
    if delta == 0:
        return
    add_movimiento_stock(
        producto_id=producto_id, tipo="ajuste",
        cantidad=delta, referencia=referencia,
        usuario_id=usuario_id, fecha=fecha,
    )


def _es_servicio(producto_id: int, conn: Conexion | None = None) -> bool:
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        row = c.execute("SELECT tipo FROM productos WHERE id=?", (producto_id,)).fetchone()
    return bool(row) and row[0] == "servicio"


def descontar_stock_venta(venta_id: int, items: list, fecha: str = "",
                           usuario_id: int | None = None,
                           conn: Conexion | None = None):
    """Descuenta stock por cada ítem de la venta que tenga producto_id y sea
    de tipo 'producto' — un servicio (`productos.tipo = 'servicio'`) nunca
    genera movimiento de stock, ni propio ni de receta: no tiene inventario.

    Si hay un resolver de recetas configurado (`configure_resolver_receta`,
    solo en productos con dominio de recetas — hoy Restolibra) y el
    producto tiene una receta activa, descuenta cada insumo de la receta
    (cantidad × cantidad vendida) en vez del propio producto — no es
    recursivo, los elaborados se stockean aparte por "producción". Si no
    hay resolver configurado o el producto no tiene receta, descuenta el
    propio producto (comportamiento original — sirve para reventa, ej.
    bebidas embotelladas).

    Si el ítem trae `modificadores` (JSON de un pedido, solo relevante con
    receta), se ajusta la cantidad de cada insumo: "quitar" -> no se
    descuenta, "doble" -> se descuenta el doble. Sin modificadores, receta
    normal.

    Si se pasa `conn`, corre dentro de esa transacción (ej. `cobrar_pedido`):
    un error acá debe abortar el cobro completo, no perderse en silencio.
    """
    resolver_receta = get_resolver_receta()
    for item in items:
        pid = item.get("producto_id")
        if not pid:
            continue
        if _es_servicio(pid, conn=conn):
            continue
        qty = abs(float(item.get("qty", 0)))
        receta = resolver_receta(pid) if resolver_receta else None
        if receta and receta["ingredientes"]:
            modos = _parse_modificadores(item.get("modificadores"))
            for ri in receta["ingredientes"]:
                modo = modos.get(ri["ingrediente_id"])
                if modo == "quitar":
                    continue
                multiplicador = 2 if modo == "doble" else 1
                add_movimiento_stock(
                    producto_id=ri["ingrediente_id"], tipo="venta",
                    cantidad=-(ri["cantidad"] * qty * multiplicador),
                    referencia=f"Venta ID {venta_id} (receta)",
                    venta_id=venta_id, usuario_id=usuario_id, fecha=fecha,
                    conn=conn,
                )
        else:
            add_movimiento_stock(
                producto_id=pid, tipo="venta",
                cantidad=-qty,
                referencia=f"Venta ID {venta_id}",
                conn=conn,
                venta_id=venta_id, usuario_id=usuario_id, fecha=fecha,
            )


def _parse_modificadores(modificadores) -> dict:
    """Convierte el JSON de modificadores de un pedido_item en un dict
    {ingrediente_id: "quitar"|"doble"} para uso interno."""
    if not modificadores:
        return {}
    try:
        lista = json.loads(modificadores)
    except (ValueError, TypeError):
        return {}
    return {int(m["ingrediente_id"]): m.get("modo") for m in lista if m.get("ingrediente_id")}


def _resumen_modificadores(modificadores) -> str:
    """Texto corto para mostrar en el pedido/comanda, ej. 'Sin Cheddar, Doble Medallón'."""
    if not modificadores:
        return ""
    try:
        lista = json.loads(modificadores)
    except (ValueError, TypeError):
        return ""
    etiquetas = {"quitar": "Sin", "doble": "Doble"}
    partes = [f"{etiquetas.get(m.get('modo'), m.get('modo'))} {m.get('ingrediente_nombre', '')}".strip()
              for m in lista if m.get("ingrediente_nombre")]
    return ", ".join(partes)
