"""La 0007, ejecutada contra una instancia que ya existe.

🔴 **El defecto que esta revisión evita.** Hasta ahora `arca_config` guardaba
**un** par de credenciales, en las columnas sin sufijo — que a partir de este
cambio son las de **producción**. Una instancia con `ambiente='homologacion'`
—las demos— pasaría a buscar su par en `*_homologacion`, que están vacías:
`paths_de()` devolvería `("", "")` y la facturación dejaría de andar **sin que
nadie haya tocado nada**.

Es la clase de regresión que no se ve en una suite: los tests crean la base con
el schema nuevo, donde el problema no existe. Sólo aparece migrando una base
que ya tenía datos, que es exactamente lo que hace el deploy.

Se corre alembic **como proceso**, igual que `test_migraciones.py`, porque así
lo invoca `scripts/run_migrations.sh`.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from libracore.db import arca_config as db_arca
from libracore.db import core
from libracore.db.schema import init_core_schema

RAIZ = Path(__file__).resolve().parents[2]

#: La revisión que hace el movimiento. Se afirma que la cadena llegó **hasta
#: acá** antes de mirar los datos: sin ese control, "las credenciales siguen
#: donde estaban" pasa perfecto cuando la migración no corrió.
REVISION = "0007_par_arca_por_ambiente"

CERT = "/certs/el-certificado.crt"
CLAVE = "/certs/la-clave.key"


def _instancia_vieja(ruta: str, ambiente: str) -> None:
    """Una base como las de producción de hoy: el par en las columnas SIN
    sufijo y **sin** las columnas del segundo par."""
    core._db_path = None
    core._database_url = None
    core.configure(db_path=ruta)
    c = core.get_connection()
    try:
        init_core_schema(c)
        # Se le sacan las columnas nuevas para que la base quede como estaba
        # antes de esta revisión. Sin esto el test mediría el schema de hoy.
        cols = [r[1] for r in c.execute("PRAGMA table_info(arca_config)")]
        viejas = [x for x in cols if not x.endswith("_homologacion")]
        assert len(viejas) < len(cols), (
            "la tabla no tiene las columnas nuevas: ¿el schema no las crea?")
        c.execute("ALTER TABLE arca_config RENAME TO arca_config_vieja")
        c.execute("CREATE TABLE arca_config AS SELECT %s FROM arca_config_vieja"
                  % ", ".join(viejas))
        c.execute("DROP TABLE arca_config_vieja")
        c.execute(
            "INSERT INTO arca_config (empresa, cuit, punto_venta, clave_path,"
            " certificado_path, ambiente, activo)"
            " VALUES ('default','20111111112',1,?,?,?,1)",
            (CLAVE, CERT, ambiente),
        )
        c.commit()
    finally:
        c.close()
        core._db_path = None
        core._database_url = None


def _migrar(ruta: str):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=RAIZ, env={**os.environ, "DATABASE_URL": ruta},
        capture_output=True, text=True,
    )


def _config(ruta: str) -> dict:
    c = sqlite3.connect(ruta)
    c.row_factory = sqlite3.Row
    try:
        return dict(c.execute("SELECT * FROM arca_config").fetchone())
    finally:
        c.close()


def _revision(ruta: str):
    c = sqlite3.connect(ruta)
    try:
        fila = c.execute("SELECT version_num FROM alembic_version").fetchone()
        return fila[0] if fila else None
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()


@pytest.mark.parametrize("ambiente", ["homologacion", "produccion"])
def test_la_instancia_conserva_sus_credenciales(tmp_path, ambiente):
    """🔑 Lo que se afirma no es dónde quedaron las columnas: es que
    **`paths_de()` sigue devolviendo el par**. Es lo que el firmante va a leer,
    y es la única forma de que el test falle por la razón que importa."""
    ruta = str(tmp_path / f"instancia_{ambiente}.db")
    _instancia_vieja(ruta, ambiente)

    r = _migrar(ruta)
    assert _revision(ruta) == REVISION, (
        f"la cadena no llegó a {REVISION} — comparar los datos no diría nada.\n"
        + r.stderr[-800:])

    cfg = _config(ruta)
    assert db_arca.paths_de(cfg) == (CERT, CLAVE), (
        f"la instancia en {ambiente!r} perdió sus credenciales al migrar")


def test_el_par_queda_en_las_columnas_del_ambiente(tmp_path):
    """El movimiento en sí: en una instancia de homologación el par pasa a las
    columnas con sufijo, y las de producción quedan **vacías**. Decir que tiene
    un certificado de producción que no tiene sería peor que decir que no."""
    ruta = str(tmp_path / "demo.db")
    _instancia_vieja(ruta, "homologacion")
    _migrar(ruta)
    assert _revision(ruta) == REVISION

    cfg = _config(ruta)
    assert (cfg["certificado_path_homologacion"], cfg["clave_path_homologacion"]) == (CERT, CLAVE)
    assert (cfg["certificado_path"], cfg["clave_path"]) == ("", "")


def test_en_produccion_el_par_no_se_mueve(tmp_path):
    """El control del anterior: si la revisión moviera **todas** las filas, una
    instancia de producción perdería su certificado real. La condición del
    `WHERE` es lo único que lo separa."""
    ruta = str(tmp_path / "cliente.db")
    _instancia_vieja(ruta, "produccion")
    _migrar(ruta)
    assert _revision(ruta) == REVISION

    cfg = _config(ruta)
    assert (cfg["certificado_path"], cfg["clave_path"]) == (CERT, CLAVE)
    assert (cfg["certificado_path_homologacion"], cfg["clave_path_homologacion"]) == ("", "")


def test_migrar_dos_veces_no_pisa_lo_que_ya_esta(tmp_path):
    """Idempotencia con datos, no sólo con DDL: si alguien ya cargó su par de
    homologación por la pantalla, el suyo manda sobre este movimiento."""
    ruta = str(tmp_path / "recargada.db")
    _instancia_vieja(ruta, "homologacion")
    _migrar(ruta)

    # Como si el operador hubiera subido el par bueno después del deploy.
    c = sqlite3.connect(ruta)
    c.execute("UPDATE arca_config SET certificado_path_homologacion='/nuevo.crt',"
              " clave_path_homologacion='/nuevo.key', certificado_path=?,"
              " clave_path=?", (CERT, CLAVE))
    c.commit()
    c.close()

    # Bajar y volver a subir la revisión no está: el `downgrade` levanta a
    # propósito. Se fuerza reponiendo la versión anterior en la tabla.
    c = sqlite3.connect(ruta)
    c.execute("UPDATE alembic_version SET version_num='0006_ambiente_arca_factura'")
    c.commit()
    c.close()

    _migrar(ruta)
    assert _revision(ruta) == REVISION

    cfg = _config(ruta)
    assert cfg["certificado_path_homologacion"] == "/nuevo.crt", (
        "la segunda corrida pisó el par que había cargado el operador")
    assert cfg["certificado_path"] == CERT, "y además se llevó el de producción"
