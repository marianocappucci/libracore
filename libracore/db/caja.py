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


def sql_no_anulado(alias: str = "") -> str:
    """Fragmento SQL: el movimiento **no está anulado**, o sea que cuenta.

    🔴 **Va en todo lo que produce un NÚMERO de plata**, y en nada de lo que
    produce una lista para mirar. Esa es la línea, y el motor la cruzaba en
    catorce consultas: hasta el 2026-08-28 sólo `get_resumen_turno_caja`
    filtraba, así que un movimiento anulado seguía contando en el arqueo de
    Contalibra y Restolibra, en lo cobrado de una factura, en el saldo de una
    cuenta corriente, en los reportes y en el tablero.

    🔑 **Es un fragmento compartido y no un `AND anulado=0` suelto**, por la
    misma razón que `sql_no_es_cuenta_corriente`: *"tener dos listas del mismo
    criterio es cómo se llega a que una consulta cuente un movimiento como deuda
    y otra no"*. Y además se puede grepear — que es lo que permite auditar el
    barrido de una sola pasada.

    `alias` para las consultas con JOIN, donde la columna necesita calificarse
    (`cm.anulado`). Sin alias, para las que consultan la tabla sola.
    """
    return f"{alias + '.' if alias else ''}anulado = 0"


def _fila_de_caja(row) -> dict:
    """La fila con `medios_pago` ya parseado.

    Estaba escrito dos veces —en el listado y en el detalle— y las dos copias
    hacían exactamente lo mismo. Se unifica al pasar por acá.
    """
    d = dict(row)
    d["medios_pago"] = json.loads(d["medios_pago"] or "[]")
    return d


def get_all_cajas(sucursal_id: int | None = None) -> list[dict]:
    """Las cajas, o sólo las de una sucursal.

    Sin `sucursal_id` devuelve todas, que es lo que hacen los cinco productos sin
    sucursales. Con él filtra — y **no** trae las que tienen la sucursal en
    blanco: una caja sin sede no es de ninguna, y mostrarla en todas es peor que
    no mostrarla.
    """
    donde = " WHERE sucursal_id=?" if sucursal_id is not None else ""
    params = (sucursal_id,) if sucursal_id is not None else ()
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM cajas{donde} ORDER BY es_default DESC, nombre", params
        ).fetchall()
    return [_fila_de_caja(r) for r in rows]


def get_caja_config(cid: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM cajas WHERE id=?", (cid,)).fetchone()
    return _fila_de_caja(row) if row else None


def get_default_caja_id() -> int | None:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM cajas WHERE es_default=1 LIMIT 1").fetchone()
        if not row:
            row = conn.execute("SELECT id FROM cajas ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


class PuntoDeVentaRepetido(ValueError):
    """Otra caja ya tiene ese punto de venta de ARCA."""


def _validar_punto_venta(conn, punto_venta, cid: int | None = None) -> None:
    """🔴 Dos cajas no pueden compartir el punto de venta de ARCA.

    ARCA numera por **(tipo, punto de venta)**, así que dos mostradores con el
    mismo punto de venta comparten la serie y compiten por el próximo número. Y
    el choque no lo detectamos nosotros: lo detecta ARCA, rechazando el segundo
    comprobante — con el cliente esperando el ticket.

    `None` no cuenta como repetido: es lo que dice "esta caja usa el punto de
    venta de la empresa", y es el estado de todas las cajas que existen hoy.
    """
    if punto_venta is None:
        return
    fila = conn.execute(
        "SELECT nombre FROM cajas WHERE punto_venta = ? AND id <> ?",
        (punto_venta, cid if cid is not None else -1),
    ).fetchone()
    if fila:
        raise PuntoDeVentaRepetido(
            f"El punto de venta {punto_venta} ya lo usa la caja {fila[0]!r}. "
            f"ARCA numera por punto de venta: dos cajas con el mismo comparten "
            f"la serie, y el segundo comprobante lo rechaza ARCA."
        )


def create_caja_config(nombre: str, descripcion: str, medios_pago: list,
                       sucursal_id: int | None = None,
                       punto_venta: int | None = None) -> int:
    with get_connection() as conn:
        _validar_punto_venta(conn, punto_venta)
        cur = conn.execute(
            "INSERT INTO cajas (nombre, descripcion, medios_pago, sucursal_id, punto_venta)"
            " VALUES (?,?,?,?,?)",
            (nombre, descripcion, json.dumps(medios_pago), sucursal_id, punto_venta),
        )
        return cur.lastrowid


def update_caja_config(cid: int, nombre: str, descripcion: str, medios_pago: list,
                       activo: int, punto_venta: int | None = None):
    """`punto_venta=None` deja la caja usando el de la empresa.

    Va con default para no romper a los llamadores que ya existen: los productos
    que no tienen varios POS lo llaman con cinco argumentos y siguen igual.
    """
    with get_connection() as conn:
        _validar_punto_venta(conn, punto_venta, cid)
        conn.execute(
            "UPDATE cajas SET nombre=?, descripcion=?, medios_pago=?, activo=?,"
            " punto_venta=? WHERE id=?",
            (nombre, descripcion, json.dumps(medios_pago), activo, punto_venta, cid),
        )


def resolver_punto_venta(usuario_id: int | None) -> int | None:
    """El punto de venta de ARCA del POS donde está parado este usuario.

    La cadena es **usuario → turno abierto → caja → punto de venta**. No hace
    falta ningún concepto nuevo de "terminal": el turno ya sabe en qué caja está
    abierto. Es la misma razón por la que cada POS necesita su propio usuario
    logueado — si dos comparten usuario, comparten turno, y entonces comparten
    punto de venta.

    Devuelve `None` cuando no hay usuario, cuando no hay turno abierto, o cuando
    la caja no tiene punto de venta propio. **`None` significa "usá el de la
    empresa"**, que es el caso de toda instancia que hoy funciona con uno solo —
    o sea todas.
    """
    if not usuario_id:
        return None
    with get_connection() as conn:
        fila = conn.execute(
            """SELECT c.punto_venta
                 FROM turnos_caja t JOIN cajas c ON c.id = t.caja_id
                WHERE t.usuario_id = ? AND t.estado = 'abierto'
                ORDER BY t.id DESC LIMIT 1""",
            (usuario_id,),
        ).fetchone()
    return fila[0] if fila and fila[0] else None


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
        where, params = [_cc_excl, sql_no_anulado()], []
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
               FROM caja_movimientos WHERE {_cc_excl} AND {sql_no_anulado()}"""
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
            f" AND {sql_no_es_cuenta_corriente()} AND {sql_no_anulado()}"
            " ORDER BY id DESC LIMIT 1",
            (factura_id,),
        ).fetchone()
        return dict(row) if row else None


def get_cobros_factura(factura_id) -> list[dict]:
    """Devuelve todos los movimientos de cobro de una factura.

    🔴 **Excluye los anulados, y acá no es cosmético**: esta lista alimenta el
    **recibo** (`recibos.py`) y el detalle del comprobante. Un recibo es un
    documento que dice *"recibimos esto"* — meterle un cobro anulado es firmar
    plata que no entró.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM caja_movimientos WHERE factura_id=? AND tipo='ingreso'"
            f" AND {sql_no_es_cuenta_corriente()} AND {sql_no_anulado()}"
            " ORDER BY id",
            (factura_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_caja_movimiento(mov_id):
    """Borra la fila. **Preferir `anular_caja_movimiento`.**

    ⚠️ Sigue existiendo porque [[contalibra]] y [[restolibra]] la usan desde
    antes; no se retira para no romperlos. Pero borrar deja un agujero en el
    arqueo que nadie puede auditar — ver el comentario de la columna `anulado`
    en `schema.py`.
    """
    with get_connection() as conn:
        conn.execute("DELETE FROM caja_movimientos WHERE id=?", (mov_id,))


def anular_caja_movimiento(mov_id):
    """Marca el movimiento como anulado. La fila **queda**.

    🔴 **Un movimiento de caja se anula, no se borra.** Borrarlo deja un agujero
    en el arqueo que nadie puede auditar, y en LibraClub rompía más: un cobro de
    turno borrado hace que la reserva vuelva a figurar impaga —el pendiente se
    calcula sumando movimientos por referencia— y un cobro por QR queda con
    `caja_movimiento_id` colgando, con lo cual el poll **no** lo vuelve a
    registrar y la plata desaparece del cajón para siempre. Pedido del humano el
    2026-08-28: *"no deberían poder borrarse, tienen que quedar registrados"*.

    Sale de los totales del arqueo y la lista lo sigue mostrando —ver
    `get_resumen_turno_caja`—, que es lo que permite auditar qué se cargó y se
    dio de baja. Idempotente: anular dos veces deja lo mismo.

    ⚠️ **Esta explicación no puede ir adentro del `CREATE TABLE`.** El
    `executescript()` del adaptador PostgreSQL parte el script por el punto y
    coma, así que un `--` con un `;` adentro corta la sentencia al medio: el
    volcado muere con *"syntax error at end of input"*. Está avisado en el
    bloque de `usuarios.activo` de `schema.py`, y volvió a pasar acá.
    """
    with get_connection() as conn:
        conn.execute("UPDATE caja_movimientos SET anulado=1 WHERE id=?", (mov_id,))
