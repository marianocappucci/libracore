"""
Cajas (configuración de puntos de cobro) y movimientos de caja. Extraído
de database.py de Contalibra/Restolibra (idéntico en ambos) como parte de
la migración real a libracore.db (Fase 3 de LibraCore, ver
wiki/entities/libracore.md).
"""
import contextlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from decimal import Decimal

from libracore import ganchos_de_caja, medios_pago, nodo_offline
from libracore.db.core import get_connection
from libracore.fechas import rango_por_dia

from .core import Conexion

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


class ExternalIdMercadoPagoInvalido(ValueError):
    """El external_id de MercadoPago tiene caracteres no permitidos."""


_RE_MP_EXTERNAL_ID_VALIDO = re.compile(r"^[a-zA-Z0-9]+$")


def _validar_mp_pos_id(mp_pos_id: str | None) -> None:
    """MercadoPago exige que el external_id del POS sea alfanumérico.

    No acepta guiones, espacios ni otros símbolos. Ver
    `procedimiento-credenciales-prueba-mercadopago` en el wiki.
    """
    if mp_pos_id is None:
        return
    valor = str(mp_pos_id).strip()
    if not valor:
        return
    if not _RE_MP_EXTERNAL_ID_VALIDO.match(valor):
        raise ExternalIdMercadoPagoInvalido(
            "El POS ID de MercadoPago debe ser alfanumérico (sin guiones, "
            "espacios ni símbolos)."
        )


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
                       punto_venta: int | None = None,
                       mp_pos_id: str | None = None) -> int:
    with get_connection() as conn:
        _validar_punto_venta(conn, punto_venta)
        _validar_mp_pos_id(mp_pos_id)
        cur = conn.execute(
            "INSERT INTO cajas (nombre, descripcion, medios_pago, sucursal_id, punto_venta, mp_pos_id)"
            " VALUES (?,?,?,?,?,?)",
            (nombre, descripcion, json.dumps(medios_pago), sucursal_id, punto_venta,
             (mp_pos_id or "").strip() or None),
        )
        return cur.lastrowid


def update_caja_config(cid: int, nombre: str, descripcion: str, medios_pago: list,
                       activo: int, punto_venta: int | None = None,
                       mp_pos_id: str | None = None):
    """`punto_venta=None` deja la caja usando el de la empresa.

    Va con default para no romper a los llamadores que ya existen: los productos
    que no tienen varios POS lo llaman con cinco argumentos y siguen igual.
    """
    with get_connection() as conn:
        _validar_punto_venta(conn, punto_venta, cid)
        _validar_mp_pos_id(mp_pos_id)
        conn.execute(
            "UPDATE cajas SET nombre=?, descripcion=?, medios_pago=?, activo=?,"
            " punto_venta=?, mp_pos_id=? WHERE id=?",
            (nombre, descripcion, json.dumps(medios_pago), activo, punto_venta,
             (mp_pos_id or "").strip() or None, cid),
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


def resolver_mp_pos_id(usuario_id: int | None) -> str | None:
    """El `external_id` del POS de MercadoPago de la caja activa de este usuario.

    Sigue la misma cadena que `resolver_punto_venta`: usuario → turno abierto
    → caja → mp_pos_id. Cada caja necesita su propio POS porque el monto se
    escribe en el POS: compartirlo haría que la última venta pise la anterior.

    Devuelve `None` cuando no hay usuario, no hay turno abierto, o la caja no
    tiene POS configurado.
    """
    if not usuario_id:
        return None
    with get_connection() as conn:
        fila = conn.execute(
            """SELECT c.mp_pos_id
                 FROM turnos_caja t JOIN cajas c ON c.id = t.caja_id
                WHERE t.usuario_id = ? AND t.estado = 'abierto'
                ORDER BY t.id DESC LIMIT 1""",
            (usuario_id,),
        ).fetchone()
    if not fila or not fila[0]:
        return None
    return str(fila[0]).strip()


def mp_pos_id_con_fallback(usuario_id: int | None, cfg_pos_id: str | None) -> str | None:
    """POS de MercadoPago de la caja activa, con fallback a config para una sola caja.

    Durante la migración, una instancia de una sola caja puede tener el
    `mp_pos_id` todavía en la configuración de instancia. Si la caja activa no
    tiene POS propio y hay exactamente una caja, se usa el de config. Con más
    de una caja no hay fallback: cada una debe tener su POS configurado.
    """
    pos_id = resolver_mp_pos_id(usuario_id)
    if pos_id:
        return pos_id
    valor_config = (cfg_pos_id or "").strip()
    if not valor_config:
        return None
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM cajas").fetchone()[0]
        if count == 1:
            return valor_config
    return None


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
                           conn: Conexion | None = None, cc_pago_id=None, *,
                           ganchos: ganchos_de_caja.GanchosDeCaja | None = None,
                           origen: str | None = None, origen_id: int | None = None,
                           extra: Mapping | None = None):
    """Anota un movimiento de caja y devuelve su id (el del que ya existía, si la referencia se repite).

    **El monto** puede ser `int`, `float` o `Decimal`. Un `Decimal` se guarda **exacto** donde la columna lo permite
    (`NUMERIC` en PostgreSQL, ADR-024; sin pasar por `float`, que desde la cifra 16 pierde); en SQLite la columna es
    `REAL` y va como el `float` más cercano. `int` y `float` se guardan como siempre (ADR-080).

    **La caja de un nodo (ADR-076).** `ganchos` y `origen`/`origen_id` son opcionales y, sin ellos, todo queda
    como estaba. `ganchos` (`GanchosDeCaja`) corre `al_registrar_movimiento(conn, datos)` **dentro de la misma
    transacción**, después del `INSERT`: si levanta, el movimiento no queda. **No corre si la referencia ya existía**
    (no hubo fila nueva que subir). `origen` y `origen_id` (juntos): el nodo y el id del movimiento en su base,
    cuando el central aplica uno que subió el nodo (`get_movimiento_de_origen` dice si ya está).

    **Lo propio del producto (ADR-080).** `extra` es un `dict` JSON que el motor no lee: viaja en `datos["extra"]` del
    gancho `al_registrar_movimiento`, para que el central pueda crear lo propio del producto (su fila hija, con el mismo
    id) al aplicar el movimiento (`libracore.caja_del_nodo`). Sin `ganchos` no se guarda en ningún lado, y sin `extra`
    el `datos` es el de siempre. Se valida antes de escribir (`ganchos_de_caja.validar_extra`).
    """
    ganchos_de_caja.validar_origen(origen, origen_id)
    ganchos_de_caja.validar_extra(extra)
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
        # Sin `caja_id` explícito, pero con un turno: la caja es la del turno,
        # no la default. LibraCommerce llama esta función con `turno_id` y sin
        # `caja_id` desde `erp/ventas.py`, y en un producto con varias cajas eso
        # dejaba TODA venta anotada en la caja default aunque el turno estuviera
        # abierto en otra — el arqueo y el cierre no se enteraban (van por
        # `turnos_caja.caja_id`), pero `get_caja_movimientos`/`get_caja_resumen`
        # filtrados por caja sí quedaban mal. Se consulta con la MISMA conexión
        # `c`, dentro de la misma transacción del INSERT. Turno sin caja (o
        # inexistente) sigue cayendo al default, como siempre.
        if caja_id is not None:
            _caja_id = caja_id
        elif turno_id is not None:
            fila_turno = c.execute(
                "SELECT caja_id FROM turnos_caja WHERE id=?", (turno_id,)
            ).fetchone()
            _caja_id = fila_turno[0] if fila_turno and fila_turno[0] else get_default_caja_id()
        else:
            _caja_id = get_default_caja_id()
        if origen is None:
            cur = c.execute(
                """INSERT INTO caja_movimientos
                   (fecha, tipo, concepto, monto, referencia, factura_id, usuario_id, caja_id,
                    medio_pago, turno_id, cc_pago_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (fecha, tipo, concepto, _monto_para_la_base(c, monto), referencia, factura_id, usuario_id, _caja_id,
                 medio_pago, turno_id, cc_pago_id),
            )
        else:
            cur = c.execute(
                """INSERT INTO caja_movimientos
                   (fecha, tipo, concepto, monto, referencia, factura_id, usuario_id, caja_id,
                    medio_pago, turno_id, cc_pago_id, origen, origen_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fecha, tipo, concepto, _monto_para_la_base(c, monto), referencia, factura_id, usuario_id, _caja_id,
                 medio_pago, turno_id, cc_pago_id, origen, origen_id),
            )
        if factura_id is not None and tipo == "ingreso":
            al_libro_de_clientes(c, [cur.lastrowid])
        if ganchos is not None:
            ganchos.al_registrar_movimiento(c, _datos_de_movimiento(c, cur.lastrowid, extra))
        return cur.lastrowid


def _monto_para_la_base(c: Conexion, monto):
    """El monto como lo recibe la columna: `float` (lo de siempre) o, para un `Decimal` contra PostgreSQL, el `Decimal`.

    🔑 `float(Decimal("12345678901234.56"))` ya perdió el último dígito antes de llegar a la columna `NUMERIC`
    (ADR-024), que lo habría guardado exacto. `sqlite3` no sabe atar un `Decimal` y su columna es `REAL`, así que ahí
    va el `float` más cercano. Un `Decimal` que no es finito (`NaN`, `Infinity`) se rechaza: `NUMERIC` lo aceptaría."""
    if isinstance(monto, Decimal):
        if not monto.is_finite():
            raise ValueError("el monto tiene que ser un número finito.")
        if not isinstance(c, sqlite3.Connection):
            return monto
    return float(monto)


def _datos_de_movimiento(c: Conexion, mov_id, extra: Mapping | None = None) -> dict:
    """Lo que viaja de un movimiento (`ganchos_de_caja.datos_de_movimiento`), leído con la conexión del cambio, con la
    sucursal de su caja y lo propio del producto (`extra`)."""
    from libracore.db.cierre_diario import sucursal_de_caja

    mov = c.execute("SELECT * FROM caja_movimientos WHERE id=?", (mov_id,)).fetchone()
    return ganchos_de_caja.datos_de_movimiento(mov, sucursal_id=sucursal_de_caja(mov["caja_id"], conn=c), extra=extra)


def get_movimiento_de_origen(origen: str, origen_id: int, conn: Conexion | None = None) -> dict | None:
    """El movimiento que subió ese nodo con ese id (ADR-076), o `None` si todavía no se aplicó. La pregunta de
    idempotencia del central: antes de aplicar «movimiento registrado» o «movimiento anulado», ¿ya está?"""
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        row = c.execute(
            "SELECT * FROM caja_movimientos WHERE origen=? AND origen_id=?", (origen, origen_id)
        ).fetchone()
    return dict(row) if row else None


def get_caja_movimientos(desde=None, hasta=None, limit=500, caja_id=None):
    with get_connection() as conn:
        where, params = [], []
        if desde and hasta:
            c_fecha, p_fecha = rango_por_dia("cm.fecha", desde, hasta)
            where += c_fecha; params += p_fecha
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
            c_fecha, p_fecha = rango_por_dia("fecha", desde, hasta)
            where += c_fecha; params += p_fecha
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


def get_cobros_factura(factura_id, *, conn: Conexion | None = None) -> list[dict]:
    """Devuelve todos los movimientos de cobro de una factura.

    🔴 **Excluye los anulados, y acá no es cosmético**: esta lista alimenta el
    **recibo** (`recibos.py`) y el detalle del comprobante. Un recibo es un
    documento que dice *"recibimos esto"* — meterle un cobro anulado es firmar
    plata que no entró.

    También es lo que **cuenta como cobrado** para el tope del cobro (ADR-058, `libracore.cobros`).
    Con `conn`, lo ve como lo ve esa transacción, sin confirmar ni cerrar nada.
    """
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        rows = c.execute(
            "SELECT * FROM caja_movimientos WHERE factura_id=? AND tipo='ingreso'"
            f" AND {sql_no_es_cuenta_corriente()} AND {sql_no_anulado()}"
            " ORDER BY id",
            (factura_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def anular_movimientos_de_cc_pago(pago_id) -> int:
    """Anula todos los movimientos de caja que generó un pago a cuenta y devuelve cuántos.

    Los cobros por factura y el resto suelto del pago comparten `cc_pago_id`:
    dar de baja el pago tiene que devolver las facturas a "Sin cobrar" y sacar la
    plata del arqueo, no dejar cobros huérfanos. Anula, no borra (ver
    `anular_caja_movimiento`). Idempotente. Un pago anterior a la columna no
    tiene movimientos ligados y devuelve 0.
    """
    with get_connection() as conn:
        ids = [f[0] for f in conn.execute(
            "SELECT id FROM caja_movimientos WHERE cc_pago_id=? AND anulado=0", (pago_id,)
        ).fetchall()]
        cur = conn.execute(
            "UPDATE caja_movimientos SET anulado=1 WHERE cc_pago_id=? AND anulado=0", (pago_id,)
        )
        al_libro_de_clientes(conn, ids)
        return cur.rowcount


def delete_caja_movimiento(mov_id):
    """Borra la fila. **Preferir `anular_caja_movimiento`.**

    ⚠️ Sigue existiendo porque [[contalibra]] y [[restolibra]] la usan desde
    antes; no se retira para no romperlos. Pero borrar deja un agujero en el
    arqueo que nadie puede auditar — ver el comentario de la columna `anulado`
    en `schema.py`.
    """
    with get_connection() as conn:
        conn.execute("DELETE FROM caja_movimientos WHERE id=?", (mov_id,))
        al_libro_de_clientes(conn, [mov_id])


def anular_caja_movimiento(mov_id, conn: Conexion | None = None, *,
                           ganchos: ganchos_de_caja.GanchosDeCaja | None = None,
                           extra: Mapping | None = None):
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

    **La caja de un nodo (ADR-076).** `conn` escribe en la transacción de quien llama (que confirma). `ganchos`
    corre `al_anular_movimiento(conn, datos)` **dentro de la misma transacción**: si levanta, el movimiento sigue
    vigente. Sólo corre cuando la anulación **cambió algo**: un movimiento ya anulado, o que no existe, no vuelve a
    subir. `extra` (ADR-080) viaja en `datos["extra"]` de ese gancho, como en el alta: lo propio de la anulación
    (un motivo, quién la pidió) que el central necesita para rehacer lo suyo.

    ⚠️ **Esta explicación no puede ir adentro del `CREATE TABLE`.** El
    `executescript()` del adaptador PostgreSQL parte el script por el punto y
    coma, así que un `--` con un `;` adentro corta la sentencia al medio: el
    volcado muere con *"syntax error at end of input"*. Está avisado en el
    bloque de `usuarios.activo` de `schema.py`, y volvió a pasar acá.
    """
    ganchos_de_caja.validar_extra(extra)
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        antes = None
        if ganchos is not None:
            antes = c.execute("SELECT anulado FROM caja_movimientos WHERE id=?", (mov_id,)).fetchone()
        c.execute("UPDATE caja_movimientos SET anulado=1 WHERE id=?", (mov_id,))
        al_libro_de_clientes(c, [mov_id])
        if ganchos is not None and antes is not None and not antes["anulado"]:
            ganchos.al_anular_movimiento(c, _datos_de_movimiento(c, mov_id, extra))


class MovimientoInvalido(ValueError):
    """Los datos de un movimiento no valen (concepto vacío, tipo o monto inválidos). La API los contesta 422."""


class MovimientoInexistente(LookupError):
    """No hay un movimiento de caja con ese id. La API lo contesta 404."""


class CorreccionNoPermitida(RuntimeError):
    """Un movimiento que no se corrige en este estado o en este lugar. La API lo contesta 409 con `detalle()`.

    Con `codigo` fijo por clase, para que la pantalla distinga el porqué sin leer el texto (como
    `nodo_offline.SoloEnElCentral`, que es lo que se levanta en un nodo)."""

    codigo = "correccion_no_permitida"

    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje

    def detalle(self) -> dict:
        """El `detail` de la respuesta: `{"codigo", "mensaje"}`."""
        return {"codigo": self.codigo, "mensaje": self.mensaje}


class MovimientoAnulado(CorreccionNoPermitida):
    """El movimiento está anulado: una anulación no se deshace ni se edita (se carga uno nuevo)."""

    codigo = "movimiento_anulado"


class MovimientoDeUnNodo(CorreccionNoPermitida):
    """El movimiento subió de un nodo (`origen` no nulo): es del nodo, y el central no lo edita."""

    codigo = "movimiento_del_nodo"


#: Los tipos de un movimiento de caja (un retiro es un egreso).
TIPOS_DE_MOVIMIENTO = ("ingreso", "egreso")


def validar_movimiento(concepto, tipo, monto) -> str:
    """Las reglas de un movimiento que anota una persona, y el `concepto` ya sin espacios. Levanta `MovimientoInvalido`.

    Son las del alta de `caja_router` (concepto obligatorio, tipo `ingreso` o `egreso`, monto mayor a cero), puestas acá
    para que el alta y la corrección (`corregir_caja_movimiento`) las compartan: dos listas del mismo criterio es cómo
    una edición termina aceptando lo que el alta rechaza. **`create_caja_movimiento` no las aplica** (lo llaman los
    flujos del motor con sus propias reglas, y cambiarlas ahí sería otro cambio): las aplica quien anota o corrige a
    mano. El orden —concepto, tipo, monto— es el del router, que contesta con el primero que falla."""
    concepto = (concepto or "").strip()
    if not concepto:
        raise MovimientoInvalido("El concepto es obligatorio.")
    if tipo not in TIPOS_DE_MOVIMIENTO:
        raise MovimientoInvalido("Tipo inválido.")
    if not monto > 0:  # no `<= 0`: un NaN tampoco es mayor a cero
        raise MovimientoInvalido("El monto debe ser mayor a cero.")
    return concepto


def corregir_caja_movimiento(mov_id, *, fecha=None, tipo=None, concepto=None, monto=None, medio_pago=None,
                             conn: Conexion | None = None,
                             nodo: Callable[[], str | None] | None = None) -> dict:
    """Corrige en el lugar un movimiento de caja **del central** y devuelve la fila como quedó (ADR-080).

    Se pueden corregir `fecha` (texto ISO), `tipo`, `concepto`, `monto` y `medio_pago`; lo que no se pasa (`None`) queda
    como estaba. No se corrigen la factura, la caja, el turno, el usuario ni la referencia: un movimiento que cambia de
    esas cosas es otro, y para eso se anula y se carga de nuevo. Las reglas del resultado son las del alta
    (`validar_movimiento`), más el tope de cobro de una factura (ADR-058; ver abajo).

    **Dónde y cuándo se corrige** (todos son errores que la API contesta 409):
    - **No en un nodo** (`SoloEnElCentral`, la de `guarda_solo_en_el_central`: se lee `LIBRAEDGE_NODE_ID` en cada
      llamada; `nodo` es para el producto que lee su propia variable). En un nodo corregir es **anular y cargar de
      nuevo** (decisión del humano, 2026-10-22): el motor sube la caja con ganchos de alta y de anulación, y una
      edición en el nodo no tiene cómo llegar al central.
    - **No si subió de un nodo** (`MovimientoDeUnNodo`, `origen` no nulo): es del nodo. Editarlo en el central lo
      dejaría distinto de su original sin que el nodo se entere.
    - **No si está anulado** (`MovimientoAnulado`).

    Un movimiento que no existe levanta `MovimientoInexistente`; datos inválidos, `MovimientoInvalido`.

    🔑 **No hay gancho de modificación**, a propósito: los ganchos de `GanchosDeCaja` existen para que lo que ocurre en
    un nodo suba al central, y acá lo que se corrige es siempre una fila **propia del central** (sin `origen`, y en un
    nodo ni siquiera se puede). No hay a quién avisar. Si algún día un nodo pudiera corregir, sería otra operación
    del outbox, y esa decisión es de ese día.

    **Lo que mueve.** El libro de clientes (`al_libro_de_clientes`): si el movimiento era un ingreso a cuenta
    corriente con factura, el cambio de monto, de tipo o de medio lo reasienta o lo revierte. Un cobro de factura
    (ingreso con `factura_id` y medio que no es cuenta corriente) no puede dejarla cobrada de más: el monto nuevo se
    compara con lo que falta cobrar **sin contar este mismo cobro**, y si lo supera levanta `CobroNoPermitido`
    (`libracore.cobros`), como el alta. Con `conn`, escribe en la transacción de quien llama (que confirma); sin él, en
    una propia.

    ⚠️ No mira si el turno o el día del movimiento ya se cerraron: ni el alta ni la anulación lo miran. Un arqueo ya
    cerrado guarda lo que vio el cajero (`monto_esperado_cierre`) y no se recalcula."""
    from libracore.cobros import es_medio_cuenta_corriente  # import tardío: `cobros` importa este módulo

    es_nodo = nodo or nodo_offline.nodo_desde_entorno()
    if es_nodo() is not None:
        raise nodo_offline.SoloEnElCentral(
            "En el nodo un movimiento de caja no se corrige: anulalo y cargalo de nuevo.")
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        actual = c.execute("SELECT * FROM caja_movimientos WHERE id=?", (mov_id,)).fetchone()
        if actual is None:
            raise MovimientoInexistente(f"No existe el movimiento de caja {mov_id}.")
        if actual["anulado"]:
            raise MovimientoAnulado("El movimiento está anulado: no se puede modificar.")
        if actual["origen"] is not None:
            raise MovimientoDeUnNodo(
                "El movimiento vino de un nodo: se corrige ahí, anulándolo y cargándolo de nuevo.")
        nuevo = {
            "fecha": actual["fecha"] if fecha is None else fecha,
            "tipo": actual["tipo"] if tipo is None else tipo,
            "concepto": actual["concepto"] if concepto is None else concepto,
            "monto": actual["monto"] if monto is None else monto,
            "medio_pago": actual["medio_pago"] if medio_pago is None else medio_pago,
        }
        nuevo["concepto"] = validar_movimiento(nuevo["concepto"], nuevo["tipo"], nuevo["monto"])
        if (actual["factura_id"] is not None and nuevo["tipo"] == "ingreso"
                and not es_medio_cuenta_corriente(nuevo["medio_pago"])):
            _exigir_cobro_posible(c, actual, nuevo["monto"])
        # Sólo lo que se pidió: reescribir el monto que no cambió lo pasaría por `float` (la lectura lo devuelve así)
        # y perdería lo que `NUMERIC` guardaba exacto.
        cambia = {"fecha": fecha, "tipo": tipo, "concepto": nuevo["concepto"] if concepto is not None else None,
                  "monto": monto, "medio_pago": medio_pago}
        columnas = [k for k, v in cambia.items() if v is not None]
        valores = [_monto_para_la_base(c, nuevo[k]) if k == "monto" else nuevo[k] for k in columnas]
        if columnas:
            c.execute(
                f"UPDATE caja_movimientos SET {', '.join(f'{k}=?' for k in columnas)} WHERE id=?",
                (*valores, mov_id),
            )
        al_libro_de_clientes(c, [mov_id])
        return dict(c.execute("SELECT * FROM caja_movimientos WHERE id=?", (mov_id,)).fetchone())


def _exigir_cobro_posible(c: Conexion, actual, monto) -> None:
    """El tope de cobro de la factura (ADR-058) para un cobro que ya existe y cambia de monto: lo que falta cobrar se
    calcula **sin este cobro** (si ya contaba), y se bloquea la factura como en el alta."""
    from libracore import cobros
    from libracore.cobros import es_medio_cuenta_corriente

    era_un_cobro = actual["tipo"] == "ingreso" and not es_medio_cuenta_corriente(actual["medio_pago"])
    propio = cobros.centavos(actual["monto"]) if era_un_cobro else 0

    def sin_este_cobro(factura_id, conn):
        estado = cobros.estado_de_cobro(factura_id, conn=conn)
        if estado is None or not propio:
            return estado
        cobrado = max(cobros.centavos(estado["cobrado"]) - propio, 0) / 100
        return {**estado, **cobros.saldo_de_cobro({"total": estado["total"]}, [{"monto": cobrado}],
                                                  estado["acreditado"]), "cobrado": cobrado}

    cobros.exigir_cobro_posible(actual["factura_id"], monto, conn=c, leer_estado=sin_este_cobro)


def al_libro_de_clientes(conn, movimientos) -> None:
    """Lleva al libro de clientes lo que cambió en estos movimientos de caja, en la
    misma transacción (opción B, etapa B1). Sólo cuentan los ingresos a cuenta
    corriente con factura; `sincronizar` decide, y revierte lo que dejó de contar.

    La llama también quien cambia un movimiento con SQL propio (vincular un cobro a
    su factura después de emitirla). Import tardío: `libro_de_clientes` importa
    este módulo.
    """
    from libracore.db import libro_de_clientes

    for mov_id in movimientos:
        libro_de_clientes.sincronizar(f"caja_mov:{mov_id}", conn=conn)
