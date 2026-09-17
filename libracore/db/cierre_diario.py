"""Cierre diario: el acto registrado de cerrar el día operativo de una
sucursal, con la foto de sus turnos, sus cajas y sus medios de pago.

## El modelo

Una sucursal cierra **un día por vez** (`cierres_diarios`, UNIQUE por
`(sucursal_id, fecha)`) y se numera correlativamente por sucursal (UNIQUE por
`(sucursal_id, numero)`). Las dos UNIQUE son índices por EXPRESIÓN sobre
`COALESCE(sucursal_id, 0)`: un UNIQUE liso no colisiona entre dos filas con
`sucursal_id IS NULL` —ninguno de los dos motores lo hace—, así que una
sucursal-NULL (turnos sin caja, o cajas sin sucursal: los datos viejos de
antes de que un producto tuviera sucursales) podría cerrar el mismo día dos
veces si no fuera por la expresión. `0` no puede chocar con un id real: los
`INTEGER PRIMARY KEY AUTOINCREMENT`/`BIGSERIAL` de esta familia arrancan en 1.

Cerrar un día junta **todas las cajas de esa sucursal** (`cajas.sucursal_id`,
vía `turnos_caja.caja_id`) y guarda tres niveles de foto, todos en
`cierres_diarios_medios` y distinguidos por qué FK llevan:

- **por turno** (`cierre_turno_id` puesto): el arqueo de un cajero.
- **por caja** (`cierre_turno_id` NULL, `caja_id` puesto): la suma de los
  turnos de esa caja en este cierre.
- **de la sucursal** (las dos NULL): el total del día.

Es una foto y no una vista: `cierre_diario.py` no vuelve a leer
`caja_movimientos` para mostrar un cierre ya hecho (ver `get_cierre` y
`get_cierre_turno`). Reimprimir dos veces el mismo ticket da lo mismo aunque
después alguien anule un movimiento del turno — anular no reabre el arqueo, y
tampoco debería cambiar un comprobante que ya se entregó.

## La guarda de apertura

`verificar_dia_abierto()` la llama `libracore.db.turnos.create_turno()` en la
MISMA transacción del alta: es el camino común de los productos que hoy
llaman al motor (VentaLibra, LibraClub; también Contalibra/Restolibra, que no
usan sucursales — para ellos la tabla `cierres_diarios` queda vacía para
siempre y la guarda es un no-op). Ningún producto tuvo que tocar su alta de
turnos para tener la protección.

## La numeración, contra la carrera

`cerrar_dia()` reintenta con una CONEXIÓN NUEVA por intento (mismo patrón que
`facturas.crear_factura`): en PostgreSQL un error aborta la transacción, así
que reutilizar la misma conexión después de un `IntegrityError` no sirve —hay
que volver a abrir. Cada reintento repite las validaciones desde cero, así que
las dos colisiones posibles en el `INSERT` terminan distinto:

- Si chocó el UNIQUE de **fecha** (otro cierre de la MISMA sucursal y el
  MISMO día ganó la carrera), el reintento encuentra ese cierre ya guardado y
  levanta `DiaYaCerradoError` — no vuelve a intentar.
- Si chocó el UNIQUE de **numero** (la misma sucursal cerrando dos días
  DISTINTOS a la vez — dos cierres pendientes que se atrasaron y se piden
  juntos), el reintento pide el número de vuelta —ya subió— y el segundo
  intento entra limpio.

## El ticket de un turno: foto o vivo, nunca dos cuentas

El caso normal no es reimprimir el cierre del día: es que el cajero cierra SU
turno e imprime el arqueo EN ESE MOMENTO, antes de que exista cualquier
cierre diario. `arqueo_de_turno()` es la entrada única para eso: usa la foto
si el turno ya entró a un cierre (`get_cierre_turno_por_turno_id`), y si no,
lo calcula en vivo (`arqueo_turno_en_vivo`) — con la MISMA `_medios_de_turno`/
`_diferencia` que arma la foto, no una segunda implementación del cálculo.

## Reabrir un día (v1.107.0)

`reabrir_dia()` anula un cierre en vez de borrarlo: le marca `anulado_en`/
`anulado_por`/`motivo_anulacion` y listo — la foto (`cierres_diarios_turnos`/
`cierres_diarios_medios`) y el `numero` quedan intactos, como corresponde a
un acto registrado. El UNIQUE de fecha es un índice PARCIAL (`WHERE
anulado_en IS NULL`) desde esta versión, así que un cierre anulado no
bloquea volver a cerrar ese mismo día — el de `numero` no cambió: sigue
liso, el anulado se queda con el suyo y el próximo cierre toma el
siguiente. Ver el docstring de `reabrir_dia()` para las reglas (motivo
obligatorio, no reabrir con un cierre posterior activo).

## `sucursal_id=None`

Es "sin sucursal" —una sucursal real, no un comodín— en las CUATRO funciones
de destino del módulo: `dia_cerrado`, `verificar_dia_abierto`,
`preview_cierre_dia` y `cerrar_dia`. La única excepción a propósito es
`listar_cierres`, que filtra por sucursal como las demás pero necesita además
un modo "todas" — ahí es `todas=True`, explícito, nunca `sucursal_id=None`.
"""
from __future__ import annotations

import contextlib
import sqlite3

from libracore.db.caja import sql_no_anulado, sql_no_es_cuenta_corriente
from libracore.db.core import Conexion, _ar_now, get_connection

#: Cuántas veces reintentar el alta ante una colisión de numeración. No es
#: sobre la colisión de DÍA (esa no se reintenta: es un estado real, no una
#: carrera transitoria) — ver el docstring del módulo.
_MAX_INTENTOS_NUMERACION = 5


class TurnosAbiertosError(RuntimeError):
    """Hay turnos sin cerrar en la sucursal para ese día operativo."""


class DiaYaCerradoError(RuntimeError):
    """Ya existe un cierre diario ACTIVO para esa sucursal y esa fecha. Un
    cierre anulado (ver `reabrir_dia`) no cuenta: por eso el índice único que
    respalda esta regla es parcial (`WHERE anulado_en IS NULL`)."""


class TurnoAbiertoError(RuntimeError):
    """El turno sigue abierto: no hay arqueo de cierre para imprimir todavía."""


class DiaCerradoError(RuntimeError):
    """El día operativo ya está cerrado para esta sucursal: no se puede abrir
    un turno con apertura en él. Un cierre anulado no cuenta — ver `reabrir_dia`."""


class CierreNoEncontradoError(RuntimeError):
    """No existe un cierre diario con ese id."""


class CierreYaAnuladoError(RuntimeError):
    """Ese cierre diario ya fue anulado antes — `reabrir_dia` no es
    idempotente: anular dos veces perdería cuál de los dos motivos y qué
    admin fue el que realmente reabrió el día."""


class CierrePosteriorError(RuntimeError):
    """La misma sucursal tiene un cierre ACTIVO de una fecha posterior.
    Reabrir dejaría un día suelto detrás de uno que ya se dio por cerrado —
    ver el docstring de `reabrir_dia`."""


class MotivoRequeridoError(ValueError):
    """El motivo de la anulación viene vacío (o sólo espacios)."""


#: El DDL de las tres tablas de este módulo, en el mismo dialecto
#: SQLite-de-siempre que usa `schema.py` — el adaptador de PostgreSQL lo
#: traduce (`AUTOINCREMENT`→`BIGSERIAL`, `REAL`→`DOUBLE PRECISION`, etc.).
#:
#: 🔴 **Vive ACÁ y no repetido en la migración.** `init_core_schema()` está
#: congelada (ver `test_schema_congelado.py`) y estas tablas no pueden entrar
#: ahí, pero el DDL en sí necesita UN solo dueño igual — si viviera sólo
#: dentro de `migrations/versions/0009_cierre_diario.py` cualquier test que
#: arma su base con `init_core_schema()` a secas (sin correr Alembic, que es
#: como arranca hoy la mayoría de la suite del motor) necesitaría importar un
#: módulo de migración, y ESE módulo importa `alembic` — una dependencia que
#: la suite normal no tiene por qué instalar sólo para armar una tabla. La
#: migración llama a `crear_tablas()`, y un test que necesite estas tablas
#: sobre un schema armado a mano también.
_DDL_TABLAS = """
    CREATE TABLE IF NOT EXISTS cierres_diarios (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        sucursal_id             INTEGER,
        numero                  INTEGER NOT NULL,
        fecha                   TEXT NOT NULL,
        usuario_id              INTEGER NOT NULL REFERENCES usuarios(id),
        monto_esperado_total    REAL NOT NULL DEFAULT 0,
        monto_declarado_total   REAL NOT NULL DEFAULT 0,
        diferencia_total        REAL NOT NULL DEFAULT 0,
        notas                   TEXT DEFAULT '',
        created_at              TEXT DEFAULT (datetime('now','-3 hours')),
        anulado_en              TEXT,
        anulado_por             INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        motivo_anulacion        TEXT
    );

    -- Parcial desde v1.107.0 (reabrir día): un cierre ANULADO no cuenta para
    -- la unicidad de fecha, así que la misma sucursal puede volver a cerrar
    -- ese día después de reabrirlo. El de NÚMERO no cambia -- el anulado
    -- conserva el suyo, ver `reabrir_dia` y `_cerrar_dia_en_transaccion`.
    --
    -- 🔑 Este `CREATE ... IF NOT EXISTS` sólo alcanza a una tabla NUEVA: una
    -- base que ya tenía `cierres_diarios` de antes de v1.107.0 (con el índice
    -- viejo, sin `WHERE`) la migra la revisión `0011_reabrir_cierre_diario`,
    -- con `op.add_column`/`op.execute` puros -- nunca acá. Si el ALTER
    -- viviera en esta función, cada arranque de un producto que la llama al
    -- iniciar (LibraClub, ver `app/servicios/facturacion.py::configurar()`)
    -- deshacía un `alembic downgrade` en el próximo boot, igual que el
    -- problema que documenta `0010_recibido_en_ventas_pagos.py`.
    CREATE UNIQUE INDEX IF NOT EXISTS ux_cierres_diarios_sucursal_fecha_activo
        ON cierres_diarios (COALESCE(sucursal_id, 0), fecha)
        WHERE anulado_en IS NULL;

    CREATE UNIQUE INDEX IF NOT EXISTS ux_cierres_diarios_sucursal_numero
        ON cierres_diarios (COALESCE(sucursal_id, 0), numero);

    CREATE TABLE IF NOT EXISTS cierres_diarios_turnos (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        cierre_diario_id    INTEGER NOT NULL REFERENCES cierres_diarios(id) ON DELETE CASCADE,
        turno_id            INTEGER NOT NULL REFERENCES turnos_caja(id),
        cajero_nombre       TEXT NOT NULL,
        caja_id             INTEGER,
        caja_nombre         TEXT,
        apertura            TEXT NOT NULL,
        cierre              TEXT NOT NULL,
        monto_inicial       REAL NOT NULL DEFAULT 0,
        monto_esperado      REAL NOT NULL DEFAULT 0,
        monto_declarado     REAL NOT NULL DEFAULT 0,
        diferencia          REAL NOT NULL DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_cierres_diarios_turnos_cierre
        ON cierres_diarios_turnos (cierre_diario_id);

    CREATE TABLE IF NOT EXISTS cierres_diarios_medios (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        cierre_diario_id    INTEGER NOT NULL REFERENCES cierres_diarios(id) ON DELETE CASCADE,
        cierre_turno_id     INTEGER REFERENCES cierres_diarios_turnos(id) ON DELETE CASCADE,
        caja_id             INTEGER,
        medio_pago          TEXT NOT NULL,
        ingresos            REAL NOT NULL DEFAULT 0,
        egresos             REAL NOT NULL DEFAULT 0,
        neto                REAL NOT NULL DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_cierres_diarios_medios_cierre
        ON cierres_diarios_medios (cierre_diario_id);
"""


def crear_tablas(conn: Conexion) -> None:
    """Crea las tres tablas de este módulo si no existen. Idempotente, como
    `init_core_schema()`. La llama la migración `0009_cierre_diario` (contra
    una base real, vía Alembic) y puede llamarla directamente un test que
    arma su base sólo con `init_core_schema()` y necesita además estas
    tablas — por ejemplo, cualquiera que ejercite
    `libracore.db.logs.get_actividad_log()` con sus partes por default, que
    desde esta versión incluyen `cierres_diarios`."""
    conn.executescript(_DDL_TABLAS)


def _fmt_fecha_ar(fecha: str) -> str:
    """`fecha` (`YYYY-MM-DD`) a `dd-mm-aaaa` para los mensajes que llegan a
    pantalla -- el estándar de la familia (`wiki/concepts/estandares-
    desarrollo.md`, sección "Fecha y hora"): el dato en sí sigue viajando en
    ISO, esto es sólo presentación en el texto del error.

    No reutiliza `ticket_generator.fmt_fecha()` a propósito: ese vive en la
    capa de impresión y arrastra `fpdf` como dependencia -- un módulo de
    `db/` no tiene motivo para cargarla sólo por un formateo de fecha."""
    if len(fecha) >= 10 and fecha[4] == "-" and fecha[7] == "-":
        return f"{fecha[8:10]}-{fecha[5:7]}-{fecha[0:4]}"
    return fecha


def _norm_sucursal(sucursal_id: int | None) -> int:
    """`sucursal_id` normalizado al mismo criterio que el índice único:
    `NULL` es `0`. Sale a un helper porque se repite en cada consulta de
    escritura de este módulo, y las dos partes —la columna y el parámetro—
    tienen que usar EXACTAMENTE el mismo criterio para que la comparación
    encuentre la fila."""
    return sucursal_id if sucursal_id is not None else 0


def sucursal_de_caja(caja_id: int | None, conn: Conexion | None = None) -> int | None:
    """La sucursal de esa caja, o `None` si la caja no tiene sucursal o si
    `caja_id` es `None` (turno suelto, sin mostrador — el caso de todo
    producto sin cajas múltiples)."""
    if caja_id is None:
        return None
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        fila = c.execute("SELECT sucursal_id FROM cajas WHERE id=?", (caja_id,)).fetchone()
    return fila["sucursal_id"] if fila else None


def _tabla_cierres_ausente(mensaje: str) -> bool:
    """Si ESTE mensaje de error dice, específicamente, que la tabla
    `cierres_diarios` no existe.

    Las dos formas que usan los motores: SQLite dice *"no such table"*;
    PostgreSQL (psycopg levanta `UndefinedTable`, SQLSTATE `42P01`) dice
    *"relation ... does not exist"*, y llega hasta acá como
    `sqlite3.ProgrammingError` — no `OperationalError` — porque
    `_equivalente_sqlite3()` de `_postgres.py` traduce por NOMBRE de clase
    subiendo el MRO: el primer ancestro de `UndefinedTable` que también
    existe en `sqlite3` es `ProgrammingError` (`OperationalError` no está en
    esa cadena). La traducción no conserva el SQLSTATE original —reconstruye
    una excepción nueva con sólo `str(e)`—, así que lo único que queda para
    distinguir "esta tabla no está" de cualquier otro error es el TEXTO.

    Por eso exige las dos cosas: la palabra "cierres_diarios" Y una de las
    dos frases. Un `ProgrammingError` por conexión cerrada
    (`"Cannot operate on a closed database."`, que también es un
    `ProgrammingError` de `sqlite3` real) no menciona la tabla y no matchea;
    un error de sintaxis que por casualidad nombre la tabla tampoco, porque
    no dice ninguna de las dos frases.
    """
    return "cierres_diarios" in mensaje and (
        "no such table" in mensaje or "does not exist" in mensaje
    )


def _columna_anulado_ausente(mensaje: str) -> bool:
    """Si ESTE mensaje dice, específicamente, que la columna `anulado_en`
    todavía no existe -- la MISMA ventana de deploy-antes-que-migración que
    `_tabla_cierres_ausente`, pero para la migración `0011_reabrir_cierre_
    diario` en vez de la `0009`: acá la tabla YA existe (`0009` corrió hace
    meses en cualquier instancia real) y lo que puede faltar es sólo la
    columna nueva, en el rato entre que se despliega este código y que
    corre `libracore-migrar`.

    SQLite dice *"no such column: anulado_en"*; PostgreSQL (`UndefinedColumn`,
    SQLSTATE `42703`, traducida por `_equivalente_sqlite3` al mismo
    `ProgrammingError` que `UndefinedTable`) dice *"column \"anulado_en\" does
    not exist"*."""
    return "anulado_en" in mensaje and (
        "no such column" in mensaje or "does not exist" in mensaje
    )


def dia_cerrado(fecha: str, sucursal_id: int | None, conn: Conexion | None = None) -> bool:
    """Si ya hay un cierre diario ACTIVO para esa sucursal y esa fecha
    (`YYYY-MM-DD`). Uno anulado (`anulado_en` puesto, ver `reabrir_dia`) no
    cuenta -- por eso el filtro incluye `AND anulado_en IS NULL`, el mismo
    criterio que respalda el índice parcial que reemplazó al UNIQUE liso.

    🔴 **Si la tabla `cierres_diarios` todavía no existe, devuelve `False` en
    vez de reventar.** No es indulgencia con un bug: la crea la migración
    `0009_cierre_diario`, y "el deploy no corre migraciones solo" es una regla
    de la propia familia (ver el README, sección Migraciones). Esta función la
    llama `turnos.create_turno()` en CADA alta de turno, de los seis
    productos — usen o no cierre diario. Si el código nuevo de este motor
    llega a una instancia antes que la migración corra, sin este fallback
    **ningún producto podría abrir un turno** hasta que alguien la corriera;
    con él, el período entre deploy y migración se comporta exactamente igual
    que antes de esta versión: nadie cerró nunca un día, así que ninguno está
    cerrado.

    🔴 **El `except` es angosto a propósito** (ver `_tabla_cierres_ausente`):
    sólo atrapa el mensaje exacto de "esta tabla no existe" en cualquiera de
    los dos motores. Cualquier otro error —una conexión cerrada, un permiso
    faltante, lo que sea— se propaga tal cual: tragárselo dejaría a
    `create_turno()` creyendo que ningún día está cerrado nunca, con el error
    real escondido.
    """
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        try:
            fila = c.execute(
                "SELECT 1 FROM cierres_diarios "
                "WHERE COALESCE(sucursal_id,0)=? AND fecha=? AND anulado_en IS NULL",
                (_norm_sucursal(sucursal_id), fecha),
            ).fetchone()
        except (sqlite3.OperationalError, sqlite3.ProgrammingError) as e:
            mensaje = str(e)
            if _columna_anulado_ausente(mensaje):
                # La tabla existe (`0009` corrió) pero todavía no la columna
                # `anulado_en` (falta `0011`): sin ella nunca hubo un
                # anulado, así que cae al mismo query que esta función tenía
                # antes de v1.107.0. Ver `_columna_anulado_ausente`.
                c.rollback()
                fila = c.execute(
                    "SELECT 1 FROM cierres_diarios WHERE COALESCE(sucursal_id,0)=? AND fecha=?",
                    (_norm_sucursal(sucursal_id), fecha),
                ).fetchone()
            elif _tabla_cierres_ausente(mensaje):
                # En PostgreSQL el error deja la transacción abortada: sin
                # este rollback, todo lo que la conexión ejecute después —el
                # INSERT del propio `create_turno`, en el caso real— muere
                # con "current transaction is aborted". Mismo motivo que el
                # rollback de `comprobantes_pendientes.upsert_comprobante`.
                c.rollback()
                return False
            else:
                raise
    return fila is not None


def verificar_dia_abierto(fecha: str, sucursal_id: int | None, conn: Conexion | None = None) -> None:
    """Levanta `DiaCerradoError` si el día operativo `fecha` ya está cerrado
    para esa sucursal. Es lo que llama `turnos.create_turno()` antes de abrir."""
    if dia_cerrado(fecha, sucursal_id, conn=conn):
        raise DiaCerradoError(
            f"El día {_fmt_fecha_ar(fecha)} ya está cerrado para esta sucursal: "
            "no se puede abrir un turno con apertura en él."
        )


def _turnos_del_dia(conn: Conexion, fecha: str, sucursal_id: int | None) -> list[dict]:
    """Los turnos —de CUALQUIER estado— cuya apertura cae en `fecha` (hora AR)
    y cuya caja pertenece a `sucursal_id`. `substr` y no `DATE()`: `apertura`
    es texto ISO (`_ar_now()`), no una columna de fecha — mismo motivo que
    `PARTE_TURNOS` en `db/logs.py`."""
    rows = conn.execute(
        """SELECT t.id, t.usuario_id, t.apertura, t.cierre, t.monto_inicial,
                  t.monto_declarado_cierre, t.monto_esperado_cierre, t.estado,
                  t.notas, t.caja_id,
                  COALESCE(u.nombre, 'usuario #' || t.usuario_id) AS usuario_nombre,
                  c.nombre AS caja_nombre
             FROM turnos_caja t
             LEFT JOIN usuarios u ON u.id = t.usuario_id
             LEFT JOIN cajas c ON c.id = t.caja_id
            WHERE substr(t.apertura, 1, 10) = ?
              AND COALESCE(c.sucursal_id, 0) = ?
            ORDER BY t.id""",
        (fecha, _norm_sucursal(sucursal_id)),
    ).fetchall()
    return [dict(r) for r in rows]


def _medios_de_turno(conn: Conexion, turno_id: int) -> list[dict]:
    """Ingresos/egresos/neto por medio de pago de UN turno.

    Mismos dos filtros que `get_resumen_turno_caja`/`get_caja_resumen`:
    `sql_no_anulado()` —lo anulado no cuenta para el arqueo, sale del total
    pero la lista de movimientos lo sigue mostrando— y
    `sql_no_es_cuenta_corriente()` —una venta a crédito no es plata que entró
    al cajón, y sumarla acá infla el "esperado" con algo que nunca se va a
    cobrar en esta caja—.
    """
    filtro = f"{sql_no_anulado()} AND {sql_no_es_cuenta_corriente()}"
    rows = conn.execute(
        f"""SELECT LOWER(COALESCE(NULLIF(medio_pago,''),'sin_especificar')) AS medio,
                   COALESCE(SUM(CASE WHEN tipo='ingreso' THEN monto ELSE 0 END), 0) AS ingresos,
                   COALESCE(SUM(CASE WHEN tipo='egreso' THEN monto ELSE 0 END), 0) AS egresos
              FROM caja_movimientos
             WHERE turno_id=? AND {filtro}
             GROUP BY medio
             ORDER BY medio""",
        (turno_id,),
    ).fetchall()
    return [
        {
            "medio_pago": r["medio"],
            "ingresos": float(r["ingresos"]),
            "egresos": float(r["egresos"]),
            "neto": round(float(r["ingresos"]) - float(r["egresos"]), 2),
        }
        for r in rows
    ]


def _diferencia(esperado: float | None, declarado: float | None) -> float:
    """`declarado - esperado`, redondeado — la MISMA cuenta para la foto
    (`_armar_snapshot`) y para el arqueo en vivo (`arqueo_turno_en_vivo`). Un
    signo o un redondeo distinto entre las dos hubiera sido la forma más
    tonta de que el ticket impreso al cerrar el turno no coincida con el que
    sale del cierre diario del mismo turno."""
    return round((declarado or 0.0) - (esperado or 0.0), 2)


def _armar_snapshot(conn: Conexion, turnos: list[dict]):
    """Calcula, para el conjunto de `turnos` de un cierre, la foto por turno,
    por caja y de la sucursal. Compartido por `preview_cierre_dia()` (que no
    graba nada) y `_cerrar_dia_en_transaccion()` (que sí)."""
    turnos_info = []
    medios_por_caja: dict[int, dict[str, dict]] = {}
    medios_dia: dict[str, dict] = {}
    esperado_total = declarado_total = 0.0

    def _acumular(acc: dict, medio: dict):
        bucket = acc.setdefault(medio["medio_pago"], {"ingresos": 0.0, "egresos": 0.0, "neto": 0.0})
        bucket["ingresos"] += medio["ingresos"]
        bucket["egresos"] += medio["egresos"]
        bucket["neto"] += medio["neto"]

    for t in turnos:
        medios_t = _medios_de_turno(conn, t["id"])
        esperado = t["monto_esperado_cierre"] or 0.0
        declarado = t["monto_declarado_cierre"] or 0.0
        esperado_total += esperado
        declarado_total += declarado
        caja_key = t["caja_id"] if t["caja_id"] is not None else 0
        acc_caja = medios_por_caja.setdefault(caja_key, {})
        for m in medios_t:
            _acumular(medios_dia, m)
            _acumular(acc_caja, m)
        turnos_info.append({
            **t,
            "diferencia": _diferencia(esperado, declarado),
            "medios": medios_t,
        })

    return {
        "turnos": turnos_info,
        "medios_por_caja": medios_por_caja,
        "medios_dia": medios_dia,
        "monto_esperado_total": round(esperado_total, 2),
        "monto_declarado_total": round(declarado_total, 2),
        "diferencia_total": _diferencia(esperado_total, declarado_total),
    }


def preview_cierre_dia(sucursal_id: int | None, fecha: str | None = None) -> dict:
    """Lo que daría cerrar el día, SIN guardar nada. Para la pantalla previa:
    qué turnos hay, cuáles siguen abiertos (bloquean el cierre) y los totales
    que resultarían si se cerrara ahora.

    `sucursal_id` es un TARGET, no un filtro: `None` es una sucursal válida
    ("sin sucursal", el caso de datos viejos o de una caja sin sucursal
    asignada) — no "todas". Es la misma semántica que `cerrar_dia()`.
    """
    fecha = fecha or _ar_now()[:10]
    with get_connection() as conn:
        turnos = _turnos_del_dia(conn, fecha, sucursal_id)
        abiertos = [t for t in turnos if t["estado"] == "abierto"]
        cerrados = [t for t in turnos if t["estado"] != "abierto"]
        snap = _armar_snapshot(conn, cerrados)

    return {
        "fecha": fecha,
        "sucursal_id": sucursal_id,
        "turnos_abiertos": abiertos,
        "turnos": snap["turnos"],
        "medios": [{"medio_pago": k, **v} for k, v in snap["medios_dia"].items()],
        "monto_esperado_total": snap["monto_esperado_total"],
        "monto_declarado_total": snap["monto_declarado_total"],
        "diferencia_total": snap["diferencia_total"],
        "puede_cerrar": len(abiertos) == 0,
        "ya_cerrado": dia_cerrado(fecha, sucursal_id),
    }


def _mensaje_turnos_abiertos(fecha: str, abiertos: list[dict]) -> str:
    detalle = "; ".join(
        f"cajero {t['usuario_nombre']!r}, caja {t['caja_nombre'] or 'sin caja'!r}, "
        f"apertura {t['apertura']}"
        for t in abiertos
    )
    plural = "s" if len(abiertos) != 1 else ""
    return (
        f"Hay {len(abiertos)} turno{plural} abierto{plural} en esta sucursal para "
        f"el {fecha}: {detalle}. Hay que cerrarlo{plural} antes de cerrar el día."
    )


def _cerrar_dia_en_transaccion(conn: Conexion, fecha: str, sucursal_id: int | None,
                               usuario_id: int, notas: str) -> int:
    turnos = _turnos_del_dia(conn, fecha, sucursal_id)
    abiertos = [t for t in turnos if t["estado"] == "abierto"]
    if abiertos:
        raise TurnosAbiertosError(_mensaje_turnos_abiertos(fecha, abiertos))
    if dia_cerrado(fecha, sucursal_id, conn=conn):
        raise DiaYaCerradoError(
            f"El día {_fmt_fecha_ar(fecha)} ya está cerrado para esta sucursal."
        )

    snap = _armar_snapshot(conn, turnos)

    numero_sucursal = _norm_sucursal(sucursal_id)
    numero = conn.execute(
        "SELECT COALESCE(MAX(numero),0)+1 FROM cierres_diarios WHERE COALESCE(sucursal_id,0)=?",
        (numero_sucursal,),
    ).fetchone()[0]

    cur = conn.execute(
        """INSERT INTO cierres_diarios
           (sucursal_id, numero, fecha, usuario_id, monto_esperado_total,
            monto_declarado_total, diferencia_total, notas)
           VALUES (?,?,?,?,?,?,?,?)""",
        (sucursal_id, numero, fecha, usuario_id, snap["monto_esperado_total"],
         snap["monto_declarado_total"], snap["diferencia_total"], notas),
    )
    cierre_id = cur.lastrowid

    for t in snap["turnos"]:
        cur_t = conn.execute(
            """INSERT INTO cierres_diarios_turnos
               (cierre_diario_id, turno_id, cajero_nombre, caja_id, caja_nombre,
                apertura, cierre, monto_inicial, monto_esperado, monto_declarado,
                diferencia)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (cierre_id, t["id"], t["usuario_nombre"], t["caja_id"], t["caja_nombre"],
             t["apertura"], t["cierre"], t["monto_inicial"] or 0.0,
             t["monto_esperado_cierre"] or 0.0, t["monto_declarado_cierre"] or 0.0,
             t["diferencia"]),
        )
        cierre_turno_id = cur_t.lastrowid
        for m in t["medios"]:
            conn.execute(
                """INSERT INTO cierres_diarios_medios
                   (cierre_diario_id, cierre_turno_id, caja_id, medio_pago,
                    ingresos, egresos, neto)
                   VALUES (?,?,?,?,?,?,?)""",
                (cierre_id, cierre_turno_id, t["caja_id"], m["medio_pago"],
                 m["ingresos"], m["egresos"], m["neto"]),
            )

    for caja_key, medios in snap["medios_por_caja"].items():
        caja_id_real = None if caja_key == 0 else caja_key
        for medio, acc in medios.items():
            conn.execute(
                """INSERT INTO cierres_diarios_medios
                   (cierre_diario_id, cierre_turno_id, caja_id, medio_pago,
                    ingresos, egresos, neto)
                   VALUES (?,NULL,?,?,?,?,?)""",
                (cierre_id, caja_id_real, medio, acc["ingresos"], acc["egresos"], acc["neto"]),
            )

    for medio, acc in snap["medios_dia"].items():
        conn.execute(
            """INSERT INTO cierres_diarios_medios
               (cierre_diario_id, cierre_turno_id, caja_id, medio_pago,
                ingresos, egresos, neto)
               VALUES (?,NULL,NULL,?,?,?,?)""",
            (cierre_id, medio, acc["ingresos"], acc["egresos"], acc["neto"]),
        )

    return cierre_id


def cerrar_dia(usuario_id: int, sucursal_id: int | None = None, fecha: str | None = None,
              notas: str = "") -> dict:
    """Cierra el día operativo `fecha` (default: hoy en hora AR) de
    `sucursal_id`. Admin lo decide el LLAMADOR — este motor recibe
    `usuario_id` y no pregunta rol; ver `caja_router.build_cierre_diario_router`.

    Levanta `TurnosAbiertosError` si queda algo sin cerrar, `DiaYaCerradoError`
    si ese día ya se cerró para esta sucursal. Reintenta ante una colisión de
    numeración (ver el docstring del módulo); las otras dos excepciones NO se
    reintentan, porque no son una carrera transitoria.
    """
    fecha = fecha or _ar_now()[:10]
    for intento in range(_MAX_INTENTOS_NUMERACION):
        try:
            with get_connection() as conn:
                cierre_id = _cerrar_dia_en_transaccion(conn, fecha, sucursal_id, usuario_id, notas)
            return get_cierre(cierre_id)
        except sqlite3.IntegrityError:
            if intento == _MAX_INTENTOS_NUMERACION - 1:
                raise
            continue
    raise AssertionError("no debería llegar acá")  # pragma: no cover


def reabrir_dia(cierre_id: int, usuario_id: int, motivo: str) -> dict:
    """Anula un cierre diario: le pone `anulado_en`/`anulado_por`/
    `motivo_anulacion` y libera el día para volver a abrir turnos. Admin lo
    decide el LLAMADOR, igual que `cerrar_dia()` — ver
    `caja_router.build_cierre_diario_router`.

    **No borra nada.** El cierre anulado sigue existiendo con su `numero` —
    es un acto registrado, igual que uno activo — y la foto en
    `cierres_diarios_turnos`/`cierres_diarios_medios` queda intacta: lo único
    que cambia es que deja de contar para `dia_cerrado()`/
    `verificar_dia_abierto()` (el índice único de fecha es parcial, `WHERE
    anulado_en IS NULL`, desde v1.107.0). Volver a cerrar el mismo día
    después de reabrirlo arma un cierre NUEVO, con el número siguiente —
    `_cerrar_dia_en_transaccion` calcula `numero` contra el máximo de la
    sucursal sin filtrar anulados, así que la numeración no tiene huecos.

    Levanta `MotivoRequeridoError` si `motivo` viene vacío (tras `strip()`)
    — se valida ANTES de tocar la base. Levanta `CierreNoEncontradoError` si
    `cierre_id` no existe, `CierreYaAnuladoError` si ya estaba anulado.

    Levanta `CierrePosteriorError` si la misma sucursal tiene un cierre
    ACTIVO de una fecha POSTERIOR a la de este: reabrir dejaría un día suelto
    detrás de uno que ya se dio por cerrado, y la numeración —correlativa por
    sucursal, no por fecha— dejaría de leerse en el mismo orden que las
    fechas.

    🔴 **Lo que NO se chequea, a propósito: turnos de fecha posterior.**
    `turnos.create_turno()` siempre estampa `apertura=_ar_now()` — no hay
    forma de abrir un turno con fecha pasada — así que reabrir un día `D` que
    no es HOY no destraba nada para la creación de turnos: los nuevos turnos
    van a parar a HOY, no a `D`. El único caso útil en la práctica es reabrir
    el cierre del día de hoy. Y la foto de un cierre es inmutable pase lo que
    pase después con `turnos_caja` (ver el docstring del módulo, "Es una
    foto y no una vista") — reabrir tampoco la toca. Sin ninguna de las dos
    cosas en juego, que existan turnos —abiertos o cerrados— en una fecha
    posterior no es una condición que haga falta bloquear.
    """
    motivo = (motivo or "").strip()
    if not motivo:
        raise MotivoRequeridoError("El motivo de la anulación no puede estar vacío.")

    with get_connection() as conn:
        fila = conn.execute(
            "SELECT * FROM cierres_diarios WHERE id=?", (cierre_id,)
        ).fetchone()
        if not fila:
            raise CierreNoEncontradoError(f"No existe el cierre diario #{cierre_id}.")
        cierre = dict(fila)
        if cierre["anulado_en"] is not None:
            raise CierreYaAnuladoError(f"El cierre diario #{cierre_id} ya está anulado.")

        suc = _norm_sucursal(cierre["sucursal_id"])
        posterior = conn.execute(
            """SELECT id, numero, fecha FROM cierres_diarios
                WHERE COALESCE(sucursal_id,0)=? AND fecha>? AND anulado_en IS NULL
                ORDER BY fecha LIMIT 1""",
            (suc, cierre["fecha"]),
        ).fetchone()
        if posterior:
            raise CierrePosteriorError(
                f"La sucursal ya tiene el cierre #{posterior['numero']} del "
                f"{_fmt_fecha_ar(posterior['fecha'])}, posterior al "
                f"{_fmt_fecha_ar(cierre['fecha'])}: reabrir este día dejaría un "
                "día suelto detrás de uno ya cerrado."
            )

        conn.execute(
            """UPDATE cierres_diarios
                  SET anulado_en=?, anulado_por=?, motivo_anulacion=?
                WHERE id=?""",
            (_ar_now(), usuario_id, motivo, cierre_id),
        )
    return get_cierre(cierre_id)


def listar_cierres(sucursal_id: int | None = None, *, todas: bool = False,
                   limit: int = 50) -> list[dict]:
    """Los cierres de `sucursal_id`, o de TODAS si `todas=True`.

    🔴 **`sucursal_id=None` es una sucursal real ("sin sucursal"), igual que
    en `cerrar_dia()`/`dia_cerrado()` — no "todas".** Antes esta función era
    la excepción: trataba `None` como "sin filtro" (mismo criterio que
    `db.caja.get_all_cajas()`), y las otras tres funciones del módulo lo
    trataban como un target real. La misma llamada —`listar_cierres()`, sin
    argumentos— quería decir una cosa acá y otra ahí, y la única forma de
    notarlo era leer el docstring. "Todas" ahora es un pedido explícito: sin
    `todas=True`, `sucursal_id=None` filtra por `COALESCE(sucursal_id,0)=0`,
    igual que cualquier otra sucursal.

    Cada fila trae `cerrado_por_nombre`, con el mismo `LEFT JOIN usuarios` que
    `get_cierre()`: el listado es donde se lee "quién cerró", y sin esto cada
    producto lo resolvía por su cuenta (LibraClub llegó a tapar esta ruta con
    un endpoint propio para agregarlo).
    """
    select = (
        "SELECT cd.*, COALESCE(u.nombre, 'usuario #' || cd.usuario_id) AS cerrado_por_nombre"
        "  FROM cierres_diarios cd LEFT JOIN usuarios u ON u.id = cd.usuario_id"
    )
    with get_connection() as conn:
        if todas:
            rows = conn.execute(
                f"{select} ORDER BY cd.id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                f"{select} WHERE COALESCE(cd.sucursal_id,0)=? ORDER BY cd.id DESC LIMIT ?",
                (_norm_sucursal(sucursal_id), limit),
            ).fetchall()
    return [dict(r) for r in rows]


def get_cierre(cierre_id: int) -> dict | None:
    """El cierre completo: su cabecera, sus turnos (con sus medios), sus
    cajas (agrupando los turnos y con el subtotal por caja) y los medios de
    la sucursal. Es la FOTO — nada de esto vuelve a leer `caja_movimientos`."""
    with get_connection() as conn:
        fila = conn.execute(
            """SELECT cd.*, COALESCE(u.nombre, 'usuario #' || cd.usuario_id) AS cerrado_por_nombre
                 FROM cierres_diarios cd
                 LEFT JOIN usuarios u ON u.id = cd.usuario_id
                WHERE cd.id=?""",
            (cierre_id,),
        ).fetchone()
        if not fila:
            return None
        cierre = dict(fila)
        turnos = [dict(r) for r in conn.execute(
            "SELECT * FROM cierres_diarios_turnos WHERE cierre_diario_id=? ORDER BY id",
            (cierre_id,),
        ).fetchall()]
        medios = [dict(r) for r in conn.execute(
            "SELECT * FROM cierres_diarios_medios WHERE cierre_diario_id=? ORDER BY id",
            (cierre_id,),
        ).fetchall()]

    por_turno: dict[int, list[dict]] = {}
    por_caja: dict[int | None, list[dict]] = {}
    de_la_sucursal: list[dict] = []
    for m in medios:
        if m["cierre_turno_id"] is not None:
            por_turno.setdefault(m["cierre_turno_id"], []).append(m)
        elif m["caja_id"] is not None:
            por_caja.setdefault(m["caja_id"], []).append(m)
        else:
            de_la_sucursal.append(m)

    for t in turnos:
        t["medios"] = por_turno.get(t["id"], [])

    cajas: dict[int | None, dict] = {}
    for t in turnos:
        key = t["caja_id"]
        cajas.setdefault(key, {"caja_id": key, "caja_nombre": t["caja_nombre"], "turnos": []})
        cajas[key]["turnos"].append(t)
    for key, info in cajas.items():
        info["medios"] = por_caja.get(key, [])

    cierre["turnos"] = turnos
    cierre["cajas"] = list(cajas.values())
    cierre["medios"] = de_la_sucursal
    return cierre


def get_cierre_turno(cierre_turno_id: int) -> dict | None:
    """El arqueo de UN turno dentro de un cierre, con su desglose por medio —
    lo que necesita `generar_ticket_cierre_turno()` para reimprimir."""
    with get_connection() as conn:
        fila = conn.execute(
            "SELECT * FROM cierres_diarios_turnos WHERE id=?", (cierre_turno_id,)
        ).fetchone()
        if not fila:
            return None
        row = dict(fila)
        medios = [dict(r) for r in conn.execute(
            "SELECT * FROM cierres_diarios_medios WHERE cierre_turno_id=? ORDER BY id",
            (cierre_turno_id,),
        ).fetchall()]
    row["medios"] = medios
    return row


def get_cierre_turno_por_turno_id(turno_id: int) -> dict | None:
    """La foto de este turno, si YA entró a algún cierre diario. `None` si
    todavía no —incluido el caso de que `turno_id` ni siquiera exista—, que
    es la señal que usa `arqueo_de_turno()` para decidir si arma el arqueo en
    vivo. `ORDER BY id DESC LIMIT 1` porque un turno entra a lo sumo a un
    cierre en la operación normal, pero no hay una UNIQUE que lo garantice —
    y ante cualquier duda es mejor la más reciente que un error de "más de
    una fila"."""
    with get_connection() as conn:
        fila = conn.execute(
            "SELECT id FROM cierres_diarios_turnos WHERE turno_id=? ORDER BY id DESC LIMIT 1",
            (turno_id,),
        ).fetchone()
    if not fila:
        return None
    return get_cierre_turno(fila["id"])


def arqueo_turno_en_vivo(turno_id: int) -> dict | None:
    """El arqueo de UN turno CERRADO, calculado EN VIVO contra
    `turnos_caja`/`caja_movimientos` — para el caso normal: el cajero cierra
    su turno y lo imprime en el momento, antes de que exista ningún cierre
    diario que lo incluya.

    Usa `_medios_de_turno()` y `_diferencia()` — LAS MISMAS que arman la foto
    en `_armar_snapshot()` — y no una segunda cuenta: dos implementaciones
    del mismo número son cómo un arqueo termina mostrando una cosa en la
    pantalla y otra en el papel.

    Devuelve `None` si `turno_id` no existe. Levanta `TurnoAbiertoError` si
    el turno sigue abierto: no hay arqueo de cierre para un turno que no
    cerró todavía. No usar directamente desde un router — ver
    `arqueo_de_turno()`, que primero mira si ya hay una foto.
    """
    with get_connection() as conn:
        fila = conn.execute(
            """SELECT t.id, t.estado, t.apertura, t.cierre, t.monto_inicial,
                      t.monto_esperado_cierre, t.monto_declarado_cierre, t.caja_id,
                      COALESCE(u.nombre, 'usuario #' || t.usuario_id) AS usuario_nombre,
                      c.nombre AS caja_nombre
                 FROM turnos_caja t
                 LEFT JOIN usuarios u ON u.id = t.usuario_id
                 LEFT JOIN cajas c ON c.id = t.caja_id
                WHERE t.id=?""",
            (turno_id,),
        ).fetchone()
        if not fila:
            return None
        t = dict(fila)
        if t["estado"] == "abierto":
            raise TurnoAbiertoError(
                f"El turno #{turno_id} sigue abierto: no hay arqueo de cierre "
                "para imprimir todavía."
            )
        medios = _medios_de_turno(conn, turno_id)

    esperado = t["monto_esperado_cierre"] or 0.0
    declarado = t["monto_declarado_cierre"] or 0.0
    return {
        "turno_id": t["id"],
        "cajero_nombre": t["usuario_nombre"],
        "caja_id": t["caja_id"],
        "caja_nombre": t["caja_nombre"],
        "apertura": t["apertura"],
        "cierre": t["cierre"],
        "monto_inicial": t["monto_inicial"] or 0.0,
        "monto_esperado": esperado,
        "monto_declarado": declarado,
        "diferencia": _diferencia(esperado, declarado),
        "medios": medios,
    }


def arqueo_de_turno(turno_id: int) -> dict | None:
    """El arqueo de un turno, listo para `generar_ticket_cierre_turno()`:
    la FOTO si el turno ya entró a un cierre diario, o el cálculo EN VIVO si
    todavía no. Es la función que llama el router — un producto no tiene que
    saber cuál de los dos casos es el suyo.

    Devuelve `None` si `turno_id` no existe. Levanta `TurnoAbiertoError` si
    el turno sigue abierto (ver `arqueo_turno_en_vivo`) — un turno que ya
    tiene foto nunca puede estar abierto, así que ese caso sólo se decide en
    el camino en vivo.
    """
    foto = get_cierre_turno_por_turno_id(turno_id)
    if foto is not None:
        return foto
    return arqueo_turno_en_vivo(turno_id)
