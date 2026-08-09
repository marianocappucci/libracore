"""Volcado textual del schema de una base, para congelarlo y para compararlo
entre instancias.

Existe por dos usos que resultaron ser el mismo:

1. **El gate del schema congelado.** `init_core_schema()` ya es un mecanismo de
   migraciones hecho a mano (34 `ALTER ... ADD COLUMN` idempotentes), y la
   decisión fue congelarlo: la revisión `0001` de Alembic va a llamarlo entero y
   desde ahí la función es de sólo lectura. Congelar el **texto** por hash no
   sirve —se pone rojo por un comentario y verde por un `DEFAULT` cambiado—, así
   que lo que se congela es el **resultado**: este volcado contra una base
   vacía, con una fixture por motor (`tests/db/fixtures/`).
2. **Comparar instancias vivas.** Es el método con el que se midieron las diez
   instancias el 2026-08-09 para contestar qué es "el schema de LibraCore".
   Estaba en scripts sueltos; acá queda uno solo, y así la fixture y la medición
   de una instancia se comparan entre sí.

**El volcado es insensible al orden y al formato**: todo sale ordenado
alfabéticamente y las columnas no dependen de si nacieron en el `CREATE TABLE` o
llegaron por un `ALTER` posterior (que es justo la diferencia entre una base
nueva y una vieja puesta al día).

**Es sensible al motor**, y a propósito: `INTEGER` en SQLite es `bigint` en
PostgreSQL, y `''` es `''::text`. Por eso hay una fixture por motor y no una
comparación cruzada; que las dos digan lo mismo es otro problema, más difícil,
que este volcado no resuelve.

> ⚠️ **Límite conocido: los `CHECK` sólo se ven en PostgreSQL.** SQLite no los
> expone por introspección (viven dentro del texto de `sqlite_master.sql`) y
> parsearlos con una regex sería peor que no tenerlos. PostgreSQL sí los
> devuelve normalizados por el motor (`pg_get_constraintdef`), así que un
> `CHECK` que cambie o desaparezca pone roja la fixture de PostgreSQL. Las dos
> fixtures se regeneran juntas: la cobertura es del par, no de cada una.

Uso por línea de comandos:

    python -m libracore.db.schema_dump <destino> [--init] [--solo-lectura]

`<destino>` es una ruta SQLite o una URL PostgreSQL. `--init` corre
`init_core_schema()` antes de volcar (es como se genera una fixture, contra una
base vacía). `--solo-lectura` abre el archivo SQLite sin escribirle, que es lo
que corresponde al mirar la base de una instancia viva.
"""
from __future__ import annotations

import sqlite3

from ._postgres import ConnectionWrapper

_SECCIONES = ("tablas", "claves primarias", "claves foráneas", "índices", "checks")


def volcar_schema(conn) -> str:
    """El schema de `conn` como texto ordenado y diffeable.

    `conn` es cualquier conexión de `libracore.db.core` — SQLite o el adaptador
    PostgreSQL. El motor se detecta del objeto y **no** de `core.is_postgres()`,
    que responde por la configuración global del proceso y no por esta conexión
    (`core.conectar()` no toca esa configuración).
    """
    if isinstance(conn, ConnectionWrapper):
        motor, datos = "postgresql", _volcar_postgres(conn)
    elif isinstance(conn, sqlite3.Connection):
        motor, datos = "sqlite", _volcar_sqlite(conn)
    else:
        raise TypeError(f"No sé volcar el schema de una conexión {type(conn).__name__}")

    lineas = [f"# motor: {motor}"]
    for seccion in _SECCIONES:
        filas = sorted(datos.get(seccion, []))
        lineas.append(f"\n## {seccion} ({len(filas)})")
        lineas.extend(filas)
    return "\n".join(lineas) + "\n"


def _normalizar(sql: str | None) -> str:
    """Colapsa los espacios de una definición para que un reformateo no cuente
    como cambio de schema."""
    return " ".join((sql or "").split())


# --------------------------------------------------------------------- SQLite


def _volcar_sqlite(conn) -> dict[str, list[str]]:
    tablas = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]

    columnas, pks, fks = [], [], []
    for tabla in tablas:
        info = conn.execute(f"PRAGMA table_info({tabla})").fetchall()
        for fila in info:
            _cid, nombre, tipo, notnull, dflt, _pk = tuple(fila)
            columnas.append(f"{tabla}|{nombre}|{tipo}|{'NN' if notnull else ''}|{_normalizar(dflt)}")

        clave = [f[1] for f in sorted((f for f in info if f[5]), key=lambda f: f[5])]
        if clave:
            pks.append(f"{tabla}|{','.join(clave)}")

        for fila in conn.execute(f"PRAGMA foreign_key_list({tabla})").fetchall():
            _id, _seq, destino, desde, hacia, on_update, on_delete, _match = tuple(fila)
            fks.append(
                f"{tabla}|{desde}|{destino}|{hacia or ''}|{on_delete}|{on_update}"
            )

    indices = []
    for nombre, tabla, sql in conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index'"
    ).fetchall():
        if sql:
            # El texto normalizado del `CREATE INDEX`: es lo único que trae el
            # `WHERE` de un índice parcial y la expresión de uno funcional
            # (`REPLACE(cuit_dni, '-', '')`), que `PRAGMA index_info` no
            # devuelve.
            indices.append(f"{tabla}|{nombre}|{_normalizar(sql)}")
        else:
            # Autoíndice de un UNIQUE declarado inline: no tiene texto propio.
            cols = ",".join(
                r[2] for r in conn.execute(f"PRAGMA index_info({nombre})").fetchall()
            )
            indices.append(f"{tabla}|{nombre}|UNIQUE implícito ({cols})")

    return {
        "tablas": columnas,
        "claves primarias": pks,
        "claves foráneas": fks,
        "índices": indices,
        # Ver el aviso del docstring: SQLite no expone los CHECK.
        "checks": [],
    }


# ----------------------------------------------------------------- PostgreSQL


def _volcar_postgres(conn) -> dict[str, list[str]]:
    columnas = [
        f"{r[0]}|{r[1]}|{r[2]}|{'NN' if r[3] else ''}|{_normalizar(r[4])}"
        for r in conn.execute(
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), "
            "       a.attnotnull, pg_get_expr(d.adbin, d.adrelid) "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public' "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
            "     AND NOT a.attisdropped "
            "LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum "
            "WHERE c.relkind = 'r'"
        ).fetchall()
    ]

    # `pg_get_constraintdef` devuelve la definición **renderizada por el motor**,
    # no el texto que se escribió: un reformateo del DDL no la mueve, y un
    # cambio real sí.
    restricciones = conn.execute(
        "SELECT c.relname, con.conname, con.contype, pg_get_constraintdef(con.oid) "
        "FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'"
    ).fetchall()

    pks, fks, checks = [], [], []
    for tabla, nombre, tipo, definicion in restricciones:
        if tipo == "p":
            pks.append(f"{tabla}|{_normalizar(definicion)}")
        elif tipo == "f":
            fks.append(f"{tabla}|{nombre}|{_normalizar(definicion)}")
        elif tipo == "c":
            checks.append(f"{tabla}|{nombre}|{_normalizar(definicion)}")

    indices = [
        f"{r[0]}|{r[1]}|{_normalizar(r[2])}"
        for r in conn.execute(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public'"
        ).fetchall()
    ]

    return {
        "tablas": columnas,
        "claves primarias": pks,
        "claves foráneas": fks,
        "índices": indices,
        "checks": checks,
    }


# ------------------------------------------------------------------------ CLI


def _abrir(destino: str, *, solo_lectura: bool):
    from . import core

    if solo_lectura:
        if core.es_url_postgres(destino):
            raise SystemExit(
                "--solo-lectura es para archivos SQLite; contra PostgreSQL usá "
                "un usuario con permiso de sólo lectura."
            )
        # Una instancia viva está en WAL: `mode=ro` la lee sin escribirle, que
        # es la forma correcta de mirarla. Copiar el archivo NO lo es — trae
        # sólo lo checkpointeado, y esta familia ya perdió una tabla así.
        conn = sqlite3.connect(f"file:{destino}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    core.configure(destino)
    return core.get_connection()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m libracore.db.schema_dump",
        description="Vuelca el schema de una base SQLite o PostgreSQL.",
    )
    parser.add_argument("destino", help="ruta del archivo SQLite o URL PostgreSQL")
    parser.add_argument(
        "--init",
        action="store_true",
        help="correr init_core_schema() antes de volcar (para generar una fixture)",
    )
    parser.add_argument(
        "--solo-lectura",
        action="store_true",
        help="abrir el archivo SQLite sin escribirle (instancia viva)",
    )
    args = parser.parse_args(argv)

    conn = _abrir(args.destino, solo_lectura=args.solo_lectura)
    try:
        if args.init:
            from .schema import init_core_schema

            init_core_schema(conn)
            conn.commit()
        print(volcar_schema(conn), end="")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
