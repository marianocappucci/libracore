"""La migración `0011_reabrir_cierre_diario`, EJECUTADA contra los dos
motores, sobre una base que ya tenía `cierres_diarios` en la forma VIEJA
-- la que dejaba la `0009` antes de este cambio (sin `anulado_en`/
`anulado_por`/`motivo_anulacion`, con el UNIQUE de fecha liso).

## Por qué no alcanza con `_crear_base_ya_existente()` de `test_migraciones.py`

Ese helper arma la base sólo con `init_core_schema()` (que NUNCA tuvo las
tablas de cierre diario) y corre la cadena entera desde cero. En ese camino
la `0009` llama a la versión VIVA de `crear_tablas()` -- que YA incluye las
columnas de anulación y el índice parcial -- así que la tabla nace en la
forma NUEVA directo, y la `0011` no tiene nada que migrar.

Para probar el camino real -- una instancia que corrió la `0009` HACE MESES,
cuando todavía tenía la forma vieja -- hay que reconstruir esa forma a mano
y estampar `alembic_version` como si la cadena se hubiera detenido ahí, y
recién entonces correr `upgrade head`. Es la misma idea que sugiere el
pedido original: "sembrala en el test con la versión anterior del esquema".
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from libracore.db import core
from libracore.db.schema import init_core_schema

RAIZ = Path(__file__).resolve().parents[2]

#: La revisión a la que se sube antes de ejercitar el downgrade de la `0011`.
#:
#: 🔑 NO es el `head`: desde la `0012` la cadena tiene arriba una revisión
#: que NO baja — las columnas de `cajas` nacidas con ALTER defensivo en
#: `init_core_schema()` (generación `0004`-`0008`; la `0012` es la última) —
#: y bajar desde el head atravesaría esa `downgrade()` y explotaría por
#: diseño, sin decir nada sobre la `0011` que es lo que este archivo mide.
#: La cobertura del `upgrade head` con la base sembrada queda en los tests
#: de upgrade; acá el tramo bajo prueba es el `0011 ↔ 0010`.
REVISION_DE_LA_0011 = "0011_reabrir_cierre_diario"

# Copia fiel del `_DDL_TABLAS` de `cierre_diario.py` ANTES de v1.107.0 (ver
# `git show origin/develop:libracore/db/cierre_diario.py`) -- sin las tres
# columnas de anulación, con el UNIQUE de fecha liso. Es un snapshot histórico
# a propósito: no debe actualizarse nunca, es "lo que la 0009 vieja creaba".
_DDL_VIEJO = """
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
        created_at              TEXT DEFAULT (datetime('now','-3 hours'))
    );

    CREATE UNIQUE INDEX IF NOT EXISTS ux_cierres_diarios_sucursal_fecha
        ON cierres_diarios (COALESCE(sucursal_id, 0), fecha);

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


def _alembic(destino: str, *args: str):
    entorno = {**os.environ, "DATABASE_URL": destino}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=RAIZ, env=entorno, capture_output=True, text=True,
    )


def _liberar():
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
        _liberar()


def _sembrar_base_vieja(destino: str, *, con_dato: bool = True) -> dict:
    """Dos pasos:

    1. `alembic upgrade` REAL hasta la revisión justo antes de la `0009` — no
       toca `cierre_diario`, así que corre sin diferencia con producción.
    2. La forma vieja de `cierre_diario`, a mano (`_DDL_VIEJO`), un cierre
       sembrado, y `alembic_version` estampada en `0010_recibido_en_ventas_
       pagos` — como si la cadena real se hubiera detenido justo ahí, con la
       `0009` vieja ya corrida. Sin esto, dejar `alembic_version` vacía haría
       que `upgrade head` vuelva a correr la `0009` — que hoy ya llama a la
       versión NUEVA de `crear_tablas()` y pisaría el escenario que este test
       quiere reproducir.

    Devuelve los ids que un test necesita para sembrar más filas o verificar.
    """
    assert _alembic(destino, "upgrade", "0008_archivo_par_por_ambiente").returncode == 0

    core.configure(destino)
    conn = core.get_connection()
    try:
        conn.executescript(_DDL_VIEJO)
        cur = conn.execute(
            "INSERT INTO usuarios (username, nombre, email, password_hash, role, activo) "
            "VALUES ('admin_viejo','Admin Viejo','','x','admin',TRUE)"
        )
        usuario_id = cur.lastrowid
        ids = {"usuario_id": usuario_id}
        if con_dato:
            cur2 = conn.execute(
                "INSERT INTO cierres_diarios "
                "(sucursal_id, numero, fecha, usuario_id, monto_esperado_total, "
                " monto_declarado_total, diferencia_total, notas) "
                "VALUES (1, 1, '2026-09-01', ?, 1000, 1000, 0, 'cierre pre-migración')",
                (usuario_id,),
            )
            ids["cierre_id"] = cur2.lastrowid
        conn.execute(
            "UPDATE alembic_version SET version_num = '0010_recibido_en_ventas_pagos'"
        )
        conn.commit()
    finally:
        conn.close()
        _liberar()
    return ids


def _url_sqlalchemy(destino: str) -> str:
    """Mismo criterio que `migrations/env.py::_url_sqlalchemy`: SQLAlchemy
    resuelve `postgresql://` a `psycopg2`, que este repo no instala -- el
    driver de LibraCore es psycopg 3."""
    if "://" in destino:
        return destino.replace("postgresql://", "postgresql+psycopg://", 1)
    return f"sqlite:///{destino}"


def _columnas(destino: str) -> set[str]:
    eng = sa.create_engine(_url_sqlalchemy(destino))
    with eng.connect() as conn:
        return {c["name"] for c in sa.inspect(conn).get_columns("cierres_diarios")}


def _indices(destino: str) -> set[str]:
    """Por SQL directo, NO por `sa.inspect(...).get_indexes()` -- ese
    inspector no ve un índice por EXPRESIÓN en SQLite (ver el docstring de
    `_indices_existentes` en la migración `0011`, que tiene el mismo
    problema y la misma solución)."""
    eng = sa.create_engine(_url_sqlalchemy(destino))
    with eng.connect() as conn:
        if eng.dialect.name == "sqlite":
            filas = conn.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='cierres_diarios'"
            )).fetchall()
        else:
            filas = conn.execute(sa.text(
                "SELECT indexname AS name FROM pg_indexes WHERE tablename='cierres_diarios'"
            )).fetchall()
    return {fila[0] for fila in filas}


# ------------------------------------------------------------------- SQLite


def test_upgrade_migra_una_base_con_cierres_existentes_sqlite(tmp_path):
    destino = str(tmp_path / "vieja.db")
    ids = _sembrar_base_vieja(destino)

    assert "anulado_en" not in _columnas(destino)
    assert "ux_cierres_diarios_sucursal_fecha_activo" not in _indices(destino)
    assert "ux_cierres_diarios_sucursal_fecha" in _indices(destino)

    r = _alembic(destino, "upgrade", "head")
    assert r.returncode == 0, r.stderr

    cols = _columnas(destino)
    assert {"anulado_en", "anulado_por", "motivo_anulacion"} <= cols
    idx = _indices(destino)
    assert "ux_cierres_diarios_sucursal_fecha_activo" in idx
    assert "ux_cierres_diarios_sucursal_fecha" not in idx
    assert "ux_cierres_diarios_sucursal_numero" in idx  # no cambia

    # El cierre sembrado antes de migrar sigue intacto.
    core.configure(destino)
    conn = core.get_connection()
    fila = dict(conn.execute(
        "SELECT * FROM cierres_diarios WHERE id=?", (ids["cierre_id"],)
    ).fetchone())
    conn.close()
    _liberar()
    assert fila["fecha"] == "2026-09-01"
    assert fila["monto_esperado_total"] == 1000
    assert fila["anulado_en"] is None


def test_verificar_reabrir_y_re_cerrar_sobre_base_migrada_sqlite(tmp_path):
    """El flujo completo, sobre la base migrada: `verificar_dia_abierto`
    sigue rechazando, `reabrir_dia` libera el día, y se puede volver a
    cerrar con un número nuevo."""
    destino = str(tmp_path / "vieja_flujo.db")
    ids = _sembrar_base_vieja(destino)
    assert _alembic(destino, "upgrade", "head").returncode == 0

    from libracore.db import cierre_diario as cd
    from libracore.db import turnos as db_turnos

    core.configure(destino)
    conn = core.get_connection()
    caja1 = conn.execute("INSERT INTO cajas (nombre, sucursal_id) VALUES ('Mostrador', 1)").lastrowid
    conn.commit()
    conn.close()

    with pytest.raises(cd.DiaCerradoError):
        cd.verificar_dia_abierto("2026-09-01", 1)

    reabierto = cd.reabrir_dia(cierre_id=ids["cierre_id"], usuario_id=ids["usuario_id"],
                               motivo="verificación de migración")
    assert reabierto["anulado_en"] is not None
    cd.verificar_dia_abierto("2026-09-01", 1)  # ya no levanta

    tid = db_turnos.create_turno(ids["usuario_id"], 0.0, caja_id=caja1)
    conn = core.get_connection()
    conn.execute(
        "UPDATE turnos_caja SET apertura='2026-09-01 09:00:00', estado='cerrado', "
        "cierre='2026-09-01 10:00:00', monto_esperado_cierre=0, monto_declarado_cierre=0 WHERE id=?",
        (tid,),
    )
    conn.commit()
    conn.close()

    recierre = cd.cerrar_dia(usuario_id=ids["usuario_id"], sucursal_id=1, fecha="2026-09-01")
    assert recierre["numero"] == 2  # el próximo, no pisa el anulado (numero 1)
    _liberar()


def test_downgrade_ok_sin_duplicados_sqlite(tmp_path):
    destino = str(tmp_path / "downgrade_ok.db")
    _sembrar_base_vieja(destino)
    assert _alembic(destino, "upgrade", REVISION_DE_LA_0011).returncode == 0

    r = _alembic(destino, "downgrade", "0010_recibido_en_ventas_pagos")
    assert r.returncode == 0, r.stderr

    cols = _columnas(destino)
    assert not ({"anulado_en", "anulado_por", "motivo_anulacion"} & cols)
    idx = _indices(destino)
    assert "ux_cierres_diarios_sucursal_fecha" in idx
    assert "ux_cierres_diarios_sucursal_fecha_activo" not in idx


def test_downgrade_falla_cerrado_con_activo_y_anulado_mismo_dia_sqlite(tmp_path):
    """El caso que el índice viejo no toleraría: dos filas de la MISMA
    sucursal y fecha (una activa, una anulada). El downgrade tiene que
    rechazar ANTES de tocar nada -- ni las columnas ni los índices cambian."""
    destino = str(tmp_path / "downgrade_choca.db")
    ids = _sembrar_base_vieja(destino)
    assert _alembic(destino, "upgrade", REVISION_DE_LA_0011).returncode == 0

    from libracore.db import cierre_diario as cd
    core.configure(destino)
    cd.reabrir_dia(cierre_id=ids["cierre_id"], usuario_id=ids["usuario_id"], motivo="para el choque")
    # El re-cierre del mismo día: misma sucursal, misma fecha, ACTIVO.
    conn = core.get_connection()
    conn.execute(
        "INSERT INTO cierres_diarios (sucursal_id, numero, fecha, usuario_id, "
        "monto_esperado_total, monto_declarado_total, diferencia_total, notas) "
        "VALUES (1, 2, '2026-09-01', ?, 0, 0, 0, 're-cierre')",
        (ids["usuario_id"],),
    )
    conn.commit()
    conn.close()
    _liberar()

    r = _alembic(destino, "downgrade", "0010_recibido_en_ventas_pagos")
    assert r.returncode != 0
    salida = (r.stdout + r.stderr).lower()
    # La frase PROPIA del chequeo -- no alcanza con "algo falló": el
    # `CREATE UNIQUE INDEX` de abajo también fallaría solo, por su cuenta,
    # ante el mismo par duplicado (con un mensaje crudo del motor). Pedir
    # esta frase es lo que distingue "el chequeo explícito lo agarró antes"
    # de "total, explotó en otro lado" -- se verificó sacando el chequeo a
    # mano: sin la frase específica, este test seguía en verde igual.
    assert "no se puede bajar esta revisión" in salida
    assert "sucursal" in salida and "fecha" in salida

    # Fallar cerrado: nada cambió.
    cols = _columnas(destino)
    assert {"anulado_en", "anulado_por", "motivo_anulacion"} <= cols
    assert "ux_cierres_diarios_sucursal_fecha_activo" in _indices(destino)


def test_upgrade_desde_base_vacia_sqlite(tmp_path):
    """La otra mitad del pedido: `upgrade head` sobre una base VACÍA llega
    directo a la forma final -- la `0009` ya llama a la versión viva de
    `crear_tablas()` -- y la `0011` no tiene nada para hacer."""
    destino = str(tmp_path / "nueva.db")
    assert _alembic(destino, "upgrade", "head").returncode == 0

    cols = _columnas(destino)
    assert {"anulado_en", "anulado_por", "motivo_anulacion"} <= cols
    idx = _indices(destino)
    assert "ux_cierres_diarios_sucursal_fecha_activo" in idx
    assert "ux_cierres_diarios_sucursal_fecha" not in idx


# ---------------------------------------------------------------- PostgreSQL


def test_upgrade_migra_una_base_con_cierres_existentes_postgres():
    url = _url_postgres()
    _limpiar_postgres(url)
    ids = _sembrar_base_vieja(url)

    assert "anulado_en" not in _columnas(url)

    r = _alembic(url, "upgrade", "head")
    assert r.returncode == 0, r.stderr

    cols = _columnas(url)
    assert {"anulado_en", "anulado_por", "motivo_anulacion"} <= cols
    idx = _indices(url)
    assert "ux_cierres_diarios_sucursal_fecha_activo" in idx
    assert "ux_cierres_diarios_sucursal_fecha" not in idx

    core.configure(url)
    conn = core.get_connection()
    fila = dict(conn.execute(
        "SELECT * FROM cierres_diarios WHERE id=?", (ids["cierre_id"],)
    ).fetchone())
    conn.close()
    _liberar()
    assert fila["fecha"] == "2026-09-01"
    assert fila["anulado_en"] is None


def test_downgrade_falla_cerrado_con_activo_y_anulado_mismo_dia_postgres():
    url = _url_postgres()
    _limpiar_postgres(url)
    ids = _sembrar_base_vieja(url)
    assert _alembic(url, "upgrade", REVISION_DE_LA_0011).returncode == 0

    from libracore.db import cierre_diario as cd
    core.configure(url)
    cd.reabrir_dia(cierre_id=ids["cierre_id"], usuario_id=ids["usuario_id"], motivo="para el choque")
    conn = core.get_connection()
    conn.execute(
        "INSERT INTO cierres_diarios (sucursal_id, numero, fecha, usuario_id, "
        "monto_esperado_total, monto_declarado_total, diferencia_total, notas) "
        "VALUES (1, 2, '2026-09-01', ?, 0, 0, 0, 're-cierre')",
        (ids["usuario_id"],),
    )
    conn.commit()
    conn.close()
    _liberar()

    r = _alembic(url, "downgrade", "0010_recibido_en_ventas_pagos")
    assert r.returncode != 0
    cols = _columnas(url)
    assert {"anulado_en", "anulado_por", "motivo_anulacion"} <= cols


def test_downgrade_ok_sin_duplicados_postgres():
    url = _url_postgres()
    _limpiar_postgres(url)
    _sembrar_base_vieja(url)
    assert _alembic(url, "upgrade", REVISION_DE_LA_0011).returncode == 0

    r = _alembic(url, "downgrade", "0010_recibido_en_ventas_pagos")
    assert r.returncode == 0, r.stderr
    cols = _columnas(url)
    assert not ({"anulado_en", "anulado_por", "motivo_anulacion"} & cols)
