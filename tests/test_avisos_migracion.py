"""La migración `0032` y la tabla `avisos_envios` (ADR-090).

- En una **base vacía** la cadena entera deja la tabla, con su clave única y sus checks.
- En una **base de antes, con datos**, la `0032` agrega la tabla vacía sin tocar una fila de lo que había, y correrla de nuevo no rompe.
- No se baja (perdería el registro de lo avisado y el próximo barrido lo mandaría de nuevo).
- La clave única `(canal, clave)` y los `CHECK` de estado hacen cumplir en la base lo que el despachador asume.

Contra SQLite siempre y contra PostgreSQL si hay `LIBRACORE_POSTGRES_URL`.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
COLUMNAS = {"id", "canal", "clave", "tipo", "destino", "asunto", "cuerpo", "plantilla", "variables", "proveedor", "estado",
            "intentos", "detalle", "id_externo", "enviado_at", "created_at", "updated_at"}


def _alembic(destino: str, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=RAIZ, env={**os.environ, "DATABASE_URL": destino, "PYTHONPATH": str(RAIZ)}, capture_output=True, text=True)


def _insertar(conn, canal="whatsapp", clave="reserva:1:confirmacion", estado="enviado"):
    conn.execute("INSERT INTO avisos_envios (canal, clave, estado) VALUES (?, ?, ?)", (canal, clave, estado))


# ── SQLite ────────────────────────────────────────────────────────────────────────────────────────────────


def test_base_vacia_la_cadena_entera_deja_la_tabla(tmp_path):
    destino = str(tmp_path / "vacia.db")
    r = _alembic(destino, "upgrade", "head")
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(destino)
    try:
        assert {f[1] for f in conn.execute("PRAGMA table_info(avisos_envios)").fetchall()} == COLUMNAS
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] >= "0032_avisos_envios"
        indices = {f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert {"idx_avisos_envios_clave", "idx_avisos_envios_estado"} <= indices
        assert conn.execute("SELECT COUNT(*) FROM avisos_envios").fetchone()[0] == 0
    finally:
        conn.close()


def test_base_de_antes_con_datos_recibe_la_tabla_sin_tocar_nada(tmp_path):
    destino = str(tmp_path / "antes.db")
    assert _alembic(destino, "upgrade", "0031_moneda_del_libro").returncode == 0
    # La 0001 de hoy ya la crea con `init_core_schema`: se la sacamos para tener una base de antes de la 0032.
    conn = sqlite3.connect(destino)
    conn.execute("DROP TABLE avisos_envios")
    conn.execute("INSERT INTO clients (name) VALUES ('Cliente Ficticio SA')")
    conn.execute("INSERT INTO cc_asientos (fecha, tercero_id, rol, concepto, debe) "
                 "VALUES ('2026-10-01', 7, 'inquilino', 'Alquiler', 1000)")
    conn.commit()
    antes = {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("clients", "cc_asientos")}
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='avisos_envios'").fetchone() is None
    conn.close()

    assert _alembic(destino, "upgrade", "head").returncode == 0

    conn = sqlite3.connect(destino)
    try:
        assert {f[1] for f in conn.execute("PRAGMA table_info(avisos_envios)").fetchall()} == COLUMNAS
        assert conn.execute("SELECT COUNT(*) FROM avisos_envios").fetchone()[0] == 0
        assert {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in antes} == antes, "no se tocó ni una fila"
    finally:
        conn.close()

    # y correrla de nuevo no rompe ni duplica nada (idempotente)
    assert _alembic(destino, "upgrade", "head").returncode == 0


def test_no_se_baja(tmp_path):
    destino = str(tmp_path / "nobaja.db")
    assert _alembic(destino, "upgrade", "head").returncode == 0
    bajar = _alembic(destino, "downgrade", "0031_moneda_del_libro")
    assert bajar.returncode != 0 and "No se baja" in bajar.stderr


def test_la_clave_unica_y_los_checks_se_hacen_cumplir_en_la_base(tmp_path):
    destino = str(tmp_path / "reglas.db")
    assert _alembic(destino, "upgrade", "head").returncode == 0
    conn = sqlite3.connect(destino)
    try:
        _insertar(conn)
        _insertar(conn, canal="email")  # el mismo hecho por otro canal es otro aviso
        with pytest.raises(sqlite3.IntegrityError):
            _insertar(conn)  # la misma (canal, clave)
        with pytest.raises(sqlite3.IntegrityError):
            _insertar(conn, clave="otra", estado="inventado")
        with pytest.raises(sqlite3.IntegrityError):
            _insertar(conn, clave="")
        with pytest.raises(sqlite3.IntegrityError):
            _insertar(conn, canal="", clave="x")
    finally:
        conn.close()


# ── PostgreSQL ────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def postgres():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("LIBRACORE_POSTGRES_URL no está definida en CI")
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    import psycopg

    def limpiar():
        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True) as c:
            c.execute("DROP SCHEMA IF EXISTS public CASCADE")
            c.execute("CREATE SCHEMA public")

    limpiar()
    yield url
    limpiar()


def _pg(url):
    import psycopg

    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True)


def test_postgres_base_vacia_deja_la_tabla_y_hace_cumplir_las_reglas(postgres):
    import psycopg

    r = _alembic(postgres, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    with _pg(postgres) as c:
        columnas = {f[0] for f in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='avisos_envios'").fetchall()}
        assert columnas == COLUMNAS
        c.execute("INSERT INTO avisos_envios (canal, clave, estado) VALUES ('whatsapp', 'k', 'enviado')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute("INSERT INTO avisos_envios (canal, clave, estado) VALUES ('whatsapp', 'k', 'fallido')")
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO avisos_envios (canal, clave, estado) VALUES ('whatsapp', 'otra', 'inventado')")


def test_postgres_base_de_antes_con_datos_recibe_la_tabla_sin_tocar_nada(postgres):
    assert _alembic(postgres, "upgrade", "0031_moneda_del_libro").returncode == 0
    with _pg(postgres) as c:
        c.execute("DROP TABLE avisos_envios")
        c.execute("INSERT INTO clients (name) VALUES ('Cliente Ficticio SA')")
        antes = c.execute("SELECT id, name FROM clients ORDER BY id").fetchall()

    assert _alembic(postgres, "upgrade", "head").returncode == 0

    with _pg(postgres) as c:
        assert c.execute("SELECT COUNT(*) FROM avisos_envios").fetchone()[0] == 0
        assert c.execute("SELECT id, name FROM clients ORDER BY id").fetchall() == antes
        # el reloj queda en hora de Argentina (lo exige `alters_para_hora_ar` en la cadena)
        defecto = c.execute("SELECT column_default FROM information_schema.columns "
                            "WHERE table_name='avisos_envios' AND column_name='created_at'").fetchone()[0]
        assert "-03:00:00" in defecto
    assert _alembic(postgres, "upgrade", "head").returncode == 0
    bajar = _alembic(postgres, "downgrade", "0031_moneda_del_libro")
    assert bajar.returncode != 0 and "No se baja" in bajar.stderr
