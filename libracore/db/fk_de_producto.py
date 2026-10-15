"""Soltar una FK que la baseline del motor crea y que un producto declara sin ella.

## El problema que esto resuelve (ADR-056)

`init_core_schema()` --la baseline de la cadena `libracore-migrar`-- crea seis tablas
comerciales con FK hacia `clients` y `proveedores` (`cc_pagos`, `cc_debitos`, `recibos`,
`remitos`, `presupuestos`, `egresos`). Los demás productos (Contalibra, Restolibra,
VentaLibra, GestioLibra, MedLibra, LibraClub, LibraCargo) no declaran esas tablas por su
cuenta: nacieron con esas FK y las conservan, no hay nada que alinear.

**LibraDesk es la excepción.** Declara esas tablas con su propio DDL, sin las FK, porque
`clients` y `proveedores` las versiona su cadena de Alembic y una FK cruzada deja el
`downgrade` de esa cadena imposible de correr (PostgreSQL rechaza el `DROP TABLE`). Mientras
la app era la primera en crear las tablas, su `CREATE TABLE IF NOT EXISTS` mandaba. Desde que
`libracore-migrar` corre antes del arranque (ADR-012 de LibraDesk, 2026-10-07), en una base
**nueva** (la demo cada noche, un alta, un restore) la baseline llega primero y deja las FK:
la base nueva queda distinta de las migradas, y dos de esas FK son `ON DELETE CASCADE`.

## Por qué no se cambia la baseline

Está congelada (`tests/db/test_schema_congelado.py`): es la `0001` de Alembic, y las bases de
los otros productos ya la corrieron. Quitar las FK de ahí no cambiaría las bases viejas y
haría que una base nueva de GestioLibra dejara de parecerse a una migrada, que es el defecto
que se está arreglando para LibraDesk. Una revisión que las suelte para todos le sacaría a
los otros siete productos la integridad que hoy tienen. La salida es la que ya existe para
`ventas_pagos` (`repuntar_fk_ventas_pagos` de LibraCommerce): **una costura idempotente que
el producto llama en su arranque**, justo después de su propio DDL.

## Cómo se usa

    from libracore.db.fk_de_producto import soltar_fk

    with get_connection() as conn:
        conn.executescript(DDL_DEL_PRODUCTO)
        soltar_fk(conn, "cc_pagos", "cliente_id", hacia="clients")

Es barata (una consulta al catálogo cuando no hay nada que soltar), no toca filas y se puede
llamar en cada arranque. Si el producto dejó de declarar la tabla sin la FK, la baseline
vuelve a crearla y este llamado la suelta de nuevo: la decisión es del producto, que la
escribe junto a su DDL.
"""
from __future__ import annotations

import re
import sqlite3

#: Los nombres entran al `ALTER TABLE` por interpolación (un identificador no se puede
#: parametrizar), así que se aceptan sólo identificadores simples.
_IDENTIFICADOR = re.compile(r"^[a-z_][a-z0-9_]*$")


def _identificador(valor: str, que: str) -> str:
    if not _IDENTIFICADOR.match(valor):
        raise ValueError(f"{que} no es un identificador simple: {valor!r}")
    return valor


def soltar_fk(conn, tabla: str, columna: str, *, hacia: str) -> list[str]:
    """Quita la FK de una sola columna `tabla.columna` hacia la tabla `hacia`.

    Devuelve los nombres de las restricciones que soltó (`[]` si no había ninguna: la
    segunda llamada, o una base donde el producto ya creó la tabla sin la FK).

    - **Sólo la FK de esa columna hacia esa tabla**: una FK de la misma columna hacia otra
      tabla, o una FK compuesta, no se tocan. `hacia` es obligatorio a propósito, para que
      un llamado no pueda llevarse una FK que el producto sí quiere.
    - **No toca datos**: soltar una FK no puede fallar por filas, ni las cambia. El `ON DELETE`
      que tenía deja de actuar desde ese momento (con `CASCADE`, borrar el padre ya no borra
      los hijos; con `SET NULL`, los deja apuntando a un id que no existe).
    - **PostgreSQL**: `DROP CONSTRAINT IF EXISTS`, así que dos procesos que arrancan juntos no
      se pisan. Hace `commit` si soltó algo, igual que `repuntar_fk_ventas_pagos`.
    - 🔴 **SQLite: no hace nada y devuelve `[]`.** SQLite no sabe quitar una restricción sin
      reconstruir la tabla, y el único producto que necesita esto (LibraDesk) corre sólo sobre
      PostgreSQL. Un producto con SQLite que lo necesite tiene que escribir el rebuild, no
      confiar en que este llamado haya hecho algo.
    """
    _identificador(tabla, "tabla")
    _identificador(columna, "columna")
    _identificador(hacia, "hacia")
    if isinstance(conn, sqlite3.Connection):
        return []

    nombres = [
        fila[0]
        for fila in conn.execute(
            """
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
            WHERE con.contype = 'f'
              AND con.conrelid = to_regclass(?)
              AND con.confrelid = to_regclass(?)
              AND cardinality(con.conkey) = 1
              AND att.attname = ?
            ORDER BY con.conname
            """,
            (tabla, hacia, columna),
        ).fetchall()
    ]
    for nombre in nombres:
        conn.execute(f'ALTER TABLE {tabla} DROP CONSTRAINT IF EXISTS "{nombre}"')
    if nombres:
        conn.commit()
    return nombres
