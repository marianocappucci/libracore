"""
Facturas electrónicas (facturas, notas de crédito, notas de débito):
numeración con retry ante colisión, alta/baja, búsqueda, filtros y
resolución de comprobantes asociados. Migrado a libracore.db (Fase 3 de
LibraCore, migración real, Tier 2 — código ya idéntico entre productos —
ver wiki/entities/libracore.md).
"""
import contextlib
import json
import sqlite3

from libracore import tipos_comprobante as tipos
from libracore.db.caja import sql_es_cuenta_corriente, sql_no_anulado, sql_no_es_cuenta_corriente
from libracore.db.core import Conexion, get_connection, sql_busqueda
from libracore.fechas import rango_por_dia


def _con(conn: Conexion | None):
    """La conexión de quien llama si la pasó, o una propia.

    🔑 **Con `conn`, la función trabaja dentro de la transacción de quien llama y
    no confirma nada**: el `nullcontext` no hace commit ni cierra. Es el idioma
    de todo `libracore.db` (`ventas`, `caja`, `stock`...) y es lo que deja a un
    producto emitir el comprobante y escribir lo suyo —las órdenes que cierra, el
    movimiento de su cuenta corriente— en **una sola transacción** (ADR-024 de LibraCargo; ADR-025 de este motor).
    Sin `conn`, cada función confirma la suya, como siempre.
    """
    return contextlib.nullcontext(conn) if conn is not None else get_connection()


@contextlib.contextmanager
def _savepoint(conn: Conexion | None, nombre: str):
    """Un `SAVEPOINT` si se trabaja en la transacción de quien llama; nada si no.

    🔴 **En PostgreSQL un error aborta la transacción entera**, así que el
    reintento de `create_factura` ante un número repetido es imposible sin esto:
    el segundo `INSERT` fallaría con «current transaction is aborted». Volver al
    savepoint deshace sólo el `INSERT` fallido y deja viva la transacción del
    producto. Sin `conn` cada intento ya es su propia transacción.
    """
    if conn is None:
        yield
        return
    # 🔴 En SQLite, un `SAVEPOINT` fuera de una transacción **abre una propia**, y
    # su `RELEASE` la confirma: el comprobante quedaría escrito aunque el producto
    # después revierta. Se abre la transacción antes, como haría el primer `INSERT`.
    # (psycopg ya abre una antes de la primera sentencia.)
    if isinstance(conn, sqlite3.Connection) and not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute(f"SAVEPOINT {nombre}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {nombre}")
        conn.execute(f"RELEASE SAVEPOINT {nombre}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {nombre}")

#: Los comprobantes que cuentan para los libros y los totales.
#:
#: 🔴 **Un comprobante emitido contra homologación NO es del cliente.** Trae CAE
#: y numeración del WSFE de homologación: si entra al libro IVA rompe la
#: correlatividad, y si entra a los totales infla la facturación del período con
#: plata que no existe.
#:
#: Es un fragmento y no ocho literales sueltos a propósito: repetir
#: `ambiente = 'produccion'` en cada consulta es de donde sale la que se olvida.
#:
#: Tampoco cuenta un comprobante **anulado** (`anulada_en`, ADR-022): no existe
#: ante ARCA y quedó en la base sólo como rastro.
SOLO_FISCALES = "ambiente = 'produccion' AND anulada_en IS NULL"


#: Filtra por emisor. `NULL` y `0` son lo mismo, «el único de la instancia»: es la
#: misma expresión que el índice `idx_facturas_numeracion`, así que lo usa.
SQL_DEL_EMISOR = "COALESCE(emisor_id, 0) = ?"


def sql_solo_fiscales(alias: str = "") -> str:
    """El filtro, con el alias de la tabla si la consulta usa uno."""
    p = f"{alias}." if alias else ""
    return f"{p}ambiente = 'produccion' AND {sql_vigente(alias)}"


def sql_vigente(alias: str = "") -> str:
    """Fragmento SQL: el comprobante **no está anulado** (ADR-022).

    Va en toda consulta que suma o cuenta comprobantes —totales, tablero, libros,
    pendientes de cobro— y en la búsqueda de notas previas. No va en los
    listados: un anulado se sigue viendo, con su marca, que es el rastro.
    """
    return f"{alias + '.' if alias else ''}anulada_en IS NULL"


# ── «Cuánto falta de una factura»: UNA definición (ADR-059) ───────────────────
#
# `pendiente = total − cobrado − acreditado`, donde **cobrado** es lo que ingresó a la caja por esa factura
# (sin anulados y sin la marca de cuenta corriente: es `get_cobros_factura`) y **acreditado** es lo que
# restan sus notas de crédito **con CAE y vigentes**. Hasta el ADR-059 las notas no restaban en ningún
# lado, salvo en `get_facturas_pendientes_cc`, que sacaba la factura entera con cualquier nota (incluso
# una parcial). Estos fragmentos son la fuente: el tope del cobro (`libracore.cobros`), el detalle del
# comprobante, la vista `sin_cobrar`, `resumen`, el tablero y la cuenta corriente los usan y no repiten el
# criterio —tener dos listas del mismo criterio es cómo se llega a que una pantalla diga «cobrada» y otra
# «pendiente»—.

#: Lo que sobra o falta de un centavo no cuenta: la misma tolerancia que `libracore.cobros.centavos`.
SQL_MEDIO_CENTAVO = "0.005"


def sql_cobrado(alias: str = "f", *, con_cuenta_corriente: bool = False) -> str:
    """Subconsulta: lo cobrado de la factura `alias` (ingresos de caja que cuentan, sin anulados).

    `con_cuenta_corriente=True` cuenta también la marca de venta a crédito como cobro: es el criterio histórico
    del tablero de los productos que no separan «fiar» de «cobrar» (`dashboard` sin `sin_fiado`)."""
    cc = "" if con_cuenta_corriente else f" AND {sql_no_es_cuenta_corriente('cm.medio_pago')}"
    return (
        "COALESCE((SELECT SUM(cm.monto) FROM caja_movimientos cm"
        f" WHERE cm.factura_id = {alias}.id AND cm.tipo = 'ingreso'{cc} AND {sql_no_anulado('cm')}), 0)"
    )


def sql_acreditado(alias: str = "f") -> str:
    """Subconsulta: lo que acreditan a la factura `alias` sus notas de crédito **con CAE y vigentes**.

    Una nota sin CAE (borrador, o `PENDIENTE`) todavía no acreditó nada y una anulada (ADR-022) nunca lo
    hará. Es de la misma factura: mismo emisor y mismo ambiente (ADR-021), porque `(tipo, pv, número)` solo
    no la identifica. Equivale a `notas_de_credito.acreditado` (un test lo fija)."""
    return (
        "COALESCE((SELECT SUM(n.total) FROM facturas n"
        f" WHERE n.tipo IN ({tipos.en_sql(tipos.NC)})"
        f" AND n.cbte_asoc_tipo = {alias}.tipo AND n.cbte_asoc_pv = {alias}.punto_venta"
        f" AND n.cbte_asoc_nro = {alias}.numero"
        f" AND COALESCE(n.emisor_id, 0) = COALESCE({alias}.emisor_id, 0)"
        f" AND n.ambiente = {alias}.ambiente"
        " AND COALESCE(n.cae, '') NOT IN ('', 'PENDIENTE')"
        f" AND {sql_vigente('n')}), 0)"
    )


def sql_pendiente(alias: str = "f", *, con_cuenta_corriente: bool = False) -> str:
    """Expresión: `total − cobrado − acreditado` de la factura `alias`, **sin** bajar de cero.

    Para filtrar usar `sql_con_pendiente`; para sumar lo que falta, `sql_pendiente_positivo`: un cobro de
    más (de antes del ADR-058) o una nota sobre una factura ya cobrada dan negativo, y restarían de la suma."""
    return (f"({alias}.total - {sql_cobrado(alias, con_cuenta_corriente=con_cuenta_corriente)}"
            f" - {sql_acreditado(alias)})")


def sql_es_cobrable(alias: str = "f") -> str:
    """Condición: `alias` es un comprobante con algo que cobrar —una factura o una nota de débito (ADR-064/066)—.

    🔑 **Es la única lista de «qué comprobante tiene un pendiente»** para las consultas: el listado `sin_cobrar`
    y su columna `pendiente`, el resumen, el tablero y la cuenta corriente la usan, igual que el tope del cobro
    usa `tipos.COBRABLES` (de ahí sale). Una consulta que repita `(1, 6, 11, …)` en vez de esto es la que, el día
    que se sume un tipo, se queda atrás: ese comprobante aparece en unas pantallas y en otras no. Las notas de
    crédito quedan afuera: no se cobran, restan (`sql_acreditado`)."""
    return f"{alias}.tipo IN ({tipos.en_sql(tipos.COBRABLES)})"


def sql_con_pendiente(alias: str = "f", *, con_cuenta_corriente: bool = False) -> str:
    """Condición: a la factura `alias` todavía le falta cobrar algo (más de medio centavo)."""
    return f"{sql_pendiente(alias, con_cuenta_corriente=con_cuenta_corriente)} > {SQL_MEDIO_CENTAVO}"


def sql_pendiente_positivo(alias: str = "f", *, con_cuenta_corriente: bool = False) -> str:
    """Expresión: lo que falta cobrar de `alias`, y 0 si no falta nada (para `SUM`)."""
    pendiente = sql_pendiente(alias, con_cuenta_corriente=con_cuenta_corriente)
    return f"(CASE WHEN {pendiente} > {SQL_MEDIO_CENTAVO} THEN {pendiente} ELSE 0 END)"


def get_cobrado_y_acreditado(factura_id, *, conn: Conexion | None = None) -> tuple[float, float] | None:
    """`(cobrado, acreditado)` de la factura, con los fragmentos de `sql_cobrado` y `sql_acreditado`; `None` si
    no existe. Es la lectura que usa el tope del cobro (ADR-058/059): la misma definición que el listado,
    el resumen y la cuenta corriente, no una cuenta aparte. Con `conn`, lo ve como lo ve esa transacción."""
    with _con(conn) as c:
        fila = c.execute(
            f"SELECT {sql_cobrado('f')}, {sql_acreditado('f')} FROM facturas f WHERE f.id = ?", (factura_id,),
        ).fetchone()
    return None if fila is None else (float(fila[0]), float(fila[1]))


class ComprobanteNoAnulable(Exception):
    """Por qué no se puede anular un comprobante. `codigo` es uno de los de abajo."""

    NO_EXISTE = "no_existe"
    CON_CAE = "con_cae"
    YA_ANULADO = "ya_anulado"
    CON_COBROS = "con_cobros"

    def __init__(self, codigo: str, mensaje: str):
        super().__init__(mensaje)
        self.codigo = codigo


def anular_factura(factura_id, usuario_id=None, motivo="", *, conn: Conexion | None = None) -> dict:
    """Anula un comprobante **sin CAE** y deja el rastro: cuándo, quién y por qué. Devuelve el comprobante.

    Es la alternativa a `delete_factura` para quien no quiere que un número
    desaparezca (ADR-022). El comprobante queda en la base con su número —que no
    se reusa— y fuera de los libros, los totales y la cuenta corriente.

    🔴 **Con CAE no se anula**: ese número existe ante ARCA y lo que corresponde es
    una nota de crédito. **Con cobros tampoco**: esa plata entró, y anular el
    comprobante la dejaría sin respaldo; primero se anulan los cobros.

    🔑 El débito de cuenta corriente que generó (el movimiento de caja «a cuenta»)
    se anula en la misma transacción. Sin eso la deuda del cliente seguiría en pie
    por un comprobante que ya no existe.

    Con `conn`, dentro de la transacción de quien llama (ver `_con`).
    """
    with _con(conn) as c:
        row = c.execute("SELECT * FROM facturas WHERE id=?", (factura_id,)).fetchone()
        if not row:
            raise ComprobanteNoAnulable(ComprobanteNoAnulable.NO_EXISTE, "El comprobante no existe.")
        if row["anulada_en"]:
            raise ComprobanteNoAnulable(ComprobanteNoAnulable.YA_ANULADO, "El comprobante ya está anulado.")
        if (row["cae"] or "") not in ("", "PENDIENTE"):
            raise ComprobanteNoAnulable(
                ComprobanteNoAnulable.CON_CAE,
                "El comprobante tiene CAE de ARCA: no se anula, se emite una nota de crédito.")
        cobros = c.execute(
            "SELECT COUNT(*) FROM caja_movimientos WHERE factura_id=? AND tipo='ingreso'"
            f" AND {sql_no_es_cuenta_corriente()} AND {sql_no_anulado()}",
            (factura_id,),
        ).fetchone()[0]
        if cobros:
            raise ComprobanteNoAnulable(
                ComprobanteNoAnulable.CON_COBROS,
                "El comprobante tiene cobros registrados: anulá los cobros antes de anularlo.")
        c.execute(
            "UPDATE facturas SET anulada_en=datetime('now','-3 hours'), anulada_por=?, "
            "anulacion_motivo=? WHERE id=?",
            (usuario_id, (motivo or "").strip()[:500], factura_id),
        )
        anulados = [f[0] for f in c.execute(
            "SELECT id FROM caja_movimientos WHERE factura_id=? AND tipo='ingreso'"
            f" AND {sql_es_cuenta_corriente()} AND {sql_no_anulado()}",
            (factura_id,),
        ).fetchall()]
        c.execute(
            "UPDATE caja_movimientos SET anulado=1 WHERE factura_id=? AND tipo='ingreso'"
            f" AND {sql_es_cuenta_corriente()} AND {sql_no_anulado()}",
            (factura_id,),
        )
        from libracore.db.caja import al_libro_de_clientes

        al_libro_de_clientes(c, anulados)
        return get_factura(factura_id, conn=c)


def get_next_factura_numero(punto_venta, tipo, ambiente: str = "produccion", emisor_id=None,
                            *, conn: Conexion | None = None):
    """El próximo número correlativo para tipo+punto_venta **en ese ambiente**.

    🔴 **El ambiente parte la secuencia, y es lo más peligroso de todo esto.**
    ARCA lleva numeraciones **independientes** en homologación y en producción.
    Sin separarlas acá, un comprobante de prueba numerado 500 —el que le tocaba
    en homologación— haría que el próximo real salga 501, cuando producción va
    por 84. La numeración local quedaría desalineada de la de ARCA y cada
    emisión posterior chocaría contra el "último autorizado" real.

    Es el defecto que **más caro sale** de los que abre poder probar desde una
    instancia viva: los totales mal se ven, un salto de numeración se descubre
    en la próxima presentación.

    El default `produccion` es el caso normal —quien no sabe de ambientes está
    facturando de verdad— y mantiene la firma vieja andando.

    El emisor también parte la secuencia: dos razones sociales con el mismo punto
    de venta numeran cada una la suya. `None` es el emisor único de la instancia.
    """
    with _con(conn) as c:
        row = c.execute(
            "SELECT MAX(numero) FROM facturas "
            f"WHERE punto_venta=? AND tipo=? AND ambiente=? AND {SQL_DEL_EMISOR}",
            (punto_venta, tipo, ambiente, emisor_id or 0),
        ).fetchone()
        return (row[0] or 0) + 1


def create_factura(tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon,
                   cliente_iva_cond, items, subtotal, iva_amount, total,
                   concepto=1, cae="", cae_vto="", observaciones="", pdf_path="",
                   cliente_domicilio="", fch_serv_desde="", fch_serv_hasta="",
                   fch_vto_pago="", cbte_asoc_tipo=0, cbte_asoc_pv=0, cbte_asoc_nro=0,
                   condicion_venta="", usuario_id=None,
                   fce_cbu="", fce_transmision="", fce_anulacion="", cbte_asoc_fecha="",
                   *, ambiente: str, emisor_id=None, conn: Conexion | None = None):
    """Crea una nueva factura electrónica. `numero` es el número calculado por el
    caller (local o vía ARCA) pero puede haber quedado obsoleto si otra factura
    concurrente para el mismo tipo+punto_venta se creó en el medio (no había
    ningún UNIQUE ni retry — hallazgo cruzado desde la auditoría de Restolibra,
    "race condition en numeración"). Si el INSERT choca contra
    idx_facturas_numero_unico, se recalcula el número y se reintenta — el
    caller debe releer la factura por id (`get_factura`) para conocer el
    número real, nunca asumir que es el que pasó.

    🔴 **`ambiente` es obligatorio y va por nombre.** La columna tiene default
    `'produccion'` en la base —lo necesita el backfill de las filas viejas, ver
    la revisión `0006`— así que un `INSERT` que la omitiera declararía real un
    comprobante que puede no serlo, y entraría al libro IVA del cliente.

    Los dos defaults posibles mienten en direcciones opuestas y las dos duelen:
    marcar de producción un comprobante de prueba ensucia los libros; marcar de
    prueba uno real lo **saca** del libro IVA en silencio, que es peor. Por eso
    no hay default: acá el ambiente **se declara**, o no se escribe la fila.

    `emisor_id` (`arca_config.id`) sólo lo pasa un producto con varias razones
    sociales. Sin él el comprobante es del emisor único de la instancia.

    Con `conn`, el comprobante se escribe en la transacción de quien llama y no se
    confirma acá (ver `_con`); cada intento va en un `SAVEPOINT` (`_savepoint`).
    """
    ambiente = (ambiente or "").strip().lower()
    if ambiente not in ("homologacion", "produccion"):
        raise ValueError(
            f"ambiente inválido para un comprobante: {ambiente!r}. "
            "Tiene que ser 'homologacion' o 'produccion'."
        )
    MAX_INTENTOS = 5
    for intento in range(MAX_INTENTOS):
        try:
            with _savepoint(conn, "libracore_factura"):
                return _insertar(
                    tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon,
                    cliente_iva_cond, items, subtotal, iva_amount, total, concepto, cae, cae_vto,
                    observaciones, pdf_path, cliente_domicilio, fch_serv_desde, fch_serv_hasta,
                    fch_vto_pago, cbte_asoc_tipo, cbte_asoc_pv, cbte_asoc_nro, condicion_venta,
                    usuario_id, ambiente, fce_cbu, fce_transmision, fce_anulacion, cbte_asoc_fecha,
                    emisor_id, conn=conn,
                )
        except sqlite3.IntegrityError:
            if intento == MAX_INTENTOS - 1:
                raise
            numero = get_next_factura_numero(
                punto_venta, tipo, ambiente, emisor_id, **({"conn": conn} if conn is not None else {}))


def _insertar(*valores, conn: Conexion | None = None) -> int:
    """El `INSERT` de un comprobante, con las columnas en el orden de `create_factura`. Devuelve el id."""
    valores = list(valores)
    valores[7] = json.dumps(valores[7], ensure_ascii=False)  # items
    with _con(conn) as c:
        cur = c.execute(
            """INSERT INTO facturas
               (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon,
                cliente_iva_cond, items, subtotal, iva_amount, total, concepto,
                cae, cae_vto, observaciones, pdf_path, cliente_domicilio,
                fch_serv_desde, fch_serv_hasta, fch_vto_pago,
                cbte_asoc_tipo, cbte_asoc_pv, cbte_asoc_nro, condicion_venta, usuario_id,
                ambiente, fce_cbu, fce_transmision, fce_anulacion, cbte_asoc_fecha,
                emisor_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(valores),
        )
        return cur.lastrowid


class NumeroYaRegistrado(Exception):
    """El número de un comprobante registrado a mano ya existe para ese emisor, tipo y punto de venta."""


def _ya_existe(tipo, punto_venta, numero, emisor_id, conn: Conexion | None = None) -> bool:
    with _con(conn) as c:
        return c.execute(
            "SELECT 1 FROM facturas WHERE tipo=? AND punto_venta=? AND numero=? "
            f"AND ambiente='produccion' AND {SQL_DEL_EMISOR}",
            (tipo, punto_venta, numero, emisor_id or 0),
        ).fetchone() is not None


def registrar_comprobante(tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon,
                          cliente_iva_cond, items, subtotal, iva_amount, total, *,
                          emisor_id=None, cae="", cae_vto="", conn: Conexion | None = None,
                          **opcionales) -> int:
    """Registra un comprobante **cuyo número viene de afuera** y devuelve su id.

    Es el caso de un producto que no emite por ARCA para esa razón social y el
    operador tipea el número del comprobante que emitió en otro lado (LibraCargo,
    su ADR-024). Puede traer el CAE, si lo tiene.

    🔴 **No reintenta con otro número**, a diferencia de `create_factura`. Ahí el
    número lo calcula el motor y, si otro lo ganó, el siguiente es igual de bueno.
    Acá el número **es el dato**: cambiarlo en silencio registraría un comprobante
    que no existe. Si ya está, levanta `NumeroYaRegistrado`.

    🔑 **Siempre `produccion`.** Un comprobante registrado a mano no salió de
    homologación: es del cliente y va al libro IVA. Los demás campos
    (`concepto`, `observaciones`, `condicion_venta`, `usuario_id`, las fechas del
    servicio, el asociado de una nota...) van por nombre, como en `create_factura`.
    Con `conn`, en la transacción de quien llama (ver `_con`).
    """
    if not (isinstance(numero, int) and not isinstance(numero, bool) and numero > 0):
        raise ValueError(f"El número de un comprobante es un entero mayor que cero, no {numero!r}.")
    if _ya_existe(tipo, punto_venta, numero, emisor_id, conn):
        raise NumeroYaRegistrado(
            f"El comprobante {punto_venta:04d}-{numero:08d} (tipo {tipo}) ya está registrado.")
    campos = {
        "concepto": 1, "observaciones": "", "pdf_path": "", "cliente_domicilio": "",
        "fch_serv_desde": "", "fch_serv_hasta": "", "fch_vto_pago": "", "cbte_asoc_tipo": 0,
        "cbte_asoc_pv": 0, "cbte_asoc_nro": 0, "condicion_venta": "", "usuario_id": None,
        "fce_cbu": "", "fce_transmision": "", "fce_anulacion": "", "cbte_asoc_fecha": "",
    }
    desconocidos = set(opcionales) - set(campos)
    if desconocidos:
        raise TypeError(f"registrar_comprobante: campos desconocidos {sorted(desconocidos)}")
    campos.update(opcionales)
    try:
        with _savepoint(conn, "libracore_registro"):
            return _insertar(
                tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, cliente_iva_cond,
                items, subtotal, iva_amount, total, campos["concepto"], cae, cae_vto,
                campos["observaciones"], campos["pdf_path"], campos["cliente_domicilio"],
                campos["fch_serv_desde"], campos["fch_serv_hasta"], campos["fch_vto_pago"],
                campos["cbte_asoc_tipo"], campos["cbte_asoc_pv"], campos["cbte_asoc_nro"],
                campos["condicion_venta"], campos["usuario_id"], "produccion", campos["fce_cbu"],
                campos["fce_transmision"], campos["fce_anulacion"], campos["cbte_asoc_fecha"],
                emisor_id, conn=conn,
            )
    except sqlite3.IntegrityError:
        # Lo ganó otro entre la consulta y el INSERT. Cualquier otra violación
        # (una FK que no existe) sale tal cual: no es un número repetido.
        if _ya_existe(tipo, punto_venta, numero, emisor_id, conn):
            raise NumeroYaRegistrado(
                f"El comprobante {punto_venta:04d}-{numero:08d} (tipo {tipo}) ya está registrado."
            ) from None
        raise


def registrar_del_nodo(*, tipo, punto_venta, numero, ambiente, fecha, cliente_cuit, cliente_razon,
                       cliente_iva_cond, items, subtotal, iva_amount, total, concepto=1, cae="", cae_vto="",
                       observaciones="", cliente_domicilio="", fch_serv_desde="", fch_serv_hasta="",
                       fch_vto_pago="", cbte_asoc_tipo=0, cbte_asoc_pv=0, cbte_asoc_nro=0, condicion_venta="",
                       emisor_id=None, conn: Conexion | None = None) -> int:
    """Escribe en el central un comprobante que emitió un nodo offline, **con su número y su ambiente** (ADR-074).

    Como `registrar_comprobante`, el número es el dato y no se reintenta con otro; a diferencia de él, el ambiente
    viene del nodo (pudo emitir contra homologación). Sin usuario: el del nodo no existe acá. Lo llama
    `facturas_del_nodo.aplicar_factura_del_nodo`, que antes mira si ya está."""
    ambiente = (ambiente or "").strip().lower()
    if ambiente not in ("homologacion", "produccion"):
        raise ValueError(f"ambiente inválido para un comprobante: {ambiente!r}.")
    with _savepoint(conn, "libracore_del_nodo"):
        return _insertar(
            tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, cliente_iva_cond, items, subtotal,
            iva_amount, total, concepto, cae, cae_vto, observaciones, "", cliente_domicilio, fch_serv_desde,
            fch_serv_hasta, fch_vto_pago, cbte_asoc_tipo, cbte_asoc_pv, cbte_asoc_nro, condicion_venta, None,
            ambiente, "", "", "", "", emisor_id, conn=conn,
        )


_TIPOS_FACTURA = tipos.FACTURAS
_TIPOS_COBRABLES = tipos.COBRABLES
_TIPOS_NC      = tipos.NC
_TIPOS_ND      = tipos.ND

_VISTA_TIPOS = {
    "facturas": _TIPOS_FACTURA,
    "nc":       _TIPOS_NC,
    "nd":       _TIPOS_ND,
}


def get_all_facturas(limit=100, vista="facturas"):
    """Obtiene facturas, notas de crédito o notas de débito (últimas primero)."""
    tipos = _VISTA_TIPOS.get(vista, _TIPOS_FACTURA)
    placeholders = ",".join("?" * len(tipos))
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM facturas WHERE tipo IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            (*tipos, limit),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


#: Valores de `envio` en `get_facturas_filtradas` (ADR-051). Otro valor no filtra.
ENVIO_ENVIADA = "enviada"
ENVIO_SIN_ENVIAR = "sin_enviar"


def get_facturas_filtradas(desde="", hasta="", q="", vista="facturas", limit=50, offset=0, envio=""):
    """Listado de facturas con filtros de fecha, búsqueda, envío por mail y paginación.

    `envio` (ADR-051) mira el rastro de `envios_email`: `enviada` es «salió al menos un mail»;
    `sin_enviar` es «no salió ninguno» —un intento fallido cuenta como no enviado, porque el
    cliente no lo recibió— y se limita a los comprobantes con CAE, que son los que se mandan.
    """
    solo_sin_cobrar = (vista == "sin_cobrar")
    # «Sin cobrar» mira todo lo que se cobra: las facturas y las notas de débito (ADR-066).
    tipos = _TIPOS_COBRABLES if solo_sin_cobrar else _VISTA_TIPOS.get(vista, _TIPOS_FACTURA)
    ph = ",".join("?" * len(tipos))
    conds = [f"f.tipo IN ({ph})"]
    params = list(tipos)
    c_fecha, p_fecha = rango_por_dia("f.fecha", desde, hasta)
    conds += c_fecha; params += p_fecha
    if q:
        conds.append(sql_busqueda(
            "CAST(f.numero AS TEXT)", "f.cliente_razon", "f.observaciones"))
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    # 🔴 El criterio de «cobrada» sale de `sql_con_pendiente` (ADR-059) y no se reescribe: se usa en dos
    # lugares de esta función —las columnas de abajo y el filtro `solo_sin_cobrar`— y si divergieran, una
    # factura podría listarse como impaga y a la vez mostrar el total cobrado completo.
    if solo_sin_cobrar:
        conds.append("f.cae != '' AND f.cae IS NOT NULL AND f.cae != 'PENDIENTE'")
        conds.append(sql_con_pendiente("f"))
    salio_un_mail = ("EXISTS (SELECT 1 FROM envios_email e WHERE e.documento_tipo = 'factura'"
                     " AND e.documento_id = f.id AND e.estado = 'enviado')")
    if envio == ENVIO_ENVIADA:
        conds.append(salio_un_mail)
    elif envio == ENVIO_SIN_ENVIAR:
        conds.append("f.cae != '' AND f.cae IS NOT NULL AND f.cae != 'PENDIENTE'")
        conds.append(f"NOT {salio_un_mail}")
    where = " AND ".join(conds)
    # `total_acreditado` y `pendiente` son nuevos (aditivos): con una nota de crédito, «cobrado < total» ya no
    # alcanza para decir «sin cobrar» —la factura acreditada por completo no debe nada—.
    # El `pendiente` es de lo que se cobra (facturas y notas de débito, ADR-066): una nota de crédito no tiene
    # «lo que falta cobrar», resta.
    cobrada_col = (f"{sql_cobrado('f')} AS total_cobrado, {sql_acreditado('f')} AS total_acreditado,"
                   f" (CASE WHEN {sql_es_cobrable('f')} THEN {sql_pendiente_positivo('f')} ELSE 0 END)"
                   " AS pendiente")
    with get_connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM facturas f WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT f.*, {cobrada_col} FROM facturas f WHERE {where} ORDER BY f.id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["items"] = json.loads(d["items"])
        result.append(d)
    return {"items": result, "total": total}


def get_factura(factura_id, *, conn: Conexion | None = None):
    """Obtiene una factura por ID. Con `conn`, la ve como la ve esa transacción."""
    with _con(conn) as c:
        row = c.execute("SELECT * FROM facturas WHERE id=?", (factura_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["items"] = json.loads(d["items"])
        return d


def update_factura_cae(factura_id, cae, cae_vto, *, conn: Conexion | None = None):
    """Actualiza CAE de una factura después de obtenerlo de ARCA.

    Borra `cae_error`: un comprobante autorizado ya no tiene un rechazo que mostrar.
    """
    with _con(conn) as c:
        c.execute(
            "UPDATE facturas SET cae=?, cae_vto=?, cae_error='' WHERE id=?",
            (cae, cae_vto, factura_id)
        )


def update_factura_cae_error(factura_id, motivo, *, conn: Conexion | None = None):
    """Deja anotado por qué ARCA no autorizó el comprobante (`''` lo borra)."""
    with _con(conn) as c:
        c.execute(
            "UPDATE facturas SET cae_error=? WHERE id=?",
            ((motivo or "")[:1000], factura_id)
        )


def update_factura_pdf_path(factura_id, pdf_path, *, conn: Conexion | None = None):
    """Actualiza el path del PDF de la factura."""
    with _con(conn) as c:
        c.execute(
            "UPDATE facturas SET pdf_path=? WHERE id=?",
            (pdf_path, factura_id)
        )


def search_facturas(query, vista="facturas"):
    """Busca facturas por número, cliente u observaciones."""
    tipos = _VISTA_TIPOS.get(vista, _TIPOS_FACTURA)
    placeholders = ",".join("?" * len(tipos))
    q = f"%{query}%"
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT * FROM facturas
               WHERE tipo IN ({placeholders})
                 AND {sql_busqueda("CAST(numero AS TEXT)", "cliente_razon", "observaciones")}
               ORDER BY id DESC""",
            (*tipos, q, q, q),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


def _y_ambiente(ambiente) -> tuple[str, tuple]:
    """El filtro opcional por ambiente: `("", ())` sin ambiente, que es no filtrar."""
    return (" AND ambiente=?", (ambiente,)) if ambiente else ("", ())


def get_notas_de_factura(tipo, punto_venta, numero, tipos_nota, emisor_id=None, ambiente=None,
                         *, conn: Conexion | None = None):
    """Devuelve notas (NC o ND) que referencian un comprobante.

    🔑 **`(tipo, pv, número)` no alcanza para identificar al original**, por dos
    motivos, y quien tiene el original a mano pasa los dos datos:
    - **El emisor.** Con dos razones sociales, las dos pueden tener la Factura A
      0001-00000005. Una nota es siempre del emisor de su original.
    - **El ambiente.** Homologación y producción numeran por separado, así que la
      factura de prueba 5 y la real 5 conviven. Sin `ambiente` no se filtra, que
      es como funcionó siempre.
    """
    placeholders = ",".join("?" * len(tipos_nota))
    filtro_amb, param_amb = _y_ambiente(ambiente)
    with _con(conn) as c:
        rows = c.execute(
            f"""SELECT * FROM facturas
               WHERE tipo IN ({placeholders})
                 AND cbte_asoc_tipo=? AND cbte_asoc_pv=? AND cbte_asoc_nro=?
                 AND {SQL_DEL_EMISOR}{filtro_amb} AND {sql_vigente()}
               ORDER BY id DESC""",
            (*tipos_nota, tipo, punto_venta, numero, emisor_id or 0, *param_amb),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


def get_nc_de_factura(tipo, punto_venta, numero, emisor_id=None, ambiente=None,
                      *, conn: Conexion | None = None):
    """Devuelve las notas de crédito que anulan un comprobante."""
    return get_notas_de_factura(tipo, punto_venta, numero, _TIPOS_NC, emisor_id, ambiente, conn=conn)


def get_nd_de_factura(tipo, punto_venta, numero, emisor_id=None, ambiente=None,
                      *, conn: Conexion | None = None):
    """Devuelve las notas de débito asociadas a un comprobante."""
    return get_notas_de_factura(tipo, punto_venta, numero, _TIPOS_ND, emisor_id, ambiente, conn=conn)


def get_factura_por_tipo_pv_nro(tipo, punto_venta, numero, emisor_id=None, ambiente=None,
                                *, conn: Conexion | None = None):
    """Busca un comprobante por emisor + tipo + punto de venta + número (y ambiente, si viene)."""
    filtro_amb, param_amb = _y_ambiente(ambiente)
    with _con(conn) as c:
        row = c.execute(
            "SELECT * FROM facturas WHERE tipo=? AND punto_venta=? AND numero=? "
            f"AND {SQL_DEL_EMISOR}{filtro_amb}",
            (tipo, punto_venta, numero, emisor_id or 0, *param_amb),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["items"] = json.loads(d["items"])
        return d


def delete_factura(factura_id):
    """Elimina una factura."""
    with get_connection() as conn:
        # Sus movimientos de caja quedan sin factura (`ON DELETE SET NULL`), y un
        # ingreso a cuenta corriente sin factura ya no es deuda: el libro lo revierte.
        movimientos = [f[0] for f in conn.execute(
            "SELECT id FROM caja_movimientos WHERE factura_id=?", (factura_id,)
        ).fetchall()]
        conn.execute("DELETE FROM facturas WHERE id=?", (factura_id,))
        from libracore.db.caja import al_libro_de_clientes

        al_libro_de_clientes(conn, movimientos)
