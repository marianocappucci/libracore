"""
Datos agregados del dashboard principal. Extraído de database.py de
Contalibra/Restolibra (idéntico en ambos) como parte de la migración real
a libracore.db (Fase 3 de LibraCore, ver wiki/entities/libracore.md).
"""
from libracore.db.core import get_connection


def get_dashboard_data(mes_desde: str, mes_hasta: str) -> dict:
    """Devuelve todos los datos necesarios para el dashboard en una sola llamada."""
    _TIPOS_FACTURA = (1, 6, 11)
    with get_connection() as conn:
        # KPI 1: total facturado en el mes (solo facturas, no NC/ND)
        row = conn.execute(
            "SELECT COALESCE(SUM(total), 0) FROM facturas WHERE tipo IN (1,6,11) AND fecha BETWEEN ? AND ?",
            (mes_desde, mes_hasta),
        ).fetchone()
        facturado_mes = row[0]

        # KPI 2/3: ingresos y egresos de caja del mes
        row = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN tipo='ingreso' THEN monto ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN tipo='egreso'  THEN monto ELSE 0 END), 0)
               FROM caja_movimientos WHERE fecha BETWEEN ? AND ?""",
            (mes_desde, mes_hasta),
        ).fetchone()
        cobrado_mes = row[0]
        egresos_mes = row[1]

        # KPI 4: saldo total de caja (histórico)
        saldo_total = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN tipo='ingreso' THEN monto ELSE -monto END), 0) FROM caja_movimientos"
        ).fetchone()[0]

        # Cantidad de facturas emitidas en el mes
        cant_facturas_mes = conn.execute(
            "SELECT COUNT(*) FROM facturas WHERE tipo IN (1,6,11) AND fecha BETWEEN ? AND ?",
            (mes_desde, mes_hasta),
        ).fetchone()[0]

        # Facturas sin cobrar (tipo factura, sin ingreso en caja)
        rows = conn.execute(
            """SELECT f.id, f.tipo, f.punto_venta, f.numero, f.fecha, f.cliente_razon, f.total
               FROM facturas f
               LEFT JOIN caja_movimientos c ON c.factura_id = f.id AND c.tipo = 'ingreso'
               WHERE f.tipo IN (1,6,11) AND c.id IS NULL
               ORDER BY f.id DESC LIMIT 8""",
        ).fetchall()
        facturas_sin_cobrar = [dict(r) for r in rows]

        # Presupuestos pendientes de respuesta
        rows = conn.execute(
            "SELECT id, number, date, client_name, total FROM presupuestos WHERE status IN ('borrador','enviado','pendiente') ORDER BY id DESC LIMIT 8"
        ).fetchall()
        presupuestos_pendientes = [dict(r) for r in rows]

        # Últimos 6 movimientos de caja
        rows = conn.execute(
            "SELECT * FROM caja_movimientos ORDER BY fecha DESC, id DESC LIMIT 6"
        ).fetchall()
        ultimos_movimientos = [dict(r) for r in rows]

    return {
        "facturado_mes":        facturado_mes,
        "cobrado_mes":          cobrado_mes,
        "egresos_mes":          egresos_mes,
        "saldo_total":          saldo_total,
        "cant_facturas_mes":    cant_facturas_mes,
        "facturas_sin_cobrar":  facturas_sin_cobrar,
        "presupuestos_pendientes": presupuestos_pendientes,
        "ultimos_movimientos":  ultimos_movimientos,
    }
