"""
Log de actividad (línea de tiempo unificada de ventas/caja/stock/facturas/
turnos/remitos/presupuestos) y log de autenticación (login/logout/intentos
fallidos). Extraído de database.py de Contalibra/Restolibra (idéntico en
ambos) como parte de la migración real a libracore.db (Fase 3 de
LibraCore, ver wiki/entities/libracore.md).
"""
from libracore.db.core import get_connection

_LOG_TIPOS = ("venta", "caja", "stock", "factura", "turno", "remito", "presupuesto")

def get_actividad_log(tipos=None, usuario_id=None, turno_id=None,
                      desde="", hasta="", limit=200, offset=0) -> list[dict]:
    """
    Devuelve una línea de tiempo unificada de todos los movimientos del sistema.
    Cada fila: {fecha, tipo, descripcion, monto, usuario, turno_id, ref_id, ref_tabla}
    """
    partes = []

    # — Ventas —
    partes.append("""
        SELECT
            v.created_at AS ts,
            v.fecha,
            'venta'       AS tipo,
            'Venta ' || v.numero ||
              CASE WHEN v.cliente_nombre != '' THEN ' — ' || v.cliente_nombre ELSE '' END
              || ' (' || v.estado || ')'  AS descripcion,
            v.total       AS monto,
            COALESCE(u.nombre, '')        AS usuario,
            v.turno_id,
            v.id          AS ref_id,
            'ventas'      AS ref_tabla
        FROM ventas v
        LEFT JOIN usuarios u ON u.id = v.usuario_id
    """)

    # — Caja —
    partes.append("""
        SELECT
            cm.created_at AS ts,
            cm.fecha,
            'caja'        AS tipo,
            cm.tipo || ': ' || cm.concepto AS descripcion,
            cm.monto      AS monto,
            COALESCE(u.nombre, '') AS usuario,
            NULL          AS turno_id,
            cm.id         AS ref_id,
            'caja_movimientos' AS ref_tabla
        FROM caja_movimientos cm
        LEFT JOIN usuarios u ON u.id = cm.usuario_id
    """)

    # — Stock —
    partes.append("""
        SELECT
            ms.created_at AS ts,
            ms.fecha,
            'stock'       AS tipo,
            ms.tipo || ' ' || p.nombre ||
              ' (' || CAST(ms.cantidad AS TEXT) || ' ' || p.unidad || ')'
              || CASE WHEN ms.referencia != '' THEN ' — ' || ms.referencia ELSE '' END
              AS descripcion,
            ABS(ms.cantidad) AS monto,
            COALESCE(u.nombre, '') AS usuario,
            NULL          AS turno_id,
            ms.id         AS ref_id,
            'movimientos_stock' AS ref_tabla
        FROM movimientos_stock ms
        JOIN productos p ON p.id = ms.producto_id
        LEFT JOIN usuarios u ON u.id = ms.usuario_id
    """)

    # — Facturas —
    partes.append("""
        SELECT
            f.created_at  AS ts,
            f.fecha,
            'factura'     AS tipo,
            'Factura tipo ' || f.tipo ||
              ' N° ' || printf('%04d', f.punto_venta) ||
              '-' || printf('%08d', f.numero) ||
              CASE WHEN f.cliente_razon IS NOT NULL AND f.cliente_razon != ''
                   THEN ' — ' || f.cliente_razon ELSE '' END
              AS descripcion,
            f.total       AS monto,
            COALESCE(u.nombre, '') AS usuario,
            NULL          AS turno_id,
            f.id          AS ref_id,
            'facturas'    AS ref_tabla
        FROM facturas f
        LEFT JOIN usuarios u ON u.id = f.usuario_id
    """)

    # — Turnos (apertura y cierre como eventos separados) —
    partes.append("""
        SELECT
            t.created_at  AS ts,
            DATE(t.apertura) AS fecha,
            'turno'       AS tipo,
            CASE t.estado
              WHEN 'abierto' THEN 'Turno #' || t.id || ' abierto — fondo $' || t.monto_inicial
              ELSE 'Turno #' || t.id || ' cerrado — declarado $' ||
                   COALESCE(CAST(t.monto_declarado_cierre AS TEXT), '0')
            END           AS descripcion,
            t.monto_inicial AS monto,
            COALESCE(u.nombre, '') AS usuario,
            t.id          AS turno_id,
            t.id          AS ref_id,
            'turnos_caja' AS ref_tabla
        FROM turnos_caja t
        JOIN usuarios u ON u.id = t.usuario_id
    """)

    # — Remitos —
    partes.append("""
        SELECT
            r.created_at  AS ts,
            r.date        AS fecha,
            'remito'      AS tipo,
            'Remito ' || r.number || ' — ' || r.client_name AS descripcion,
            r.total       AS monto,
            COALESCE(u.nombre, '') AS usuario,
            NULL          AS turno_id,
            r.id          AS ref_id,
            'remitos'     AS ref_tabla
        FROM remitos r
        LEFT JOIN usuarios u ON u.id = r.usuario_id
    """)

    # — Presupuestos —
    partes.append("""
        SELECT
            p.created_at  AS ts,
            p.date        AS fecha,
            'presupuesto' AS tipo,
            'Presupuesto ' || p.number || ' — ' || p.client_name ||
              ' (' || p.status || ')' AS descripcion,
            p.total       AS monto,
            COALESCE(u.nombre, '') AS usuario,
            NULL          AS turno_id,
            p.id          AS ref_id,
            'presupuestos' AS ref_tabla
        FROM presupuestos p
        LEFT JOIN usuarios u ON u.id = p.usuario_id
    """)

    # ── filtros post-UNION ──────────────────────────────────────────────────────
    where, params = [], []

    if tipos:
        marks = ",".join("?" * len(tipos))
        where.append(f"tipo IN ({marks})")
        params.extend(tipos)

    if usuario_id:
        # usuario solo está en ventas, stock, turnos; el resto da ''
        where.append("usuario_id_filter = ?")
        # se resuelve diferente — usamos subquery wrapper
    if desde:
        where.append("fecha >= ?"); params.append(desde)
    if hasta:
        where.append("fecha <= ?"); params.append(hasta)
    if turno_id:
        where.append("turno_id = ?"); params.append(turno_id)

    union_sql = "\nUNION ALL\n".join(partes)

    # Para filtrar por usuario necesitamos un wrapper con un JOIN auxiliar
    if usuario_id:
        # Re-construir solo las tablas que tienen usuario
        sql = f"""
            SELECT * FROM (
                {union_sql}
            ) sub
            WHERE usuario = (SELECT nombre FROM usuarios WHERE id=?)
        """
        params_final = [usuario_id] + params
        if where:
            sql += " AND " + " AND ".join(where)
    else:
        sql = f"""
            SELECT * FROM (
                {union_sql}
            ) sub
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        params_final = params

    sql += " ORDER BY ts DESC, ref_id DESC LIMIT ? OFFSET ?"
    params_final += [limit, offset]

    with get_connection() as conn:
        rows = conn.execute(sql, params_final).fetchall()
    return [dict(r) for r in rows]


def get_actividad_count(tipos=None, usuario_id=None, turno_id=None,
                        desde="", hasta="") -> int:
    """Cuenta total de filas para paginación."""
    rows = get_actividad_log(tipos=tipos, usuario_id=usuario_id, turno_id=turno_id,
                             desde=desde, hasta=hasta, limit=10000, offset=0)
    return len(rows)


def registrar_auth_event(evento: str, username: str, ip: str = "", detalle: str = ""):
    """Registra un evento de login, logout o intento fallido."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO auth_log (evento, username, ip, detalle) VALUES (?,?,?,?)",
            (evento, username, ip or "", detalle or ""),
        )
        conn.commit()


def get_auth_log(limit: int = 200, offset: int = 0) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM auth_log ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def contar_login_fallidos_recientes(ip: str, minutos: int = 15) -> int:
    """Cuenta intentos de login fallidos desde esta IP en los últimos
    `minutos` — base del rate limiting de `/login`. Ventana deslizante
    sobre `auth_log`, sin tabla ni estado nuevo."""
    if not ip:
        return 0

    # 🔴 El corte de la ventana se calcula distinto segun el motor, y no es un
    # capricho: `auth_log.ts` NO tiene el mismo tipo en todos los productos.
    #
    # Donde la tabla la crea ESTE DDL la columna es TEXT, y ahi lo correcto es
    # comparar lexicograficamente contra el texto de `datetime('now',
    # 'localtime')` -- que es justo lo que emite el adaptador. Pero donde la
    # crea el modelo de libraauth (porque `db_usuarios` se importa antes de que
    # corra este DDL) la columna es `timestamp`, y `timestamp >= text` no existe
    # en PostgreSQL. Era el error que trababa las suites de Contalibra y
    # Restolibra: 266 y 122 apariciones, la mayoria de sus rojos, el 2026-08-10.
    #
    # Pasando un `datetime` de Python la comparacion es timestamp contra
    # timestamp y anda con la columna de cualquiera de los dos tipos -- si es
    # TEXT, PostgreSQL castea. `datetime.now()` es hora local, igual que
    # `LOCALTIMESTAMP` y que el `datetime('now','localtime')` de SQLite.
    #
    # En SQLite la consulta queda como estaba: `sqlite3` no adapta objetos
    # `datetime` desde Python 3.12, y alla la columna siempre es TEXT.
    from . import core

    with get_connection() as conn:
        if core.is_postgres():
            from datetime import datetime as _dt, timedelta as _td

            row = conn.execute(
                """SELECT COUNT(*) FROM auth_log
                   WHERE evento='login_fallido' AND ip=? AND ts >= ?""",
                (ip, _dt.now() - _td(minutes=int(minutos))),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT COUNT(*) FROM auth_log
                   WHERE evento='login_fallido' AND ip=?
                     AND ts >= datetime('now', 'localtime', ?)""",
                (ip, f"-{int(minutos)} minutes"),
            ).fetchone()
    return int(row[0])
