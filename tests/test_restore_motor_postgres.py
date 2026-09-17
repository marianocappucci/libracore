"""El motor de restore de una instancia PostgreSQL (2026-09-17).

Lo que estos tests fijan, y por que cada uno existe:

- **Vuelve lo de antes Y se va lo de despues, tablas incluidas.** El restore
  anterior (`pg_restore --clean` sobre la base viva) dejaba vivas las tablas
  que no estaban en el dump, y eso rompia las migraciones de despues.
- **Las migraciones corren contra la base restaurada, nunca contra la viva**,
  y una que falla no deja nada a medias.
- **Cualquier falla antes del intercambio deja la viva intacta**: dump
  corrupto, FK que los datos no cumplen, `pg_restore` de otra version, la
  segunda base que no se puede intercambiar.
- **La `__antes_restore` anterior se reemplaza recien despues de un
  intercambio exitoso.**

Todos corren contra PostgreSQL real, sobre bases propias
(`tests/pg_descartable.py`).
"""
import os
import sys
import zipfile

import pytest
from pg_descartable import conectar, existe

from libracore import respaldo_postgres as rp
from libracore.respaldo import (
    BackupInvalido,
    Instancia,
    crear_backup,
    instancia_desde_entorno,
    restaurar,
)

SIN_MIGRACIONES = ()


def _ejecutar(url, *sentencias):
    with conectar(url) as conn:
        for sentencia in sentencias:
            conn.execute(sentencia)
        conn.commit()


def _filas(url, consulta):
    with conectar(url) as conn:
        return [tuple(f) for f in conn.execute(consulta).fetchall()]


def _tablas(url):
    return {f[0] for f in _filas(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}


@pytest.fixture
def viva(bases):
    url = bases.nueva()
    _ejecutar(
        url,
        "CREATE TABLE clientes (id serial PRIMARY KEY, nombre text)",
        "INSERT INTO clientes (nombre) VALUES ('Antes del backup')",
    )
    return url


@pytest.fixture
def core(bases):
    url = bases.nueva("_core")
    _ejecutar(
        url,
        "CREATE TABLE usuarios (id serial PRIMARY KEY, nombre text)",
        "INSERT INTO usuarios (nombre) VALUES ('Admin de antes')",
    )
    return url


def _nombres(url, tabla="clientes"):
    return [f[0] for f in _filas(url, f"SELECT nombre FROM {tabla} ORDER BY id")]


# ── El contrato ──────────────────────────────────────────────────────────────

def test_vuelve_lo_de_antes_y_se_va_lo_de_despues_tablas_incluidas(viva, tmp_path):
    instancia = Instancia(nombre="probe", postgres_url=viva)
    zip_ = crear_backup(instancia, tmp_path / "backups")

    _ejecutar(
        viva,
        "DELETE FROM clientes",
        "INSERT INTO clientes (nombre) VALUES ('Despues del backup')",
        # 🔴 La tabla que el `--clean` de antes dejaba viva.
        "CREATE TABLE creada_despues (id int)",
    )

    r = restaurar(instancia, zip_, tmp_path / "backups", migraciones=SIN_MIGRACIONES)

    assert r["ok"] is True
    assert _nombres(viva) == ["Antes del backup"]
    assert "creada_despues" not in _tablas(viva), "una tabla posterior al backup sobrevivio al restore"


def test_dos_bases_vuelven_las_dos(viva, core, tmp_path):
    instancia = Instancia(nombre="probe", postgres_url=viva, postgres_extra=[core])
    zip_ = crear_backup(instancia, tmp_path / "backups")
    _ejecutar(viva, "INSERT INTO clientes (nombre) VALUES ('Despues')")
    _ejecutar(core, "INSERT INTO usuarios (nombre) VALUES ('Despues')")

    restaurar(instancia, zip_, tmp_path / "backups", migraciones=SIN_MIGRACIONES)

    assert _nombres(viva) == ["Antes del backup"]
    assert _nombres(core, "usuarios") == ["Admin de antes"]


def test_el_reporte_dice_que_base_quedo_y_como_volver(viva, bases, tmp_path):
    instancia = Instancia(nombre="probe", postgres_url=viva)
    zip_ = crear_backup(instancia, tmp_path / "backups")
    _ejecutar(viva, "INSERT INTO clientes (nombre) VALUES ('Despues')")
    base = viva.rsplit("/", 1)[1]

    r = restaurar(instancia, zip_, tmp_path / "backups", migraciones=SIN_MIGRACIONES)

    anterior = base + rp.SUFIJO_ANTERIOR
    assert r["antes_restore"] == [anterior]
    assert anterior in r["como_volver"]
    # Y la vuelta atras tiene lo que habia justo antes del restore.
    assert _nombres(rp.con_base(viva, anterior)) == ["Antes del backup", "Despues"]


# ── Las migraciones ─────────────────────────────────────────────────────────

_MIGRACION_QUE_MARCA = (
    "import os, psycopg; "
    "c = psycopg.connect(os.environ['LCR_TEST_URL'].replace('postgresql+psycopg://', 'postgresql://', 1)); "
    "c.execute('CREATE TABLE marca_migracion (id int)'); c.commit()"
)


def test_las_migraciones_corren_contra_la_restaurada_y_no_contra_la_viva(viva, tmp_path, monkeypatch):
    """🔴 Una migracion que viera la URL viva escribiria en la base que el
    restore promete no tocar hasta el intercambio."""
    instancia = Instancia(nombre="probe", postgres_url=viva)
    zip_ = crear_backup(instancia, tmp_path / "backups")
    monkeypatch.setenv("LCR_TEST_URL", viva)

    r = restaurar(instancia, zip_, tmp_path / "backups",
                  migraciones=[[sys.executable, "-c", _MIGRACION_QUE_MARCA]])

    assert "marca_migracion" in _tablas(viva), "la migracion no llego a la base restaurada"
    anterior = rp.con_base(viva, r["antes_restore"][0])
    assert "marca_migracion" not in _tablas(anterior), "la migracion toco la base viva antes del intercambio"


def test_una_migracion_que_falla_deja_la_viva_intacta(viva, bases, tmp_path, monkeypatch):
    # La base nombrada en el entorno: si no, el restore frena antes por eso y
    # este test pasaria sin llegar a correr la migracion.
    monkeypatch.setenv("LCR_TEST_URL", viva)
    instancia = Instancia(nombre="probe", postgres_url=viva)
    zip_ = crear_backup(instancia, tmp_path / "backups")
    _ejecutar(viva, "INSERT INTO clientes (nombre) VALUES ('Despues')")

    with pytest.raises(BackupInvalido) as e:
        restaurar(instancia, zip_, tmp_path / "backups",
                  migraciones=[[sys.executable, "-c", "import sys; sys.stderr.write('ERROR: la cadena explota'); sys.exit(3)"]])

    assert "la cadena explota" in str(e.value)
    assert _nombres(viva) == ["Antes del backup", "Despues"]
    base = viva.rsplit("/", 1)[1]
    assert not existe(bases.servidor, base + rp.SUFIJO_TEMPORAL), "quedo la temporal colgada"


def test_sin_migraciones_legibles_no_toca_nada(viva, tmp_path, monkeypatch):
    """Con `migraciones=None` se leen de la imagen. Si no hay `scripts/`, no se
    restaura — ni siquiera se hace el backup previo."""
    instancia = Instancia(nombre="probe", postgres_url=viva)
    zip_ = crear_backup(instancia, tmp_path / "zips")
    _ejecutar(viva, "INSERT INTO clientes (nombre) VALUES ('Despues')")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(sys.modules, "scripts.panel_admin", raising=False)
    monkeypatch.delitem(sys.modules, "scripts", raising=False)

    with pytest.raises(BackupInvalido, match="migraciones"):
        restaurar(instancia, zip_, tmp_path / "backups")

    assert _nombres(viva) == ["Antes del backup", "Despues"]
    assert not (tmp_path / "backups").exists() or not list((tmp_path / "backups").iterdir())


# ── Lo que tiene que abortar sin tocar la viva ──────────────────────────────

def test_un_dump_corrupto_de_la_segunda_base_no_cambia_ninguna(viva, core, bases, tmp_path):
    """Y se detecta ANTES del backup previo: `pg_restore --list` lee el indice
    sin ejecutar nada. Sin esa validacion la viva igual quedaria intacta —el
    restore va a una temporal—, pero se haria un backup previo y se crearian
    bases para descubrir algo que se sabia leyendo el archivo."""
    instancia = Instancia(nombre="probe", postgres_url=viva, postgres_extra=[core])
    zip_ = crear_backup(instancia, tmp_path / "zips")
    nombre_core = f"{core.rsplit('/', 1)[1]}.dump"
    roto = tmp_path / "roto.zip"
    with zipfile.ZipFile(zip_) as origen, zipfile.ZipFile(roto, "w") as destino:
        for entrada in origen.namelist():
            datos = origen.read(entrada)
            if entrada == f"bases/{nombre_core}":
                datos = datos[: len(datos) // 3]
            destino.writestr(entrada, datos)
    _ejecutar(viva, "INSERT INTO clientes (nombre) VALUES ('Despues')")

    with pytest.raises(BackupInvalido):
        restaurar(instancia, roto, tmp_path / "backups", migraciones=SIN_MIGRACIONES)

    assert _nombres(viva) == ["Antes del backup", "Despues"]
    assert _nombres(core, "usuarios") == ["Admin de antes"]
    for url in (viva, core):
        assert not existe(bases.servidor, url.rsplit("/", 1)[1] + rp.SUFIJO_TEMPORAL)
    assert not (tmp_path / "backups").exists(), "hizo el backup previo antes de validar el dump"


def test_una_fk_que_los_datos_no_cumplen_aborta_y_dice_por_que(bases, tmp_path):
    """El dump trae una FK y datos que no la cumplen (se cargaron con los
    triggers apagados). `pg_restore` falla al crear la constraint."""
    url = bases.nueva()
    _ejecutar(
        url,
        "CREATE TABLE padres (id int PRIMARY KEY)",
        "CREATE TABLE hijos (id int, padre_id int REFERENCES padres(id))",
        "SET session_replication_role = replica",
        "INSERT INTO hijos VALUES (1, 999)",
    )
    instancia = Instancia(nombre="probe", postgres_url=url)
    zip_ = crear_backup(instancia, tmp_path / "backups")
    _ejecutar(url, "CREATE TABLE marca_viva (id int)")

    with pytest.raises(BackupInvalido) as e:
        restaurar(instancia, zip_, tmp_path / "backups", migraciones=SIN_MIGRACIONES)

    # 🔴 La causa, no la ultima linea de stderr (`Command was: ...`).
    assert "violates foreign key constraint" in str(e.value), str(e.value)
    assert "marca_viva" in _tablas(url), "la base viva cambio"


def test_pg_restore_mas_nuevo_que_el_servidor_no_toca_nada(viva, tmp_path, monkeypatch):
    instancia = Instancia(nombre="probe", postgres_url=viva)
    zip_ = crear_backup(instancia, tmp_path / "zips")
    _ejecutar(viva, "INSERT INTO clientes (nombre) VALUES ('Despues')")
    monkeypatch.setattr(rp, "version_pg_restore", lambda: 99)

    with pytest.raises(BackupInvalido, match="pg_restore 99"):
        restaurar(instancia, zip_, tmp_path / "backups", migraciones=SIN_MIGRACIONES)

    assert _nombres(viva) == ["Antes del backup", "Despues"]
    assert not (tmp_path / "backups").exists(), "hizo el backup previo antes de validar la version"


def test_si_falla_el_intercambio_de_la_segunda_la_primera_vuelve_atras(viva, core, bases, tmp_path, monkeypatch):
    instancia = Instancia(nombre="probe", postgres_url=viva, postgres_extra=[core])
    zip_ = crear_backup(instancia, tmp_path / "backups")
    _ejecutar(viva, "INSERT INTO clientes (nombre) VALUES ('Despues')")
    _ejecutar(core, "INSERT INTO usuarios (nombre) VALUES ('Despues')")

    original = rp._intercambiar_una

    def falla_en_core(conn, base):
        if base.endswith("_core"):
            raise RuntimeError("simulada: el rename de core no se pudo")
        return original(conn, base)

    monkeypatch.setattr(rp, "_intercambiar_una", falla_en_core)

    with pytest.raises(BackupInvalido, match="simulada"):
        restaurar(instancia, zip_, tmp_path / "backups", migraciones=SIN_MIGRACIONES)

    # Las dos del mismo momento: el de despues.
    assert _nombres(viva) == ["Antes del backup", "Despues"]
    assert _nombres(core, "usuarios") == ["Admin de antes", "Despues"]
    for url in (viva, core):
        base = url.rsplit("/", 1)[1]
        assert not existe(bases.servidor, base + rp.SUFIJO_TEMPORAL)
        assert not existe(bases.servidor, base + rp.SUFIJO_ANTERIOR_NUEVA)


def test_una_sesion_idle_in_transaction_no_frena_el_intercambio(viva, tmp_path):
    instancia = Instancia(nombre="probe", postgres_url=viva)
    zip_ = crear_backup(instancia, tmp_path / "backups")
    colgada = conectar(viva)
    colgada.execute("SELECT count(*) FROM clientes")  # abre la transaccion y la deja

    try:
        r = restaurar(instancia, zip_, tmp_path / "backups", migraciones=SIN_MIGRACIONES)
        assert r["ok"] is True
        import psycopg

        with pytest.raises(psycopg.OperationalError):
            colgada.execute("SELECT 1")
    finally:
        colgada.close()


# ── La vuelta atras ─────────────────────────────────────────────────────────

def test_la_anterior_se_reemplaza_recien_despues_de_un_restore_exitoso(viva, tmp_path, monkeypatch):
    monkeypatch.setenv("LCR_TEST_URL", viva)
    instancia = Instancia(nombre="probe", postgres_url=viva)
    zip_ = crear_backup(instancia, tmp_path / "zips")
    base = viva.rsplit("/", 1)[1]
    anterior = rp.con_base(viva, base + rp.SUFIJO_ANTERIOR)

    _ejecutar(viva, "INSERT INTO clientes (nombre) VALUES ('Estado 1')")
    restaurar(instancia, zip_, tmp_path / "backups", migraciones=SIN_MIGRACIONES)
    assert _nombres(anterior) == ["Antes del backup", "Estado 1"]

    # Un restore que falla NO se lleva la vuelta atras que habia.
    _ejecutar(viva, "INSERT INTO clientes (nombre) VALUES ('Estado 2')")
    with pytest.raises(BackupInvalido, match="fallo"):
        restaurar(instancia, zip_, tmp_path / "backups",
                  migraciones=[[sys.executable, "-c", "import sys; sys.exit(1)"]])
    assert _nombres(anterior) == ["Antes del backup", "Estado 1"]

    # Uno que sale bien, si.
    restaurar(instancia, zip_, tmp_path / "backups", migraciones=SIN_MIGRACIONES)
    assert _nombres(anterior) == ["Antes del backup", "Estado 2"]


# ── Lo que arma el restore dentro del contenedor ────────────────────────────

def test_la_instancia_desde_el_entorno_pone_primero_la_del_dominio(tmp_path):
    (tmp_path / "logos").mkdir()
    (tmp_path / "backups").mkdir()
    entorno = {
        "GESTIOLIBRA_LIBRACORE_DB_PATH": "postgresql+psycopg://u:p@db:5432/gestiolibra_core",
        "DATABASE_URL": "postgresql://u:p@db:5432/gestiolibra",
        "OTRA": "no es una url",
    }

    inst = instancia_desde_entorno("gestiolibra", tmp_path, entorno)

    assert inst.postgres_url == "postgresql://u:p@db:5432/gestiolibra"
    assert inst.postgres_extra == ["postgresql://u:p@db:5432/gestiolibra_core"]
    # Los mismos nombres que el ZIP del cron, o `_validar` lo rechaza.
    assert inst.nombres_en_zip == {"gestiolibra.dump", "gestiolibra_core.dump"}
    assert [d.name for d in inst.directorios] == ["logos"]


def test_el_entorno_de_las_migraciones_solo_cambia_las_urls_de_la_instancia():
    pares = [("postgresql://u:p@db:5432/acme", "postgresql://u:p@db:5432/acme__restore")]
    entorno = {
        "ACME_DATABASE_URL": "postgresql+psycopg://u:p@db:5432/acme",
        "OTRA_BASE": "postgresql://u:p@db:5432/otra",
        "MISMO_NOMBRE_OTRO_HOST": "postgresql://u:p@otro:5432/acme",
        "PATH": "/usr/bin",
    }

    salida = rp.entorno_para_temporales(pares, entorno)

    assert salida["ACME_DATABASE_URL"] == "postgresql+psycopg://u:p@db:5432/acme__restore"
    assert salida["OTRA_BASE"] == entorno["OTRA_BASE"]
    assert salida["MISMO_NOMBRE_OTRO_HOST"] == entorno["MISMO_NOMBRE_OTRO_HOST"]
    assert salida["PATH"] == "/usr/bin"


def test_los_errores_de_stderr_traen_la_causa_y_no_la_ultima_linea():
    stderr = (
        "pg_restore: error: could not execute query: ERROR:  unrecognized configuration parameter\n"
        "Command was: SET transaction_timeout = 0;\n"
    )
    assert "unrecognized configuration parameter" in rp.errores_de_stderr(stderr)


def test_los_errores_de_stderr_traen_la_excepcion_de_un_traceback_de_python():
    """🔴 El caso de LibraCargo (2026-09-17): de un traceback de alembic solo
    sobrevivia el link de "(Background on this error at: ...)"."""
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "/app/.venv/lib/python3.12/site-packages/sqlalchemy/engine/base.py", line 1988, in _exec_single_context\n'
        "    self._handle_dbapi_exception(\n"
        '  File "/app/.venv/lib/python3.12/site-packages/psycopg/cursor.py", line 117, in execute\n'
        "    raise ex.with_traceback(None)\n"
        'sqlalchemy.exc.ProgrammingError: (psycopg.errors.DuplicateObject) type "accion_auditoria" already exists\n'
        "[SQL: CREATE TYPE accion_auditoria AS ENUM ('alta', 'modificacion', 'baja')]\n"
        "(Background on this error at: https://sqlalche.me/e/20/f405)\n"
    )

    salida = rp.errores_de_stderr(stderr)

    assert 'type "accion_auditoria" already exists' in salida, salida
    assert "[SQL: CREATE TYPE accion_auditoria" in salida, salida
    assert "sqlalche.me" not in salida, salida
    assert "_handle_dbapi_exception" not in salida, salida


# ── Lo que el restore le deja a la app que sigue corriendo ──────────────────

def _engine(url):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    return sqlalchemy.create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))


def _por_el_pool(engine, consulta):
    from sqlalchemy import text

    with engine.connect() as conn:
        return [f[0] for f in conn.execute(text(consulta))]


def test_los_pools_de_la_app_siguen_andando_despues_del_intercambio(viva, core, tmp_path):
    """🔴 El intercambio termina las conexiones de las vivas. Con DOS pools —el
    del dominio y el de auth, como en los productos— y **sin** pasar
    `reabrir_conexiones`: el 2026-09-17 cuatro productos fallaron con
    `AdminShutdown` en la primera request despues del restore, y dos de ellos
    pasaban `engine.dispose` del engine del dominio."""
    dominio, auth = _engine(viva), _engine(core)
    try:
        # Los pools con conexiones abiertas ANTES del restore.
        assert _por_el_pool(dominio, "SELECT nombre FROM clientes") == ["Antes del backup"]
        assert _por_el_pool(auth, "SELECT nombre FROM usuarios") == ["Admin de antes"]
        instancia = Instancia(nombre="probe", postgres_url=viva, postgres_extra=[core])
        zip_ = crear_backup(instancia, tmp_path / "backups")
        _ejecutar(viva, "INSERT INTO clientes (nombre) VALUES ('Despues')")
        _ejecutar(core, "INSERT INTO usuarios (nombre) VALUES ('Despues')")

        restaurar(instancia, zip_, tmp_path / "backups", migraciones=SIN_MIGRACIONES)

        assert _por_el_pool(dominio, "SELECT nombre FROM clientes ORDER BY id") == ["Antes del backup"]
        assert _por_el_pool(auth, "SELECT nombre FROM usuarios ORDER BY id") == ["Admin de antes"]
    finally:
        dominio.dispose()
        auth.dispose()


def test_una_conexion_en_uso_durante_el_intercambio_no_vuelve_zombie_al_pool(viva, tmp_path):
    """La request que restaura tiene su propia conexion tomada mientras dura el
    intercambio. Esa muere; lo que importa es que al devolverla el pool no la
    guarde y se la de a la request siguiente."""
    from sqlalchemy import exc, text

    dominio = _engine(viva)
    try:
        instancia = Instancia(nombre="probe", postgres_url=viva)
        zip_ = crear_backup(instancia, tmp_path / "backups")
        _ejecutar(viva, "INSERT INTO clientes (nombre) VALUES ('Despues')")
        en_uso = dominio.connect()
        en_uso.execute(text("SELECT 1"))

        restaurar(instancia, zip_, tmp_path / "backups", migraciones=SIN_MIGRACIONES)

        with pytest.raises(exc.OperationalError):
            en_uso.execute(text("SELECT 1"))
        en_uso.close()
        for _ in range(3):  # mas pedidos que conexiones vivas: ninguno toma la muerta
            assert _por_el_pool(dominio, "SELECT nombre FROM clientes ORDER BY id") == ["Antes del backup"]
    finally:
        dominio.dispose()


def test_un_engine_a_otra_base_solo_se_reconecta(viva, bases, tmp_path):
    """El descarte es de todo pool del proceso, no solo de las bases
    restauradas: a un engine de otra base le cuesta una reconexion, y nada mas."""
    otra = bases.nueva("_otra")
    _ejecutar(otra, "CREATE TABLE cosas (id int)", "INSERT INTO cosas VALUES (7)")
    ajeno = _engine(otra)
    try:
        pid_antes = _por_el_pool(ajeno, "SELECT pg_backend_pid()")[0]
        instancia = Instancia(nombre="probe", postgres_url=viva)
        zip_ = crear_backup(instancia, tmp_path / "backups")

        restaurar(instancia, zip_, tmp_path / "backups", migraciones=SIN_MIGRACIONES)

        assert _por_el_pool(ajeno, "SELECT id FROM cosas") == [7]
        assert _por_el_pool(ajeno, "SELECT pg_backend_pid()")[0] != pid_antes, "no se reconecto"
    finally:
        ajeno.dispose()


def test_una_base_que_ninguna_variable_nombra_no_se_restaura(viva, tmp_path, monkeypatch):
    """🔴 Como el conftest de LibraDesk: la URL se le pasa a la app en el proceso,
    no en el entorno. La reescritura no encuentra que cambiar y la migracion
    correria contra otra cosa. Frena antes de tocar nada y nombra la base."""
    base = viva.rsplit("/", 1)[1]
    for clave, valor in list(os.environ.items()):
        if rp.es_url_postgres(valor) and rp.base_de(valor) == base:
            monkeypatch.delenv(clave)
    instancia = Instancia(nombre="probe", postgres_url=viva)
    zip_ = crear_backup(instancia, tmp_path / "zips")
    _ejecutar(viva, "INSERT INTO clientes (nombre) VALUES ('Despues')")

    with pytest.raises(BackupInvalido, match="ninguna variable de entorno") as e:
        restaurar(instancia, zip_, tmp_path / "backups",
                  migraciones=[[sys.executable, "-c", "pass"]])

    assert base in str(e.value)
    assert _nombres(viva) == ["Antes del backup", "Despues"]
    assert not (tmp_path / "backups").exists(), "hizo el backup previo antes de ver que no podia migrar"


def test_las_bases_sin_variable_se_miden_como_la_reescritura():
    entorno = {
        "ACME_DATABASE_URL": "postgresql+psycopg://u:p@db:5432/acme",
        "MISMO_NOMBRE_OTRO_HOST": "postgresql://u:p@otro:5432/acme_core",
    }

    assert rp.bases_sin_variable(
        ["postgresql://u:p@db:5432/acme", "postgresql://u:p@db:5432/acme_core"], entorno
    ) == ["acme_core"]
