"""
Turnos de caja: apertura, cierre y resumen de ventas/cobros por turno.
Extraído de database.py de Contalibra/Restolibra (idéntico en ambos) como
parte de la migración real a libracore.db (Fase 3 de LibraCore, ver
wiki/entities/libracore.md).
"""
import contextlib
from collections.abc import Callable

from libracore import ganchos_de_caja
from libracore.db import cierre_diario
from libracore.db.core import Conexion, _ar_now, get_connection


def _conexion(conn: Conexion | None):
    """La conexión de quien llama (sin confirmar ni cerrar nada) o una propia, que confirma al salir."""
    return contextlib.nullcontext(conn) if conn is not None else get_connection()


def create_turno(usuario_id: int, monto_inicial: float, notas: str = "",
                 caja_id: int | None = None, *, conn: Conexion | None = None,
                 ganchos: ganchos_de_caja.GanchosDeCaja | None = None,
                 apertura: str | None = None, origen: str | None = None,
                 origen_id: int | None = None) -> int:
    """Abre el turno. `caja_id` dice **sobre qué mostrador**.

    🔑 Opcional a propósito: los productos que no tienen cajas múltiples abren el
    turno suelto, como siempre. El que sí las tiene guarda la caja acá y no en
    cada movimiento, para que el arqueo del turno sea el de ESE mostrador.

    🔴 **Guarda de cierre diario, acá y no en cada producto.** Éste es el
    camino común: VentaLibra y LibraClub abren turnos llamando a esta función
    directamente, así que la guarda los cubre a los dos sin que ninguno haya
    tenido que tocar su alta de turnos. Para un producto sin sucursales
    (Contalibra, Restolibra) `cierre_diario.dia_cerrado()` da siempre `False`
    —esa tabla queda vacía si nadie llama a `cerrar_dia()`— así que no cambia
    nada. Levanta `cierre_diario.DiaCerradoError` si el día operativo de la
    sucursal de esa caja ya se cerró.

    **La caja de un nodo (ADR-076).** Todo lo que sigue es opcional y sin nada de
    eso la función se comporta como siempre:

    - `conn`: escribe en la transacción de quien llama, que confirma (como el
      resto de `libracore.db`). Sin `conn`, abre la suya y confirma al salir.
    - `ganchos`: `GanchosDeCaja`. Corre `al_abrir_turno(conn, datos)` **dentro de
      la misma transacción**, después del `INSERT`: si levanta, el turno no queda.
    - `apertura`: la fecha y hora de apertura (`AAAA-MM-DD HH:MM:SS`, hora de
      Argentina). Sin ella, ahora. El central que aplica un turno del nodo pasa
      la que escribió el nodo: la guarda del cierre diario mira ese día.
    - `origen` y `origen_id`: el nodo y el id del turno en su base. Van juntos.
    """
    ganchos_de_caja.validar_origen(origen, origen_id)
    apertura = apertura or _ar_now()
    with _conexion(conn) as c:
        sucursal_id = cierre_diario.sucursal_de_caja(caja_id, conn=c) if caja_id is not None else None
        cierre_diario.verificar_dia_abierto(apertura[:10], sucursal_id, conn=c)
        if origen is None:
            cur = c.execute(
                """INSERT INTO turnos_caja (usuario_id, apertura, monto_inicial, notas, caja_id)
                   VALUES (?,?,?,?,?)""",
                (usuario_id, apertura, monto_inicial, notas, caja_id),
            )
        else:
            cur = c.execute(
                """INSERT INTO turnos_caja (usuario_id, apertura, monto_inicial, notas, caja_id, origen, origen_id)
                   VALUES (?,?,?,?,?,?,?)""",
                (usuario_id, apertura, monto_inicial, notas, caja_id, origen, origen_id),
            )
        turno_id = cur.lastrowid
        if ganchos is not None:
            turno = c.execute("SELECT * FROM turnos_caja WHERE id=?", (turno_id,)).fetchone()
            ganchos.al_abrir_turno(c, ganchos_de_caja.datos_de_turno_abierto(turno, sucursal_id=sucursal_id))
        return turno_id


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


def turno_abierto_de_caja(caja_id: int) -> dict | None:
    """El turno abierto sobre ESE mostrador (de cualquier usuario), o `None`.

    `get_turno_activo` mira por usuario; ésta mira por caja, que es la pregunta
    de "¿hay alguien cobrando en este cajón?". El motor no impone "una caja, un
    turno" (LibraClub sólo evita dos turnos del mismo usuario): lo impone el
    producto que lo quiere, con esta consulta, desde `validar_apertura`.
    """
    with get_connection() as conn:
        row = conn.execute(
            """SELECT t.*, u.nombre AS usuario_nombre
               FROM turnos_caja t JOIN usuarios u ON u.id = t.usuario_id
               WHERE t.caja_id=? AND t.estado='abierto'
               ORDER BY t.id DESC LIMIT 1""",
            (caja_id,),
        ).fetchone()
    return dict(row) if row else None


def cajas_con_turno_abierto() -> set[int]:
    """Los ids de las cajas que ahora mismo tienen un turno abierto."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT caja_id FROM turnos_caja WHERE estado='abierto' AND caja_id IS NOT NULL"
        ).fetchall()
    return {r[0] for r in rows}


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


def get_turno(tid: int, conn: Conexion | None = None) -> dict | None:
    with _conexion(conn) as c:
        row = c.execute(
            """SELECT t.*, u.nombre AS usuario_nombre
               FROM turnos_caja t JOIN usuarios u ON u.id = t.usuario_id
               WHERE t.id=?""",
            (tid,),
        ).fetchone()
    return dict(row) if row else None


def get_turno_de_origen(origen: str, origen_id: int, conn: Conexion | None = None) -> dict | None:
    """El turno que subió ese nodo con ese id (ADR-076), o `None` si todavía no se aplicó. Es la pregunta de
    idempotencia del central: antes de aplicar «turno abierto» o «turno cerrado» de un nodo, ¿ya está?"""
    with _conexion(conn) as c:
        row = c.execute(
            """SELECT t.*, u.nombre AS usuario_nombre
               FROM turnos_caja t JOIN usuarios u ON u.id = t.usuario_id
               WHERE t.origen=? AND t.origen_id=?""",
            (origen, origen_id),
        ).fetchone()
    return dict(row) if row else None


def get_resumen_turno(tid: int, conn: Conexion | None = None) -> dict:
    """Devuelve ventas y totales por medio de pago del turno."""
    with _conexion(conn) as c:
        ventas = c.execute(
            """SELECT v.id, v.numero, v.fecha, v.cliente_nombre, v.total, v.estado
               FROM ventas v WHERE v.turno_id=? ORDER BY v.id""",
            (tid,),
        ).fetchall()
        pagos = c.execute(
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


def get_resumen_turno_caja(tid: int, conn: Conexion | None = None) -> dict:
    """Resumen del turno calculado sobre `caja_movimientos`, no sobre
    `ventas`.

    Es la variante para productos cuyas ventas NO viven en la tabla `ventas`
    de LibraCore — VentaLibra las tiene en LibraCommerce, así que
    `get_resumen_turno()` (que hace JOIN con `ventas`/`ventas_pagos`) le
    devolvería siempre vacío y el arqueo daría cero.

    Contar sobre la caja además es más fiel a lo que se arquea: entra todo lo
    que pasó por el cajón, incluidos ingresos y egresos que no son ventas.

    Con `conn`, lo ve como lo ve esa transacción (ADR-076: el cierre del turno arquea sobre la misma
    transacción que lo cierra)."""
    with _conexion(conn) as c:
        # 🔑 **La lista los trae TODOS, con su marca; los totales NO.** Es la
        # razón de ser de `anulado`: el arqueo tiene que dar lo que hay en el
        # cajón, y la pantalla tiene que poder mostrar qué se dio de baja. Una
        # lista que esconde los anulados no se distingue de una que los borra.
        movimientos = c.execute(
            """SELECT id, fecha, tipo, concepto, monto, medio_pago, referencia, anulado
               FROM caja_movimientos WHERE turno_id=? ORDER BY id""",
            (tid,),
        ).fetchall()
        por_medio = c.execute(
            """SELECT medio_pago, SUM(CASE WHEN tipo='egreso' THEN -monto ELSE monto END) AS total
               FROM caja_movimientos WHERE turno_id=? AND anulado=0 GROUP BY medio_pago""",
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


def _cerrar_en(c: Conexion, tid: int, resumen: Callable[[int, Conexion], dict], monto_declarado: float,
               notas: str, monto_esperado: float | None, cierre: str | None,
               ganchos: ganchos_de_caja.GanchosDeCaja | None) -> dict | None:
    """El cierre de un turno sobre la conexión `c`: lo que comparten `cerrar_turno_caja` y `cerrar_turno`, que sólo
    difieren en de dónde sale el efectivo cobrado (`resumen`). Devuelve el turno cerrado o `None` si no existe."""
    turno = get_turno(tid, conn=c)
    if not turno:
        return None
    if monto_esperado is None:
        monto_esperado = round(turno["monto_inicial"] + resumen(tid, c)["efectivo_ventas"], 2)
    cierre = cierre or _ar_now()
    c.execute(
        """UPDATE turnos_caja
           SET estado='cerrado', cierre=?, monto_declarado_cierre=?,
               monto_esperado_cierre=?, notas=?
           WHERE id=?""",
        (cierre, monto_declarado, monto_esperado, notas, tid),
    )
    cerrado = get_turno(tid, conn=c)
    if ganchos is not None:
        sucursal_id = cierre_diario.sucursal_de_caja(cerrado["caja_id"], conn=c)
        ganchos.al_cerrar_turno(c, ganchos_de_caja.datos_de_turno_cerrado(
            cerrado, sucursal_id=sucursal_id, medios=cierre_diario._medios_de_turno(c, tid)))
    return cerrado


def cerrar_turno_con(tid: int, monto_declarado: float, notas: str = "", *,
                     resumen: Callable[[int, Conexion], dict], conn: Conexion | None = None,
                     ganchos: ganchos_de_caja.GanchosDeCaja | None = None, cierre: str | None = None,
                     monto_esperado: float | None = None) -> dict | None:
    """El cierre común de un turno para quien arquea contra su propio resumen (`resumen(tid, conn)` con
    `efectivo_ventas`), como `libracommerce.erp.ventas.cerrar_turno`. Los opcionales son los de `cerrar_turno_caja`
    (ADR-076): así el cierre de un turno de otra capa también corre `al_cerrar_turno` y sube desde un nodo. Devuelve el
    turno cerrado o `None` si no existe."""
    with _conexion(conn) as c:
        return _cerrar_en(c, tid, resumen, monto_declarado, notas, monto_esperado, cierre, ganchos)


def cerrar_turno_caja(tid: int, monto_declarado: float, notas: str = "", *, conn: Conexion | None = None,
                      ganchos: ganchos_de_caja.GanchosDeCaja | None = None, cierre: str | None = None,
                      monto_esperado: float | None = None) -> dict | None:
    """Cierra el turno arqueando contra `caja_movimientos`
    (ver get_resumen_turno_caja). Devuelve el turno cerrado, con el esperado
    y la diferencia ya calculados, para no obligar al caller a releerlo.

    **La caja de un nodo (ADR-076).** Opcionales, y sin ellos todo queda como estaba: `conn` (la transacción de
    quien llama, que confirma), `ganchos` (corre `al_cerrar_turno(conn, datos)` **adentro de la misma
    transacción**, con el arqueo y los medios: si levanta, el turno sigue abierto), `cierre` (la hora de cierre que
    escribió el nodo) y `monto_esperado` (el que vio el cajero en el nodo: el central lo guarda tal cual y no lo
    recalcula, porque puede no haber recibido todavía todos los movimientos)."""
    with _conexion(conn) as c:
        return _cerrar_en(c, tid, lambda t, cx: get_resumen_turno_caja(t, conn=cx), monto_declarado, notas,
                          monto_esperado, cierre, ganchos)


def cerrar_turno(tid: int, monto_declarado: float, notas: str = "", *, conn: Conexion | None = None,
                 ganchos: ganchos_de_caja.GanchosDeCaja | None = None, cierre: str | None = None,
                 monto_esperado: float | None = None):
    """Cierra el turno arqueando contra la tabla `ventas` del motor (`get_resumen_turno`). Los opcionales son los
    de `cerrar_turno_caja` (ADR-076). Devuelve `None`, como siempre."""
    with _conexion(conn) as c:
        _cerrar_en(c, tid, lambda t, cx: get_resumen_turno(t, conn=cx), monto_declarado, notas, monto_esperado,
                   cierre, ganchos)


def vincular_venta_turno(venta_id: int, turno_id: int, conn: Conexion | None = None):
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        c.execute("UPDATE ventas SET turno_id=? WHERE id=?", (turno_id, venta_id))
