"""
Cajas (configuración de puntos de cobro) y movimientos de caja. Extraído
de database.py de Contalibra/Restolibra (idéntico en ambos) como parte de
la migración real a libracore.db (Fase 3 de LibraCore, ver
wiki/entities/libracore.md).
"""
import json
from .core import Conexion
import contextlib

from libracore import medios_pago
from libracore.db.core import get_connection

# 🔴 **La lista vive en `libracore.medios_pago`, no acá.** Esto es un alias para
# no romper a los seis productos que importan este nombre desde hace meses; el
# vocabulario —qué se puede elegir, qué grafías viejas hay que saber leer y a
# cuál equivale cada una— está en ese módulo, con el porqué de cada una.
#
# Importar el alias trae **la lista nueva**: `tarjeta_debito`, `tarjeta_credito`
# y `cheque` se suman a las seis de siempre. Es a propósito — un producto que
# arma su caja con `list(MEDIOS_PAGO_LABELS)` pasa a ofrecerlos sin tocar nada.
MEDIOS_PAGO_LABELS = medios_pago.ELEGIBLES

# --- La cuenta corriente como medio -------------------------------------
#
# No es un medio de cobro: es la marca de que la venta o el comprobante se
# hicieron a crédito. Ver `libracore.cobros` para el porqué completo.
#
# Ese criterio se consulta desde SIETE lugares del SQL de este motor —tres acá,
# tres en `cuenta_corriente.py` y uno en `facturas.py`—, y hasta el 2026-08-03
# el literal estaba escrito a mano en los siete. Un octavo lugar, el `frozenset`
# de `cobros.py`, tenía su propia copia.
#
# Las dos grafías son las que conviven en la base: la vieja con espacio, que
# escriben los movimientos de la emisión, y la del selector actual. **No sacar
# ninguna de las dos**: hay movimientos históricos con cada una, y perder una
# cambiaría saldos ya calculados.
MEDIO_CUENTA_CORRIENTE = "cuenta_corriente"
MEDIOS_CUENTA_CORRIENTE = ("cuenta corriente", "cuenta_corriente")

# Se arma una sola vez y se interpola: son valores fijos de este módulo, nunca
# entrada de usuario. Va como texto y no como parámetros porque estos fragmentos
# se concatenan dentro de consultas que ya llevan sus propios `?` posicionales,
# y sumar parámetros ahí obligaría a que cada caller los ordene bien --
# exactamente el tipo de detalle que hace que una consulta de saldos falle en
# silencio.
_LISTA_CC = ",".join(f"'{m}'" for m in MEDIOS_CUENTA_CORRIENTE)


def sql_es_cuenta_corriente(columna: str = "medio_pago") -> str:
    """Fragmento SQL: el movimiento ES una marca de cuenta corriente (deuda)."""
    return f"LOWER({columna}) IN ({_LISTA_CC})"


def sql_no_es_cuenta_corriente(columna: str = "medio_pago") -> str:
    """Fragmento SQL: el movimiento NO es cuenta corriente, o sea que es plata
    de verdad y cuenta como cobrado."""
    return f"LOWER({columna}) NOT IN ({_LISTA_CC})"


def get_all_cajas() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM cajas ORDER BY es_default DESC, nombre"
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["medios_pago"] = json.loads(d["medios_pago"] or "[]")
        result.append(d)
    return result


def get_caja_config(cid: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM cajas WHERE id=?", (cid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["medios_pago"] = json.loads(d["medios_pago"] or "[]")
    return d


def get_default_caja_id() -> int | None:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM cajas WHERE es_default=1 LIMIT 1").fetchone()
        if not row:
            row = conn.execute("SELECT id FROM cajas ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def create_caja_config(nombre: str, descripcion: str, medios_pago: list) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO cajas (nombre, descripcion, medios_pago) VALUES (?,?,?)",
            (nombre, descripcion, json.dumps(medios_pago)),
        )
        return cur.lastrowid


def update_caja_config(cid: int, nombre: str, descripcion: str, medios_pago: list, activo: int):
    with get_connection() as conn:
        conn.execute(
            "UPDATE cajas SET nombre=?, descripcion=?, medios_pago=?, activo=? WHERE id=?",
            (nombre, descripcion, json.dumps(medios_pago), activo, cid),
        )


def set_default_caja(cid: int):
    with get_connection() as conn:
        conn.execute("UPDATE cajas SET es_default=0")
        conn.execute("UPDATE cajas SET es_default=1 WHERE id=?", (cid,))


def delete_caja_config(cid: int):
    with get_connection() as conn:
        tiene = conn.execute(
            "SELECT COUNT(*) FROM caja_movimientos WHERE caja_id=?", (cid,)
        ).fetchone()[0]
        if tiene:
            raise ValueError("No se puede eliminar una caja con movimientos registrados.")
        if conn.execute("SELECT es_default FROM cajas WHERE id=?", (cid,)).fetchone()[0]:
            raise ValueError("No se puede eliminar la caja por defecto.")
        conn.execute("DELETE FROM cajas WHERE id=?", (cid,))


def create_caja_movimiento(fecha, tipo, concepto, monto, referencia="", factura_id=None,
                           usuario_id=None, caja_id=None, medio_pago="", turno_id=None,
                           conn: Conexion | None = None):
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        # Idempotencia: si ya existe un movimiento con la misma referencia PARA LA MISMA
        # factura (o, si no hay factura, otro movimiento sin factura con esa referencia),
        # no duplicar. Antes el chequeo era global por referencia sin mirar factura_id: una
        # misma transferencia real que cubre dos facturas distintas (misma referencia
        # bancaria en ambos cobros) bloqueaba silenciosamente el movimiento de la segunda
        # factura, aunque el saldo de cuenta corriente sí se actualizaba — la factura
        # quedaba "Sin cobrar" pese a estar paga.
        if referencia:
            if factura_id is not None:
                exists = c.execute(
                    "SELECT id FROM caja_movimientos WHERE referencia=? AND factura_id=? LIMIT 1",
                    (referencia, factura_id),
                ).fetchone()
            else:
                exists = c.execute(
                    "SELECT id FROM caja_movimientos WHERE referencia=? AND factura_id IS NULL LIMIT 1",
                    (referencia,),
                ).fetchone()
            if exists:
                return exists[0]
        _caja_id = caja_id or get_default_caja_id()
        cur = c.execute(
            """INSERT INTO caja_movimientos
               (fecha, tipo, concepto, monto, referencia, factura_id, usuario_id, caja_id,
                medio_pago, turno_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (fecha, tipo, concepto, float(monto), referencia, factura_id, usuario_id, _caja_id,
             medio_pago, turno_id),
        )
        return cur.lastrowid


def get_caja_movimientos(desde=None, hasta=None, limit=500, caja_id=None):
    with get_connection() as conn:
        where, params = [], []
        if desde and hasta:
            where.append("cm.fecha BETWEEN ? AND ?"); params += [desde, hasta]
        if caja_id:
            where.append("cm.caja_id = ?"); params.append(caja_id)
        sql = """SELECT cm.*, c.nombre AS caja_nombre, u.nombre AS usuario_nombre
                 FROM caja_movimientos cm
                 LEFT JOIN cajas c ON c.id = cm.caja_id
                 LEFT JOIN usuarios u ON u.id = cm.usuario_id"""
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY cm.fecha DESC, cm.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_caja_resumen(desde=None, hasta=None, caja_id=None):
    """Devuelve {ingresos, egresos, saldo_periodo, saldo_total}.

    Excluye movimientos con medio_pago='cuenta_corriente' — no es efectivo
    real, es una venta/factura a cuenta (o su reversión, ver `anular_venta`),
    así que no debe inflar (ni, en la reversión, desinflar) el resumen de
    caja. Mismo criterio que ya usa `get_facturas_filtradas` para saber si
    una factura está "cobrada" (ver `_cc_excl` ahí)."""
    _cc_excl = sql_no_es_cuenta_corriente()
    with get_connection() as conn:
        where, params = [_cc_excl], []
        if desde and hasta:
            where.append("fecha BETWEEN ? AND ?"); params += [desde, hasta]
        if caja_id:
            where.append("caja_id = ?"); params.append(caja_id)
        w = "WHERE " + " AND ".join(where)
        row = conn.execute(
            f"""SELECT
                  COALESCE(SUM(CASE WHEN tipo='ingreso' THEN monto ELSE 0 END), 0) AS ingresos,
                  COALESCE(SUM(CASE WHEN tipo='egreso'  THEN monto ELSE 0 END), 0) AS egresos
                FROM caja_movimientos {w}""",
            params,
        ).fetchone()
        ingresos = row["ingresos"]
        egresos  = row["egresos"]

        total = conn.execute(
            f"""SELECT COALESCE(SUM(CASE WHEN tipo='ingreso' THEN monto ELSE -monto END), 0)
               FROM caja_movimientos WHERE {_cc_excl}"""
        ).fetchone()[0]

        return {
            "ingresos":     ingresos,
            "egresos":      egresos,
            "saldo_periodo": ingresos - egresos,
            "saldo_total":  total,
        }


def get_cobro_factura(factura_id):
    """Devuelve el último movimiento de cobro de una factura, o None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM caja_movimientos WHERE factura_id=? AND tipo='ingreso'"
            f" AND {sql_no_es_cuenta_corriente()}"
            " ORDER BY id DESC LIMIT 1",
            (factura_id,),
        ).fetchone()
        return dict(row) if row else None


def get_cobros_factura(factura_id) -> list[dict]:
    """Devuelve todos los movimientos de cobro de una factura."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM caja_movimientos WHERE factura_id=? AND tipo='ingreso'"
            f" AND {sql_no_es_cuenta_corriente()}"
            " ORDER BY id",
            (factura_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_caja_movimiento(mov_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM caja_movimientos WHERE id=?", (mov_id,))
