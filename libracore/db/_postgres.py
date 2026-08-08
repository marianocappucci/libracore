"""Small DB-API compatibility layer used while LibraCore becomes dual-backend."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import Connection


_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+", re.IGNORECASE)
_INSERT_IGNORE_RE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", re.IGNORECASE)
_TABLE_INFO_RE = re.compile(r"^\s*PRAGMA\s+table_info\s*\(\s*([\w]+)\s*\)\s*;?\s*$", re.IGNORECASE)
_SQLITE_MASTER_RE = re.compile(
    r"^\s*SELECT\s+1\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*(['\"]?)([\w]+)\1\s*;?\s*$",
    re.IGNORECASE,
)


def _paramstyle(sql: str) -> str:
    """Translate the qmark SQL used by LibraCore to psycopg's format style."""
    sql = sql.replace("?", "%s")
    sql = re.sub(r"\bdatetime\('now'(?:\s*,\s*'localtime')?\)", "CURRENT_TIMESTAMP", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bdatetime\('now'\s*,\s*'localtime'\s*,\s*%s\)", "CURRENT_TIMESTAMP + %s::interval", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bdate\('now'\)", "CURRENT_DATE", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bdate\(([^()]+)\)", r"CAST(\1 AS DATE)", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bgroup_concat\(([^,()]+),\s*(['\"][^'\"]*['\"])\)", r"string_agg(\1, \2)", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"\bprintf\('%0([0-9]+)d',\s*([^()]+)\)",
        r"lpad(cast(\2 AS text), \1, '0')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bstrftime\('%Y-%m',\s*([^()]+)\)",
        r"to_char(cast(\1 AS date), 'YYYY-MM')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(r"->>\s*'\$\.([A-Za-z_][A-Za-z0-9_]*)'", r"->> '\1'", sql)
    sql = re.sub(
        r"\bROUND\(SUM\((.*?)\),\s*([0-9]+)\)",
        r"ROUND(CAST(SUM(\1) AS NUMERIC), \2)",
        sql,
        flags=re.IGNORECASE,
    )
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
    # SQLite acepta declarar una FK hacia una tabla que todavía no fue creada;
    # PostgreSQL no. `schema.py` agrega esta constraint después de crear todas
    # las tablas, sólo en el backend PostgreSQL.
    sql = re.sub(
        r"\s+REFERENCES\s+(?:turnos_caja|ventas)\(id\)\s+ON\s+DELETE\s+SET\s+NULL",
        "",
        sql,
        flags=re.IGNORECASE,
    )
    return sql


class Row:
    """A row addressable by both integer position and column name."""

    def __init__(self, values: tuple, columns: tuple[str, ...]):
        self._values = values
        self._columns = columns

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._columns.index(key)]

    def __iter__(self) -> Iterator:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class Cursor:
    def __init__(self, connection: "ConnectionWrapper"):
        self._connection = connection
        from psycopg.rows import tuple_row

        self._cursor = connection._connection.cursor(row_factory=tuple_row)
        self._lastrowid = None

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def description(self):
        return self._cursor.description

    def execute(self, sql: str, params: Sequence | None = None):
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
                    "WHERE table_schema = 'public' AND table_name = %s"
                )
                params = (sqlite_master.group(2),)
            ignore_insert = bool(_INSERT_IGNORE_RE.match(sql))
            if ignore_insert:
                sql = _INSERT_IGNORE_RE.sub("INSERT INTO ", sql, count=1)
            sql = _paramstyle(sql)
            if ignore_insert:
                sql = f"{sql.rstrip().rstrip(';')} ON CONFLICT DO NOTHING"
        if _INSERT_RE.match(sql) and " returning " not in sql.lower():
            sql = f"{sql.rstrip().rstrip(';')} RETURNING id"
            self._cursor.execute(sql, params or ())
            row = self._cursor.fetchone()
            self._lastrowid = row[0] if row else None
        else:
            self._cursor.execute(sql, params or ())
        return self

    def executemany(self, sql: str, params):
        self._cursor.executemany(_paramstyle(sql), params)
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        return self._row(row)

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]

    def close(self):
        self._cursor.close()

    def _row(self, row):
        if row is None:
            return None
        columns = tuple(column.name for column in self._cursor.description)
        return Row(row, columns)


class ConnectionWrapper:
    def __init__(self, connection: Any):
        self._connection = connection

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
