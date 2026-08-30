"""Lo que un cursor de `sqlite3` sabe hacer y el del adaptador no sabía.

Los productos no hablan con psycopg: hablan con la forma de `sqlite3`, porque
así fue escrita toda la capa cruda. Cada hueco de esa forma aparece **lejos del
`execute`** —en el `for`, en el `if cur.rowcount`— y por eso cuesta reconocerlo.

Los tres de acá los encontró la suite de [[contalibra]] el 2026-08-10, uno por
corrida: `rowcount`, iterar el cursor, y el corte de la ventana del rate limit.
"""
import os
import sqlite3

import pytest

from libracore.db import core


def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("LIBRACORE_POSTGRES_URL no está definida en CI")
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


@pytest.fixture
def conn():
    core.configure(_url())
    c = core.get_connection()
    c.execute("DROP SCHEMA IF EXISTS public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, k TEXT)")
    c.execute("INSERT INTO t (k) VALUES ('a')")
    c.execute("INSERT INTO t (k) VALUES ('b')")
    c.commit()
    yield c
    c.close()
    core._db_path = None
    core._database_url = None


def test_el_cursor_se_puede_recorrer_directo(conn):
    """`for fila in conn.execute(...)` es como lee media capa cruda."""
    filas = [fila["k"] for fila in conn.execute("SELECT k FROM t ORDER BY k")]
    assert filas == ["a", "b"]


def test_rowcount_dice_cuantas_filas_toco(conn):
    """Es como el codigo distingue "no habia nada que borrar" de "se borro"."""
    assert conn.execute("UPDATE t SET k='z' WHERE k='a'").rowcount == 1
    assert conn.execute("DELETE FROM t WHERE k='no-existe'").rowcount == 0


def test_fetchmany_trae_de_a_tandas(conn):
    cur = conn.execute("SELECT k FROM t ORDER BY k")
    assert [f["k"] for f in cur.fetchmany(1)] == ["a"]


def test_las_filas_siguen_siendo_direccionables_por_nombre(conn):
    """Iterar no puede devolver tuplas peladas: el resto de la capa hace
    `fila["columna"]` y `dict(fila)`."""
    fila = next(iter(conn.execute("SELECT id, k FROM t ORDER BY k")))
    assert fila["k"] == "a"
    assert dict(fila)["k"] == "a"


def test_el_rate_limit_cuenta_contra_una_columna_timestamp(conn):
    """🔴 El caso que trababa a Contalibra y Restolibra.

    `auth_log.ts` es `timestamp` cuando la tabla la crea el modelo de libraauth,
    y comparar `timestamp >= text` no existe en PostgreSQL. Se arma la tabla con
    ESE tipo a propósito —el que tienen esos productos— y se verifica que la
    ventana deslizante cuente bien.
    """
    from libracore.db.logs import contar_login_fallidos_recientes

    conn.execute("""
        CREATE TABLE auth_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TIMESTAMP NOT NULL DEFAULT LOCALTIMESTAMP,
            evento   TEXT NOT NULL,
            username TEXT NOT NULL,
            ip       TEXT,
            detalle  TEXT
        )
    """)
    conn.execute(
        "INSERT INTO auth_log (evento, username, ip) VALUES ('login_fallido','x','1.2.3.4')"
    )
    # Uno viejo, fuera de la ventana: no tiene que contarse.
    conn.execute(
        "INSERT INTO auth_log (ts, evento, username, ip) "
        "VALUES (LOCALTIMESTAMP - interval '2 hours', 'login_fallido', 'x', '1.2.3.4')"
    )
    conn.commit()

    assert contar_login_fallidos_recientes("1.2.3.4", minutos=15) == 1
    assert contar_login_fallidos_recientes("9.9.9.9", minutos=15) == 0


def test_el_rate_limit_no_depende_del_reloj_del_proceso(conn, monkeypatch):
    """🔴 Si el proceso y la base no coinciden de zona, contaba CERO.

    `auth_log.ts` lo escribe el DEFAULT de la tabla, con el reloj de la **base**.
    Hasta el 2026-08-30 la ventana se calculaba con `datetime.now()`, el reloj
    del **proceso**. Con las dos zonas desalineadas los intentos recientes
    parecían viejos y la función devolvía 0: **el rate limiting de `/login` se
    apagaba sin que nada avisara.**

    Se descubrió corriendo la suite contra un PostgreSQL con
    `America/Argentina/Buenos_Aires` —la zona que el estándar de la familia manda
    para producción— con el proceso en UTC. Contra una base en UTC pasaba, que es
    por lo que el CI no lo veía.

    Este test lo fija **sin depender de cómo esté configurada la base que lo
    corra**: mueve el reloj del proceso tres horas y verifica que la cuenta no
    cambie. Antes del arreglo, sólo eso bastaba para romperlo.
    """
    import libracore.db.logs as _logs

    conn.execute("""
        CREATE TABLE auth_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TIMESTAMP NOT NULL DEFAULT LOCALTIMESTAMP,
            evento   TEXT NOT NULL,
            username TEXT NOT NULL,
            ip       TEXT,
            detalle  TEXT
        )
    """)
    conn.execute(
        "INSERT INTO auth_log (evento, username, ip)"
        " VALUES ('login_fallido','x','1.2.3.4')"
    )
    conn.commit()

    assert _logs.contar_login_fallidos_recientes("1.2.3.4", minutos=15) == 1, (
        "el control: con los relojes como están, cuenta bien"
    )

    import datetime as _dt_mod

    class RelojCorrido(_dt_mod.datetime):
        """El proceso cree que son tres horas más tarde que la base."""

        @classmethod
        def now(cls, tz=None):
            return _dt_mod.datetime.now(tz) + _dt_mod.timedelta(hours=3)

    monkeypatch.setattr(_dt_mod, "datetime", RelojCorrido)

    assert _logs.contar_login_fallidos_recientes("1.2.3.4", minutos=15) == 1, (
        "la ventana se calculó con el reloj del proceso: un desfasaje de zona "
        "apaga el rate limiting en silencio"
    )


def test_en_sqlite_el_rate_limit_sigue_igual(tmp_path):
    """La columna ahí es TEXT y la comparación lexicográfica no se toca."""
    from libracore.db.logs import contar_login_fallidos_recientes
    from libracore.db.schema import init_core_schema

    core.configure(str(tmp_path / "t.db"))
    c = core.get_connection()
    try:
        init_core_schema(c)
        c.execute(
            "INSERT INTO auth_log (evento, username, ip) "
            "VALUES ('login_fallido','x','1.2.3.4')"
        )
        c.commit()
        assert contar_login_fallidos_recientes("1.2.3.4", minutos=15) == 1
        assert contar_login_fallidos_recientes("9.9.9.9", minutos=15) == 0
    finally:
        c.close()
        core._db_path = None
        core._database_url = None


def test_iterar_un_pragma_ignorado_no_rompe(conn):
    """Un PRAGMA no produjo consulta: recorrerlo tiene que dar vacío, no
    reventar en el `for`."""
    assert list(conn.execute("PRAGMA foreign_keys = ON")) == []
    assert conn.execute("PRAGMA foreign_keys = ON").rowcount == -1


def test_no_se_rompio_sqlite(tmp_path):
    core.configure(str(tmp_path / "s.db"))
    c = core.get_connection()
    try:
        c.execute("CREATE TABLE t (k TEXT)")
        c.execute("INSERT INTO t (k) VALUES ('a')")
        c.commit()
        assert [f["k"] for f in c.execute("SELECT k FROM t")] == ["a"]
        assert isinstance(c.execute("DELETE FROM t").rowcount, int)
    finally:
        c.close()
        core._db_path = None
        core._database_url = None
