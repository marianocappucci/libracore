"""El concepto de la factura a cuenta corriente deja de decir «Factura» dos veces (ADR-047): la migración `0025`.

Hasta la v1.161.0 la emisión a crédito escribía `Factura Factura C 0001-00000001 — Juan` en
`caja_movimientos`, y el libro de clientes (`cc_asientos`) copió ese texto al asentar. La `0025` corrige las
filas que ya estaban, en las dos tablas, y **sólo** las que empiezan exactamente con `Factura Factura `.

Cada test arma una base en la `0024` con filas viejas y filas que no se pueden tocar, migra y compara la
tabla entera columna por columna: lo único que cambia es el `concepto` de las filas que corresponde.
Se mide en SQLite y en PostgreSQL.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
REVISION = "0025_concepto_sin_factura_doble"
ANTERIOR = "0024_retirar_cc_resumen"

#: (concepto de antes, concepto de después). Lo que no cambia, igual de los dos lados.
VIEJAS = [
    ("Factura Factura C 0001-00000001 — Juan Perez", "Factura C 0001-00000001 — Juan Perez"),
    ("Factura Factura A 0005-00000074", "Factura A 0005-00000074"),
    ("Factura Factura FCE MiPyME C 0001-00000009 — Ana", "Factura FCE MiPyME C 0001-00000009 — Ana"),
]
INTACTAS = [
    # Ya está bien (lo que escribe la emisión de hoy).
    "Factura C 0001-00000002 — Juan Perez",
    # El cobro de la factura: otro formato, otro texto.
    "Cobro Factura C 0001-00000001 — Juan Perez",
    "Cobro FACTURA C 0005-00000005 — RAZON SOCIAL",
    # La frase está, pero no al principio.
    "Reversión: Factura Factura C 0001-00000001 — Juan Perez",
    "Anulación de Factura Factura C 0001-00000001",
    " Factura Factura C 0001-00000001",
    # Parecidos que no son el prefijo exacto: otra palabra, sin el espacio de después, otras mayúsculas.
    "Factura Facturación mensual",
    "Factura Factura",
    "factura factura C 0001-00000003",
    "FACTURA FACTURA C 0001-00000004",
    "Factura  Factura C 0001-00000005",
    # Otras cosas de la cuenta.
    "Pago a cuenta",
    "Venta 12",
]
FILAS = VIEJAS + [(c, c) for c in INTACTAS]


def _alembic(destino, *args):
    env = {**os.environ, "DATABASE_URL": destino}
    return subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic.ini", *args], cwd=RAIZ, env=env,
                          capture_output=True, text=True)


def _cargar(conn, marca):
    """Inserta `FILAS` en las dos tablas. `marca` es el placeholder del driver (`?` o `%s`)."""
    for n, (antes, _) in enumerate(FILAS, start=1):
        conn.execute(
            f"INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, referencia, medio_pago) "
            f"VALUES ({marca}, 'ingreso', {marca}, {marca}, 'ref-{n}', 'Cuenta Corriente')",
            (f"2026-09-{n:02d}", antes, 1000.0 + n),
        )
        conn.execute(
            f"INSERT INTO cc_asientos (fecha, tercero_id, rol, concepto, debe, haber, origen) "
            f"VALUES ({marca}, 7, 'cliente', {marca}, {marca}, 0, 'caja_mov:{n}')",
            (f"2026-09-{n:02d}", antes, 1000.0 + n),
        )


def _foto(conn):
    """Las dos tablas enteras, ordenadas por id, con el `concepto` aparte para poder compararlo."""
    out = {}
    for tabla in ("caja_movimientos", "cc_asientos"):
        cur = conn.execute(f"SELECT * FROM {tabla} ORDER BY id")
        columnas = [d[0] for d in cur.description]
        out[tabla] = [dict(zip(columnas, fila, strict=True)) for fila in cur.fetchall()]
    return out


def _verificar(antes, despues):
    for tabla in ("caja_movimientos", "cc_asientos"):
        assert len(antes[tabla]) == len(despues[tabla]) == len(FILAS)
        for previa, actual, (_, esperado) in zip(antes[tabla], despues[tabla], FILAS, strict=True):
            assert actual["concepto"] == esperado, (tabla, previa["concepto"])
            # Nada más cambia: ni montos, ni fechas, ni vínculos, ni ids, ni el origen del asiento.
            assert {k: v for k, v in actual.items() if k != "concepto"} == \
                   {k: v for k, v in previa.items() if k != "concepto"}


def _filas_tocadas(salida, tabla):
    """El `N` de «0025: <tabla>: N fila(s) con el concepto corregido.» que la migración escribe a stderr."""
    for linea in salida.splitlines():
        if linea.startswith(f"0025: {tabla}:"):
            return int(linea.split(":")[2].split()[0])
    raise AssertionError(f"la migración no dijo cuántas filas tocó en {tabla}:\n{salida}")


# ── SQLite ──────────────────────────────────────────────────────────────────


def _sqlite_en_la_anterior(destino):
    assert _alembic(destino, "upgrade", ANTERIOR).returncode == 0
    conn = sqlite3.connect(destino)
    _cargar(conn, "?")
    conn.commit()
    conn.close()


def _sqlite_foto(destino):
    conn = sqlite3.connect(destino)
    try:
        return _foto(conn)
    finally:
        conn.close()


def test_sqlite_corrige_solo_las_filas_que_empiezan_con_factura_factura(tmp_path):
    destino = str(tmp_path / "vieja.db")
    _sqlite_en_la_anterior(destino)
    antes = _sqlite_foto(destino)
    # La base de partida trae lo que hay que corregir.
    assert sum(f["concepto"].startswith("Factura Factura ") for f in antes["caja_movimientos"]) == len(VIEJAS)

    resultado = _alembic(destino, "upgrade", REVISION)
    assert resultado.returncode == 0, resultado.stderr

    _verificar(antes, _sqlite_foto(destino))
    assert _filas_tocadas(resultado.stderr, "caja_movimientos") == len(VIEJAS)
    assert _filas_tocadas(resultado.stderr, "cc_asientos") == len(VIEJAS)


def test_sqlite_es_idempotente(tmp_path):
    destino = str(tmp_path / "idempotente.db")
    _sqlite_en_la_anterior(destino)
    antes = _sqlite_foto(destino)
    assert _alembic(destino, "upgrade", REVISION).returncode == 0
    primera = _sqlite_foto(destino)
    # Estampar la anterior y repetir: la segunda corrida no encuentra nada que corregir.
    assert _alembic(destino, "stamp", ANTERIOR).returncode == 0
    resultado = _alembic(destino, "upgrade", REVISION)
    assert resultado.returncode == 0, resultado.stderr

    assert _sqlite_foto(destino) == primera
    _verificar(antes, primera)
    assert _filas_tocadas(resultado.stderr, "caja_movimientos") == 0
    assert _filas_tocadas(resultado.stderr, "cc_asientos") == 0


def test_sqlite_sin_filas_viejas_no_toca_nada(tmp_path):
    destino = str(tmp_path / "limpia.db")
    assert _alembic(destino, "upgrade", ANTERIOR).returncode == 0
    resultado = _alembic(destino, "upgrade", REVISION)
    assert resultado.returncode == 0, resultado.stderr
    assert _filas_tocadas(resultado.stderr, "caja_movimientos") == 0


def test_sqlite_una_base_sin_el_libro_corrige_la_caja_y_sigue(tmp_path):
    """Un producto que arma a mano las tablas del motor que usa puede no tener `cc_asientos`."""
    destino = str(tmp_path / "sin_libro.db")
    _sqlite_en_la_anterior(destino)
    conn = sqlite3.connect(destino)
    conn.execute("DROP TABLE cc_asientos")
    conn.commit()
    conn.close()

    resultado = _alembic(destino, "upgrade", REVISION)
    assert resultado.returncode == 0, resultado.stderr
    assert "cc_asientos no existe" in resultado.stderr
    assert _filas_tocadas(resultado.stderr, "caja_movimientos") == len(VIEJAS)


def test_la_0025_cuelga_de_la_0024():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini")))
    assert script.get_revision(REVISION).down_revision == ANTERIOR
    assert REVISION in {r.revision for r in script.walk_revisions()}


def test_el_id_de_la_revision_entra_en_alembic_version():
    """`alembic_version.version_num` es VARCHAR(32) en PostgreSQL: un id más largo migra en SQLite y revienta
    en producción (`StringDataRightTruncation`). Se mide la cadena entera, no sólo la `0025`."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini")))
    largos = {r.revision: len(r.revision) for r in script.walk_revisions() if len(r.revision) > 32}
    assert not largos, largos


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


def _pg_en_la_anterior(url):
    assert _alembic(url, "upgrade", ANTERIOR).returncode == 0
    with _pg(url) as c:
        _cargar(c, "%s")


def _pg_foto(url):
    with _pg(url) as c:
        return _foto(c)


def test_postgres_corrige_solo_las_filas_que_empiezan_con_factura_factura(bases):
    url = bases.nueva()
    _pg_en_la_anterior(url)
    antes = _pg_foto(url)
    assert sum(f["concepto"].startswith("Factura Factura ") for f in antes["cc_asientos"]) == len(VIEJAS)

    resultado = _alembic(url, "upgrade", REVISION)
    assert resultado.returncode == 0, resultado.stderr

    _verificar(antes, _pg_foto(url))
    assert _filas_tocadas(resultado.stderr, "caja_movimientos") == len(VIEJAS)
    assert _filas_tocadas(resultado.stderr, "cc_asientos") == len(VIEJAS)


def test_postgres_es_idempotente(bases):
    url = bases.nueva()
    _pg_en_la_anterior(url)
    antes = _pg_foto(url)
    assert _alembic(url, "upgrade", REVISION).returncode == 0
    primera = _pg_foto(url)
    assert _alembic(url, "stamp", ANTERIOR).returncode == 0
    resultado = _alembic(url, "upgrade", REVISION)
    assert resultado.returncode == 0, resultado.stderr

    assert _pg_foto(url) == primera
    _verificar(antes, primera)
    assert _filas_tocadas(resultado.stderr, "caja_movimientos") == 0
    assert _filas_tocadas(resultado.stderr, "cc_asientos") == 0
