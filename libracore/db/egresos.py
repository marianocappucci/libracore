"""
Categorías de egreso, proveedores, egresos y sus pagos. Extraído de
database.py de Contalibra/Restolibra (idéntico en ambos) como parte de la
migración real a libracore.db (Fase 3 de LibraCore, ver
wiki/entities/libracore.md).
"""
from libracore.db.core import get_connection
from libracore.db.caja import get_default_caja_id


def get_categorias_egreso() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM categorias_egreso ORDER BY nombre").fetchall()
    return [dict(r) for r in rows]


def create_categoria_egreso(nombre: str) -> int:
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO categorias_egreso (nombre) VALUES (?)", (nombre.strip(),))
        return cur.lastrowid


def delete_categoria_egreso(cid: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM categorias_egreso WHERE id=?", (cid,))


def get_all_proveedores(limit: int = 500) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM proveedores ORDER BY nombre LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_proveedor(pid: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM proveedores WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def search_proveedores(q: str) -> list[dict]:
    pat = f"%{q}%"
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM proveedores WHERE nombre LIKE ? OR cuit_dni LIKE ? ORDER BY nombre LIMIT 50",
            (pat, pat),
        ).fetchall()
    return [dict(r) for r in rows]


def create_proveedor(nombre: str, cuit_dni: str = "", email: str = "",
                     phone: str = "", address: str = "", iva_condition: str = "") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO proveedores (nombre, cuit_dni, email, phone, address, iva_condition) VALUES (?,?,?,?,?,?)",
            (nombre, cuit_dni, email, phone, address, iva_condition),
        )
        return cur.lastrowid


def update_proveedor(pid: int, nombre: str, cuit_dni: str = "", email: str = "",
                     phone: str = "", address: str = "", iva_condition: str = ""):
    with get_connection() as conn:
        conn.execute(
            "UPDATE proveedores SET nombre=?, cuit_dni=?, email=?, phone=?, address=?, iva_condition=? WHERE id=?",
            (nombre, cuit_dni, email, phone, address, iva_condition, pid),
        )


def delete_proveedor(pid: int):
    with get_connection() as conn:
        tiene = conn.execute("SELECT COUNT(*) FROM egresos WHERE proveedor_id=?", (pid,)).fetchone()[0]
        if tiene:
            raise ValueError("No se puede eliminar un proveedor con egresos asociados.")
        conn.execute("DELETE FROM proveedores WHERE id=?", (pid,))


def create_egreso(fecha: str, concepto: str, total: float, proveedor_id=None,
                  proveedor_nombre: str = "", tipo_comprobante: str = "otro",
                  numero: str = "", categoria: str = "", monto_neto: float = 0,
                  iva_pct: float = 0, iva_monto: float = 0,
                  observaciones: str = "", usuario_id=None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO egresos
               (fecha, proveedor_id, proveedor_nombre, tipo_comprobante, numero,
                categoria, concepto, monto_neto, iva_pct, iva_monto, total,
                estado, observaciones, usuario_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'pendiente',?,?)""",
            (fecha, proveedor_id, proveedor_nombre, tipo_comprobante, numero,
             categoria, concepto, monto_neto, iva_pct, iva_monto, total,
             observaciones, usuario_id),
        )
        return cur.lastrowid


def get_egreso(eid: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM egresos WHERE id=?", (eid,)).fetchone()
    return dict(row) if row else None


def get_all_egresos(desde: str = "", hasta: str = "", categoria: str = "",
                    estado: str = "", proveedor_id: int = 0, limit: int = 200) -> list[dict]:
    conds = []
    params: list = []
    if desde:
        conds.append("e.fecha >= ?"); params.append(desde)
    if hasta:
        conds.append("e.fecha <= ?"); params.append(hasta)
    if categoria:
        conds.append("e.categoria = ?"); params.append(categoria)
    if estado:
        conds.append("e.estado = ?"); params.append(estado)
    if proveedor_id:
        conds.append("e.proveedor_id = ?"); params.append(proveedor_id)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT e.*, p.nombre AS prov_nombre_lookup
                FROM egresos e
                LEFT JOIN proveedores p ON p.id = e.proveedor_id
                {where} ORDER BY e.fecha DESC, e.id DESC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_resumen_egresos(desde: str = "", hasta: str = "") -> dict:
    conds = []
    params: list = []
    if desde:
        conds.append("fecha >= ?"); params.append(desde)
    if hasta:
        conds.append("fecha <= ?"); params.append(hasta)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    with get_connection() as conn:
        row = conn.execute(
            f"""SELECT
                COALESCE(SUM(total), 0)                              AS total_periodo,
                COALESCE(SUM(CASE WHEN estado='pagado'   THEN total ELSE 0 END), 0) AS pagado,
                COALESCE(SUM(CASE WHEN estado!='pagado'  THEN total ELSE 0 END), 0) AS pendiente
                FROM egresos {where}""",
            params,
        ).fetchone()
    return dict(row)


def delete_egreso(eid: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM egresos WHERE id=?", (eid,))


def get_pagos_egreso(eid: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM egresos_pagos WHERE egreso_id=? ORDER BY id",
            (eid,),
        ).fetchall()
    return [dict(r) for r in rows]


def create_pago_egreso(egreso_id: int, fecha: str, monto: float,
                       caja_id=None, medio_pago: str = "",
                       referencia: str = "", usuario_id=None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO egresos_pagos (egreso_id, fecha, monto, caja_id, medio_pago, referencia, usuario_id)
               VALUES (?,?,?,?,?,?,?)""",
            (egreso_id, fecha, monto, caja_id or get_default_caja_id(),
             medio_pago, referencia, usuario_id),
        )
        pago_id = cur.lastrowid

        # Recalcular estado del egreso
        total = conn.execute("SELECT total FROM egresos WHERE id=?", (egreso_id,)).fetchone()[0]
        pagado = conn.execute(
            "SELECT COALESCE(SUM(monto),0) FROM egresos_pagos WHERE egreso_id=?", (egreso_id,)
        ).fetchone()[0]

        if pagado >= total:
            nuevo_estado = "pagado"
        elif pagado > 0:
            nuevo_estado = "parcial"
        else:
            nuevo_estado = "pendiente"

        conn.execute("UPDATE egresos SET estado=? WHERE id=?", (nuevo_estado, egreso_id))
        return pago_id
