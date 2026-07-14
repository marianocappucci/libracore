"""
Datos para Libro IVA Ventas (facturas) y Libro IVA Compras (egresos tipo
factura). Extraído de database.py de Contalibra/Restolibra (idéntico en
ambos) como parte de la migración real a libracore.db (Fase 3 de
LibraCore, ver wiki/entities/libracore.md).
"""
import json

from libracore.db.core import get_connection


def get_facturas_para_iva(desde: str, hasta: str) -> list[dict]:
    """Todas las facturas del período para Libro IVA Ventas."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM facturas
               WHERE fecha >= ? AND fecha <= ?
               ORDER BY fecha, punto_venta, numero""",
            (desde, hasta),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d.get("items") or "[]")
            result.append(d)
        return result


def get_egresos_para_iva(desde: str, hasta: str) -> list[dict]:
    """Egresos tipo factura del período para Libro IVA Compras, con CUIT proveedor."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT e.*, p.cuit_dni AS proveedor_cuit, p.iva_condition AS proveedor_iva_cond
               FROM egresos e
               LEFT JOIN proveedores p ON e.proveedor_id = p.id
               WHERE e.fecha >= ? AND e.fecha <= ?
               AND e.tipo_comprobante = 'factura'
               ORDER BY e.fecha, e.id""",
            (desde, hasta),
        ).fetchall()
        return [dict(r) for r in rows]
