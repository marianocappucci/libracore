"""Small DB-API compatibility layer used while LibraCore becomes dual-backend."""

from __future__ import annotations

import re
import sqlite3
from decimal import Decimal
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import Connection


#: Marcador interno para los `?` mientras dura la traduccion. Un caracter que
#: no puede aparecer en SQL escrito a mano, para que nada lo confunda con texto.
_MARCA = "\x00"

#: Los modificadores de `datetime('now', ...)` / `date('now', ...)` que el
#: adaptador sabe traducir: los que tienen forma de intervalo (`-3 hours`,
#: `+15 minutes`). Se compilan aca y no adentro de `_paramstyle`, que corre
#: en cada consulta.
_MODIFICADOR_DATETIME = re.compile(
    r"\bdatetime\('now'\s*,\s*'([+-]?\d+\s+\w+)'\s*\)", re.IGNORECASE
)
_MODIFICADOR_DATE = re.compile(
    r"\bdate\('now'\s*,\s*'([+-]?\d+\s+\w+)'\s*\)", re.IGNORECASE
)

_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+", re.IGNORECASE)
_INSERT_IGNORE_RE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", re.IGNORECASE)
_TABLE_INFO_RE = re.compile(r"^\s*PRAGMA\s+table_info\s*\(\s*([\w]+)\s*\)\s*;?\s*$", re.IGNORECASE)
_SQLITE_MASTER_RE = re.compile(
    r"^\s*SELECT\s+1\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*(['\"]?)([\w]+)\1\s*;?\s*$",
    re.IGNORECASE,
)


def _replace_qmarks(sql: str) -> str:
    """Replace qmark parameters without touching comments or SQL literals."""
    out = []
    i = 0
    quote = None
    while i < len(sql):
        if quote:
            out.append(sql[i])
            if sql[i] == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            end = len(sql) if end < 0 else end
            out.append(sql[i:end])
            i = end
            continue
        if sql[i] in ("'", '"'):
            quote = sql[i]
            out.append(sql[i])
        elif sql[i] == "?":
            out.append(_MARCA)
        else:
            out.append(sql[i])
        i += 1
    return "".join(out)


def _argumentos_de_nivel_uno(texto: str) -> tuple[list[str], str] | None:
    """Parte la lista de argumentos de una llamada, respetando el anidamiento.

    `texto` es lo que va DESPUÉS del paréntesis de apertura. Devuelve los
    argumentos de primer nivel y el resto de la cadena, o `None` si el
    paréntesis nunca cierra (SQL truncado: mejor no tocar nada).
    """
    profundidad = 0
    comilla = None
    args: list[str] = []
    actual: list[str] = []
    for i, ch in enumerate(texto):
        if comilla:
            actual.append(ch)
            if ch == comilla:
                comilla = None
            continue
        if ch in ("'", '"'):
            comilla = ch
            actual.append(ch)
        elif ch == "(":
            profundidad += 1
            actual.append(ch)
        elif ch == ")":
            if profundidad == 0:
                args.append("".join(actual))
                return [a.strip() for a in args], texto[i + 1:]
            profundidad -= 1
            actual.append(ch)
        elif ch == "," and profundidad == 0:
            args.append("".join(actual))
            actual = []
        else:
            actual.append(ch)
    return None


def _castear_round(sql: str) -> str:
    """`ROUND(x, n)` sobre `double precision` no existe en PostgreSQL.

    Sólo hay `round(numeric, integer)` y `round(double precision)` de un
    argumento, así que la forma de dos argumentos hay que castearla. La versión
    anterior de esta traducción era una regex que exigía literalmente
    `ROUND(SUM(...), n)`, y la consulta real del reporte de stock bajo es
    `ROUND(COALESCE(SUM(ms.cantidad), 0), 3)` — no matcheaba, pasaba entera al
    motor y reventaba con *"function round(double precision, integer) does not
    exist"*. Por eso ahora se parsean los paréntesis en vez de adivinar la
    forma: cualquier expresión como primer argumento queda cubierta.
    """
    out = []
    i = 0
    bajo = sql.lower()
    while i < len(sql):
        if bajo.startswith("round(", i) and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            partido = _argumentos_de_nivel_uno(sql[i + len("round("):])
            if partido is not None:
                args, resto = partido
                if len(args) == 2:
                    # El primer argumento puede traer otro ROUND adentro.
                    #
                    # 🔴 Y el resultado vuelve a `double precision`. Sin eso,
                    # `ROUND` devuelve NUMERIC, psycopg lo entrega como
                    # `decimal.Decimal`, y el codigo de la familia --que hace
                    # aritmetica con `float` porque en SQLite estas columnas son
                    # REAL-- muere con *unsupported operand type(s) for *:
                    # 'float' and 'decimal.Decimal'*. Lo encontro la suite de
                    # Restolibra el 2026-08-10, y lejos de la consulta: en el
                    # calculo del costo de una receta.
                    #
                    # El NUMERIC lo introduce ESTA traduccion, asi que le toca a
                    # ella deshacerlo y devolver el mismo tipo que SQLite.
                    out.append(
                        f"CAST(ROUND(CAST({_castear_round(args[0])} AS NUMERIC), "
                        f"{args[1]}) AS DOUBLE PRECISION)"
                    )
                    sql = resto
                    bajo = sql.lower()
                    i = 0
                    continue
        out.append(sql[i])
        i += 1
    return "".join(out)


#: Los formatos de `strftime` que usa la familia, y su equivalente en
#: PostgreSQL. Estaba resuelto solo `%Y-%m`, y los otros pasaban crudos al motor
#: y morian con *function strftime(unknown, text) does not exist*.
_FORMATOS_STRFTIME = {
    "%Y": "to_char(cast({x} AS date), 'YYYY')",
    "%Y-%m": "to_char(cast({x} AS date), 'YYYY-MM')",
    "%Y-%m-%d": "to_char(cast({x} AS date), 'YYYY-MM-DD')",
    # 🔴 `%s` es el segundero desde epoch, y es el que se usa para restar dos
    # horarios: la reserva de una mesa comprueba que dos horas no esten a menos
    # de N segundos. `EXTRACT(EPOCH ...)` da lo mismo, y el `::timestamp` es
    # necesario porque en esta familia las fechas viajan como texto.
    "%s": "EXTRACT(EPOCH FROM cast({x} AS timestamp))",
}


def _traducir_strftime(sql: str) -> str:
    """`strftime(fmt, x)` a su equivalente de PostgreSQL.

    Un formato no contemplado se deja pasar **tal cual**, para que falle en el
    motor con su nombre a la vista, en vez de traducirse a cualquier cosa: un
    formato mal traducido daria un numero o una fecha equivocada, y eso no se
    nota hasta mucho despues.
    """
    def reemplazo(m: re.Match) -> str:
        plantilla = _FORMATOS_STRFTIME.get(m.group(1))
        if plantilla is None:
            return m.group(0)
        return plantilla.format(x=m.group(2).strip())

    return re.sub(
        r"\bstrftime\('([^']+)',\s*([^()]+)\)",
        reemplazo,
        sql,
        flags=re.IGNORECASE,
    )


def _paramstyle(sql: str) -> str:
    """Translate the qmark SQL used by LibraCore to psycopg's format style."""
    sql = _replace_qmarks(sql)
    # `datetime('now')` en SQLite devuelve TEXTO con formato fijo
    # 'YYYY-MM-DD HH:MM:SS', y así queda guardado en las 30 columnas
    # `created_at TEXT DEFAULT (datetime('now'))` del schema. `CURRENT_TIMESTAMP`
    # a secas, castrado a texto por la columna TEXT, escribe
    # '2026-08-08 23:45:24.986262+00' — con microsegundos y offset de zona. Es
    # el mismo dato pero NO el mismo string, y esa diferencia sale por dos
    # lados: cualquier `strptime(...)` sobre la columna, y la comparación
    # lexicográfica de rangos de fecha, que es como este motor filtra.
    # Se emite el formato de SQLite, byte por byte.
    #
    # `AT TIME ZONE 'UTC'` no es decorativo: `datetime('now')` de SQLite es
    # UTC, y `CURRENT_TIMESTAMP` de PostgreSQL depende del `TimeZone` de la
    # sesión. Sin fijarlo, un sidecar con TZ local guardaría tres horas
    # corridas respecto de lo que guardaba la misma base en SQLite.
    _FORMATO = "'YYYY-MM-DD HH24:MI:SS'"
    sql = re.sub(
        rf"\bdatetime\('now'\s*,\s*'localtime'\s*,\s*{_MARCA}\)",
        f"to_char(LOCALTIMESTAMP + {_MARCA}::interval, {_FORMATO})",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bdatetime\('now'\s*,\s*'localtime'\)",
        f"to_char(LOCALTIMESTAMP, {_FORMATO})",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bdatetime\('now'\)",
        f"to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', {_FORMATO})",
        sql,
        flags=re.IGNORECASE,
    )
    # `datetime('now', '-3 hours')` es el "ahora" en hora de Argentina, y es
    # lo que estampan los DEFAULT de `schema.py` (ver `AHORA_AR` alla). Sale del
    # MISMO instante UTC que la traduccion de arriba y no de `LOCALTIMESTAMP`:
    # asi el valor no depende de la zona de la sesion del servidor, que se fija
    # en el `initdb` y que `TZ` no mueve.
    #
    # Solo se traduce el modificador con forma de intervalo (`+-N unidad`), que
    # es el que PostgreSQL entiende con la misma sintaxis. Cualquier otro
    # (`'start of month'`, `'weekday 0'`) se deja pasar tal cual para que falle
    # en el motor con su nombre a la vista, igual que en `_traducir_strftime`.
    sql = _MODIFICADOR_DATETIME.sub(
        lambda m: (f"to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC'"
                   f" + interval '{m.group(1)}', {_FORMATO})"),
        sql,
    )
    sql = _MODIFICADOR_DATE.sub(
        lambda m: (f"to_char((CURRENT_TIMESTAMP AT TIME ZONE 'UTC'"
                   f" + interval '{m.group(1)}')::date, 'YYYY-MM-DD')"),
        sql,
    )
    # `date('now')` devuelve TEXTO en SQLite, y las fechas de este motor se
    # guardan como TEXT ISO ('YYYY-MM-DD'), así que las comparaciones son
    # lexicográficas — que para ISO coincide con el orden cronológico. Traducir
    # a `CURRENT_DATE` a secas rompía eso: `valid_until < CURRENT_DATE` es
    # `text < date` y PostgreSQL no tiene ese operador. Se traduce a texto para
    # que la comparación siga siendo la misma que en SQLite, sin depender de
    # que todos los valores guardados sean fechas válidas.
    sql = re.sub(r"\bdate\('now'\)", "to_char(CURRENT_DATE, 'YYYY-MM-DD')", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bdate\(([^()]+)\)", r"CAST(\1 AS DATE)", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bgroup_concat\(([^,()]+),\s*(['\"][^'\"]*['\"])\)", r"string_agg(\1, \2)", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"\bprintf\('%0([0-9]+)d',\s*([^()]+)\)",
        r"lpad(cast(\2 AS text), \1, '0')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = _traducir_strftime(sql)
    sql = re.sub(r"->>\s*'\$\.([A-Za-z_][A-Za-z0-9_]*)'", r"->> '\1'", sql)
    sql = _castear_round(sql)
    sql = re.sub(
        r"\bjson_each\(([^()]+)\)\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"jsonb_array_elements(\1::jsonb) AS \2(value)",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(r"\bCAST\(([^()]+)\s+AS\s+REAL\)", r"CAST(\1 AS DOUBLE PRECISION)", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bAUTOINCREMENT\b", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bBLOB\b", "BYTEA", sql, flags=re.IGNORECASE)
    sql = _diferir_fks_hacia_adelante(sql)
    return _escapar_porcentajes(sql)


def _escapar_porcentajes(sql: str) -> str:
    """Duplica los `%` LITERALES, que psycopg leeria como marcadores.

    🔴 psycopg usa `%s`, asi que escanea la consulta entera buscando `%`. Un
    porcentaje escrito en el SQL --`LIKE 'sqlite_%'`, `LIKE '%_old'`-- lo lee
    como el comienzo de un marcador y falla con *the query has N placeholders
    but M parameters were passed*, o con *only '%s', '%b', '%t' are allowed as
    placeholders*. El error **no nombra el `%`**: habla de cuantos parametros
    faltan, asi que se lee como un bug del llamador. Lo encontro la suite de
    Restolibra el 2026-08-10, en el seed de la demo.

    Va AL FINAL de la traduccion y no al principio: adelantado, las regex que
    buscan `%Y-%m` y compania ya no encontrarian nada, porque el `%` seria `%%`.

    Los marcadores que genero esta capa viajan como un centinela --no como
    `%s`-- justamente para poder distinguirlos: asi se duplican TODOS los `%`
    del SQL y recien despues se ponen los marcadores de verdad. Sin eso,
    `strftime('%s', ...)` --la forma epoch de SQLite, que esta en el codigo de
    Restolibra-- se contaba como marcador y la consulta fallaba diciendo que
    faltaban parametros.
    """
    return sql.replace("%", "%%").replace(_MARCA, "%s")


# SQLite acepta declarar una FK hacia una tabla que todavía no fue creada;
# PostgreSQL no. Estas dos son las únicas del schema que lo hacen, y
# `schema.py` las vuelve a agregar como constraint con nombre después de crear
# todas las tablas, sólo en el backend PostgreSQL.
#
# 🔴 La lista es por (tabla, columna) y no por tabla referenciada, que es como
# estaba antes: `REFERENCES turnos_caja(id) ON DELETE SET NULL` matcheaba en
# CUALQUIER `CREATE TABLE`, incluidas tres que no lo necesitaban porque
# `turnos_caja` ya existe cuando se crean — `ventas` acá, y `venta_links` en
# Contalibra y en Restolibra. A esas tres se les sacaba la FK y nadie se la
# volvía a poner: en PostgreSQL quedaban sin integridad referencial, y en
# SQLite con ella. Lo encontró el volcado de `schema_dump.py` al diffear los
# dos motores (42 FKs contra 41).
_FKS_DIFERIDAS = (
    ("caja_movimientos", "turno_id"),
    ("movimientos_stock", "venta_id"),
)

_CREATE_TABLE_RE = re.compile(
    r"\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _diferir_fks_hacia_adelante(sql: str) -> str:
    """Saca del `CREATE TABLE` sólo las FKs que apuntan a una tabla que todavía
    no existe.

    La coincidencia es **columna + su cláusula**, no el nombre de la tabla
    referenciada suelto: así no depende de cómo esté formateado el DDL (el
    schema declara una columna por línea, pero los tests lo escriben en una
    sola) y no puede alcanzar a la columna de al lado.
    """
    match = _CREATE_TABLE_RE.match(sql)
    if not match:
        return sql

    tabla = match.group(1).lower()
    for columna in (c for t, c in _FKS_DIFERIDAS if t == tabla):
        sql = re.sub(
            rf"(\b{columna}\s+[A-Za-z0-9_ ]*?)"
            r"\s+REFERENCES\s+[A-Za-z_][A-Za-z0-9_]*\(id\)\s+ON\s+DELETE\s+SET\s+NULL",
            r"\1",
            sql,
            flags=re.IGNORECASE,
        )
    return sql


class Row:
    """A row addressable by both integer position and column name.

    Emula `sqlite3.Row`, y la parte que menos se ve es la que más se usa:
    `dict(fila)`. `dict()` acepta un objeto como mapping si tiene `keys()` y
    `__getitem__`; sin `keys()` cae al camino de iterable-de-pares, encuentra
    valores sueltos y muere con *"cannot convert dictionary update sequence
    element #0 to a sequence"*.

    No es un detalle: `return [dict(r) for r in rows]` es **el** patrón de
    retorno de esta capa —95 llamados en 20 módulos de `libracore/db/`—, así
    que sin este método casi toda lectura del motor falla contra PostgreSQL.
    Lo encontró la verificación del piloto LibraDesk el 2026-08-09, ejecutando
    las lecturas de `remitos_presupuestos` **con filas sembradas**: 5 de 7
    funciones fallaban. Con las tablas vacías pasaban todas, porque `dict()`
    nunca llegaba a ejecutarse.
    """

    def __init__(self, values: tuple, columns: tuple[str, ...]):
        self._values = values
        self._columns = columns

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._columns.index(key)]

    def keys(self) -> list[str]:
        """Los nombres de columna, como `sqlite3.Row.keys()`."""
        return list(self._columns)

    # Sin `__contains__` a propósito: `sqlite3.Row` tampoco lo define, así que
    # `x in fila` itera VALORES en los dos backends. Definirlo sobre los
    # nombres de columna haría que la misma expresión signifique cosas
    # distintas según el motor.

    def __iter__(self) -> Iterator:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


_TABLA_INSERT_RE = re.compile(
    r"^\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+[\"']?([\w.]+)[\"']?", re.IGNORECASE
)


def _tabla_del_insert(sql: str) -> str | None:
    m = _TABLA_INSERT_RE.match(sql)
    return m.group(1).split(".")[-1] if m else None


_PRAGMA_RE = re.compile(r"^\s*PRAGMA\s+(\w+)\s*(?:=\s*([^;]+))?\s*;?\s*$", re.IGNORECASE)

# Un PRAGMA es una directiva de SQLite: PostgreSQL ni siquiera lo parsea. El
# `executescript()` de este adaptador ya los saltea desde siempre; un
# `execute()` directo, en cambio, le llegaba crudo a psycopg y moria con
# *"syntax error at or near PRAGMA"*. Eso hacia que `init_schema()` de
# LibraCommerce no pudiera crear NI UNA tabla contra PostgreSQL: su primera
# linea es `PRAGMA foreign_keys = ON`.
#
# Se ignoran los que en PostgreSQL son un no-op **porque el motor ya hace eso**
# o porque no tienen equivalente y no cambian el resultado:
_PRAGMAS_IGNORABLES = {
    "foreign_keys",      # ver abajo: solo el ON
    "journal_mode",      # WAL es de SQLite
    "synchronous",
    "busy_timeout",      # el timeout de PostgreSQL se configura en la conexion
    "temp_store",
    "cache_size",
    "encoding",
    "optimize",
}


def _revisar_pragma(nombre: str, valor: str) -> None:
    """Decide si ese PRAGMA se puede ignorar o hay que frenar.

    🔴 `foreign_keys = OFF` **no es ignorable**. No es una preferencia: es la
    forma de decir "voy a hacer algo que viola la integridad referencial" —el
    rebuild de 12 pasos de SQLite, por ejemplo—. Tragarselo en PostgreSQL
    dejaria a ese codigo creyendo que las FK estan apagadas mientras el motor
    las sigue aplicando, y el error saldria mucho despues y en otro lado.
    Mejor fallar acá, con el motivo escrito.
    """
    if nombre == "foreign_keys" and valor in ("off", "0", "false"):
        raise NotImplementedError(
            "PRAGMA foreign_keys = OFF no tiene equivalente en PostgreSQL y no "
            "se puede ignorar: el codigo que lo pide cuenta con que las FK "
            "quedan apagadas. Reescribi esa operacion para que no las necesite "
            "apagadas, o hacela solo en el backend SQLite."
        )
    if nombre not in _PRAGMAS_IGNORABLES:
        raise NotImplementedError(
            f"PRAGMA {nombre} no esta contemplado en el backend PostgreSQL. Si "
            "es un no-op alla, agregalo a _PRAGMAS_IGNORABLES; si cambia el "
            "comportamiento, hay que traducirlo."
        )


# Los productos atrapan las excepciones de `sqlite3` -- 23 lugares entre los
# seis productos y los dos motores, 20 de ellos `IntegrityError`. Contra
# PostgreSQL psycopg tira SU jerarquia, asi que ninguno de esos `except`
# atrapaba nada: el error subia crudo hasta el usuario. Medido en VentaLibra el
# 2026-08-10: 12 de sus 16 rojos eran un `UniqueViolation` que el producto creia
# estar manejando.
#
# Las dos jerarquias son la misma de la DB-API (PEP 249) y comparten los
# nombres, asi que la traduccion es por nombre de clase y no una tabla a mano.
@contextmanager
def _errores_como_sqlite3():
    """Convierte los errores de psycopg en su equivalente de `sqlite3`.

    Se traduce en el adaptador y no en cada producto por el mismo motivo que el
    resto de esta capa: es una sola diferencia de dialecto y arreglarla aca la
    cierra para los seis consumidores a la vez.

    ⚠️ **Lo que esto NO arregla**: en PostgreSQL un error **aborta la
    transaccion**, y en SQLite no. Un `except IntegrityError` que despues sigue
    usando la misma conexion funciona en SQLite y contra PostgreSQL se encuentra
    con *"current transaction is aborted"*. Eso es una diferencia de
    comportamiento, no de nombres, y hay que mirarla caso por caso.
    """
    import psycopg

    try:
        yield
    except psycopg.Error as e:
        raise _equivalente_sqlite3(type(e))(str(e)) from e


def _equivalente_sqlite3(clase: type) -> type:
    """La clase de `sqlite3` que le corresponde a una excepcion de psycopg.

    Se sube por la jerarquia hasta encontrar un nombre que las dos compartan.
    Hace falta porque psycopg nombra sus clases concretas por SQLSTATE
    --`UniqueViolation`, `NotNullViolation`, `ForeignKeyViolation`--, y esos
    nombres no existen en `sqlite3`; el que comparten es el de la DB-API que
    tienen de padre (`IntegrityError`). Mirar solo el nombre de la clase
    concreta hacia caer todo a `DatabaseError`, que es justo lo que ningun
    producto atrapa.
    """
    for ancestro in clase.__mro__:
        equivalente = getattr(sqlite3, ancestro.__name__, None)
        if isinstance(equivalente, type) and issubclass(equivalente, Exception):
            return equivalente
    return sqlite3.DatabaseError


class Cursor:
    def __init__(self, connection: "ConnectionWrapper"):
        self._connection = connection
        from psycopg.rows import tuple_row

        self._cursor = connection._connection.cursor(row_factory=tuple_row)
        self._lastrowid = None
        self._pragma_ignorada = False

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        """Cuantas filas toco el ultimo UPDATE/DELETE.

        Es DB-API estandar y `sqlite3` lo tiene, asi que los productos lo usan
        para distinguir "no habia nada que borrar" de "se borro". Sin esto,
        contra PostgreSQL el request moria con *'Cursor' object has no attribute
        'rowcount'*. Lo encontro la suite de Contalibra el 2026-08-10.

        Con un PRAGMA ignorado no hubo consulta: se devuelve -1, que es lo que
        la DB-API define para "no aplica".
        """
        if self._pragma_ignorada:
            return -1
        return self._cursor.rowcount

    def execute(self, sql: str, params: Sequence | None = None):
        self._pragma_ignorada = False
        pragma = _PRAGMA_RE.match(sql)
        if pragma and not _TABLE_INFO_RE.match(sql):
            _revisar_pragma(pragma.group(1).lower(), (pragma.group(2) or "").strip().lower())
            self._pragma_ignorada = True
            return self

        # ⚠️ Esta rama arma su SQL y lo ejecuta DIRECTO, sin pasar por
        # `_paramstyle`: por eso su marcador es un `%s` de verdad y no el
        # centinela. La rama de `sqlite_master`, mas abajo, si pasa por la
        # traduccion y por eso usa `_MARCA`. La asimetria es real y molesta:
        # si algun dia las dos pasan por el mismo camino, unificar.
        table_info = _TABLE_INFO_RE.match(sql)
        if table_info:
            table = table_info.group(1)
            sql = (
                "SELECT ordinal_position - 1 AS cid, column_name AS name, "
                "data_type AS type, CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull, "
                "column_default AS dflt_value, CASE WHEN column_name = 'id' THEN 1 ELSE 0 END AS pk "
                "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s "
                "ORDER BY ordinal_position"
            )
            params = (table,)
        else:
            sqlite_master = _SQLITE_MASTER_RE.match(sql)
            if sqlite_master:
                sql = (
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = " + _MARCA
                )
                params = (sqlite_master.group(2),)
            ignore_insert = bool(_INSERT_IGNORE_RE.match(sql))
            if ignore_insert:
                sql = _INSERT_IGNORE_RE.sub("INSERT INTO ", sql, count=1)
            sql = _paramstyle(sql)
            if ignore_insert:
                sql = f"{sql.rstrip().rstrip(';')} ON CONFLICT DO NOTHING"
        if (
            _INSERT_RE.match(sql)
            and " returning " not in sql.lower()
            and self._connection._tiene_id(_tabla_del_insert(sql))
        ):
            # `RETURNING id` es como se emula `lastrowid`, pero **no todas las
            # tablas tienen `id`**: `modulos` tiene `modulo` como clave. Antes
            # se agregaba a ciegas y el INSERT moria con *"column id does not
            # exist"* — o sea que aplicar un plan de modulos era imposible
            # contra PostgreSQL. Lo encontro la suite de LibraDesk el
            # 2026-08-09.
            sql = f"{sql.rstrip().rstrip(';')} RETURNING id"
            with _errores_como_sqlite3():
                self._cursor.execute(sql, params or ())
                row = self._cursor.fetchone()
            self._lastrowid = row[0] if row else None
        else:
            with _errores_como_sqlite3():
                self._cursor.execute(sql, params or ())
        return self

    def executemany(self, sql: str, params):
        with _errores_como_sqlite3():
            self._cursor.executemany(_paramstyle(sql), params)
        return self

    def fetchone(self):
        if self._pragma_ignorada:
            return None
        row = self._cursor.fetchone()
        return self._row(row)

    def fetchall(self):
        if self._pragma_ignorada:
            return []
        return [self._row(row) for row in self._cursor.fetchall()]

    def fetchmany(self, size=None):
        if self._pragma_ignorada:
            return []
        filas = self._cursor.fetchmany(size) if size else self._cursor.fetchmany()
        return [self._row(row) for row in filas]

    def __iter__(self):
        """Un cursor de `sqlite3` se puede recorrer directo, y el codigo de la
        familia lo hace: `for fila in conn.execute(...)`.

        Sin esto, contra PostgreSQL eso muere con *'Cursor' object is not
        iterable* -- lejos del `execute`, en el `for`. Lo encontro la suite de
        Contalibra el 2026-08-10, en `libracore.db.clients`.
        """
        if self._pragma_ignorada:
            return iter(())
        return (self._row(row) for row in self._cursor)

    def close(self):
        self._cursor.close()

    def _row(self, row):
        if row is None:
            return None
        columns = tuple(column.name for column in self._cursor.description)
        return Row(tuple(_como_en_sqlite(v) for v in row), columns)


def _como_en_sqlite(valor):
    """Los `NUMERIC` de PostgreSQL vuelven como `float`, igual que en SQLite.

    🔴 **Por que, y por que es una decision y no un detalle.** LibraCommerce
    declara 19 columnas de dinero y cantidades como `NUMERIC`
    (`sale_items.unit_price`, `catalog_items.default_cost`, …). SQLite las
    devuelve como `float` porque no tiene decimal nativo; psycopg las devuelve
    como `decimal.Decimal`. Y todo el codigo de la familia hace aritmetica con
    `float`, asi que cualquier multiplicacion mixta muere con *unsupported
    operand type(s) for *: 'float' and 'decimal.Decimal'* -- lejos de la
    consulta, en el calculo. Lo encontro la suite de Restolibra el 2026-08-10,
    en el costo de una receta.

    Se elige `float` y no `Decimal` porque la premisa de esta capa es que **los
    dos motores se comporten igual**, y hoy el comportamiento real de toda la
    familia --en las 12 instancias vivas-- es `float`. Devolver `Decimal` solo
    contra PostgreSQL haria que el mismo calculo diera distinto segun el motor,
    que es peor que la imprecision.

    > Si algun dia la familia quiere aritmetica decimal de verdad para dinero,
    > es una migracion deliberada de los DOS motores, no un efecto lateral de
    > cambiar de base.
    """
    if isinstance(valor, Decimal):
        return float(valor)
    return valor


class ConnectionWrapper:
    def __init__(self, connection: Any):
        self._connection = connection
        self._con_id: dict[str | None, bool] = {}

    def _tiene_id(self, tabla: str | None) -> bool:
        """Si esa tabla tiene una columna `id`, para decidir si vale el
        `RETURNING id` que emula `lastrowid`.

        Se consulta al catalogo una vez por tabla y por conexion, y se
        cachea: un `apply_plan_modules` hace un INSERT por modulo y no tiene
        sentido preguntar lo mismo diez veces. La cache vive en la conexion,
        asi que un cambio de schema entre conexiones se ve igual.
        """
        if tabla is None:
            return False
        if tabla not in self._con_id:
            with self._connection.cursor() as cur:
                # `to_regclass` + `pg_attribute` y no `information_schema`:
                # aquel filtra por `table_schema` y una TEMP TABLE vive en
                # `pg_temp_N`, asi que quedaba fuera y el `lastrowid` volvia
                # None. `to_regclass` resuelve por `search_path`, que incluye
                # el schema temporal. Lo agarro el test de compatibilidad que
                # ya existia, que usa justamente una temporal.
                cur.execute(
                    "SELECT 1 FROM pg_attribute "
                    "WHERE attrelid = to_regclass(%s) AND attname = 'id' "
                    "AND attnum > 0 AND NOT attisdropped",
                    (tabla,),
                )
                self._con_id[tabla] = cur.fetchone() is not None
        return self._con_id[tabla]

    def execute(self, sql: str, params: Sequence | None = None):
        return self.cursor().execute(sql, params)

    def executemany(self, sql: str, params):
        return self.cursor().executemany(sql, params)

    def executescript(self, script: str):
        for statement in script.split(";"):
            statement = statement.strip()
            if statement and not statement.upper().startswith("PRAGMA "):
                self.execute(statement)
        return self

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()
