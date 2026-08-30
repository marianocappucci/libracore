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
    # Donde la tabla la crea ESTE DDL la columna es TEXT. Pero donde la crea el
    # modelo de libraauth (porque `db_usuarios` se importa antes de que corra
    # este DDL) la columna es `timestamp`, y `timestamp >= text` no existe en
    # PostgreSQL. Era el error que trababa las suites de Contalibra y Restolibra:
    # 266 y 122 apariciones, la mayoria de sus rojos, el 2026-08-10. Por eso el
    # `::timestamp` sobre la columna: sirve para los dos tipos --sobre una
    # columna que ya es timestamp no hace nada-- y deja una sola consulta.
    #
    # 🔴 **Y el corte lo calcula la BASE, no el proceso.** Hasta el 2026-08-30
    # esto pasaba un `datetime.now()` de Python, o sea el reloj del PROCESO,
    # contra un `ts` que escribe el DEFAULT de la tabla con el reloj de la BASE
    # (`datetime('now','localtime')`, que el adaptador traduce a
    # `to_char(LOCALTIMESTAMP, ...)`). Si las dos zonas no coinciden, los
    # intentos recientes parecen viejos y la funcion devuelve **cero**, que
    # significa "nadie agoto intentos".
    #
    # Medido: con la base en `America/Argentina/Buenos_Aires` --la zona que el
    # estandar de la familia manda para produccion-- y el proceso en UTC, contaba
    # 0 en vez de 1. Contra una base en UTC pasaba, que es por lo que el CI no lo
    # veia. Y ya se desalinearon una vez de verdad: el barrido de huso del
    # 2026-08-23, donde el contenedor se movio y la base no.
    #
    # Con `LOCALTIMESTAMP` el mismo reloj escribe y compara, asi que la funcion
    # deja de depender de que nadie desalinee nada.
    #
    # ⚠️ **PERO NADIE LA LLAMA, y el commit que la arreglo decia otra cosa.**
    # Barrido del 2026-08-30 sobre los 12 repos: los unicos usos de esta funcion
    # y de `registrar_auth_event` son el `def` de aca, los tests, y el re-export
    # de `app/db_logs.py` en Contalibra y Restolibra --que reexporta, no llama--.
    # Cero call sites.
    #
    # El rate limiting que SI corre en los productos es el de
    # `libraauth.auth_events`, que llega por `session_auth.py` desde el router de
    # login del motor. Ese tiene su propio par escritor/lector y los dos usan el
    # reloj del PROCESO (`default=datetime.now` en el modelo, `datetime.now()` en
    # `contar_fallidos_recientes`), asi que es coherente consigo mismo: el
    # desfasaje de zona no lo afecta.
    #
    # O sea que el arreglo de arriba **no cerro un agujero vivo**: dejo sana una
    # funcion que hoy no defiende nada. Vale igual, porque esta exportada como
    # API publica de dos productos y el proximo que la enchufe no tiene por que
    # heredar el defecto. Pero no leerlo como que /login estaba desprotegido.
    #
    # 🔴 Y ojo con el detalle que hace que las dos implementaciones no sean
    # intercambiables: escriben `ts` con relojes distintos sobre LA MISMA tabla.
    # El modelo de libraauth declara `default=datetime.now` (proceso) **y**
    # `server_default=LOCALTIMESTAMP` (base); cual de los dos gana depende de si
    # la fila entra por el ORM o por SQL crudo, que es justo lo que distingue a
    # los dos escritores. Mezclarlos en una misma instancia reintroduce el
    # problema por la puerta de al lado.
    #
    # En SQLite la consulta queda como estaba, y por el mismo motivo:
    # `datetime('now','localtime', ?)` **ya** se evalua del lado de la base.
    from . import core

    with get_connection() as conn:
        if core.is_postgres():
            row = conn.execute(
                """SELECT COUNT(*) FROM auth_log
                   WHERE evento='login_fallido' AND ip=?
                     AND ts::timestamp >= LOCALTIMESTAMP - make_interval(mins => ?)""",
                (ip, int(minutos)),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT COUNT(*) FROM auth_log
                   WHERE evento='login_fallido' AND ip=?
                     AND ts >= datetime('now', 'localtime', ?)""",
                (ip, f"-{int(minutos)} minutes"),
            ).fetchone()
    return int(row[0])
