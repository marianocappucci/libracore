"""`list-backups` y `restore-db`, contra una instancia migrada a PostgreSQL.

**El hueco que cierran estos tests.** `cmd_backup` distingue motor desde el
2026-08-10 --PostgreSQL va a `pg_dump`/`.dump`, SQLite a una copia WAL-safe
`.db`-- pero los dos comandos que *leen* esos respaldos se habian quedado con
el glob de `*.db`. Contra una instancia bien respaldada:

- `list-backups` decia *"Sin backups de DB"*, que es un mensaje tranquilizador
  al reves: el respaldo existia y el que preguntaba se iba creyendo que no.
- `restore-db` copiaba un `.db` sobre `data/<db>.db` --un archivo que en esa
  instancia no lee nadie--, imprimia `[OK] DB restaurada` y no cambiaba **un
  solo dato**. Es el caso peor: no falla, miente.

Cada test trae su control por el lado SQLite, para que el verde no pueda
deberse a que el comando dejo de hacer nada en los dos casos.
"""
import os
import sqlite3
import time

import pytest

from libracore.provisioning import panel_admin as pa

from .test_panel_admin import _mkclient, cfg, fake_docker  # noqa: F401 - fixtures


@pytest.fixture
def instancia(cfg):                                        # noqa: F811
    """Un cliente con un respaldo de cada formato en su carpeta."""
    cdir = _mkclient(cfg, "acme", db_content=b"SQLite format 3\x00" + b"\x00" * 200)
    bdir = cdir / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "testprod_20260914_030000.dump").write_bytes(b"PGDMP" + b"\x00" * 100)
    return cdir


def _como_postgres(monkeypatch):
    monkeypatch.setattr(pa, "_urls_postgres_del_contenedor",
                        lambda c: ["postgresql://u:p@db:5432/acme"])


def _como_sqlite(monkeypatch):
    monkeypatch.setattr(pa, "_urls_postgres_del_contenedor", lambda c: [])


def test_list_backups_ve_el_dump_de_postgres(instancia, monkeypatch, capsys):
    _como_postgres(monkeypatch)

    pa.cmd_list_backups("acme")

    salida = capsys.readouterr().out
    assert "Sin backups de DB" not in salida, salida
    assert "testprod_20260914_030000.dump" in salida, salida
    assert "PostgreSQL" in salida, salida


def test_list_backups_avisa_cuando_ninguno_es_del_motor(cfg, monkeypatch, capsys):  # noqa: F811
    """Una instancia PostgreSQL cuyo unico respaldo es el `.db` de antes de migrar.

    Lo peor que puede hacer el listado ahi es mostrarlo sin decir nada: es
    exactamente el archivo que alguien elegiria para restaurar.
    """
    cdir = _mkclient(cfg, "acme", db_content=b"SQLite format 3\x00")
    bdir = cdir / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "testprod_20260801_030000.db").write_bytes(b"SQLite format 3\x00")
    _como_postgres(monkeypatch)

    pa.cmd_list_backups("acme")

    salida = capsys.readouterr().out
    assert "testprod_20260801_030000.db" in salida, salida
    assert "[AVISO]" in salida, salida


def test_list_backups_en_sqlite_sigue_listando_el_db(instancia, monkeypatch, capsys):
    """El control: por el lado SQLite el comando sigue haciendo lo de siempre."""
    (instancia / "backups" / "testprod_20260910_030000.db").write_bytes(
        b"SQLite format 3\x00"
    )
    _como_sqlite(monkeypatch)

    pa.cmd_list_backups("acme")

    salida = capsys.readouterr().out
    assert "testprod_20260910_030000.db" in salida, salida
    assert "SQLite" in salida, salida
    assert "[AVISO]" not in salida, salida


def test_restore_db_se_niega_contra_postgres(instancia, cfg, monkeypatch, capsys):  # noqa: F811
    """🔴 El defecto: restauraba un `.db` sobre una instancia que no lo lee."""
    _como_postgres(monkeypatch)
    bdir = instancia / "backups"
    (bdir / "testprod_20260801_030000.db").write_bytes(b"SQLite format 3\x00" + b"VIEJO")
    destino = instancia / "data" / cfg.db_filename
    antes = destino.read_bytes()

    def no_preguntar(*a, **k):
        raise AssertionError("no tiene que llegar a preguntar nada")

    monkeypatch.setattr("builtins.input", no_preguntar)

    pa.cmd_restore_db("acme")

    salida = capsys.readouterr().out
    assert "[ERROR]" in salida, salida
    assert "pg_restore" in salida, salida
    # Lo que importa: no toco la base ni dijo que la habia restaurado.
    assert destino.read_bytes() == antes
    assert "restaurada" not in salida.lower(), salida


def test_restore_db_en_sqlite_sigue_restaurando(instancia, cfg, monkeypatch, capsys):  # noqa: F811
    """El control del anterior.

    Sin esto, un `cmd_restore_db` que se negara **siempre** pasaria el test de
    arriba igual, y el comando quedaria roto para las instancias que si sabe
    restaurar.
    """
    _como_sqlite(monkeypatch)
    bdir = instancia / "backups"
    respaldo = bdir / "testprod_20260910_030000.db"
    # Una base SQLite de verdad: el comando valida magic bytes e integridad, y
    # un archivo de bytes sueltos no probaria el camino completo.
    conn = sqlite3.connect(str(respaldo))
    conn.execute("CREATE TABLE marca (valor TEXT)")
    conn.execute("INSERT INTO marca VALUES ('el-respaldo')")
    conn.commit()
    conn.close()
    bueno = respaldo.read_bytes()
    destino = instancia / "data" / cfg.db_filename
    destino.write_bytes(b"otra cosa")
    monkeypatch.setattr("builtins.input", lambda *a, **k: "1")

    pa.cmd_restore_db("acme")

    salida = capsys.readouterr().out
    assert "[OK] DB restaurada" in salida, salida
    assert destino.read_bytes() == bueno


def test_el_numero_elegido_es_el_de_la_fila_listada(instancia, cfg, monkeypatch, capsys):  # noqa: F811
    """La lista que se imprime y la que se indexa tienen que ser la misma.

    Al pasar el listado a los dos formatos, `restore-db` seguia indexando sobre
    los `.db` solos: el operador veia el `.dump` en la fila 1 y elegir "1" le
    restauraba otro archivo.
    """
    _como_sqlite(monkeypatch)
    bdir = instancia / "backups"
    viejo = bdir / "testprod_20260101_030000.db"
    viejo.write_bytes(b"SQLite format 3\x00" + b"V" * 50)
    # El `.dump` es el mas nuevo, asi que tiene que quedar en la fila 1.
    ahora = time.time()
    os.utime(bdir / "testprod_20260914_030000.dump", (ahora, ahora))
    os.utime(viejo, (ahora - 86400, ahora - 86400))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "1")

    pa.cmd_restore_db("acme")

    salida = capsys.readouterr().out
    # Elegir la fila 1 toma el `.dump`, y ahi el validador lo rechaza por lo
    # que es. Lo que NO puede pasar es que "1" agarre el `.db` de otra fila.
    assert "no es una base de datos SQLite" in salida, salida
    assert "pg_restore" in salida, salida
