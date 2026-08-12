"""`verificar_backup`: mirar el ZIP hecho, no confiar en que no fallo.

Existe porque el historial de este modulo es una lista de backups que salieron
"bien" y estaban vacios — el `if not origen.exists()` que se salteaba la base en
silencio, el `if db_src.exists()` del cron, el dump de 0 bytes que quedaba con
nombre de backup. Todos devolvian exito.

Los dos casos que tiene que hacer ruidosos:

1. Falta una base. Es el de Gestiolibra y MedLibra, que tienen dos: con una sola
   el ZIP se descarga, pesa poco y **no se puede restaurar**.
2. La base esta pero pesa cero. Peor que si faltara, porque la rotacion lo
   cuenta como un backup bueno.
"""
import zipfile

import pytest

from libracore.respaldo import BackupInvalido, Instancia, verificar_backup


def _zip_con(tmp_path, contenidos: dict, nombre="backup.zip"):
    destino = tmp_path / nombre
    with zipfile.ZipFile(destino, "w") as z:
        for ruta, datos in contenidos.items():
            z.writestr(ruta, datos)
    return destino


def _instancia_dos_bases():
    return Instancia(
        nombre="testprod",
        postgres_url="postgresql://u:p@h:5432/testprod",
        postgres_extra=["postgresql://u:p@h:5432/testprod_core"],
    )


def test_pasa_cuando_estan_todas_con_contenido(tmp_path):
    destino = _zip_con(tmp_path, {
        "bases/testprod.dump": b"PGDMP" + b"x" * 100,
        "bases/testprod_core.dump": b"PGDMP" + b"y" * 200,
        "datos/logos/logo.png": b"PNG",
    })

    detalle = verificar_backup(destino, _instancia_dos_bases())

    assert sorted(detalle["bases"]) == ["testprod.dump", "testprod_core.dump"]
    assert detalle["archivo"] == "backup.zip"


def test_falla_si_falta_una_de_las_dos_bases(tmp_path):
    """El caso de Gestiolibra y MedLibra. Un backup con una mitad no se restaura:
    o volves el dominio y te quedan usuarios de otro momento, o al reves."""
    destino = _zip_con(tmp_path, {
        "bases/testprod.dump": b"PGDMP" + b"x" * 100,
        "datos/logos/logo.png": b"PNG",
    })

    with pytest.raises(BackupInvalido) as e:
        verificar_backup(destino, _instancia_dos_bases())

    assert "testprod_core.dump" in str(e.value)


def test_falla_si_una_base_pesa_cero(tmp_path):
    destino = _zip_con(tmp_path, {
        "bases/testprod.dump": b"PGDMP" + b"x" * 100,
        "bases/testprod_core.dump": b"",
    })

    with pytest.raises(BackupInvalido) as e:
        verificar_backup(destino, _instancia_dos_bases())

    assert "0 bytes" in str(e.value)


def test_falla_si_el_zip_no_trae_ninguna_base(tmp_path):
    """El ZIP con los logos y sin datos: el caso que el cliente descubria recien
    al intentar restaurar."""
    destino = _zip_con(tmp_path, {"datos/logos/logo.png": b"PNG"})

    with pytest.raises(BackupInvalido):
        verificar_backup(destino, _instancia_dos_bases())


def test_una_carpeta_dentro_de_bases_no_cuenta_como_base(tmp_path):
    """Las entradas de directorio del ZIP pesan 0: sin filtrarlas, un ZIP
    perfectamente sano fallaria por 'una base de 0 bytes' que no es una base."""
    destino = tmp_path / "backup.zip"
    with zipfile.ZipFile(destino, "w") as z:
        z.writestr("bases/", b"")
        z.writestr("bases/testprod.dump", b"PGDMP" + b"x" * 100)

    instancia = Instancia(
        nombre="testprod", postgres_url="postgresql://u:p@h:5432/testprod",
    )

    assert verificar_backup(destino, instancia)["bases"] == {"testprod.dump": 105}


def test_sqlite_se_verifica_por_el_nombre_del_archivo(tmp_path):
    """En SQLite las bases entran con su nombre de archivo, no como `.dump`."""
    base = tmp_path / "testprod.db"
    base.write_bytes(b"SQLite format 3\x00" + b"z" * 50)
    destino = _zip_con(tmp_path, {"bases/testprod.db": base.read_bytes()})

    instancia = Instancia(nombre="testprod", bases=[base])

    assert verificar_backup(destino, instancia)["bases"] == {"testprod.db": 66}
