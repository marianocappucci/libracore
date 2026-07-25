"""
Depósitos (multi-depósito de stock), categorías de producto y catálogo de
productos. Migrado a libracore.db (Fase 3 de LibraCore, migración real,
Tier 2 — `generar_codigo_producto` y el manejo de `estacion`/`vendible`
eran solo de Restolibra y pasan a core, confirmado con el usuario — ver
wiki/entities/libracore.md).
"""
from libracore.db.core import get_connection


def get_all_depositos() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM depositos ORDER BY es_default DESC, nombre"
        ).fetchall()
    return [dict(r) for r in rows]


def get_deposito(did: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM depositos WHERE id=?", (did,)).fetchone()
    return dict(row) if row else None


def get_default_deposito_id() -> int | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM depositos WHERE es_default=1 LIMIT 1"
        ).fetchone()
        if not row:
            row = conn.execute("SELECT id FROM depositos ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def create_deposito(nombre: str, descripcion: str = "") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO depositos (nombre, descripcion) VALUES (?,?)",
            (nombre, descripcion),
        )
        return cur.lastrowid


def update_deposito(did: int, nombre: str, descripcion: str, activo: int):
    with get_connection() as conn:
        conn.execute(
            "UPDATE depositos SET nombre=?, descripcion=?, activo=? WHERE id=?",
            (nombre, descripcion, activo, did),
        )


def set_default_deposito(did: int):
    with get_connection() as conn:
        conn.execute("UPDATE depositos SET es_default=0")
        conn.execute("UPDATE depositos SET es_default=1 WHERE id=?", (did,))


def delete_deposito(did: int):
    with get_connection() as conn:
        tiene = conn.execute(
            "SELECT COUNT(*) FROM movimientos_stock WHERE deposito_id=?", (did,)
        ).fetchone()[0]
        if tiene:
            raise ValueError("No se puede eliminar un depósito con movimientos de stock.")
        es_default = conn.execute(
            "SELECT es_default FROM depositos WHERE id=?", (did,)
        ).fetchone()
        if es_default and es_default[0]:
            raise ValueError("No se puede eliminar el depósito por defecto.")
        conn.execute("DELETE FROM depositos WHERE id=?", (did,))


def get_stock_por_deposito(deposito_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT p.id, p.codigo, p.nombre, p.unidad, p.categoria,
                   p.stock_minimo, p.activo,
                   COALESCE(SUM(m.cantidad), 0) AS stock_actual
            FROM productos p
            LEFT JOIN movimientos_stock m ON m.producto_id = p.id AND m.deposito_id = ?
            WHERE p.activo = 1 AND p.tipo = 'producto'
            GROUP BY p.id
            HAVING stock_actual != 0 OR p.stock_minimo > 0
            ORDER BY p.nombre
        """, (deposito_id,)).fetchall()
    return [dict(r) for r in rows]


def get_stock_producto_todos_depositos(producto_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT d.id, d.nombre, d.es_default,
                   COALESCE(SUM(m.cantidad), 0) AS stock_actual
            FROM depositos d
            LEFT JOIN movimientos_stock m ON m.deposito_id = d.id AND m.producto_id = ?
            WHERE d.activo = 1
            GROUP BY d.id
            ORDER BY d.es_default DESC, d.nombre
        """, (producto_id,)).fetchall()
    return [dict(r) for r in rows]


def transferir_stock(producto_id: int, origen_id: int, destino_id: int,
                     cantidad: float, usuario_id: int | None = None,
                     fecha: str = "", observaciones: str = ""):
    from datetime import date as _date
    _fecha = fecha or _date.today().isoformat()
    stock_origen = 0.0
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cantidad),0) FROM movimientos_stock WHERE producto_id=? AND deposito_id=?",
            (producto_id, origen_id),
        ).fetchone()
        stock_origen = float(row[0])
    if cantidad > stock_origen:
        raise ValueError(f"Stock insuficiente en depósito origen (disponible: {stock_origen}).")
    ref = observaciones or "Transferencia entre depósitos"
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO movimientos_stock (producto_id, tipo, cantidad, referencia, usuario_id, fecha, deposito_id)
               VALUES (?,?,?,?,?,?,?)""",
            (producto_id, "transferencia_salida", -cantidad, ref, usuario_id, _fecha, origen_id),
        )
        conn.execute(
            """INSERT INTO movimientos_stock (producto_id, tipo, cantidad, referencia, usuario_id, fecha, deposito_id)
               VALUES (?,?,?,?,?,?,?)""",
            (producto_id, "transferencia_entrada", cantidad, ref, usuario_id, _fecha, destino_id),
        )


def get_categorias_producto() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id, nombre FROM categorias_producto ORDER BY nombre").fetchall()
    return [dict(r) for r in rows]


def create_categoria_producto(nombre: str) -> int:
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO categorias_producto (nombre) VALUES (?)", (nombre,))
        return cur.lastrowid


def delete_categoria_producto(cid: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM categorias_producto WHERE id=?", (cid,))


def create_producto(nombre: str, codigo: str = "", descripcion: str = "",
                    precio_venta: float = 0, precio_costo: float = 0,
                    unidad: str = "u", categoria: str = "",
                    stock_minimo: float = 0, estacion: str = "",
                    vendible: int = 1, tipo: str = "producto") -> int:
    if tipo not in ("producto", "servicio"):
        raise ValueError(f"tipo inválido: {tipo!r} (debe ser 'producto' o 'servicio')")
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO productos
               (codigo, nombre, descripcion, precio_venta, precio_costo,
                unidad, categoria, stock_minimo, estacion, vendible, tipo)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (codigo or None, nombre, descripcion, precio_venta, precio_costo,
             unidad, categoria, stock_minimo, estacion or "", vendible, tipo),
        )
        return cur.lastrowid


def generar_codigo_producto(categoria: str = "") -> str:
    """Genera un código único para un producto: prefijo según la categoría
    (3 primeras letras/dígitos en mayúscula, o 'PRD' si no hay) + secuencia
    correlativa dentro de ese prefijo. Ej.: categoría 'Bebidas' -> 'BEB-0001'."""
    import re
    base = re.sub(r"[^A-Za-z0-9]", "", (categoria or ""))[:3].upper() or "PRD"
    pat = re.compile(r"^" + re.escape(base) + r"-(\d+)$")
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT codigo FROM productos WHERE codigo LIKE ?", (base + "-%",)
        ).fetchall()
    maxn = 0
    for r in rows:
        m = pat.match(r["codigo"] or "")
        if m:
            maxn = max(maxn, int(m.group(1)))
    return f"{base}-{maxn + 1:04d}"


def get_all_productos(solo_activos: bool = False, q: str = "",
                      solo_vendibles: bool = False, tipo: str = "") -> list[dict]:
    with get_connection() as conn:
        where = []
        params = []
        if solo_activos:
            where.append("activo=1")
        if solo_vendibles:
            where.append("vendible=1")
        if tipo:
            if tipo not in ("producto", "servicio"):
                raise ValueError(f"tipo inválido: {tipo!r} (debe ser 'producto' o 'servicio')")
            where.append("tipo=?")
            params.append(tipo)
        if q:
            where.append("(nombre LIKE ? OR codigo LIKE ? OR categoria LIKE ?)")
            params += [f"%{q}%", f"%{q}%", f"%{q}%"]
        sql = "SELECT * FROM productos"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY nombre"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_producto(pid: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM productos WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None


def get_producto_by_codigo(codigo: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM productos WHERE codigo=? AND activo=1", (codigo,)
        ).fetchone()
        return dict(row) if row else None


def update_producto(pid: int, nombre: str, codigo: str, descripcion: str,
                    precio_venta: float, precio_costo: float,
                    unidad: str, categoria: str, activo: int,
                    stock_minimo: float = 0, estacion: str = "",
                    vendible: int = 1, tipo: str = "producto"):
    if tipo not in ("producto", "servicio"):
        raise ValueError(f"tipo inválido: {tipo!r} (debe ser 'producto' o 'servicio')")
    with get_connection() as conn:
        conn.execute(
            """UPDATE productos SET nombre=?, codigo=?, descripcion=?,
               precio_venta=?, precio_costo=?, unidad=?, categoria=?,
               activo=?, stock_minimo=?, estacion=?, vendible=?, tipo=?
               WHERE id=?""",
            (nombre, codigo or None, descripcion, precio_venta, precio_costo,
             unidad, categoria, activo, stock_minimo, estacion or "", vendible, tipo, pid),
        )


def delete_producto(pid: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM productos WHERE id=?", (pid,))
