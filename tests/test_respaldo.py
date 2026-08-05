"""Backup y restore de una instancia completa.

Lo que fijan, en orden de lo que se pierde sin que se note:

1. 🔴 Que el backup **incluya todo lo que compone la instancia** — las dos
   bases y los archivos en disco. Es la razon por la que este modulo existe en
   vez de copiar el endpoint de Contalibra: tres productos tienen `usuarios` en
   una base separada y MedLibra tiene los documentos clinicos en disco. Un
   backup al que le falta algo se ve exactamente igual que uno completo.
2. Que restaurar el backup de OTRO producto se rechace.
3. Que el restore no arranque sin haber guardado antes el estado actual.
4. Que la copia de una base en uso sea coherente.
"""
import sqlite3
import zipfile

import pytest

from libracore.respaldo import (
    MAX_BACKUPS,
    BackupInvalido,
    Instancia,
    crear_backup,
    listar_backups,
    restaurar_backup,
)


def _base(path, filas=("uno",)):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS cosas (nombre TEXT)")
    conn.executemany("INSERT INTO cosas (nombre) VALUES (?)", [(f,) for f in filas])
    conn.commit()
    conn.close()
    return path


def _leer(path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [f[0] for f in conn.execute("SELECT nombre FROM cosas").fetchall()]
    finally:
        conn.close()


@pytest.fixture
def instancia(tmp_path):
    """Una instancia con DOS bases y DOS directorios: el caso de MedLibra, que
    es el que mas se pierde con un backup de un solo archivo."""
    datos = tmp_path / "datos"
    _base(datos / "producto.db", ["turno-1"])
    _base(datos / "producto_libracore.db", ["usuario-admin"])
    (datos / "logos").mkdir(parents=True)
    (datos / "logos" / "logo.png").write_bytes(b"PNG-falso")
    (datos / "documentos").mkdir()
    (datos / "documentos" / "estudio.pdf").write_bytes(b"%PDF-1.4 estudio")
    return Instancia(
        nombre="producto",
        bases=[datos / "producto.db", datos / "producto_libracore.db"],
        directorios=[datos / "logos", datos / "documentos"],
    )


# ── 🔴 Que no se pierda nada ──────────────────────────────────────────────

def test_el_backup_incluye_las_dos_bases_y_los_archivos(instancia, tmp_path):
    """El motivo entero por el que este modulo existe.

    Contalibra baja **un** `.db` y le alcanza porque tiene una sola base.
    Copiado tal cual a Gestiolibra, MedLibra o VentaLibra, el backup sale sin
    `usuarios` — o sea que no se puede restaurar— y en MedLibra ademas sin los
    estudios subidos.
    """
    zip_path = crear_backup(instancia, tmp_path / "backups")

    with zipfile.ZipFile(zip_path) as z:
        dentro = set(z.namelist())

    assert "bases/producto.db" in dentro
    assert "bases/producto_libracore.db" in dentro, "falta la base de usuarios"
    assert "datos/logos/logo.png" in dentro
    assert "datos/documentos/estudio.pdf" in dentro, "faltan los documentos en disco"


def test_una_base_que_todavia_no_existe_no_rompe_el_backup(tmp_path):
    """Una instancia recien creada que nunca arranco: la segunda base todavia
    no se creo. Que el backup falle ahi seria dejar sin respaldo justo a la
    instancia mas facil de perder."""
    datos = tmp_path / "datos"
    _base(datos / "producto.db")
    inst = Instancia(
        nombre="producto",
        bases=[datos / "producto.db", datos / "no_existe_todavia.db"],
    )

    zip_path = crear_backup(inst, tmp_path / "backups")
    with zipfile.ZipFile(zip_path) as z:
        assert z.namelist() == ["bases/producto.db"]


def test_una_instancia_sin_bases_no_se_acepta():
    """Configurarla mal daria un backup vacio que igual se descarga: el
    cliente se lleva un ZIP de 200 bytes creyendo que tiene sus datos."""
    with pytest.raises(ValueError):
        Instancia(nombre="x", bases=[])


def test_la_copia_de_una_base_en_uso_es_coherente(instancia, tmp_path):
    """Se usa la API de backup de SQLite y no `copy2` tras un checkpoint,
    justamente para que una escritura concurrente no parta el archivo. Acá se
    deja la conexion abierta con una transaccion sin commitear: lo que tiene
    que quedar en el backup es el estado commiteado, entero."""
    conn = sqlite3.connect(str(instancia.bases[0]))
    conn.execute("BEGIN")
    conn.execute("INSERT INTO cosas (nombre) VALUES ('sin-commitear')")

    zip_path = crear_backup(instancia, tmp_path / "backups")
    conn.rollback()
    conn.close()

    with zipfile.ZipFile(zip_path) as z:
        z.extract("bases/producto.db", tmp_path / "extraido")
    filas = _leer(tmp_path / "extraido" / "bases" / "producto.db")
    assert filas == ["turno-1"]


# ── Listado y rotacion ────────────────────────────────────────────────────

def test_dos_backups_en_el_mismo_segundo_no_se_pisan(instancia, tmp_path):
    """El timestamp tiene resolucion de segundo. Sin desempate, el segundo
    sobreescribe al primero **sin avisar** — y pasa de verdad: el cliente que
    aprieta "Backup rapido" dos veces, y sobre todo el backup automatico previo
    a un restore cayendo en el mismo segundo que uno manual, que es justo el
    que no se puede perder."""
    destino = tmp_path / "backups"
    for _ in range(3):
        crear_backup(instancia, destino)

    assert len(listar_backups(destino)) == 3


def test_el_listado_va_del_mas_reciente_al_mas_viejo(instancia, tmp_path):
    destino = tmp_path / "backups"
    for _ in range(3):
        crear_backup(instancia, destino)

    filas = listar_backups(destino)
    assert [f["filename"] for f in filas] == sorted(
        [f["filename"] for f in filas], reverse=True,
    )


def test_sin_carpeta_no_hay_backups_y_no_es_un_error(tmp_path):
    assert listar_backups(tmp_path / "no_existe") == []


def test_se_conservan_como_mucho_diez(instancia, tmp_path):
    """El disco del VPS ya viene ajustado (62% al 2026-08-04): una instancia
    que hace un backup por dia sin rotar se lo come sola."""
    destino = tmp_path / "backups"
    for _ in range(MAX_BACKUPS + 4):
        crear_backup(instancia, destino)

    assert len(listar_backups(destino)) <= MAX_BACKUPS


# ── Restore ───────────────────────────────────────────────────────────────

def test_restaurar_vuelve_las_dos_bases_y_los_archivos(instancia, tmp_path):
    zip_path = crear_backup(instancia, tmp_path / "backups")
    contenido = zip_path.read_bytes()

    # Se ensucia todo lo que el backup tenia que haber guardado.
    _base(instancia.bases[0], ["turno-2-posterior"])
    _base(instancia.bases[1], ["usuario-nuevo"])
    (instancia.directorios[1] / "estudio.pdf").unlink()

    resultado = restaurar_backup(instancia, contenido, tmp_path / "backups")

    assert resultado["ok"] is True
    assert sorted(resultado["bases_restauradas"]) == [
        "producto.db", "producto_libracore.db",
    ]
    assert _leer(instancia.bases[0]) == ["turno-1"]
    assert _leer(instancia.bases[1]) == ["usuario-admin"]
    assert (instancia.directorios[1] / "estudio.pdf").exists()


def test_el_restore_guarda_primero_el_estado_actual(instancia, tmp_path):
    """Es la operacion mas destructiva que el cliente puede disparar solo desde
    una pantalla. El backup previo tiene que quedar, y con un nombre que lo
    distinga — es el que se busca cuando el restore fue el error."""
    destino = tmp_path / "backups"
    contenido = crear_backup(instancia, destino).read_bytes()
    _base(instancia.bases[0], ["lo-que-habia-antes-del-restore"])

    resultado = restaurar_backup(instancia, contenido, destino)

    previo = resultado["backup_previo"]
    assert "antes_restore" in previo
    with zipfile.ZipFile(destino / previo) as z:
        z.extract("bases/producto.db", tmp_path / "rescate")
    assert "lo-que-habia-antes-del-restore" in _leer(
        tmp_path / "rescate" / "bases" / "producto.db"
    )


def test_el_backup_de_otro_producto_se_rechaza(instancia, tmp_path):
    """Los nombres de archivo de la familia se parecen
    (`medlibra_libracore.db`, `gestiolibra_libracore.db`). Sin este chequeo,
    restaurar el backup equivocado deja la instancia con las bases de otro
    sistema — y no hay deshacer."""
    otra_datos = tmp_path / "otro"
    _base(otra_datos / "otro_producto.db")
    otra = Instancia(nombre="otro", bases=[otra_datos / "otro_producto.db"])
    ajeno = crear_backup(otra, tmp_path / "backups_otro").read_bytes()

    with pytest.raises(BackupInvalido) as exc:
        restaurar_backup(instancia, ajeno, tmp_path / "backups")
    assert "otro sistema" in str(exc.value)


def test_un_archivo_que_no_es_zip_se_rechaza_con_un_mensaje_util(instancia, tmp_path):
    with pytest.raises(BackupInvalido) as exc:
        restaurar_backup(instancia, b"no soy un zip", tmp_path / "backups")
    assert ".zip" in str(exc.value)


def test_un_zip_sin_bases_se_rechaza(instancia, tmp_path):
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("datos/logos/logo.png", b"solo el logo")

    with pytest.raises(BackupInvalido) as exc:
        restaurar_backup(instancia, buffer.getvalue(), tmp_path / "backups")
    assert "ninguna base" in str(exc.value)


def test_una_base_corrupta_se_rechaza_antes_de_pisar_nada(instancia, tmp_path):
    """Y **antes de pisar la primera**: a mitad de camino la instancia queda
    con una base nueva y otra vieja, que es un estado del que no se sale solo.
    """
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("bases/producto.db", b"esto no es SQLite")
        z.writestr("bases/producto_libracore.db", instancia.bases[1].read_bytes())

    with pytest.raises(BackupInvalido):
        restaurar_backup(instancia, buffer.getvalue(), tmp_path / "backups")

    # La instancia quedo intacta: ninguna de las dos se toco.
    assert _leer(instancia.bases[0]) == ["turno-1"]
    assert _leer(instancia.bases[1]) == ["usuario-admin"]


def test_una_ruta_con_dos_puntos_se_rechaza(instancia, tmp_path):
    """Zip slip. Lo sube un admin, pero un admin tambien puede estar
    restaurando un archivo que le mandaron."""
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("bases/producto.db", instancia.bases[0].read_bytes())
        z.writestr("datos/../../../etc/algo", b"x")

    with pytest.raises(BackupInvalido) as exc:
        restaurar_backup(instancia, buffer.getvalue(), tmp_path / "backups")
    assert "ruta invalida" in str(exc.value)


def test_los_sidecar_del_wal_viejo_no_sobreviven_al_restore(instancia, tmp_path):
    """Un `-wal` de la base anterior describe transacciones que la base nueva
    no tiene. Si queda, SQLite se lo aplica encima y la corrompe."""
    contenido = crear_backup(instancia, tmp_path / "backups").read_bytes()
    wal = instancia.bases[0].with_name(instancia.bases[0].name + "-wal")
    wal.write_bytes(b"wal viejo")

    restaurar_backup(instancia, contenido, tmp_path / "backups")

    assert not wal.exists()
