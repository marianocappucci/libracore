"""Se retiran los restos del resumen de cuenta corriente por mail (ADR-042): la migración `0024`.

`init_core_schema()` ya no crea `cc_resumenes_enviados` ni las tres columnas `clients.cc_resumen_*`;
la `0024` las saca de las bases que las traen. Como la versión viva de la función ya no las pone,
cada test reconstruye lo que había antes con el DDL de entonces. Se mide en SQLite y en PostgreSQL
(el `DROP COLUMN` y el CHECK se resuelven distinto en cada uno), y que una base nueva —que nunca las
tuvo— atraviese la cadena entera sin que la `0003` ni la `0018` se queden esperando la tabla.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
REVISION = "0024_retirar_cc_resumen"
ANTERIOR = "0023_fce_varios_cbu"
COLUMNAS = ("cc_resumen_auto", "cc_resumen_frecuencia", "cc_resumen_ultimo_envio")


def _alembic(destino, *args):
    env = {**os.environ, "DATABASE_URL": destino}
    return subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic.ini", *args], cwd=RAIZ, env=env,
                          capture_output=True, text=True)


# El DDL de antes, con las dos formas del motor.
_SQLITE_VIEJO = """
    CREATE TABLE cc_resumenes_enviados (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id    INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
        fecha         TEXT NOT NULL,
        periodo_desde TEXT NOT NULL,
        periodo_hasta TEXT NOT NULL,
        saldo         REAL NOT NULL DEFAULT 0,
        email         TEXT DEFAULT '',
        estado        TEXT NOT NULL DEFAULT 'ok',
        detalle       TEXT DEFAULT '',
        automatico    INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT DEFAULT (datetime('now','-3 hours'))
    );
    CREATE INDEX idx_cc_resumenes_cliente ON cc_resumenes_enviados(cliente_id, fecha);
    ALTER TABLE clients ADD COLUMN cc_resumen_auto INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE clients ADD COLUMN cc_resumen_frecuencia TEXT NOT NULL DEFAULT 'mensual'
        CHECK (cc_resumen_frecuencia IN ('semanal', 'quincenal', 'mensual'));
    ALTER TABLE clients ADD COLUMN cc_resumen_ultimo_envio TEXT DEFAULT '';
"""

_POSTGRES_VIEJO = """
    CREATE TABLE cc_resumenes_enviados (
        id            BIGSERIAL PRIMARY KEY,
        cliente_id    INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
        fecha         TEXT NOT NULL,
        periodo_desde TEXT NOT NULL,
        periodo_hasta TEXT NOT NULL,
        saldo         NUMERIC NOT NULL DEFAULT 0,
        email         TEXT DEFAULT '',
        estado        TEXT NOT NULL DEFAULT 'ok',
        detalle       TEXT DEFAULT '',
        automatico    INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT
    );
    CREATE INDEX idx_cc_resumenes_cliente ON cc_resumenes_enviados(cliente_id, fecha);
    ALTER TABLE clients ADD COLUMN cc_resumen_auto INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE clients ADD COLUMN cc_resumen_frecuencia TEXT NOT NULL DEFAULT 'mensual'
        CHECK (cc_resumen_frecuencia IN ('semanal', 'quincenal', 'mensual'));
    ALTER TABLE clients ADD COLUMN cc_resumen_ultimo_envio TEXT DEFAULT '';
"""

_FILAS = """
    INSERT INTO clients (name, cc_resumen_auto, cc_resumen_frecuencia, cc_resumen_ultimo_envio)
    VALUES ('Cliente Ficticio SA', 1, 'semanal', '2026-09-01');
    INSERT INTO cc_resumenes_enviados (cliente_id, fecha, periodo_desde, periodo_hasta, saldo, email)
    VALUES (1, '2026-09-01', '2026-08-01', '2026-08-31', 100.5, 'cliente@ejemplo.test');
"""


def _sqlite_en_la_anterior(destino):
    assert _alembic(destino, "upgrade", ANTERIOR).returncode == 0
    conn = sqlite3.connect(destino)
    conn.executescript(_SQLITE_VIEJO + _FILAS)
    conn.close()


def _sqlite_restos(destino):
    conn = sqlite3.connect(destino)
    try:
        tablas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indices = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        columnas = {r[1] for r in conn.execute("PRAGMA table_info(clients)")}
    finally:
        conn.close()
    return tablas, indices, columnas


def test_sqlite_la_migracion_borra_la_tabla_el_indice_y_las_tres_columnas(tmp_path):
    destino = str(tmp_path / "vieja.db")
    _sqlite_en_la_anterior(destino)
    resultado = _alembic(destino, "upgrade", REVISION)
    assert resultado.returncode == 0, resultado.stderr

    tablas, indices, columnas = _sqlite_restos(destino)
    assert "cc_resumenes_enviados" not in tablas
    assert "idx_cc_resumenes_cliente" not in indices
    assert not set(COLUMNAS) & columnas
    # Lo demás de `clients` y sus filas quedan como estaban.
    assert {"name", "cuit_dni", "auto_facturar", "iva_condition"} <= columnas
    conn = sqlite3.connect(destino)
    assert conn.execute("SELECT name FROM clients").fetchall() == [("Cliente Ficticio SA",)]
    conn.close()


def test_sqlite_es_idempotente(tmp_path):
    destino = str(tmp_path / "idempotente.db")
    _sqlite_en_la_anterior(destino)
    assert _alembic(destino, "upgrade", REVISION).returncode == 0
    # Estampar la anterior y repetir: ya no hay nada que borrar y no debe fallar.
    assert _alembic(destino, "stamp", ANTERIOR).returncode == 0
    resultado = _alembic(destino, "upgrade", REVISION)
    assert resultado.returncode == 0, resultado.stderr


def test_una_base_nueva_atraviesa_la_cadena_sin_las_tablas_retiradas(tmp_path):
    """La `0003` nombra `cc_resumenes_enviados.created_at` y la `0018` `...saldo`: sobre una base que nunca
    tuvo la tabla (la cadena llama al `init_core_schema()` de hoy) tienen que seguir de largo."""
    destino = str(tmp_path / "nueva.db")
    resultado = _alembic(destino, "upgrade", "head")
    assert resultado.returncode == 0, resultado.stderr
    tablas, _, columnas = _sqlite_restos(destino)
    assert "cc_resumenes_enviados" not in tablas
    assert not set(COLUMNAS) & columnas


def test_la_0024_cuelga_de_la_0023():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini")))
    assert script.get_revision(REVISION).down_revision == ANTERIOR
    assert REVISION in {r.revision for r in script.walk_revisions()}


def test_la_migracion_no_se_baja(tmp_path):
    destino = str(tmp_path / "bajar.db")
    assert _alembic(destino, "upgrade", REVISION).returncode == 0
    resultado = _alembic(destino, "downgrade", ANTERIOR)
    assert resultado.returncode != 0
    assert "No se baja" in resultado.stderr


# ── PostgreSQL ──────────────────────────────────────────────────────────────


def _pg(url):
    import psycopg

    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True)


def _pg_restos(url):
    with _pg(url) as c:
        tabla = c.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'cc_resumenes_enviados'").fetchone()
        indice = c.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'idx_cc_resumenes_cliente'").fetchone()
        columnas = {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'clients'").fetchall()}
        check = c.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = 'clients_cc_resumen_frecuencia_check'").fetchone()
    return tabla, indice, columnas, check


def test_postgres_la_migracion_borra_la_tabla_el_indice_el_check_y_las_tres_columnas(bases):
    url = bases.nueva()
    assert _alembic(url, "upgrade", ANTERIOR).returncode == 0
    with _pg(url) as c:
        c.execute(_POSTGRES_VIEJO + _FILAS)
    tabla, _, columnas, check = _pg_restos(url)
    assert tabla and check and set(COLUMNAS) <= columnas, "la base de partida tiene que traer los restos"

    resultado = _alembic(url, "upgrade", REVISION)
    assert resultado.returncode == 0, resultado.stderr

    tabla, indice, columnas, check = _pg_restos(url)
    assert tabla is None and indice is None and check is None
    assert not set(COLUMNAS) & columnas
    assert {"name", "cuit_dni", "auto_facturar", "iva_condition"} <= columnas
    with _pg(url) as c:
        assert c.execute("SELECT name FROM clients").fetchall() == [("Cliente Ficticio SA",)]


def test_postgres_es_idempotente(bases):
    url = bases.nueva()
    assert _alembic(url, "upgrade", ANTERIOR).returncode == 0
    with _pg(url) as c:
        c.execute(_POSTGRES_VIEJO)
    assert _alembic(url, "upgrade", REVISION).returncode == 0
    assert _alembic(url, "stamp", ANTERIOR).returncode == 0
    resultado = _alembic(url, "upgrade", REVISION)
    assert resultado.returncode == 0, resultado.stderr


def test_postgres_una_base_nueva_atraviesa_la_cadena_sin_las_tablas_retiradas(bases):
    url = bases.nueva()
    resultado = _alembic(url, "upgrade", "head")
    assert resultado.returncode == 0, resultado.stderr
    tabla, indice, columnas, check = _pg_restos(url)
    assert tabla is None and indice is None and check is None
    assert not set(COLUMNAS) & columnas
