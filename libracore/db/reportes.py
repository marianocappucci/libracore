"""
Reportes agregados de solo lectura (ventas, medios de pago, productos top,
caja, stock bajo, resumen). Extraído de database.py de Contalibra/
Restolibra (idéntico en ambos) como parte de la migración real a
libracore.db (Fase 3 de LibraCore, ver wiki/entities/libracore.md).
"""
from libracore.db.core import get_connection


def get_reporte_ventas(desde: str = "", hasta: str = "", agrupacion: str = "dia") -> list[dict]:
    """Ventas agrupadas por día, semana o mes."""
    fmt = {"dia": "%Y-%m-%d", "semana": "%Y-W%W", "mes": "%Y-%m"}.get(agrupacion, "%Y-%m-%d")
    where, params = [], []
    if desde:
        where.append("fecha >= ?"); params.append(desde)
    if hasta:
        where.append("fecha <= ?"); params.append(hasta)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT strftime('{fmt}', fecha) AS periodo,
               COUNT(*) AS cantidad,
               ROUND(SUM(total), 2) AS total
        FROM ventas {w}
        GROUP BY periodo ORDER BY periodo
    """
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_reporte_medios_pago(desde: str = "", hasta: str = "") -> list[dict]:
    """Totales por medio de pago en el período."""
    where, params = [], []
    if desde:
        where.append("v.fecha >= ?"); params.append(desde)
    if hasta:
        where.append("v.fecha <= ?"); params.append(hasta)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT vp.medio, COUNT(DISTINCT vp.venta_id) AS operaciones,
               ROUND(SUM(vp.monto), 2) AS total
        FROM ventas_pagos vp
        JOIN ventas v ON v.id = vp.venta_id {w}
        GROUP BY vp.medio ORDER BY total DESC
    """
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_reporte_productos_top(desde: str = "", hasta: str = "", limit: int = 20) -> list[dict]:
    """Productos más vendidos (por cantidad y por monto) en el período."""
    where, params = [], []
    if desde:
        where.append("v.fecha >= ?"); params.append(desde)
    if hasta:
        where.append("v.fecha <= ?"); params.append(hasta)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT ji.value->>'$.nombre' AS nombre,
               ROUND(SUM(CAST(ji.value->>'$.qty' AS REAL)), 2) AS cantidad,
               ROUND(SUM(CAST(ji.value->>'$.qty' AS REAL) *
                         CAST(ji.value->>'$.precio' AS REAL)), 2) AS total
        FROM ventas v, json_each(v.items) ji {w}
        GROUP BY nombre ORDER BY cantidad DESC LIMIT ?
    """
    params.append(limit)
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_reporte_caja(desde: str = "", hasta: str = "") -> list[dict]:
    """Movimientos de caja por tipo en el período."""
    where, params = [], []
    if desde:
        where.append("fecha >= ?"); params.append(desde)
    if hasta:
        where.append("fecha <= ?"); params.append(hasta)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT tipo, COUNT(*) AS cantidad, ROUND(SUM(monto), 2) AS total
        FROM caja_movimientos {w}
        GROUP BY tipo ORDER BY total DESC
    """
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_reporte_caja_medios(desde: str = "", hasta: str = "", caja_id: int = 0) -> list[dict]:
    """Movimientos de caja agrupados por caja y medio de pago."""
    where, params = ["cm.fecha BETWEEN ? AND ?"], [desde or "1900-01-01", hasta or "2999-12-31"]
    if caja_id:
        where.append("cm.caja_id = ?"); params.append(caja_id)
    sql = f"""
        SELECT
            COALESCE(c.nombre, 'Sin caja')  AS caja_nombre,
            COALESCE(cm.caja_id, 0)         AS caja_id,
            LOWER(COALESCE(NULLIF(cm.medio_pago,''), 'sin_especificar')) AS medio,
            cm.tipo,
            COUNT(*)                         AS operaciones,
            ROUND(SUM(cm.monto), 2)          AS total
        FROM caja_movimientos cm
        LEFT JOIN cajas c ON c.id = cm.caja_id
        WHERE {" AND ".join(where)}
        GROUP BY cm.caja_id, c.nombre, LOWER(COALESCE(NULLIF(cm.medio_pago,''), 'sin_especificar')), cm.tipo
        ORDER BY caja_nombre, cm.tipo DESC, medio
    """
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_reporte_stock_bajo() -> list[dict]:
    """Productos con stock actual por debajo del mínimo."""
    sql = """
        SELECT p.id, p.nombre, p.codigo, p.stock_minimo,
               ROUND(COALESCE(SUM(ms.cantidad), 0), 3) AS stock_actual
        FROM productos p
        LEFT JOIN movimientos_stock ms ON ms.producto_id = p.id
        GROUP BY p.id
        HAVING COALESCE(SUM(ms.cantidad), 0) < p.stock_minimo
        ORDER BY (p.stock_minimo - stock_actual) DESC
    """
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def get_reporte_resumen(desde: str = "", hasta: str = "") -> dict:
    """KPIs rápidos para el período."""
    where, params = [], []
    if desde:
        where.append("fecha >= ?"); params.append(desde)
    if hasta:
        where.append("fecha <= ?"); params.append(hasta)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    with get_connection() as conn:
        v = conn.execute(
            f"SELECT COUNT(*) cnt, ROUND(SUM(total),2) total FROM ventas {w}", params
        ).fetchone()
        f_row = conn.execute(
            f"SELECT COUNT(*) cnt FROM facturas {w}", params
        ).fetchone()
        caja = conn.execute(
            f"SELECT ROUND(SUM(CASE WHEN tipo='ingreso' THEN monto ELSE -monto END),2) saldo FROM caja_movimientos {w}", params
        ).fetchone()
    return {
        "ventas_cantidad": v["cnt"] or 0,
        "ventas_total":    v["total"] or 0.0,
        "facturas_cantidad": f_row["cnt"] or 0,
        "caja_saldo":      caja["saldo"] or 0.0,
    }
