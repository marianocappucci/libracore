"""
Recibos: numeración con retry ante colisión, alta, listado, búsqueda por
origen y anulación.

El recibo **no es fiscal** — no lleva CAE ni pasa por ARCA — así que la
numeración es interna y no hay dos pasos como en `facturas`: se pide el
próximo número y se inserta. Lo que sí se copia de `facturas` es el retry ante
`IntegrityError`, porque el agujero es el mismo: entre el `SELECT MAX(numero)`
y el `INSERT` puede colarse otro recibo del mismo punto de venta. Ahí eso
costó una auditoría (ver `create_factura`); acá nace cerrado.

La orquestación — de qué cobros se arma un recibo, cuándo emitir y cuándo
devolver el que ya existe — no vive acá sino en `libracore.recibos`. Este
módulo sólo escribe y lee filas.
"""
import json
import sqlite3

from libracore.db.core import get_connection

# Los tres orígenes posibles. Un recibo siempre nace de una operación que ya
# ocurrió: no se emite un recibo "suelto" porque el papel afirma que entró
# plata, y esa plata está registrada en algún lado.
ORIGEN_FACTURA = "factura"
ORIGEN_VENTA   = "venta"
ORIGEN_CC_PAGO = "cc_pago"

ORIGENES = (ORIGEN_FACTURA, ORIGEN_VENTA, ORIGEN_CC_PAGO)


def _row_a_dict(row) -> dict:
    d = dict(row)
    d["pagos"] = json.loads(d["pagos"] or "[]")
    d["anulado"] = bool(d["anulado"])
    return d


def get_next_recibo_numero(punto_venta: int = 1) -> int:
    """Devuelve el próximo número correlativo para `punto_venta`."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(numero) FROM recibos WHERE punto_venta=?", (punto_venta,)
        ).fetchone()
        return (row[0] or 0) + 1


def create_recibo(fecha, cliente_razon, origen_tipo, total, pagos,
                  punto_venta=1, numero=None, cliente_id=None, cliente_cuit="",
                  cliente_domicilio="", origen_id=None, concepto="",
                  observaciones="", usuario_id=None) -> int:
    """Inserta un recibo y devuelve su id.

    `numero` se calcula solo si no se pasa. Ante colisión con
    `idx_recibos_numero_unico` se recalcula y se reintenta — igual que
    `create_factura()`, y con la misma consecuencia para el caller: **el número
    real se lee releyendo el recibo** (`get_recibo`), nunca asumiendo que es el
    que se pasó.

    `pagos` es la lista de dicts que se guarda como snapshot. Cada uno lleva
    `fecha`, `medio_pago`, `referencia`, `monto` y —cuando el cobro salió de la
    caja— `caja_movimiento_id`, que es lo que después permite saber qué cobros
    ya tienen recibo.
    """
    if origen_tipo not in ORIGENES:
        raise ValueError(
            f"origen_tipo invalido: {origen_tipo!r} (esperado uno de {ORIGENES})"
        )

    MAX_INTENTOS = 5
    for intento in range(MAX_INTENTOS):
        if numero is None:
            numero = get_next_recibo_numero(punto_venta)
        try:
            with get_connection() as conn:
                cur = conn.execute(
                    """INSERT INTO recibos
                       (punto_venta, numero, fecha, cliente_id, cliente_razon,
                        cliente_cuit, cliente_domicilio, origen_tipo, origen_id,
                        concepto, total, pagos, observaciones, usuario_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (punto_venta, numero, fecha, cliente_id, cliente_razon,
                     cliente_cuit, cliente_domicilio, origen_tipo, origen_id,
                     concepto, float(total),
                     json.dumps(pagos or [], ensure_ascii=False),
                     observaciones, usuario_id),
                )
                return cur.lastrowid
        except sqlite3.IntegrityError as e:
            # Sólo la colisión de numeración se reintenta. `create_factura()`
            # atrapa cualquier IntegrityError porque sus columnas casi no
            # tienen FK; acá `cliente_id` y `usuario_id` sí las tienen, y un id
            # colgado reintentado cinco veces terminaría reportando "no se pudo
            # numerar" sobre un problema que no es de numeración.
            if "recibos.punto_venta" not in str(e) and "numero" not in str(e):
                raise
            if intento == MAX_INTENTOS - 1:
                raise
            numero = None


def get_recibo(recibo_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM recibos WHERE id=?", (recibo_id,)).fetchone()
        return _row_a_dict(row) if row else None


def get_recibos_de_origen(origen_tipo: str, origen_id: int,
                          incluir_anulados: bool = False) -> list[dict]:
    """Los recibos emitidos sobre una operación, del más viejo al más nuevo.

    Por defecto **excluye los anulados**, que es lo que quiere quien pregunta
    "¿esto ya tiene recibo?": un recibo anulado no cubre nada, y su plata
    vuelve a estar disponible para uno nuevo.
    """
    sql = "SELECT * FROM recibos WHERE origen_tipo=? AND origen_id=?"
    if not incluir_anulados:
        sql += " AND anulado=0"
    sql += " ORDER BY id"
    with get_connection() as conn:
        rows = conn.execute(sql, (origen_tipo, origen_id)).fetchall()
        return [_row_a_dict(r) for r in rows]


def get_recibos(desde="", hasta="", q="", cliente_id=None, incluir_anulados=True,
                limit=50, offset=0) -> list[dict]:
    """Listado para la pantalla, más nuevos primero."""
    conds, params = [], []
    if desde:
        conds.append("fecha >= ?"); params.append(desde)
    if hasta:
        conds.append("fecha <= ?"); params.append(hasta)
    if cliente_id is not None:
        conds.append("cliente_id = ?"); params.append(cliente_id)
    if q:
        conds.append("(CAST(numero AS TEXT) LIKE ? OR cliente_razon LIKE ?"
                     " OR cliente_cuit LIKE ? OR concepto LIKE ?)")
        params += [f"%{q}%"] * 4
    if not incluir_anulados:
        conds.append("anulado = 0")
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM recibos {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [_row_a_dict(r) for r in rows]


def contar_recibos(desde="", hasta="", q="", cliente_id=None,
                   incluir_anulados=True) -> int:
    """Total que matchea los mismos filtros que `get_recibos`, para paginar."""
    conds, params = [], []
    if desde:
        conds.append("fecha >= ?"); params.append(desde)
    if hasta:
        conds.append("fecha <= ?"); params.append(hasta)
    if cliente_id is not None:
        conds.append("cliente_id = ?"); params.append(cliente_id)
    if q:
        conds.append("(CAST(numero AS TEXT) LIKE ? OR cliente_razon LIKE ?"
                     " OR cliente_cuit LIKE ? OR concepto LIKE ?)")
        params += [f"%{q}%"] * 4
    if not incluir_anulados:
        conds.append("anulado = 0")
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    with get_connection() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM recibos {where}", tuple(params)
        ).fetchone()[0]


def anular_recibo(recibo_id: int, motivo: str = "", usuario_id=None) -> bool:
    """Marca el recibo como anulado. Devuelve False si ya lo estaba.

    **No borra.** El número queda consumido a propósito: un correlativo con
    huecos es una pregunta sin respuesta cuando alguien audita, y el papel ya
    salió impreso con ese número. Anular no toca la caja ni la cuenta
    corriente — el recibo es el comprobante del cobro, no el cobro; revertir la
    plata es una operación aparte y del producto.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE recibos SET anulado=1, anulado_motivo=?, "
            "anulado_at=datetime('now'), usuario_id=COALESCE(?, usuario_id) "
            "WHERE id=? AND anulado=0",
            (motivo, usuario_id, recibo_id),
        )
        return cur.rowcount > 0
