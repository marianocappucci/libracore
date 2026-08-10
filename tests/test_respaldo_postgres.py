"""El backup de una instancia que corre sobre PostgreSQL.

Por qué existe, que es lo que menos se ve:

Hasta el 2026-08-09 una instancia sobre PostgreSQL producía un backup **vacío y
en silencio**. El producto pasaba el nombre de la base como si fuera una ruta
de archivo, `_copiar_base` no lo encontraba, y su `if not origen.exists():
return` —puesto para instancias recién creadas que todavía no arrancaron— se lo
saltaba sin decir nada. El cliente se bajaba un ZIP con los logos, sin datos, y
se enteraba recién al intentar restaurar.

Los tests de acá están escritos contra esa forma de fallar:

- No alcanza con que el ZIP traiga una entrada en `bases/`: se **restaura** y se
  busca el dato del otro lado. Un dump vacío pesa unos cientos de bytes y tiene
  la misma pinta que uno bueno.
- Se verifica que un dump que no se puede hacer sea un **error**, no un ZIP
  incompleto. Es la diferencia entre un backup que falta y un backup que
  miente.
"""
import os
import zipfile

import pytest

from libracore.respaldo import BackupInvalido, Instancia, crear_backup, restaurar_backup


def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    return url


def _conectar(url):
    import psycopg

    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1))


@pytest.fixture
def base(tmp_path):
    """Una base con una tabla y una fila reconocible."""
    url = _url()
    with _conectar(url) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("CREATE TABLE clientes (id serial PRIMARY KEY, nombre text)")
        conn.execute("INSERT INTO clientes (nombre) VALUES (%s)", ("Antes del backup",))
        conn.commit()
    return url


def _nombres(url):
    with _conectar(url) as conn:
        return [f[0] for f in conn.execute("SELECT nombre FROM clientes").fetchall()]


def test_el_backup_trae_el_dump_y_el_dump_trae_los_datos(base, tmp_path):
    """El ZIP tiene que traer la base **con contenido**, no una entrada vacía."""
    instancia = Instancia(nombre="probe", postgres_url=base)

    ruta = crear_backup(instancia, tmp_path / "backups")

    with zipfile.ZipFile(ruta) as z:
        assert "bases/probe.dump" in z.namelist()
        dump = z.read("bases/probe.dump")
    assert dump.startswith(b"PGDMP"), "no es un dump de pg_dump"
    # Contraprueba del tamaño: un dump de una base vacía también empieza con
    # PGDMP. Lo que distingue es que adentro esté la fila, y eso lo prueba el
    # round trip de abajo.


def test_el_round_trip_devuelve_los_datos_borrados(base, tmp_path):
    """Backup -> se pierde todo -> restore. El caso real por el que existe."""
    instancia = Instancia(nombre="probe", postgres_url=base)
    ruta = crear_backup(instancia, tmp_path / "backups")

    with _conectar(base) as conn:
        conn.execute("DELETE FROM clientes")
        conn.execute("INSERT INTO clientes (nombre) VALUES (%s)", ("Despues del backup",))
        conn.commit()
    assert _nombres(base) == ["Despues del backup"]

    with open(ruta, "rb") as f:
        r = restaurar_backup(instancia, f.read(), tmp_path / "backups")

    assert r["ok"] is True
    assert r["bases_restauradas"] == ["probe.dump"]
    # Las dos aserciones: vuelve lo de antes Y se va lo de después. Con sólo la
    # primera, un restore que agregara filas sin limpiar pasaría igual.
    assert _nombres(base) == ["Antes del backup"]


def test_restaurar_hace_un_backup_previo(base, tmp_path):
    """La red del restore: si el que se restaura estaba mal, el estado anterior
    todavía existe. No es opcional — es lo único que hay si el dump venía mal."""
    instancia = Instancia(nombre="probe", postgres_url=base)
    ruta = crear_backup(instancia, tmp_path / "backups")

    with open(ruta, "rb") as f:
        r = restaurar_backup(instancia, f.read(), tmp_path / "backups")

    previo = tmp_path / "backups" / r["backup_previo"]
    assert previo.exists()
    assert "antes_restore" in previo.name


def test_un_dump_cortado_no_llega_a_tocar_la_base(base, tmp_path):
    """Validar ANTES de restaurar. Un archivo truncado tiene que dar
    `BackupInvalido` con la base intacta, no un restore a medio camino."""
    instancia = Instancia(nombre="probe", postgres_url=base)
    ruta = crear_backup(instancia, tmp_path / "backups")

    with zipfile.ZipFile(ruta) as z:
        entero = z.read("bases/probe.dump")
    roto = tmp_path / "roto.zip"
    with zipfile.ZipFile(roto, "w") as z:
        z.writestr("bases/probe.dump", entero[: len(entero) // 3])

    with open(roto, "rb") as f:
        with pytest.raises(BackupInvalido):
            restaurar_backup(instancia, f.read(), tmp_path / "backups")

    assert _nombres(base) == ["Antes del backup"]


def test_sin_pg_dump_el_backup_FALLA_en_vez_de_salir_vacio(base, tmp_path, monkeypatch):
    """🔴 La defensa central de este archivo.

    El defecto original no era que el backup fallara: era que **salía bien y
    vacío**. Acá se saca `pg_dump` del PATH y se exige un error explícito, no
    un ZIP sin base.
    """
    instancia = Instancia(nombre="probe", postgres_url=base)
    monkeypatch.setenv("PATH", str(tmp_path / "vacio"))

    with pytest.raises(BackupInvalido) as e:
        crear_backup(instancia, tmp_path / "backups")

    # El mensaje va a la pantalla del cliente: tiene que decir qué falta.
    assert "pg_dump" in str(e.value)


def test_una_instancia_no_puede_ser_de_los_dos_motores(tmp_path):
    """Pasar las dos cosas es un error de cableado —la ruta SQLite vieja además
    de la URL nueva—, y dejarlo pasar daría un ZIP con una base de cada
    momento."""
    with pytest.raises(ValueError):
        Instancia(nombre="probe", bases=[tmp_path / "x.db"], postgres_url="postgresql://x/y")


# ── Dos bases PostgreSQL en la misma instancia ────────────────────────────

@pytest.fixture
def base_core(base):
    """Una SEGUNDA base, la de LibraCore, en el mismo servidor.

    Es la forma real de [[gestiolibra]] y [[medlibra]] despues del corte: el
    dominio y LibraCore no pueden compartir schema -- los dos declaran una tabla
    `clients` con `id` de tipos incompatibles -- asi que quedan como dos bases,
    igual que eran dos archivos en SQLite.
    """
    servidor = base.rsplit("/", 1)[0]
    nombre = base.rsplit("/", 1)[1].split("?")[0] + "_core"
    with _conectar(servidor + "/postgres") as conn:
        conn.autocommit = True
        if not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (nombre,)
        ).fetchone():
            conn.execute(f'CREATE DATABASE "{nombre}"')
    url = f"{servidor}/{nombre}"
    with _conectar(url) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("CREATE TABLE usuarios (id serial PRIMARY KEY, nombre text)")
        conn.execute("INSERT INTO usuarios (nombre) VALUES (%s)", ("Admin de antes",))
        conn.commit()
    return url


def test_el_zip_trae_las_dos_bases(base, base_core, tmp_path):
    """🔴 Un backup con una sola no se puede restaurar: o volves el dominio y te
    quedan usuarios de otro momento, o al reves. Y no falla -- da un ZIP que se
    descarga y pesa poco, que es la peor forma de fallar que tiene un backup."""
    instancia = Instancia(nombre="probe", postgres_url=base, postgres_extra=[base_core])

    destino = crear_backup(instancia, tmp_path / "backups")

    with zipfile.ZipFile(destino) as z:
        dentro = sorted(n for n in z.namelist() if n.startswith("bases/"))
    assert len(dentro) == 2, f"esperaba dos dumps, vinieron {dentro}"
    assert any("core" in n for n in dentro), dentro


def test_el_restore_devuelve_las_dos(base, base_core, tmp_path):
    """No alcanza con que el ZIP traiga dos entradas: un dump vacio pesa unos
    cientos de bytes y tiene la misma pinta que uno bueno. Se restaura y se
    busca el dato **de las dos** del otro lado."""
    instancia = Instancia(nombre="probe", postgres_url=base, postgres_extra=[base_core])
    destino = crear_backup(instancia, tmp_path / "backups")

    for url, tabla, valor in ((base, "clientes", "Despues"), (base_core, "usuarios", "Despues")):
        with _conectar(url) as conn:
            conn.execute(f"INSERT INTO {tabla} (nombre) VALUES (%s)", (valor,))
            conn.commit()

    restaurar_backup(instancia, destino.read_bytes(), tmp_path / "backups")

    assert _nombres(base) == ["Antes del backup"]
    with _conectar(base_core) as conn:
        usuarios = [f[0] for f in conn.execute("SELECT nombre FROM usuarios").fetchall()]
    assert usuarios == ["Admin de antes"], usuarios


def test_una_extra_sin_principal_no_se_acepta():
    """La principal es la que da el nombre del dump y la que se restaura
    primero: `postgres_extra` sola seria un cableado a medias."""
    with pytest.raises(ValueError, match="postgres_extra"):
        Instancia(nombre="probe", postgres_extra=["postgresql://x/y"])
