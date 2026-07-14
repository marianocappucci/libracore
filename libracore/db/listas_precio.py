"""
Listas de precio: definición, ítems por producto, ajustes porcentuales e
importación entre listas. Extraído de database.py de Contalibra/
Restolibra (idéntico en ambos) como parte de la migración real a
libracore.db (Fase 3 de LibraCore, ver wiki/entities/libracore.md).
"""
from libracore.db.core import get_connection


def get_all_listas_precio(solo_activas: bool = False) -> list[dict]:
    with get_connection() as conn:
        where = "WHERE activa=1" if solo_activas else ""
        rows = conn.execute(
            f"SELECT * FROM listas_precio {where} ORDER BY es_default DESC, nombre"
        ).fetchall()
    return [dict(r) for r in rows]


def get_lista_precio(lista_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM listas_precio WHERE id=?", (lista_id,)).fetchone()
    return dict(row) if row else None


def create_lista_precio(nombre: str, descripcion: str = "") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO listas_precio (nombre, descripcion) VALUES (?,?)",
            (nombre, descripcion),
        )
        return cur.lastrowid


def update_lista_precio(lista_id: int, nombre: str, descripcion: str, activa: int):
    with get_connection() as conn:
        conn.execute(
            "UPDATE listas_precio SET nombre=?, descripcion=?, activa=? WHERE id=?",
            (nombre, descripcion, activa, lista_id),
        )


def delete_lista_precio(lista_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM listas_precio WHERE id=?", (lista_id,))


def get_lista_precio_items(lista_id: int, categoria: str = "") -> list[dict]:
    """Devuelve todos los productos activos con su precio en la lista dada."""
    with get_connection() as conn:
        where = "AND p.categoria=?" if categoria else ""
        params = [lista_id]
        if categoria:
            params.append(categoria)
        rows = conn.execute(f"""
            SELECT p.id, p.codigo, p.nombre, p.unidad, p.categoria,
                   p.precio_venta, p.precio_costo,
                   COALESCE(lpi.precio, 0) AS precio_lista,
                   CASE WHEN lpi.producto_id IS NOT NULL THEN 1 ELSE 0 END AS en_lista
            FROM productos p
            LEFT JOIN lista_precio_items lpi
                   ON lpi.lista_id=? AND lpi.producto_id=p.id
            WHERE p.activo=1 {where}
            ORDER BY p.categoria, p.nombre
        """, params).fetchall()
    return [dict(r) for r in rows]


def get_precio_en_lista(lista_id: int, producto_id: int) -> float | None:
    """Devuelve el precio del producto en la lista, o None si no está definido."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT precio FROM lista_precio_items WHERE lista_id=? AND producto_id=?",
            (lista_id, producto_id),
        ).fetchone()
    return float(row["precio"]) if row else None


def get_precios_lista_dict(lista_id: int) -> dict[int, float]:
    """Devuelve {producto_id: precio} para toda la lista (para el endpoint JSON)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT producto_id, precio FROM lista_precio_items WHERE lista_id=?",
            (lista_id,),
        ).fetchall()
    return {r["producto_id"]: r["precio"] for r in rows}


def save_lista_precio_items(lista_id: int, precios: dict):
    """Guarda o actualiza los precios de los productos en la lista.
    precios: {producto_id: precio}. Precio 0 elimina el ítem de la lista.
    """
    with get_connection() as conn:
        for pid_s, precio_s in precios.items():
            pid   = int(pid_s)
            precio = float(precio_s)
            if precio <= 0:
                conn.execute(
                    "DELETE FROM lista_precio_items WHERE lista_id=? AND producto_id=?",
                    (lista_id, pid),
                )
            else:
                conn.execute(
                    """INSERT INTO lista_precio_items (lista_id, producto_id, precio)
                       VALUES (?,?,?)
                       ON CONFLICT(lista_id, producto_id) DO UPDATE SET precio=excluded.precio""",
                    (lista_id, pid, precio),
                )


def apply_porcentaje_lista(lista_id: int, porcentaje: float,
                            base: str = "lista", categoria: str = "") -> int:
    """Aplica un ajuste porcentual a los precios de la lista.

    base: 'lista' (sobre precio actual), 'venta' (sobre precio_venta), 'costo' (sobre precio_costo).
    Devuelve la cantidad de productos actualizados.
    """
    factor = 1 + porcentaje / 100
    with get_connection() as conn:
        cat_where = "AND p.categoria=?" if categoria else ""
        cat_param = [categoria] if categoria else []

        if base == "lista":
            # Actualiza solo los que ya tienen precio en la lista
            rows = conn.execute(f"""
                SELECT lpi.producto_id, lpi.precio
                FROM lista_precio_items lpi
                JOIN productos p ON p.id = lpi.producto_id
                WHERE lpi.lista_id=? AND p.activo=1 {cat_where}
            """, [lista_id] + cat_param).fetchall()
            for r in rows:
                nuevo = round(r["precio"] * factor, 2)
                conn.execute(
                    "UPDATE lista_precio_items SET precio=? WHERE lista_id=? AND producto_id=?",
                    (nuevo, lista_id, r["producto_id"]),
                )
            return len(rows)
        else:
            col = "precio_venta" if base == "venta" else "precio_costo"
            rows = conn.execute(f"""
                SELECT id, {col} AS base_precio
                FROM productos WHERE activo=1 {cat_where}
            """, cat_param).fetchall()
            for r in rows:
                nuevo = round(r["base_precio"] * factor, 2)
                conn.execute(
                    """INSERT INTO lista_precio_items (lista_id, producto_id, precio)
                       VALUES (?,?,?)
                       ON CONFLICT(lista_id, producto_id) DO UPDATE SET precio=excluded.precio""",
                    (lista_id, r["id"], nuevo),
                )
            return len(rows)


def importar_precios_lista(lista_id: int, fuente: str, fuente_lista_id: int | None = None):
    """Importa precios a la lista desde otra fuente.

    fuente: 'venta', 'costo', 'lista' (requiere fuente_lista_id).
    """
    with get_connection() as conn:
        if fuente == "lista" and fuente_lista_id:
            rows = conn.execute(
                "SELECT producto_id, precio FROM lista_precio_items WHERE lista_id=?",
                (fuente_lista_id,),
            ).fetchall()
            for r in rows:
                conn.execute(
                    """INSERT INTO lista_precio_items (lista_id, producto_id, precio)
                       VALUES (?,?,?)
                       ON CONFLICT(lista_id, producto_id) DO UPDATE SET precio=excluded.precio""",
                    (lista_id, r["producto_id"], r["precio"]),
                )
        else:
            col = "precio_venta" if fuente == "venta" else "precio_costo"
            rows = conn.execute(
                f"SELECT id, {col} AS precio FROM productos WHERE activo=1"
            ).fetchall()
            for r in rows:
                conn.execute(
                    """INSERT INTO lista_precio_items (lista_id, producto_id, precio)
                       VALUES (?,?,?)
                       ON CONFLICT(lista_id, producto_id) DO UPDATE SET precio=excluded.precio""",
                    (lista_id, r["id"], r["precio"]),
                )
