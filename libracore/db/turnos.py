"""
Turnos de caja: apertura, cierre y resumen de ventas/cobros por turno.
Extraído de database.py de Contalibra/Restolibra (idéntico en ambos) como
parte de la migración real a libracore.db (Fase 3 de LibraCore, ver
wiki/entities/libracore.md).
"""
import contextlib

from libracore.db.core import Conexion, _ar_now, get_connection


def create_turno(usuario_id: int, monto_inicial: float, notas: str = "") -> int:
    apertura = _ar_now()
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO turnos_caja (usuario_id, apertura, monto_inicial, notas)
               VALUES (?,?,?,?)""",
            (usuario_id, apertura, monto_inicial, notas),
        )
        return cur.lastrowid


def get_turno_activo(usuario_id: int, conn: Conexion | None = None) -> dict | None:
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        row = c.execute(
            """SELECT t.*, u.nombre AS usuario_nombre
               FROM turnos_caja t JOIN usuarios u ON u.id = t.usuario_id
               WHERE t.usuario_id=? AND t.estado='abierto'
               ORDER BY t.id DESC LIMIT 1""",
            (usuario_id,),
        ).fetchone()
    return dict(row) if row else None


def get_turno_activo_any() -> dict | None:
    """Devuelve el primer turno abierto (para cajero sin usuario_id explícito)."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT t.*, u.nombre AS usuario_nombre
               FROM turnos_caja t JOIN usuarios u ON u.id = t.usuario_id
               WHERE t.estado='abierto' ORDER BY t.id DESC LIMIT 1"""
        ).fetchone()
    return dict(row) if row else None


def get_all_turnos(usuario_id: int | None = None, limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        if usuario_id:
            rows = conn.execute(
                """SELECT t.*, u.nombre AS usuario_nombre
                   FROM turnos_caja t JOIN usuarios u ON u.id = t.usuario_id
                   WHERE t.usuario_id=? ORDER BY t.id DESC LIMIT ?""",
                (usuario_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT t.*, u.nombre AS usuario_nombre
                   FROM turnos_caja t JOIN usuarios u ON u.id = t.usuario_id
                   ORDER BY t.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_turno(tid: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """SELECT t.*, u.nombre AS usuario_nombre
               FROM turnos_caja t JOIN usuarios u ON u.id = t.usuario_id
               WHERE t.id=?""",
            (tid,),
        ).fetchone()
    return dict(row) if row else None


def get_resumen_turno(tid: int) -> dict:
    """Devuelve ventas y totales por medio de pago del turno."""
    with get_connection() as conn:
        ventas = conn.execute(
            """SELECT v.id, v.numero, v.fecha, v.cliente_nombre, v.total, v.estado
               FROM ventas v WHERE v.turno_id=? ORDER BY v.id""",
            (tid,),
        ).fetchall()
        pagos = conn.execute(
            """SELECT vp.medio, SUM(vp.monto) AS total
               FROM ventas_pagos vp
               JOIN ventas v ON v.id = vp.venta_id
               WHERE v.turno_id=? AND v.estado='cobrada'
               GROUP BY vp.medio""",
            (tid,),
        ).fetchall()
    return {
        "ventas": [dict(v) for v in ventas],
        "pagos_por_medio": {r["medio"]: r["total"] for r in pagos},
        "total_ventas": sum(r["total"] for r in pagos),
        "efectivo_ventas": next((r["total"] for r in pagos if r["medio"] == "efectivo"), 0.0),
    }


def get_resumen_turno_caja(tid: int) -> dict:
    """Resumen del turno calculado sobre `caja_movimientos`, no sobre
    `ventas`.

    Es la variante para productos cuyas ventas NO viven en la tabla `ventas`
    de LibraCore — VentaLibra las tiene en LibraCommerce, así que
    `get_resumen_turno()` (que hace JOIN con `ventas`/`ventas_pagos`) le
    devolvería siempre vacío y el arqueo daría cero.

    Contar sobre la caja además es más fiel a lo que se arquea: entra todo lo
    que pasó por el cajón, incluidos ingresos y egresos que no son ventas."""
    with get_connection() as conn:
        movimientos = conn.execute(
            """SELECT id, fecha, tipo, concepto, monto, medio_pago, referencia
               FROM caja_movimientos WHERE turno_id=? ORDER BY id""",
            (tid,),
        ).fetchall()
        por_medio = conn.execute(
            """SELECT medio_pago, SUM(CASE WHEN tipo='egreso' THEN -monto ELSE monto END) AS total
               FROM caja_movimientos WHERE turno_id=? GROUP BY medio_pago""",
            (tid,),
        ).fetchall()
    pagos = {(r["medio_pago"] or "sin_medio"): r["total"] for r in por_medio}
    return {
        "movimientos": [dict(m) for m in movimientos],
        "pagos_por_medio": pagos,
        "total_ventas": sum(pagos.values()),
        # Lo unico que se cuenta a mano al cerrar es el efectivo: lo demas
        # queda en el resumen de la terminal o del banco.
        "efectivo_ventas": pagos.get("efectivo", 0.0),
    }


def cerrar_turno_caja(tid: int, monto_declarado: float, notas: str = "") -> dict | None:
    """Cierra el turno arqueando contra `caja_movimientos`
    (ver get_resumen_turno_caja). Devuelve el turno cerrado, con el esperado
    y la diferencia ya calculados, para no obligar al caller a releerlo."""
    turno = get_turno(tid)
    if not turno:
        return None
    resumen = get_resumen_turno_caja(tid)
    monto_esperado = round(turno["monto_inicial"] + resumen["efectivo_ventas"], 2)
    cierre = _ar_now()
    with get_connection() as conn:
        conn.execute(
            """UPDATE turnos_caja
               SET estado='cerrado', cierre=?, monto_declarado_cierre=?,
                   monto_esperado_cierre=?, notas=?
               WHERE id=?""",
            (cierre, monto_declarado, monto_esperado, notas, tid),
        )
    return get_turno(tid)


def cerrar_turno(tid: int, monto_declarado: float, notas: str = ""):
    turno = get_turno(tid)
    if not turno:
        return
    resumen = get_resumen_turno(tid)
    monto_esperado = round(turno["monto_inicial"] + resumen["efectivo_ventas"], 2)
    cierre = _ar_now()
    with get_connection() as conn:
        conn.execute(
            """UPDATE turnos_caja
               SET estado='cerrado', cierre=?, monto_declarado_cierre=?,
                   monto_esperado_cierre=?, notas=?
               WHERE id=?""",
            (cierre, monto_declarado, monto_esperado, notas, tid),
        )


def vincular_venta_turno(venta_id: int, turno_id: int, conn: Conexion | None = None):
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        c.execute("UPDATE ventas SET turno_id=? WHERE id=?", (turno_id, venta_id))
