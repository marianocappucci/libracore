"""Las credenciales de ARCA por servicio: el almacén y la migración `0022` (ADR-032).

La facturación (`wsfe`) NO pasa por acá —sigue en `arca_config`—: lo que se mide es
que la tabla nueva guarda un par por (empresa, servicio, ambiente), que no se
mezclan entre sí, y que la migración sube y baja sin tocar nada de la facturación.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from libracore import arca_credenciales, config_manager
from libracore.db import arca_config as db_arca
from libracore.db import arca_credenciales_servicio as db
from libracore.db import core
from libracore.db.schema import init_core_schema

RAIZ = Path(__file__).resolve().parents[2]
REVISION = "0022_credenciales_por_servicio"
ANTERIOR = "0021_pre_factura"


@pytest.fixture
def base(tmp_path):
    core.configure(db_path=str(tmp_path / "servicios.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    yield conn
    conn.close()
    core._db_path = None


def test_sin_nada_cargado_devuelve_vacio(base):
    assert db.paths_de_servicio("acme", "wscpe", "homologacion") == ("", "")


def test_guardar_y_leer_un_par(base):
    db.guardar_paths_de_servicio("acme", "wscpe", "homologacion",
                                 certificado_path="/c/a.crt", clave_path="/c/a.key")
    assert db.paths_de_servicio("acme", "wscpe", "homologacion") == ("/c/a.crt", "/c/a.key")


def test_None_es_no_lo_toques(base):
    """Subir el certificado no borra la clave que ya estaba."""
    db.guardar_paths_de_servicio("acme", "wscpe", "homologacion", clave_path="/c/a.key")
    db.guardar_paths_de_servicio("acme", "wscpe", "homologacion", certificado_path="/c/a.crt")
    assert db.paths_de_servicio("acme", "wscpe", "homologacion") == ("/c/a.crt", "/c/a.key")
    db.guardar_paths_de_servicio("acme", "wscpe", "homologacion")  # no hace nada
    assert db.paths_de_servicio("acme", "wscpe", "homologacion") == ("/c/a.crt", "/c/a.key")


def test_una_sola_fila_por_empresa_servicio_ambiente(base):
    for _ in range(3):
        db.guardar_paths_de_servicio("acme", "wscpe", "produccion", certificado_path="/x.crt")
    assert len(db.listar_de_empresa("acme")) == 1


def test_los_ambientes_los_servicios_y_las_empresas_no_se_mezclan(base):
    db.guardar_paths_de_servicio("acme", "wscpe", "homologacion", certificado_path="/h.crt")
    db.guardar_paths_de_servicio("acme", "wscpe", "produccion", certificado_path="/p.crt")
    db.guardar_paths_de_servicio("acme", "otro", "produccion", certificado_path="/o.crt")
    db.guardar_paths_de_servicio("beta", "wscpe", "produccion", certificado_path="/b.crt")

    assert db.paths_de_servicio("acme", "wscpe", "homologacion")[0] == "/h.crt"
    assert db.paths_de_servicio("acme", "wscpe", "produccion")[0] == "/p.crt"
    assert db.paths_de_servicio("acme", "otro", "produccion")[0] == "/o.crt"
    assert db.paths_de_servicio("beta", "wscpe", "produccion")[0] == "/b.crt"
    assert db.paths_de_servicio("beta", "wscpe", "homologacion") == ("", "")


def test_un_ambiente_raro_no_tiene_credenciales_y_no_se_puede_guardar(base):
    """Nunca cae a producción: ni al leer ni al escribir."""
    db.guardar_paths_de_servicio("acme", "wscpe", "produccion", certificado_path="/p.crt")
    assert db.paths_de_servicio("acme", "wscpe", "testing") == ("", "")
    assert db.paths_de_servicio("acme", "wscpe", "") == ("", "")
    with pytest.raises(ValueError, match="Ambiente desconocido"):
        db.guardar_paths_de_servicio("acme", "wscpe", "testing", certificado_path="/x.crt")
    assert len(db.listar_de_empresa("acme")) == 1


def test_el_ambiente_se_normaliza(base):
    db.guardar_paths_de_servicio("acme", "wscpe", "  Homologacion ", certificado_path="/h.crt")
    assert db.paths_de_servicio("acme", "wscpe", "homologacion")[0] == "/h.crt"


def test_borrar_saca_un_ambiente_y_deja_el_otro(base):
    db.guardar_paths_de_servicio("acme", "wscpe", "homologacion", certificado_path="/h.crt")
    db.guardar_paths_de_servicio("acme", "wscpe", "produccion", certificado_path="/p.crt")
    db.borrar_paths_de_servicio("acme", "wscpe", "homologacion")
    assert db.paths_de_servicio("acme", "wscpe", "homologacion") == ("", "")
    assert db.paths_de_servicio("acme", "wscpe", "produccion")[0] == "/p.crt"
    db.borrar_paths_de_servicio("acme", "wscpe", "homologacion")  # idempotente


def test_la_base_rechaza_un_duplicado_y_un_ambiente_inventado(base):
    base.execute("INSERT INTO arca_credenciales_servicio (empresa, servicio, ambiente) "
                 "VALUES ('a','wscpe','produccion')")
    base.commit()
    with pytest.raises(Exception):  # noqa: B017 - SQLite e IntegrityError de psycopg difieren
        base.execute("INSERT INTO arca_credenciales_servicio (empresa, servicio, ambiente) "
                     "VALUES ('a','wscpe','produccion')")
    base.rollback()
    with pytest.raises(Exception):  # noqa: B017
        base.execute("INSERT INTO arca_credenciales_servicio (empresa, servicio, ambiente) "
                     "VALUES ('a','wscpe','pruebas')")
    base.rollback()


def test_no_toca_la_facturacion(base):
    """El almacén nuevo y `arca_config` son mundos aparte: guardar y borrar acá no
    mueve una columna de la fila de facturación."""
    db_arca.crear_arca_config("acme", "20000000001", 1, "/f.key", "/f.crt", "produccion")
    db_arca.actualizar_arca_config("acme", certificado_path_homologacion="/fh.crt",
                                   clave_path_homologacion="/fh.key")
    antes = db_arca.obtener_arca_config("acme")

    db.guardar_paths_de_servicio("acme", "wscpe", "produccion",
                                 certificado_path="/s.crt", clave_path="/s.key")
    db.borrar_paths_de_servicio("acme", "wscpe", "produccion")

    assert db_arca.obtener_arca_config("acme") == antes
    assert db_arca.paths_de(antes, "produccion") == ("/f.crt", "/f.key")


# -- El rescate de una ruta vieja ------------------------------------------


def test_el_rescate_encuentra_el_archivo_movido_de_volumen(base, tmp_path, monkeypatch):
    certs = tmp_path / "certs"
    certs.mkdir()
    monkeypatch.setattr(config_manager, "CERTS_DIR", str(certs))
    (certs / "wscpe-h.crt").write_bytes(b"x")
    (certs / "wscpe-h.key").write_bytes(b"x")
    db.guardar_paths_de_servicio("acme", "wscpe", "homologacion",
                                 certificado_path="/volumen/viejo/wscpe-h.crt",
                                 clave_path="/volumen/viejo/wscpe-h.key")
    assert arca_credenciales.paths_en_disco_de_servicio("acme", "wscpe", "homologacion") == (
        str(certs / "wscpe-h.crt"), str(certs / "wscpe-h.key"))


def test_el_rescate_no_inventa_un_par_que_no_existe(base, tmp_path, monkeypatch):
    """Sin fila no hay a qué caer: ni siquiera si hay archivos sueltos en el volumen
    (y mucho menos los de la facturación de producción)."""
    certs = tmp_path / "certs"
    certs.mkdir()
    monkeypatch.setattr(config_manager, "CERTS_DIR", str(certs))
    cert_prod, clave_prod = config_manager.ARCHIVOS_POR_AMBIENTE["produccion"]
    (certs / cert_prod).write_bytes(b"x")
    (certs / clave_prod).write_bytes(b"x")
    assert arca_credenciales.paths_en_disco_de_servicio("acme", "wscpe", "produccion") == ("", "")
    assert arca_credenciales.paths_en_disco_de_servicio("acme", "wscpe", "testing") == ("", "")


# -- La migración ------------------------------------------------------------


def _alembic(destino: str, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=RAIZ, env={**os.environ, "DATABASE_URL": destino},
        capture_output=True, text=True,
    )


def _tablas(ruta: str) -> set[str]:
    c = sqlite3.connect(ruta)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def _version(ruta: str) -> str:
    c = sqlite3.connect(ruta)
    try:
        return c.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        c.close()


def test_la_0022_es_el_head(tmp_path):
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini")))
    assert script.get_current_head() == REVISION
    assert script.get_revision(REVISION).down_revision == ANTERIOR


def test_la_migracion_sube_baja_y_vuelve_a_subir_sqlite(tmp_path):
    destino = str(tmp_path / "mig.db")
    assert _alembic(destino, "upgrade", "head").returncode == 0
    assert "arca_credenciales_servicio" in _tablas(destino)
    assert _version(destino) == REVISION

    # Se carga algo en la facturación para medir que bajar no la toca.
    c = sqlite3.connect(destino)
    c.execute("INSERT INTO arca_config (empresa, cuit, punto_venta, clave_path, certificado_path)"
              " VALUES ('acme','20000000001',1,'/f.key','/f.crt')")
    c.commit()
    c.close()

    r = _alembic(destino, "downgrade", "-1")
    assert r.returncode == 0, r.stderr
    assert "arca_credenciales_servicio" not in _tablas(destino)
    assert _version(destino) == ANTERIOR
    c = sqlite3.connect(destino)
    assert c.execute("SELECT certificado_path FROM arca_config").fetchone()[0] == "/f.crt"
    c.close()

    assert _alembic(destino, "upgrade", "head").returncode == 0
    assert "arca_credenciales_servicio" in _tablas(destino)
    assert _version(destino) == REVISION


def test_subir_dos_veces_no_pisa_lo_cargado_sqlite(tmp_path):
    destino = str(tmp_path / "idem.db")
    assert _alembic(destino, "upgrade", "head").returncode == 0
    c = sqlite3.connect(destino)
    c.execute("INSERT INTO arca_credenciales_servicio (empresa, servicio, ambiente, certificado_path)"
              " VALUES ('acme','wscpe','produccion','/p.crt')")
    c.commit()
    c.execute("UPDATE alembic_version SET version_num=?", (ANTERIOR,))
    c.commit()
    c.close()
    assert _alembic(destino, "upgrade", "head").returncode == 0
    c = sqlite3.connect(destino)
    assert c.execute("SELECT certificado_path FROM arca_credenciales_servicio").fetchone()[0] == "/p.crt"
    c.close()


def test_la_migracion_sube_baja_y_vuelve_a_subir_postgres(bases):
    import psycopg

    url = bases.nueva()

    def existe() -> bool:
        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1)) as c:
            return c.execute(
                "SELECT to_regclass('public.arca_credenciales_servicio') IS NOT NULL").fetchone()[0]

    assert _alembic(url, "upgrade", "head").returncode == 0
    assert existe()
    r = _alembic(url, "downgrade", "-1")
    assert r.returncode == 0, r.stderr
    assert not existe()
    assert ANTERIOR in _alembic(url, "current").stdout
    assert _alembic(url, "upgrade", "head").returncode == 0
    assert existe()
    assert REVISION in _alembic(url, "current").stdout
