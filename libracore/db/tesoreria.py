"""
Tesorería: cuentas (efectivo/banco/digital/otro), movimientos y
transferencias entre cuentas. Extraído de database.py de Contalibra/
Restolibra (idéntico en ambos) como parte de la migración real a
libracore.db (Fase 3 de LibraCore, ver wiki/entities/libracore.md).
"""
from libracore.db.core import get_connection

_TIPOS_CUENTA = {
    "efectivo": "Efectivo",
    "banco":    "Banco",
    "digital":  "Billetera digital",
    "otro":     "Otro",
}

def get_all_cuentas_tesoreria() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT c.*,
                c.saldo_inicial
                + COALESCE(SUM(CASE WHEN m.tipo IN ('ingreso','transferencia_entrada') THEN m.monto ELSE 0 END),0)
                - COALESCE(SUM(CASE WHEN m.tipo IN ('egreso', 'transferencia_salida')  THEN m.monto ELSE 0 END),0)
                AS saldo
            FROM cuentas_tesoreria c
            LEFT JOIN movimientos_tesoreria m ON m.cuenta_id = c.id
            WHERE c.activa = 1
            GROUP BY c.id
            ORDER BY c.orden, c.nombre
        """).fetchall()
    return [dict(r) for r in rows]


def get_cuenta_tesoreria(cid: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT c.*,
                c.saldo_inicial
                + COALESCE(SUM(CASE WHEN m.tipo IN ('ingreso','transferencia_entrada') THEN m.monto ELSE 0 END),0)
                - COALESCE(SUM(CASE WHEN m.tipo IN ('egreso', 'transferencia_salida')  THEN m.monto ELSE 0 END),0)
                AS saldo
            FROM cuentas_tesoreria c
            LEFT JOIN movimientos_tesoreria m ON m.cuenta_id = c.id
            WHERE c.id = ?
            GROUP BY c.id
        """, (cid,)).fetchone()
    return dict(row) if row else None


def create_cuenta_tesoreria(nombre, tipo, banco="", numero="", descripcion="", saldo_inicial=0) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO cuentas_tesoreria (nombre, tipo, banco, numero, descripcion, saldo_inicial)
               VALUES (?,?,?,?,?,?)""",
            (nombre, tipo, banco, numero, descripcion, float(saldo_inicial)),
        )
        return cur.lastrowid


def update_cuenta_tesoreria(cid, nombre, tipo, banco="", numero="", descripcion="", saldo_inicial=0):
    with get_connection() as conn:
        conn.execute(
            """UPDATE cuentas_tesoreria
               SET nombre=?, tipo=?, banco=?, numero=?, descripcion=?, saldo_inicial=?
               WHERE id=?""",
            (nombre, tipo, banco, numero, descripcion, float(saldo_inicial), cid),
        )


def delete_cuenta_tesoreria(cid: int):
    with get_connection() as conn:
        conn.execute("UPDATE cuentas_tesoreria SET activa=0 WHERE id=?", (cid,))


def get_movimientos_tesoreria(cuenta_id: int | None = None, limit: int = 200,
                               desde: str = "", hasta: str = "") -> list[dict]:
    conds, params = [], []
    if cuenta_id:
        conds.append("(m.cuenta_id=? OR m.cuenta_destino_id=?)")
        params += [cuenta_id, cuenta_id]
    if desde:
        conds.append("m.fecha >= ?"); params.append(desde)
    if hasta:
        conds.append("m.fecha <= ?"); params.append(hasta + " 23:59:59")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT m.*,
                   co.nombre AS cuenta_nombre,
                   cd.nombre AS cuenta_destino_nombre,
                   u.nombre AS usuario_nombre
            FROM movimientos_tesoreria m
            JOIN cuentas_tesoreria co ON co.id = m.cuenta_id
            LEFT JOIN cuentas_tesoreria cd ON cd.id = m.cuenta_destino_id
            LEFT JOIN usuarios u ON u.id = m.usuario_id
            {where}
            ORDER BY m.fecha DESC, m.id DESC
            LIMIT ?
        """, params + [limit]).fetchall()
    return [dict(r) for r in rows]


def create_movimiento_tesoreria(fecha, cuenta_id, tipo, monto, concepto="",
                                 referencia="", cuenta_destino_id=None,
                                 transferencia_id=None, usuario_id=None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO movimientos_tesoreria
               (fecha, cuenta_id, tipo, monto, concepto, referencia,
                cuenta_destino_id, transferencia_id, usuario_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (fecha, cuenta_id, tipo, float(monto), concepto, referencia or "",
             cuenta_destino_id, transferencia_id, usuario_id),
        )
        return cur.lastrowid


def create_transferencia_tesoreria(fecha, cuenta_origen_id, cuenta_destino_id,
                                    monto, concepto="", referencia="", usuario_id=None):
    """Crea dos movimientos enlazados: salida del origen, entrada al destino."""
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO movimientos_tesoreria
               (fecha, cuenta_id, tipo, monto, concepto, referencia,
                cuenta_destino_id, usuario_id)
               VALUES (?,?,'transferencia_salida',?,?,?,?,?)""",
            (fecha, cuenta_origen_id, float(monto), concepto, referencia or "",
             cuenta_destino_id, usuario_id),
        )
        salida_id = cur.lastrowid
        conn.execute(
            """INSERT INTO movimientos_tesoreria
               (fecha, cuenta_id, tipo, monto, concepto, referencia,
                cuenta_destino_id, transferencia_id, usuario_id)
               VALUES (?,?,'transferencia_entrada',?,?,?,?,?,?)""",
            (fecha, cuenta_destino_id, float(monto), concepto, referencia or "",
             cuenta_origen_id, salida_id, usuario_id),
        )
        conn.execute(
            "UPDATE movimientos_tesoreria SET transferencia_id=? WHERE id=?",
            (salida_id, salida_id),
        )


def delete_movimiento_tesoreria(mid: int):
    with get_connection() as conn:
        mov = conn.execute(
            "SELECT tipo, transferencia_id FROM movimientos_tesoreria WHERE id=?", (mid,)
        ).fetchone()
        if not mov:
            return
        # Si es parte de una transferencia, eliminar ambos lados
        if mov["transferencia_id"]:
            conn.execute(
                "DELETE FROM movimientos_tesoreria WHERE transferencia_id=?",
                (mov["transferencia_id"],),
            )
        else:
            conn.execute("DELETE FROM movimientos_tesoreria WHERE id=?", (mid,))


def get_resumen_tesoreria() -> dict:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT
                COALESCE(SUM(c.saldo_inicial
                    + COALESCE(ing.monto,0) - COALESCE(egr.monto,0)), 0) AS total
            FROM cuentas_tesoreria c
            LEFT JOIN (
                SELECT cuenta_id, SUM(monto) AS monto
                FROM movimientos_tesoreria
                WHERE tipo IN ('ingreso','transferencia_entrada')
                GROUP BY cuenta_id
            ) ing ON ing.cuenta_id = c.id
            LEFT JOIN (
                SELECT cuenta_id, SUM(monto) AS monto
                FROM movimientos_tesoreria
                WHERE tipo IN ('egreso','transferencia_salida')
                GROUP BY cuenta_id
            ) egr ON egr.cuenta_id = c.id
            WHERE c.activa = 1
        """).fetchone()
    return {"total": row["total"] if row else 0}
