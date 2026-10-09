"""La FCE con varios CBU (ADR-040): los helpers de `db.arca_config` y la migración `0023`.

`fce_cbu` se conserva como el CBU predeterminado; `fce_cbus` es la lista JSON
`[{"cbu", "alias", "etiqueta"}]`. Lo que se mide acá es que quien lee una config
legada (sin lista) o con la lista rota siga viendo un CBU, y que elegir uno que no
está cargado no pase.
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from libracore.db import arca_config as db
from libracore.db import core
from libracore.db.schema import init_core_schema

RAIZ = Path(__file__).resolve().parents[2]
REVISION = "0023_fce_varios_cbu"
ANTERIOR = "0022_credenciales_por_servicio"

A, B, C = "1" * 22, "2" * 22, "3" * 22


def _config(lista=None, **extra):
    cfg = {"fce_cbu": "", "fce_cbus": json.dumps(lista) if lista is not None else ""}
    cfg.update(extra)
    return cfg


# ── cbus_fce ────────────────────────────────────────────────────────────────


def test_cbus_fce_devuelve_la_lista_con_las_tres_claves():
    cfg = _config([{"cbu": A, "alias": "Caja.Chica", "etiqueta": " Santander "}, {"cbu": B}])
    assert db.cbus_fce(cfg) == [
        {"cbu": A, "alias": "caja.chica", "etiqueta": "Santander"},
        {"cbu": B, "alias": "", "etiqueta": ""},
    ]


def test_cbus_fce_sin_lista_usa_el_fce_cbu_legado():
    assert db.cbus_fce(_config(fce_cbu=A)) == [{"cbu": A, "alias": "", "etiqueta": ""}]


def test_cbus_fce_sin_nada_es_vacia():
    assert db.cbus_fce(None) == []
    assert db.cbus_fce({}) == []
    assert db.cbus_fce(_config()) == []


@pytest.mark.parametrize("roto", ["{no es json", "null", '{"cbu": "%s"}' % A, "[1, 2]", '"texto"'])
def test_cbus_fce_con_el_json_roto_es_vacia_y_no_levanta(roto):
    assert db.cbus_fce({"fce_cbu": "", "fce_cbus": roto}) == []
    # y con predeterminado cargado, cae a la lista de uno
    assert db.cbus_fce({"fce_cbu": A, "fce_cbus": roto}) == [{"cbu": A, "alias": "", "etiqueta": ""}]


def test_cbus_fce_normaliza_el_cbu_guardado():
    cfg = _config([{"cbu": "1111 1111-1111 1111-1111 11"}])
    assert db.cbus_fce(cfg)[0]["cbu"] == A


# ── cbu_para_fce ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pedido", [None, "", "   "])
def test_sin_pedido_sale_el_predeterminado(pedido):
    cfg = _config([{"cbu": A}, {"cbu": B}], fce_cbu=B)
    assert db.cbu_para_fce(cfg, pedido) == B


def test_sin_predeterminado_sale_el_primero_de_la_lista():
    assert db.cbu_para_fce(_config([{"cbu": A}, {"cbu": B}]), None) == A


def test_sin_nada_cargado_el_predeterminado_es_vacio():
    assert db.cbu_para_fce(None, None) == ""
    assert db.cbu_para_fce(_config(), "") == ""


def test_la_config_legada_sin_lista_sigue_dando_su_cbu():
    cfg = _config(fce_cbu=A)
    assert db.cbu_para_fce(cfg, None) == A
    assert db.cbu_para_fce(cfg, A) == A


def test_el_elegido_se_normaliza_antes_de_buscarlo():
    cfg = _config([{"cbu": A}, {"cbu": B}], fce_cbu=A)
    assert db.cbu_para_fce(cfg, " 2222 2222-2222 2222-2222 22 ") == B


def test_se_puede_elegir_por_alias_y_devuelve_el_cbu():
    cfg = _config([{"cbu": A, "alias": "cuenta.uno"}, {"cbu": B, "alias": "cuenta.dos"}], fce_cbu=A)
    assert db.cbu_para_fce(cfg, "cuenta.dos") == B
    assert db.cbu_para_fce(cfg, "  CUENTA.DOS ") == B


def test_el_alias_de_una_fila_sin_alias_no_matchea_con_vacio_ni_con_otro():
    cfg = _config([{"cbu": A}, {"cbu": B, "alias": "cuenta.dos"}], fce_cbu=A)
    with pytest.raises(ValueError):
        db.cbu_para_fce(cfg, "cuenta.tres")


@pytest.mark.parametrize("pedido", [C, "9" * 22, "no.existe", "123"])
def test_un_elegido_fuera_de_la_lista_levanta(pedido):
    cfg = _config([{"cbu": A}, {"cbu": B}], fce_cbu=A)
    with pytest.raises(ValueError, match="no está entre los cargados en la configuración de ARCA"):
        db.cbu_para_fce(cfg, pedido)


def test_sin_nada_cargado_cualquier_elegido_levanta():
    with pytest.raises(ValueError):
        db.cbu_para_fce(None, A)


# ── cuenta_fce / cuenta_de_cobro ────────────────────────────────────────────


def test_cuenta_fce_devuelve_la_elegida_o_la_predeterminada():
    cfg = _config([{"cbu": A, "etiqueta": "Uno"}, {"cbu": B, "alias": "cuenta.dos"}], fce_cbu=A)
    assert db.cuenta_fce(cfg) == {"cbu": A, "alias": "", "etiqueta": "Uno"}
    assert db.cuenta_fce(cfg, B) == {"cbu": B, "alias": "cuenta.dos", "etiqueta": ""}
    assert db.cuenta_fce(None) is None


def test_cuenta_fce_de_un_cbu_que_ya_no_esta_en_la_lista_no_lo_pierde():
    assert db.cuenta_fce(_config([{"cbu": A}], fce_cbu=A), C) == {"cbu": C, "alias": "", "etiqueta": ""}


def test_cuenta_de_cobro_solo_existe_para_una_fce():
    cfg = _config([{"cbu": A}], fce_cbu=A)
    assert db.cuenta_fce(cfg)["cbu"] == A
    assert db.cuenta_de_cobro({"tipo_comprobante": 1, "fce_cbu": A}) is None
    assert db.cuenta_de_cobro({"tipo_comprobante": None}) is None


# ── actualizar_arca_config ──────────────────────────────────────────────────


@pytest.fixture
def base(tmp_path):
    core.configure(db_path=str(tmp_path / "fce.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    db.crear_arca_config("acme", "20111111119", 1, "", "")
    yield conn
    conn.close()
    core._db_path = None


def test_actualizar_guarda_la_lista_y_none_no_la_toca(base):
    lista = [{"cbu": A, "alias": "cuenta.uno", "etiqueta": "Galicia ñandú"}, {"cbu": B, "alias": "", "etiqueta": ""}]
    db.actualizar_arca_config("acme", fce_cbu=A, fce_cbus=lista)
    assert db.cbus_fce(db.obtener_arca_config("acme")) == lista
    # `None` = no la toqués (un PUT que no conoce la lista no la borra)
    db.actualizar_arca_config("acme", punto_venta=7)
    assert db.cbus_fce(db.obtener_arca_config("acme")) == lista
    # lista vacía = borrala, y queda como '' y no como '[]'
    db.actualizar_arca_config("acme", fce_cbu="", fce_cbus=[])
    assert db.obtener_arca_config("acme")["fce_cbus"] == ""


# ── La migración ────────────────────────────────────────────────────────────


def _alembic(destino, *args):
    env = {**os.environ, "DATABASE_URL": destino}
    return subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic.ini", *args], cwd=RAIZ, env=env,
                          capture_output=True, text=True)


def _base_en_la_anterior(destino):
    """Una base como la que dejaba la `0022`: sin la columna nueva, con filas de cada forma."""
    assert _alembic(destino, "upgrade", ANTERIOR).returncode == 0
    conn = sqlite3.connect(destino)
    # La `0022` ya llama a la versión VIVA de `init_core_schema`, que trae la columna: se la quita
    # para reconstruir lo que había antes de que existiera.
    conn.execute("ALTER TABLE arca_config DROP COLUMN fce_cbus")
    conn.execute("ALTER TABLE comprobantes_pendientes DROP COLUMN fce_cbu")
    conn.execute(
        "INSERT INTO comprobantes_pendientes (origen_producto, origen_tipo, origen_id, cliente_razon, items, total) "
        "VALUES ('libracargo', 'pre_factura', '1', 'Cliente SA', '[]', 0)")
    conn.executemany(
        "INSERT INTO arca_config (empresa, cuit, punto_venta, clave_path, certificado_path, fce_cbu) "
        "VALUES (?, '20111111119', 1, '', '', ?)",
        [("con-cbu", A), ("sin-cbu", ""), ("otra", B)],
    )
    conn.commit()
    conn.close()


def test_la_migracion_rellena_la_lista_desde_el_cbu_que_ya_estaba(tmp_path):
    destino = str(tmp_path / "vieja.db")
    _base_en_la_anterior(destino)
    resultado = _alembic(destino, "upgrade", REVISION)
    assert resultado.returncode == 0, resultado.stderr

    conn = sqlite3.connect(destino)
    conn.row_factory = sqlite3.Row
    filas = {r["empresa"]: dict(r) for r in conn.execute("SELECT * FROM arca_config")}
    conn.close()
    assert json.loads(filas["con-cbu"]["fce_cbus"]) == [{"cbu": A, "alias": "", "etiqueta": ""}]
    assert filas["con-cbu"]["fce_cbu"] == A, "el predeterminado no se toca"
    assert filas["sin-cbu"]["fce_cbus"] == "", "sin CBU no hay nada que rellenar"
    assert db.cbus_fce(filas["con-cbu"]) == [{"cbu": A, "alias": "", "etiqueta": ""}]


def test_la_migracion_agrega_la_cuenta_a_la_pre_factura_y_las_que_estaban_quedan_en_null(tmp_path):
    destino = str(tmp_path / "pre_factura.db")
    _base_en_la_anterior(destino)
    assert _alembic(destino, "upgrade", REVISION).returncode == 0
    conn = sqlite3.connect(destino)
    assert conn.execute("SELECT fce_cbu FROM comprobantes_pendientes").fetchall() == [(None,)]
    conn.close()


def test_la_migracion_no_pisa_una_lista_ya_armada(tmp_path):
    destino = str(tmp_path / "con_lista.db")
    _base_en_la_anterior(destino)
    assert _alembic(destino, "upgrade", REVISION).returncode == 0
    conn = sqlite3.connect(destino)
    propia = json.dumps([{"cbu": B, "alias": "cuenta.dos", "etiqueta": "la mía"}])
    conn.execute("UPDATE arca_config SET fce_cbus=? WHERE empresa='otra'", (propia,))
    conn.commit()
    conn.close()
    # Volver a correr el relleno (la revisión se estampa para repetirla).
    assert _alembic(destino, "stamp", ANTERIOR).returncode == 0
    assert _alembic(destino, "upgrade", REVISION).returncode == 0
    conn = sqlite3.connect(destino)
    assert conn.execute("SELECT fce_cbus FROM arca_config WHERE empresa='otra'").fetchone()[0] == propia
    conn.close()


def test_la_0023_cuelga_de_la_0022():
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
