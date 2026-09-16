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

from libracore import provisioning
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


# ---------------------------------------------------------------------------
# El hueco que quedo abierto el 2026-09-15 y se vio en el VPS el 2026-09-16:
# los respaldos que corren de verdad NO estan en `<cliente>/backups/`.
# ---------------------------------------------------------------------------

def _con_backup_zip(actual, valor: bool):
    """Reconfigura el producto cambiando solo `backup_zip`.

    `ProductConfig` es un dataclass frozen a proposito, asi que no se muta: se
    vuelve a llamar a `configure` con los mismos valores. Reconfigurar es lo que
    hace de verdad un producto al arrancar, asi que el test pasa por el mismo
    camino que la realidad.
    """
    provisioning.configure(
        product_name=actual.product_name, image_name=actual.image_name,
        container_prefix=actual.container_prefix, db_filename=actual.db_filename,
        repo_root=actual.repo_root, base_port=actual.base_port, backup_zip=valor,
    )
    return provisioning.get_config()


@pytest.fixture
def con_zips(cfg, monkeypatch):                                    # noqa: F811
    """Una instancia como las reales: `backup_zip`, con los ZIP en data/backups.

    Ademas deja en la carpeta vieja un `.dump` **mas antiguo**, que es
    exactamente la trampa: el listado lo mostraba como si fuera el respaldo
    vigente mientras el ZIP de anoche quedaba invisible.
    """
    cdir = _mkclient(cfg, "acme", db_content=b"SQLite format 3\x00")
    viejo = cdir / "backups"
    viejo.mkdir(parents=True, exist_ok=True)
    (viejo / "testprod_20260812_073353.dump").write_bytes(b"PGDMP" + b"\x00" * 50)
    zdir = cdir / "data" / "backups"
    zdir.mkdir(parents=True, exist_ok=True)
    (zdir / "backup_automatico_20260916_040001.zip").write_bytes(b"PK" + b"\x00" * 80)
    ahora = time.time()
    os.utime(zdir / "backup_automatico_20260916_040001.zip", (ahora, ahora))
    os.utime(viejo / "testprod_20260812_073353.dump", (ahora - 35 * 86400, ahora - 35 * 86400))
    _con_backup_zip(cfg, True)
    return cdir


def test_list_backups_ve_el_zip_de_data_backups(con_zips, monkeypatch, capsys):
    """🔴 El defecto: el ZIP de anoche no aparecia, y el .dump de hace un mes si."""
    _como_postgres(monkeypatch)

    pa.cmd_list_backups("acme")

    salida = capsys.readouterr().out
    assert "backup_automatico_20260916_040001.zip" in salida, salida
    # Y el viejo tiene que quedar MARCADO, no simplemente listado al lado.
    lineas = [l for l in salida.splitlines() if "testprod_20260812" in l]
    assert lineas and lineas[0].lstrip().startswith("!"), salida


def test_el_zip_queda_primero_por_ser_el_mas_nuevo(con_zips, monkeypatch, capsys):
    """El orden importa: el numero 1 es el que elige quien restaura."""
    _como_postgres(monkeypatch)

    pa.cmd_list_backups("acme")

    filas = [l for l in capsys.readouterr().out.splitlines() if ".zip" in l or ".dump" in l]
    assert ".zip" in filas[0], filas


def test_restore_db_manda_a_la_pantalla_cuando_el_respaldo_es_zip(con_zips, monkeypatch, capsys):
    """Negarse no alcanza: tiene que decir cual ES el camino que restaura."""
    _como_postgres(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no tiene que preguntar")))

    pa.cmd_restore_db("acme")

    salida = capsys.readouterr().out
    assert "[ERROR]" in salida, salida
    assert "Configuracion" in salida, salida
    assert "pg_restore" not in salida, salida


def test_sin_backup_zip_el_formato_vivo_sigue_siendo_el_dump(con_zips, cfg, monkeypatch, capsys):  # noqa: F811
    """El control: apagando `backup_zip`, lo esperado vuelve a ser el `.dump`.

    Sin esto, un `_motor_de_la_instancia` que devolviera `.zip` **siempre**
    pasaria los tests de arriba igual, y romperia a las instancias que no usan
    ese camino.
    """
    _con_backup_zip(cfg, False)
    _como_postgres(monkeypatch)

    pa.cmd_list_backups("acme")

    salida = capsys.readouterr().out
    lineas = [l for l in salida.splitlines() if "testprod_20260812" in l]
    assert lineas and not lineas[0].lstrip().startswith("!"), salida
    lineas_zip = [l for l in salida.splitlines() if ".zip" in l and "AVISO" not in l]
    assert lineas_zip and lineas_zip[0].lstrip().startswith("!"), salida
