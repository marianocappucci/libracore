"""El backup del cron armando el MISMO ZIP que la pantalla del producto.

Lo que fijan, en orden de lo que se pierde sin que se note:

1. 🔴 Que el ZIP del cron **traiga la base**. El defecto que este camino vino a
   cerrar es que el `tar.gz` empaquetaba `data/` mientras el dump se escribia en
   `clientes/<slug>/backups/`, afuera: el tar salia de tamano normal, con los
   logos, y sin una fila. Medido sobre las nueve instancias del VPS el
   2026-08-12.
2. Que el ZIP **no se lleve adentro a los backups anteriores**, que es lo que
   pasaria empaquetando `data/` entero ahora que el ZIP vive ahi.
3. Que el nombre del dump coincida con el que declara el producto, porque de eso
   depende que el ZIP del cron se pueda restaurar desde la pantalla.
4. Que sin el flag el comportamiento sea exactamente el de antes — Contalibra y
   Restolibra todavia dependen de el.
"""
import json
import subprocess
import zipfile

import pytest

from libracore import provisioning
from libracore.provisioning import panel_admin as pa
from libracore.respaldo import BackupInvalido

from .test_panel_admin import _mkclient, _reset_config, cfg, fake_docker  # noqa: F401


PGDUMP_FALSO = b"PGDMP" + b"\x00" * 64


@pytest.fixture
def cfg_zip(tmp_path, fake_docker):  # noqa: F811
    """Un producto con `backup_zip=True`, que es el camino nuevo."""
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo_root, base_port=9000, backup_zip=True,
    )
    return provisioning.get_config()


def _cliente_con_datos(config, slug="cliente", db_content=b""):
    """Un cliente con logos y un backup viejo ya en `data/backups/`.

    El parametro se llama `config` y no `cfg` a proposito: `cfg` es el nombre de
    una fixture importada arriba, y sombrearla acá la deja sin usar sin que se
    note.
    """
    cdir = _mkclient(config, slug, db_content=db_content)
    (cdir / "data" / "logos").mkdir()
    (cdir / "data" / "logos" / "logo.png").write_bytes(b"PNG-falso")
    (cdir / "data" / "backups").mkdir()
    (cdir / "data" / "backups" / "backup_viejo.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 500)
    return cdir


def _sin_postgres(monkeypatch):
    monkeypatch.setattr(pa, "_urls_postgres_del_contenedor", lambda c: [])


def _con_postgres(monkeypatch, urls):
    monkeypatch.setattr(pa, "_urls_postgres_del_contenedor", lambda c: urls)

    def _dump(url, destino):
        # Lo que hace el de verdad: corre `pg_dump` dentro del sidecar y se
        # trae el archivo. Aca alcanza con dejar algo con la firma correcta.
        from pathlib import Path

        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        Path(destino).write_bytes(PGDUMP_FALSO)

    monkeypatch.setattr(pa, "_dump_postgres_por_docker", _dump)


# ── el ZIP trae lo que tiene que traer ────────────────────────────────────────

def test_zip_de_postgres_trae_la_base_y_los_logos(cfg_zip, monkeypatch):
    _cliente_con_datos(cfg_zip)
    _con_postgres(monkeypatch, ["postgresql://u:p@side:5432/testprod"])

    pa.cmd_backup("cliente", quiet=True)

    zips = sorted((cfg_zip.clientes_dir / "cliente" / "data" / "backups").glob("backup_automatico_*.zip"))
    assert len(zips) == 1, "tiene que quedar exactamente un backup nuevo"
    with zipfile.ZipFile(zips[0]) as z:
        nombres = z.namelist()
    # La base, con el nombre que declara el producto.
    assert "bases/testprod.dump" in nombres
    assert "datos/logos/logo.png" in nombres


def test_zip_de_sqlite_trae_la_base(cfg_zip, monkeypatch):
    import sqlite3

    cdir = _cliente_con_datos(cfg_zip)
    conn = sqlite3.connect(str(cdir / "data" / cfg_zip.db_filename))
    conn.execute("CREATE TABLE cosas (nombre TEXT)")
    conn.execute("INSERT INTO cosas VALUES ('una fila')")
    conn.commit()
    conn.close()
    _sin_postgres(monkeypatch)

    pa.cmd_backup("cliente", quiet=True)

    zips = sorted((cdir / "data" / "backups").glob("backup_automatico_*.zip"))
    with zipfile.ZipFile(zips[0]) as z:
        assert "bases/testprod.db" in z.namelist()
        assert z.getinfo("bases/testprod.db").file_size > 0


def test_el_zip_no_se_lleva_adentro_los_backups_anteriores(cfg_zip, monkeypatch):
    """Si `backups/` entrara al ZIP, cada backup pesaria como los diez previos."""
    _cliente_con_datos(cfg_zip)
    _con_postgres(monkeypatch, ["postgresql://u:p@side:5432/testprod"])

    pa.cmd_backup("cliente", quiet=True)

    zips = sorted((cfg_zip.clientes_dir / "cliente" / "data" / "backups").glob("backup_automatico_*.zip"))
    with zipfile.ZipFile(zips[0]) as z:
        adentro = z.namelist()
    assert not [n for n in adentro if "backups/" in n], adentro


def test_carpeta_nueva_de_datos_entra_sola(cfg_zip, monkeypatch):
    """Sin lista fija: el dia que un producto agrega una carpeta, entra."""
    cdir = _cliente_con_datos(cfg_zip)
    (cdir / "data" / "documentos_clinicos").mkdir()
    (cdir / "data" / "documentos_clinicos" / "estudio.pdf").write_bytes(b"%PDF-falso")
    _con_postgres(monkeypatch, ["postgresql://u:p@side:5432/testprod"])

    pa.cmd_backup("cliente", quiet=True)

    zips = sorted((cdir / "data" / "backups").glob("backup_automatico_*.zip"))
    with zipfile.ZipFile(zips[0]) as z:
        assert "datos/documentos_clinicos/estudio.pdf" in z.namelist()


def test_las_dos_bases_de_un_producto_con_base_separada(cfg_zip, monkeypatch):
    """Gestiolibra y MedLibra tienen dos. Con una sola el backup no se restaura."""
    _cliente_con_datos(cfg_zip)
    _con_postgres(monkeypatch, [
        "postgresql://u:p@side:5432/testprod",
        "postgresql://u:p@side:5432/testprod_core",
    ])

    pa.cmd_backup("cliente", quiet=True)

    zips = sorted((cfg_zip.clientes_dir / "cliente" / "data" / "backups").glob("backup_automatico_*.zip"))
    with zipfile.ZipFile(zips[0]) as z:
        bases = sorted(n for n in z.namelist() if n.startswith("bases/"))
    assert bases == ["bases/testprod.dump", "bases/testprod_core.dump"]


# ── el falso verde que este camino tiene que hacer ruidoso ────────────────────

def test_un_dump_vacio_hace_fallar_el_backup(cfg_zip, monkeypatch):
    """Un archivo con nombre de backup y 0 bytes es peor que ninguno: la
    rotacion lo cuenta y alguien puede creer que tiene una copia."""
    _cliente_con_datos(cfg_zip)
    monkeypatch.setattr(pa, "_urls_postgres_del_contenedor",
                        lambda c: ["postgresql://u:p@side:5432/testprod"])

    def _dump_vacio(url, destino):
        from pathlib import Path

        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        Path(destino).write_bytes(b"")

    monkeypatch.setattr(pa, "_dump_postgres_por_docker", _dump_vacio)

    with pytest.raises(BackupInvalido) as e:
        pa.cmd_backup("cliente", quiet=True)
    assert "0 bytes" in str(e.value)


# ── sin el flag, nada cambia ──────────────────────────────────────────────────

def test_sin_el_flag_sigue_haciendo_el_tar_gz(cfg, monkeypatch):  # noqa: F811
    """Contalibra y Restolibra todavia dependen de este camino."""
    _cliente_con_datos(cfg)
    _sin_postgres(monkeypatch)

    pa.cmd_backup("cliente", quiet=True)

    assert list(cfg.clientes_dir.glob("cliente_backup_*.tar.gz"))
    assert not list((cfg.clientes_dir / "cliente" / "data" / "backups").glob("backup_automatico_*.zip"))


def test_con_el_flag_no_deja_tar_gz(cfg_zip, monkeypatch):
    _cliente_con_datos(cfg_zip)
    _con_postgres(monkeypatch, ["postgresql://u:p@side:5432/testprod"])

    pa.cmd_backup("cliente", quiet=True)

    assert not list(cfg_zip.clientes_dir.glob("cliente_backup_*.tar.gz"))


# ── cual de las dos bases es la principal ─────────────────────────────────────

def test_el_orden_del_compose_no_decide_cual_es_la_principal(cfg_zip, monkeypatch):
    """Con las variables al reves, el ZIP tiene que salir igual.

    Si se respetara el orden en que vienen, la principal seria la de LibraCore y
    las dos bases caerian en `testprod.dump`: una pisa a la otra y
    `nombres_en_zip` —que es un set— tendria un solo elemento, asi que la
    verificacion pasaria con medio backup.
    """
    _cliente_con_datos(cfg_zip)
    _con_postgres(monkeypatch, [
        "postgresql://u:p@side:5432/testprod_core",   # primero la de LibraCore
        "postgresql://u:p@side:5432/testprod",
    ])

    pa.cmd_backup("cliente", quiet=True)

    zips = sorted((cfg_zip.clientes_dir / "cliente" / "data" / "backups").glob("backup_automatico_*.zip"))
    with zipfile.ZipFile(zips[0]) as z:
        bases = sorted(n for n in z.namelist() if n.startswith("bases/"))
    assert bases == ["bases/testprod.dump", "bases/testprod_core.dump"]


def test_principal_primero_deja_el_orden_si_ninguna_coincide():
    """Reordenar a ciegas seria peor que no tocar nada."""
    urls = ["postgresql://u:p@h:5432/otra", "postgresql://u:p@h:5432/distinta"]

    assert pa._principal_primero(urls, "testprod") == urls


def test_una_instancia_con_dos_bases_del_mismo_nombre_no_se_construye():
    """La red de abajo: aunque el orden fallara, `Instancia` no deja pasar dos
    bases que caerian en el mismo archivo."""
    from libracore.respaldo import Instancia

    with pytest.raises(ValueError) as e:
        Instancia(
            nombre="testprod",
            postgres_url="postgresql://u:p@h:5432/testprod_core",
            postgres_extra=["postgresql://u:p@h:5432/testprod"],
        )
    assert "mismo archivo" in str(e.value)


def test_directorios_de_datos_excluye_backups(tmp_path):
    data = tmp_path / "data"
    (data / "logos").mkdir(parents=True)
    (data / "backups").mkdir()
    (data / "arca_certs").mkdir()
    (data / "suelto.db").write_bytes(b"")

    nombres = [d.name for d in pa._directorios_de_datos(data)]

    assert nombres == ["arca_certs", "logos"]
