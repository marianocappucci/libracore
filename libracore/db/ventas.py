"""
Ventas del punto de venta: numeración con retry ante colisión concurrente,
alta con pagos/caja/stock/turno en una única transacción, anulación con
reversión completa, y vinculación a factura/remito/MercadoPago. El dominio
más entrelazado — depende de libracore.db.caja (movimientos de caja por
cada pago), libracore.db.stock (descuento/reposición de stock),
libracore.db.turnos (vinculación al turno activo) y
libracore.db.cuenta_corriente (acreditar cuenta corriente al anular).
Migrado a libracore.db (Fase 3 de LibraCore, migración real, Tier 2 —
código ya idéntico entre productos, último módulo del corte — ver
wiki/entities/libracore.md).
"""
import contextlib
import json
import sqlite3

from libracore import medios_pago
from libracore.db.caja import create_caja_movimiento
from libracore.db.core import Conexion, _ar_now, get_connection
from libracore.db.cuenta_corriente import create_cc_pago
from libracore.db.stock import add_movimiento_stock, descontar_stock_venta
from libracore.db.turnos import get_turno_activo, vincular_venta_turno


def get_next_venta_numero(conn: Conexion | None = None) -> str:
    """Si se pasa `conn`, calcula el número dentro de esa transacción (ya con el
    write-lock tomado por el caller) para no chocar con otro cobro concurrente —
    ver `cobrar_pedido`. Sin `conn`, sigue siendo best-effort (uso legacy)."""
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        row = c.execute(
            "SELECT numero FROM ventas ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row:
        try:
            n = int(row["numero"].split("-")[-1]) + 1
        except (ValueError, IndexError):
            n = 1
    else:
        n = 1
    return f"V-{n:05d}"


def create_venta(numero: str, fecha: str, items: list, subtotal: float,
                 descuento: float, total: float, cliente_id: int | None,
                 cliente_nombre: str, usuario_id: int | None,
                 observaciones: str = "", estado: str = "cobrada",
                 conn: Conexion | None = None) -> int:
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        cur = c.execute(
            """INSERT INTO ventas
               (numero, fecha, items, subtotal, descuento, total,
                cliente_id, cliente_nombre, usuario_id, observaciones, estado)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (numero, fecha, json.dumps(items, ensure_ascii=False),
             subtotal, descuento, total,
             cliente_id, cliente_nombre, usuario_id, observaciones, estado),
        )
        return cur.lastrowid


def add_venta_pago(venta_id: int, medio: str, monto: float, referencia: str = "",
                   conn: Conexion | None = None):
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        c.execute(
            "INSERT INTO ventas_pagos (venta_id, medio, monto, referencia) VALUES (?,?,?,?)",
            (venta_id, medio, monto, referencia),
        )


def crear_venta_directa(fecha: str, items: list, subtotal: float, descuento: float,
                        total: float, cliente_id: int | None, cliente_nombre: str,
                        usuario_id: int | None, observaciones: str, estado: str,
                        pagos: list[dict], stock_habilitado: bool) -> int:
    """Crea una venta directa del módulo Ventas (mostrador, sin pedido/mesa de
    por medio) con sus pagos, un movimiento de caja por cada medio, descuento
    de stock y vinculación al turno activo — todo en una única transacción,
    mismo patrón que `cobrar_pedido`. Antes, cada paso abría su propia
    conexión: si algo fallaba a mitad de camino quedaba una venta huérfana
    sin pagos/caja/stock, y dos submits casi simultáneos (doble click) podían
    duplicar todo.

    El número de venta se calcula recién al entrar a la transacción; si dos o
    más ventas concurrentes chocan en el mismo número (`UNIQUE` en
    `ventas.numero`), se reintenta con un número fresco — mismo mecanismo que
    ya usa `crear_pedido` para mesas duplicadas. Cada intento fallido reduce
    la contención en al menos uno (el que ganó ese round ya commiteó), así
    que el número de reintentos necesarios está acotado por la cantidad de
    submits realmente simultáneos — en la práctica 1 (doble click)."""
    MAX_INTENTOS = 10
    for intento in range(MAX_INTENTOS):
        with get_connection() as conn:
            try:
                numero = get_next_venta_numero(conn=conn)
                venta_id = create_venta(
                    numero=numero, fecha=fecha, items=items,
                    subtotal=subtotal, descuento=descuento, total=total,
                    cliente_id=cliente_id, cliente_nombre=cliente_nombre,
                    usuario_id=usuario_id, observaciones=observaciones, estado=estado,
                    conn=conn,
                )
                for p in pagos:
                    add_venta_pago(venta_id, p["medio"], p["monto"],
                                   p.get("referencia", ""), conn=conn)
                    label = medios_pago.label(p["medio"])
                    create_caja_movimiento(
                        fecha=fecha, tipo="ingreso",
                        concepto=f"Venta {numero} — {label}",
                        monto=p["monto"], referencia=p.get("referencia", ""),
                        medio_pago=p["medio"], usuario_id=usuario_id, conn=conn,
                    )

                if stock_habilitado:
                    descontar_stock_venta(venta_id, items, fecha=fecha,
                                          usuario_id=usuario_id, conn=conn)

                if usuario_id:
                    turno = get_turno_activo(usuario_id, conn=conn)
                    if turno:
                        vincular_venta_turno(venta_id, turno["id"], conn=conn)

                conn.commit()
                return venta_id
            except sqlite3.IntegrityError:
                conn.rollback()
                if intento < MAX_INTENTOS - 1:
                    continue
                raise
            except Exception:
                conn.rollback()
                raise
    raise RuntimeError("No se pudo generar un número de venta único")


def get_all_ventas(desde: str = "", hasta: str = "", q: str = "",
                   tab: str = "todas", limit: int = 100, offset: int = 0) -> list[dict]:
    with get_connection() as conn:
        where, params = [], []
        if desde:
            where.append("v.fecha >= ?"); params.append(desde)
        if hasta:
            where.append("v.fecha <= ?"); params.append(hasta)
        if q:
            where.append("(v.numero LIKE ? OR v.cliente_nombre LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if tab == "sin_facturar":
            where.append("v.factura_id IS NULL AND v.estado != 'anulada'")
        elif tab == "facturadas":
            where.append("v.factura_id IS NOT NULL")
        sql = """SELECT v.*,
                        GROUP_CONCAT(p.medio || ':' || p.monto, '|') AS pagos_raw,
                        f.tipo    AS fac_tipo,
                        f.punto_venta AS fac_pv,
                        f.numero  AS fac_numero
                 FROM ventas v
                 LEFT JOIN ventas_pagos p ON p.venta_id = v.id
                 LEFT JOIN facturas f ON f.id = v.factura_id"""
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY v.id ORDER BY v.fecha DESC, v.id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = conn.execute(sql, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["items"] = json.loads(d["items"])
        d["pagos"] = _parse_pagos_raw(d.pop("pagos_raw", "") or "")
        if d.get("fac_tipo") and d.get("fac_numero"):
            pv  = str(d.get("fac_pv") or 0).zfill(4)
            num = str(d["fac_numero"]).zfill(8)
            d["factura_display"] = f"{d['fac_tipo']} {pv}-{num}"
        else:
            d["factura_display"] = None
        result.append(d)
    return result


def get_venta(vid: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM ventas WHERE id=?", (vid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["items"] = json.loads(d["items"])
        pagos = conn.execute(
            "SELECT * FROM ventas_pagos WHERE venta_id=? ORDER BY id", (vid,)
        ).fetchall()
        d["pagos"] = [dict(p) for p in pagos]
    return d


def _parse_pagos_raw(raw: str) -> list[dict]:
    pagos = []
    for part in raw.split("|"):
        if ":" in part:
            medio, monto = part.split(":", 1)
            try:
                pagos.append({"medio": medio, "monto": float(monto)})
            except ValueError:
                pass
    return pagos


def anular_venta(vid: int, usuario_id: int | None = None) -> None:
    """Anula una venta: repone el stock que se había descontado, revierte con
    un egreso cada movimiento de caja generado por sus pagos y, si tenía un
    pago a cuenta corriente, acredita la deuda del cliente (mismo mecanismo
    que un pago manual a cuenta — ver `nc_crear` en facturas.py). Todo en una
    única transacción; no-op si la venta ya estaba anulada, para no revertir
    dos veces si se reintenta la acción."""
    with get_connection() as conn:
        try:
            venta = conn.execute("SELECT * FROM ventas WHERE id=?", (vid,)).fetchone()
            if not venta:
                raise ValueError("Venta inexistente")
            if venta["estado"] == "anulada":
                return

            fecha = _ar_now().split(" ")[0]

            for m in conn.execute(
                "SELECT producto_id, cantidad, deposito_id FROM movimientos_stock "
                "WHERE venta_id=? AND tipo='venta'", (vid,)
            ).fetchall():
                add_movimiento_stock(
                    producto_id=m["producto_id"], tipo="anulacion",
                    cantidad=-m["cantidad"], referencia=f"Anulación venta ID {vid}",
                    venta_id=vid, usuario_id=usuario_id, fecha=fecha,
                    deposito_id=m["deposito_id"], conn=conn,
                )

            for p in conn.execute(
                "SELECT id, medio, monto FROM ventas_pagos WHERE venta_id=?", (vid,)
            ).fetchall():
                label = medios_pago.label(p["medio"])
                create_caja_movimiento(
                    fecha=fecha, tipo="egreso",
                    concepto=f"Anulación venta {venta['numero']} — {label}",
                    monto=p["monto"], referencia=f"anulacion:venta:{vid}:pago:{p['id']}",
                    medio_pago=p["medio"], usuario_id=usuario_id, conn=conn,
                )
                if p["medio"] == "cuenta_corriente" and venta["cliente_id"]:
                    create_cc_pago(
                        cliente_id=venta["cliente_id"], monto=p["monto"], fecha=fecha,
                        concepto=f"Anulación venta {venta['numero']}",
                        referencia="", medio_pago="cuenta_corriente",
                        caja_id=None, usuario_id=usuario_id, conn=conn,
                    )

            conn.execute("UPDATE ventas SET estado='anulada' WHERE id=?", (vid,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def vincular_venta_factura(vid: int, factura_id: int):
    with get_connection() as conn:
        conn.execute("UPDATE ventas SET factura_id=? WHERE id=?", (factura_id, vid))


def vincular_venta_remito(vid: int, remito_id: int):
    with get_connection() as conn:
        conn.execute("UPDATE ventas SET remito_id=? WHERE id=?", (remito_id, vid))


def set_venta_mp_order(venta_id: int, mp_order_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE ventas SET mp_order_id=? WHERE id=?",
            (mp_order_id, venta_id),
        )
        conn.commit()


def set_venta_mp_payment(venta_id: int, mp_payment_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE ventas SET mp_payment_id=? WHERE id=?",
            (mp_payment_id, venta_id),
        )
        conn.commit()


def get_venta_by_mp_order(mp_order_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM ventas WHERE mp_order_id=?", (mp_order_id,)
        ).fetchone()
        return dict(row) if row else None


def add_venta_pago_referencia_mp(venta_id: int, payment_id: str) -> None:
    """Actualiza la referencia del pago MP/billetera de la venta con el payment_id."""
    with get_connection() as conn:
        # El criterio de "medio electrónico" sale de `medios_pago`, no de un
        # `IN (...)` escrito acá: el mismo literal estaba repetido en tres repos
        # y era el único lugar donde `qr` existía como valor.
        conn.execute(
            f"""UPDATE ventas_pagos SET referencia=?
               WHERE venta_id=? AND {medios_pago.sql_es_electronico("medio")}
               AND (referencia IS NULL OR referencia='')""",
            (f"MP#{payment_id}", venta_id),
        )
        conn.commit()
