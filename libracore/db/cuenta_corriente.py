"""
Cuenta corriente por cliente: saldo y movimientos combinando ventas a
cuenta corriente, facturas cobradas a cuenta corriente, débitos directos
(`cc_debitos`) y pagos manuales (`cc_pagos`). Extraído de database.py de
Contalibra/Restolibra (idéntico en ambos) como parte de la migración real
a libracore.db (Fase 3 de LibraCore, ver wiki/entities/libracore.md).

## De dónde salen las ventas

El criterio de cálculo es siempre el mismo — **débitos por venta + débitos
por factura + débitos directos − abonos** — pero la tabla donde viven las
ventas no: los productos migrados a LibraCommerce las tienen en `sales`
(con `customer_party_id`/`occurred_on`/`number`) y los que todavía no, en
`ventas` (con `cliente_id`/`fecha`/`numero`). Eso se declara con un
`OrigenVentas` en vez de duplicar las funciones.

Hasta el 2026-07-28 esa duplicación era literal: Contalibra y Restolibra
tenían cada uno una copia byte-a-byte de este módulo (`db_cuenta_corriente.py`)
que sólo cambiaba el `JOIN`. Tres copias del mismo algoritmo de dinero, que
había que corregir tres veces.

## Cuando las ventas ni siquiera están en esta base

`OrigenVentas` alcanza mientras las ventas vivan en la misma base que la
caja. VentaLibra las tiene en un archivo SQLite separado (el de
LibraCommerce; acá sólo viven caja y facturas), así que ningún `JOIN` las
alcanza. Para ese caso está `cc_debitos`: el producto registra el débito
explícitamente al confirmar la venta fiada, con la misma forma que un
`cc_pago` pero del otro signo. La tabla queda vacía en los productos que no
la usan, así que suma cero y su saldo no cambia.
"""
import sqlite3
import contextlib
from dataclasses import dataclass

from libracore.db.core import get_connection

_TIPO_LABEL = {
    1: "FACTURA A", 6: "FACTURA B", 11: "FACTURA C",
    2: "ND A", 3: "NC A", 7: "ND B", 8: "NC B", 12: "ND C", 13: "NC C",
}


@dataclass(frozen=True)
class OrigenVentas:
    """En qué tabla de esta base están las ventas y cómo se llaman sus
    columnas. Los nombres se interpolan en el SQL, así que sólo pueden salir
    de las constantes de abajo — nunca de entrada de usuario."""

    tabla: str
    columna_cliente: str
    columna_fecha: str
    columna_numero: str


#: Productos que todavía no migraron sus ventas a LibraCommerce.
VENTAS_LIBRACORE = OrigenVentas("ventas", "cliente_id", "fecha", "numero")
#: Productos con las ventas ya en `sales`, en esta misma base (Contalibra
#: desde P7, Restolibra desde P8).
VENTAS_LIBRACOMMERCE = OrigenVentas("sales", "customer_party_id", "occurred_on", "number")


def _cuit_de(conn, cliente_id: int) -> str:
    row = conn.execute("SELECT cuit_dni FROM clients WHERE id=?", (cliente_id,)).fetchone()
    return (row["cuit_dni"] if row else "") or ""


def get_cc_saldo(cliente_id: int, origen: OrigenVentas = VENTAS_LIBRACORE) -> float:
    with get_connection() as conn:
        cuit = _cuit_de(conn, cliente_id)
        debitos_venta = conn.execute(f"""
            SELECT COALESCE(SUM(vp.monto), 0)
            FROM ventas_pagos vp
            JOIN {origen.tabla} v ON vp.venta_id = v.id
            WHERE v.{origen.columna_cliente} = ? AND vp.medio = 'cuenta_corriente'
        """, (cliente_id,)).fetchone()[0]
        debitos_factura = 0.0
        if cuit:
            debitos_factura = conn.execute("""
                SELECT COALESCE(SUM(cm.monto), 0)
                FROM caja_movimientos cm
                JOIN facturas f ON cm.factura_id = f.id
                WHERE f.cliente_cuit = ? AND cm.tipo = 'ingreso'
                  AND LOWER(cm.medio_pago) IN ('cuenta corriente','cuenta_corriente')
            """, (cuit,)).fetchone()[0]
        debitos_directos = conn.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM cc_debitos WHERE cliente_id = ?",
            (cliente_id,),
        ).fetchone()[0]
        abonos = conn.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM cc_pagos WHERE cliente_id = ?",
            (cliente_id,),
        ).fetchone()[0]
    return (
        float(debitos_venta) + float(debitos_factura) + float(debitos_directos) - float(abonos)
    )


def get_cc_movimientos(cliente_id: int, origen: OrigenVentas = VENTAS_LIBRACORE) -> list[dict]:
    with get_connection() as conn:
        cuit = _cuit_de(conn, cliente_id)
        movs = []

        rows = conn.execute(f"""
            SELECT v.{origen.columna_fecha} AS fecha, v.{origen.columna_numero} AS numero,
                   vp.monto, v.id AS venta_id
            FROM ventas_pagos vp
            JOIN {origen.tabla} v ON vp.venta_id = v.id
            WHERE v.{origen.columna_cliente} = ? AND vp.medio = 'cuenta_corriente'
        """, (cliente_id,)).fetchall()
        for r in rows:
            movs.append({
                "fecha": (r["fecha"] or "")[:10], "tipo": "debito",
                "concepto": f"Venta #{r['numero']}",
                "monto": r["monto"], "referencia": "", "medio": "",
                "venta_id": r["venta_id"], "factura_id": None, "cc_pago_id": None,
                "usuario_nombre": None,
            })

        if cuit:
            rows = conn.execute("""
                SELECT cm.fecha, f.tipo AS ftipo, f.punto_venta, f.numero,
                       cm.monto, f.id AS factura_id, cm.referencia, u.nombre AS usuario_nombre
                FROM caja_movimientos cm
                JOIN facturas f ON cm.factura_id = f.id
                LEFT JOIN usuarios u ON u.id = cm.usuario_id
                WHERE f.cliente_cuit = ? AND cm.tipo = 'ingreso'
                  AND LOWER(cm.medio_pago) IN ('cuenta corriente','cuenta_corriente')
            """, (cuit,)).fetchall()
            for r in rows:
                lbl = _TIPO_LABEL.get(r["ftipo"], "COMP")
                pv  = str(r["punto_venta"]).zfill(4)
                num = str(r["numero"]).zfill(8)
                movs.append({
                    "fecha": (r["fecha"] or "")[:10], "tipo": "debito",
                    "concepto": f"{lbl} {pv}-{num}",
                    "monto": r["monto"], "referencia": r["referencia"] or "",
                    "medio": "", "venta_id": None,
                    "factura_id": r["factura_id"], "cc_pago_id": None,
                    "usuario_nombre": r["usuario_nombre"],
                })

        rows = conn.execute("""
            SELECT cc_debitos.id, fecha, concepto, monto, referencia, u.nombre AS usuario_nombre
            FROM cc_debitos
            LEFT JOIN usuarios u ON u.id = cc_debitos.usuario_id
            WHERE cc_debitos.cliente_id = ? ORDER BY fecha, cc_debitos.id
        """, (cliente_id,)).fetchall()
        for r in rows:
            movs.append({
                "fecha": (r["fecha"] or "")[:10], "tipo": "debito",
                "concepto": r["concepto"] or "Venta a cuenta corriente",
                "monto": r["monto"], "referencia": r["referencia"] or "",
                "medio": "", "venta_id": None, "factura_id": None, "cc_pago_id": None,
                "cc_debito_id": r["id"], "usuario_nombre": r["usuario_nombre"],
            })

        rows = conn.execute("""
            SELECT cc_pagos.id, fecha, concepto, monto, referencia, medio_pago, u.nombre AS usuario_nombre
            FROM cc_pagos
            LEFT JOIN usuarios u ON u.id = cc_pagos.usuario_id
            WHERE cc_pagos.cliente_id = ? ORDER BY fecha, cc_pagos.id
        """, (cliente_id,)).fetchall()
        for r in rows:
            movs.append({
                "fecha": (r["fecha"] or "")[:10], "tipo": "credito",
                "concepto": r["concepto"] or "Pago a cuenta",
                "monto": r["monto"], "referencia": r["referencia"] or "",
                "medio": r["medio_pago"] or "",
                "venta_id": None, "factura_id": None, "cc_pago_id": r["id"],
                "usuario_nombre": r["usuario_nombre"],
            })

    return sorted(movs, key=lambda x: x["fecha"])


def get_cc_movimientos_periodo(
    cliente_id: int, desde: str, hasta: str, origen: OrigenVentas = VENTAS_LIBRACORE
) -> dict:
    """Movimientos de un rango de fechas más el saldo con el que se entra al rango.

    `desde`/`hasta` son fechas ISO (YYYY-MM-DD) inclusive. Se resuelve sobre
    `get_cc_movimientos()` en vez de repetir las tres consultas: la cuenta
    corriente es chica por cliente y así no hay riesgo de que el resumen que se
    manda por mail se calcule distinto que la pantalla.
    """
    movs = get_cc_movimientos(cliente_id, origen)

    def _signo(m):
        return float(m["monto"]) if m["tipo"] == "debito" else -float(m["monto"])

    anteriores = [m for m in movs if m["fecha"] and m["fecha"] < desde]
    del_periodo = [m for m in movs if desde <= (m["fecha"] or "") <= hasta]
    # Un movimiento sin fecha no se puede ubicar en el tiempo: entra en el
    # saldo anterior para que el saldo final siga cerrando con get_cc_saldo().
    sin_fecha = [m for m in movs if not m["fecha"]]

    saldo_anterior = sum(_signo(m) for m in anteriores + sin_fecha)
    total_debitos = sum(float(m["monto"]) for m in del_periodo if m["tipo"] == "debito")
    total_creditos = sum(float(m["monto"]) for m in del_periodo if m["tipo"] == "credito")

    return {
        "desde": desde,
        "hasta": hasta,
        "saldo_anterior": saldo_anterior,
        "movimientos": del_periodo,
        "total_debitos": total_debitos,
        "total_creditos": total_creditos,
        "saldo_final": saldo_anterior + total_debitos - total_creditos,
    }


def registrar_resumen_enviado(cliente_id: int, fecha: str, desde: str, hasta: str,
                              saldo: float, email: str, estado: str = "ok",
                              detalle: str = "", automatico: bool = True) -> int:
    """Deja rastro de cada intento de envío (ok o error) y, si salió bien,
    adelanta `cc_resumen_ultimo_envio` del cliente — que es lo que evita que
    una segunda corrida del cron el mismo día reenvíe el mismo resumen."""
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO cc_resumenes_enviados
               (cliente_id, fecha, periodo_desde, periodo_hasta, saldo, email,
                estado, detalle, automatico)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (cliente_id, fecha, desde, hasta, float(saldo), email or "",
             estado, detalle or "", 1 if automatico else 0),
        )
        if estado == "ok":
            conn.execute(
                "UPDATE clients SET cc_resumen_ultimo_envio=? WHERE id=?",
                (fecha, cliente_id),
            )
        return cur.lastrowid


def get_resumenes_enviados(cliente_id: int | None = None, limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        if cliente_id is None:
            rows = conn.execute(
                """SELECT r.*, c.name AS cliente_nombre
                   FROM cc_resumenes_enviados r
                   LEFT JOIN clients c ON c.id = r.cliente_id
                   ORDER BY r.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT r.*, c.name AS cliente_nombre
                   FROM cc_resumenes_enviados r
                   LEFT JOIN clients c ON c.id = r.cliente_id
                   WHERE r.cliente_id = ? ORDER BY r.id DESC LIMIT ?""",
                (cliente_id, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def get_clientes_con_saldo_cc(origen: OrigenVentas = VENTAS_LIBRACORE) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(f"""
            WITH dv AS (
                SELECT v.{origen.columna_cliente} AS cid, SUM(vp.monto) AS total
                FROM ventas_pagos vp JOIN {origen.tabla} v ON vp.venta_id = v.id
                WHERE vp.medio = 'cuenta_corriente' AND v.{origen.columna_cliente} IS NOT NULL
                GROUP BY v.{origen.columna_cliente}
            ),
            df AS (
                SELECT c.id AS cid, SUM(cm.monto) AS total
                FROM caja_movimientos cm
                JOIN facturas f ON cm.factura_id = f.id
                JOIN clients c ON c.cuit_dni = f.cliente_cuit
                WHERE cm.tipo = 'ingreso'
                  AND LOWER(cm.medio_pago) IN ('cuenta corriente','cuenta_corriente')
                GROUP BY c.id
            ),
            dd AS (
                SELECT cliente_id AS cid, SUM(monto) AS total
                FROM cc_debitos GROUP BY cliente_id
            ),
            cr AS (
                SELECT cliente_id AS cid, SUM(monto) AS total
                FROM cc_pagos GROUP BY cliente_id
            )
            SELECT c.id, c.name, c.cuit_dni, c.external_ref,
                   COALESCE(dv.total,0) + COALESCE(df.total,0) + COALESCE(dd.total,0)
                   - COALESCE(cr.total,0) AS saldo
            FROM clients c
            LEFT JOIN dv ON dv.cid = c.id
            LEFT JOIN df ON df.cid = c.id
            LEFT JOIN dd ON dd.cid = c.id
            LEFT JOIN cr ON cr.cid = c.id
            WHERE dv.cid IS NOT NULL OR df.cid IS NOT NULL
               OR dd.cid IS NOT NULL OR cr.cid IS NOT NULL
            ORDER BY saldo DESC, c.name
        """).fetchall()
    return [dict(r) for r in rows]


def create_cc_pago(cliente_id: int, monto: float, fecha: str, concepto: str,
                   referencia: str, medio_pago: str, caja_id, usuario_id,
                   conn: sqlite3.Connection | None = None) -> int:
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        cur = c.execute(
            """INSERT INTO cc_pagos
               (cliente_id, monto, fecha, concepto, referencia, medio_pago, caja_id, usuario_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (cliente_id, float(monto), fecha, concepto, referencia, medio_pago, caja_id, usuario_id),
        )
        return cur.lastrowid


def delete_cc_pago(pago_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM cc_pagos WHERE id=?", (pago_id,))


def create_cc_debito(cliente_id: int, monto: float, fecha: str, concepto: str = "",
                     referencia: str = "", usuario_id=None,
                     conn: sqlite3.Connection | None = None) -> int:
    """Registra deuda que no nace de una venta de ESTA base.

    Idempotente por `referencia` cuando se pasa una: el producto la arma con
    el id de su venta (`sale-12`), así que un reintento del cobro no fía dos
    veces lo mismo. Devuelve el id existente si ya estaba registrada — mismo
    criterio que `create_caja_movimiento`.
    """
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        if referencia:
            row = c.execute(
                "SELECT id FROM cc_debitos WHERE referencia = ?", (referencia,)
            ).fetchone()
            if row:
                return row["id"]
        cur = c.execute(
            """INSERT INTO cc_debitos
               (cliente_id, monto, fecha, concepto, referencia, usuario_id)
               VALUES (?,?,?,?,?,?)""",
            (cliente_id, float(monto), fecha, concepto, referencia, usuario_id),
        )
        return cur.lastrowid


def delete_cc_debito(debito_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM cc_debitos WHERE id=?", (debito_id,))
