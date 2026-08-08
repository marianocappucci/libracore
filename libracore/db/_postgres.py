"""Small DB-API compatibility layer used while LibraCore becomes dual-backend."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from psycopg import Connection
from psycopg.rows import tuple_row


_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+", re.IGNORECASE)


def _paramstyle(sql: str) -> str:
    """Translate the qmark SQL used by LibraCore to psycopg's format style."""
    return sql.replace("?", "%s")


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
        self._cursor = connection._connection.cursor(row_factory=tuple_row)
        self._lastrowid = None

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def description(self):
        return self._cursor.description

    def execute(self, sql: str, params: Sequence | None = None):
        sql = _paramstyle(sql)
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
    def __init__(self, connection: Connection):
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
