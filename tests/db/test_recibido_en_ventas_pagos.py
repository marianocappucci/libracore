"""La `0010`: el vuelto de un pago, ejecutada contra una instancia que ya
existe — y bajada.

Mismo motivo que `test_credenciales_de_arca_no_se_pierden.py`: la clase de
regresión que esto cubre no se ve creando la base con el schema nuevo, donde
la columna siempre estuvo. Sólo aparece migrando una base que ya tenía datos
(el caso real del deploy) y, acá además, bajándola de nuevo — que es lo que
distingue a esta revisión de `0004`-`0008`, que no bajan.

Se corre alembic **como proceso**, igual que el resto de la cadena.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import head_de_la_cadena

from libracore.db import core
from libracore.db.schema import init_core_schema

RAIZ = Path(__file__).resolve().parents[2]

#: La revisión anterior a ésta, para poder armar una base "como estaba antes"
#: y para bajar hasta ahí. Es la `down_revision` de la 0010 — hardcodeada acá
#: nomás, no en un literal que se repita en dos lugares.
REVISION_ANTERIOR = "0009_cierre_diario"

REVISION = head_de_la_cadena()


def _alembic(destino: str, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=RAIZ,
        env={**os.environ, "DATABASE_URL": destino},
        capture_output=True,
        text=True,
    )


def _revision_actual(destino: str) -> str | None:
    core.configure(destino)
    conn = core.get_connection()
    try:
        try:
            fila = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        except Exception:
            return None
        return fila[0] if fila else None
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


def _instancia_vieja(destino: str) -> int:
    """Una base ya migrada hasta la revisión ANTERIOR a la `0010`, con una
    venta y un pago reales — el caso de una instancia que ya tenía datos
    cuando esta revisión se agregó a la cadena."""
    core.configure(destino)
    conn = core.get_connection()
    try:
        init_core_schema(conn)
        conn.commit()
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None

    r = _alembic(destino, "upgrade", REVISION_ANTERIOR)
    assert r.returncode == 0, r.stderr

    core.configure(destino)
    conn = core.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO ventas (numero, fecha, items) VALUES (?, ?, ?)",
            ("V-00001", "2026-09-14", "[]"),
        )
        venta_id = cur.lastrowid
        conn.execute(
            "INSERT INTO ventas_pagos (venta_id, medio, monto, referencia) "
            "VALUES (?, ?, ?, ?)",
            (venta_id, "efectivo", 500.0, "ref-vieja"),
        )
        conn.commit()
        pago_id = conn.execute(
            "SELECT id FROM ventas_pagos WHERE venta_id=?", (venta_id,)
        ).fetchone()[0]
        return pago_id
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


def _columnas(destino: str, tabla: str) -> list[str]:
    core.configure(destino)
    conn = core.get_connection()
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({tabla})").fetchall()]
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


def _pago(destino: str, pago_id: int) -> dict:
    core.configure(destino)
    conn = core.get_connection()
    try:
        fila = conn.execute(
            "SELECT * FROM ventas_pagos WHERE id=?", (pago_id,)
        ).fetchone()
        return dict(fila)
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


def _insertar_pago_sin_recibido(destino: str, venta_id: int) -> int:
    """El INSERT que hace hoy `db/ventas.py:add_venta_pago` — nombra las
    columnas de siempre y no toca `recibido`. Tiene que seguir andando igual
    después de la migración."""
    core.configure(destino)
    conn = core.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO ventas_pagos (venta_id, medio, monto, referencia) "
            "VALUES (?, ?, ?, ?)",
            (venta_id, "tarjeta", 100.0, ""),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


def _url_postgres() -> str:
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("LIBRACORE_POSTGRES_URL no está definida en CI")
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


def _limpiar_postgres(url: str):
    core.configure(url)
    conn = core.get_connection()
    try:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


# --------------------------------------------------------------------- núcleo


def _ejercer_upgrade_y_downgrade(destino: str):
    pago_id = _instancia_vieja(destino)
    antes = _columnas(destino, "ventas_pagos")
    assert "recibido" not in antes, (
        "la base de partida ya tiene la columna: el test no prueba nada")

    # ── upgrade ──────────────────────────────────────────────────────────
    r = _alembic(destino, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert _revision_actual(destino) == REVISION, (
        f"la cadena no llegó a {REVISION} — comparar los datos no diría nada.\n"
        + r.stderr[-800:]
    )

    cols_despues_upgrade = _columnas(destino, "ventas_pagos")
    assert "recibido" in cols_despues_upgrade
    assert set(antes) <= set(cols_despues_upgrade), (
        "el upgrade se llevó puesta una columna existente")

    fila_vieja = _pago(destino, pago_id)
    assert fila_vieja["recibido"] is None, (
        "una fila que ya existía antes de la revisión tiene que quedar NULL, "
        "no inventarle un vuelto")
    assert fila_vieja["monto"] == 500.0
    assert fila_vieja["medio"] == "efectivo"
    assert fila_vieja["referencia"] == "ref-vieja"

    # Un INSERT que no menciona `recibido` — el que hace hoy `add_venta_pago`
    # — sigue andando igual, y la columna nueva le queda NULL.
    nuevo_id = _insertar_pago_sin_recibido(destino, venta_id=1)
    fila_nueva = _pago(destino, nuevo_id)
    assert fila_nueva["recibido"] is None
    assert fila_nueva["monto"] == 100.0

    # ── downgrade ────────────────────────────────────────────────────────
    r = _alembic(destino, "downgrade", REVISION_ANTERIOR)
    assert r.returncode == 0, r.stderr
    assert _revision_actual(destino) == REVISION_ANTERIOR

    cols_despues_downgrade = _columnas(destino, "ventas_pagos")
    assert "recibido" not in cols_despues_downgrade, (
        "el downgrade no sacó la columna")
    assert set(cols_despues_downgrade) == set(antes), (
        "el downgrade se llevó puesta (o dejó de más) alguna otra columna")

    # Las filas y sus otras columnas sobreviven a la ida y vuelta.
    core.configure(destino)
    conn = core.get_connection()
    try:
        filas = conn.execute(
            "SELECT id, venta_id, medio, monto, referencia, estado "
            "FROM ventas_pagos ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None

    assert len(filas) == 2, "el downgrade se llevó puesta alguna fila"
    vieja, nueva = (dict(f) for f in filas)
    assert vieja["id"] == pago_id
    assert vieja["monto"] == 500.0
    assert vieja["medio"] == "efectivo"
    assert vieja["referencia"] == "ref-vieja"
    assert nueva["id"] == nuevo_id
    assert nueva["monto"] == 100.0


# --------------------------------------------------------------------- SQLite


def test_upgrade_y_downgrade_en_sqlite(tmp_path):
    _ejercer_upgrade_y_downgrade(str(tmp_path / "recibido.db"))


# ----------------------------------------------------------------- PostgreSQL


def test_upgrade_y_downgrade_en_postgres():
    url = _url_postgres()
    _limpiar_postgres(url)
    _ejercer_upgrade_y_downgrade(url)
