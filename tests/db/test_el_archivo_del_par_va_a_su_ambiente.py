"""La `0008`, ejecutada contra el estado que deja la `0007`.

🔴 **El defecto que cierra.** La `0007` movió el par de credenciales a las
columnas de su ambiente, pero movió los **paths**, no los **archivos**. Una
instancia que estaba en homologación quedó con su par apuntando a
`certificado.crt` — que desde `v1.72.0` es el nombre de **producción**.

El día que el operador suba el certificado real por la pantalla nueva, el upload
escribe ese mismo archivo y **pisa el de homologación**. Es la operación
destructiva que separar los pares vino a evitar, reaparecida una capa más abajo.

Lo delicado no es el rename: es **a quién NO hay que tocarle nada**. La
instancia de cliente de Contalibra está en `produccion` con su par bajo
`certificado.crt`, que es su nombre correcto — si esta revisión se lo moviera,
le rompería la facturación real. Por eso hay tantos controles negativos como
casos positivos.

Se corre alembic **como proceso**, igual que el resto de la cadena, porque así
lo invoca el deploy.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import head_de_la_cadena

from libracore import config_manager
from libracore.config_manager import ARCHIVOS_POR_AMBIENTE
from libracore.db import core
from libracore.db.schema import init_core_schema

RAIZ = Path(__file__).resolve().parents[2]

#: 🔑 Del head de alembic, no de un literal — ver `head_de_la_cadena`.
REVISION = head_de_la_cadena()

CERT_PROD, CLAVE_PROD = ARCHIVOS_POR_AMBIENTE["produccion"]
CERT_HOMO, CLAVE_HOMO = ARCHIVOS_POR_AMBIENTE["homologacion"]


@pytest.fixture
def certs(tmp_path, monkeypatch):
    """Un `CERTS_DIR` propio. La revisión lo lee de `config_manager`, que a su
    vez sale de `DATA_DIR`: en el contenedor es una variable de entorno, así que
    acá se fija por entorno **y** por atributo — el proceso de alembic es otro
    intérprete y no ve el `monkeypatch`."""
    d = tmp_path / "arca_certs"
    d.mkdir()
    monkeypatch.setattr(config_manager, "CERTS_DIR", str(d))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return d


def _instancia(ruta, *, ambiente, columnas):
    """Una base con el schema puesto y una fila de `arca_config`.

    Las cuatro columnas del par se escriben siempre —vacías si el caso no las
    nombra— porque `certificado_path` y `clave_path` son NOT NULL sin default:
    omitirlas hace fallar el INSERT, no el caso de prueba.
    """
    todas = {
        "certificado_path": "", "clave_path": "",
        "certificado_path_homologacion": "", "clave_path_homologacion": "",
        **columnas,
    }
    core._db_path = None
    core._database_url = None
    core.configure(db_path=ruta)
    c = core.get_connection()
    try:
        init_core_schema(c)
        campos = ", ".join(todas)
        marcas = ", ".join("?" for _ in todas)
        c.execute(
            f"INSERT INTO arca_config (empresa, cuit, punto_venta, ambiente,"
            f" activo, {campos}) VALUES ('default','20111111112',1,?,1,{marcas})",
            (ambiente, *todas.values()),
        )
        c.commit()
    finally:
        c.close()
        core._db_path = None
        core._database_url = None


def _migrar(ruta, certs_dir):
    entorno = {**os.environ, "DATABASE_URL": ruta,
               "DATA_DIR": str(Path(certs_dir).parent)}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=RAIZ, env=entorno, capture_output=True, text=True,
    )


def _fila(ruta):
    c = sqlite3.connect(ruta)
    c.row_factory = sqlite3.Row
    try:
        return dict(c.execute("SELECT * FROM arca_config").fetchone())
    finally:
        c.close()


def _revision(ruta):
    c = sqlite3.connect(ruta)
    try:
        fila = c.execute("SELECT version_num FROM alembic_version").fetchone()
        return fila[0] if fila else None
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()


# -- El caso que la 0007 deja abierto ---------------------------------------

def test_el_par_de_homologacion_deja_de_usar_el_nombre_de_produccion(
        tmp_path, certs):
    """🔴 El estado exacto que produce la `0007`: selector en homologación y el
    par apuntando al archivo con nombre de producción."""
    (certs / CERT_PROD).write_text("el par de homologacion, con el nombre viejo")
    (certs / CLAVE_PROD).write_text("la clave de homologacion")
    ruta = str(tmp_path / "demo.db")
    _instancia(ruta, ambiente="homologacion", columnas={
        "certificado_path_homologacion": str(certs / CERT_PROD),
        "clave_path_homologacion": str(certs / CLAVE_PROD),
    })

    r = _migrar(ruta, certs)
    assert _revision(ruta) == REVISION, (
        f"la cadena no llegó a {REVISION} — comparar no diría nada\n{r.stderr[-700:]}")

    fila = _fila(ruta)
    assert os.path.basename(fila["certificado_path_homologacion"]) == CERT_HOMO
    assert os.path.basename(fila["clave_path_homologacion"]) == CLAVE_HOMO
    assert (certs / CERT_HOMO).exists(), "el archivo no se movió"
    assert not (certs / CERT_PROD).exists(), (
        "quedó el archivo con el nombre de producción: el próximo upload real lo pisa")
    assert (certs / CERT_HOMO).read_text().startswith("el par de homologacion")


def test_el_par_sigue_siendo_legible_despues(tmp_path, certs):
    """🔑 Lo que importa no es dónde quedó el archivo: es que `paths_en_disco()`
    —lo que el firmante abre— siga devolviendo un par que existe."""
    (certs / CERT_PROD).write_text("cert")
    (certs / CLAVE_PROD).write_text("clave")
    ruta = str(tmp_path / "demo.db")
    _instancia(ruta, ambiente="homologacion", columnas={
        "certificado_path_homologacion": str(certs / CERT_PROD),
        "clave_path_homologacion": str(certs / CLAVE_PROD),
    })
    _migrar(ruta, certs)
    assert _revision(ruta) == REVISION

    from libracore import arca_credenciales as ac
    cert, clave = ac.paths_en_disco(_fila(ruta))
    assert cert and clave and os.path.exists(cert) and os.path.exists(clave), (
        "la instancia se quedó sin credenciales legibles")


def test_el_path_viejo_de_otro_DATA_DIR_se_resuelve_por_nombre(tmp_path, certs):
    """El caso real de `contalibra-dev`: la columna apuntaba a un path de un
    `DATA_DIR` que ya no existe, y el archivo estaba en el `CERTS_DIR` actual
    bajo el nombre viejo. Se busca también por nombre."""
    (certs / CERT_PROD).write_text("cert")
    (certs / CLAVE_PROD).write_text("clave")
    ruta = str(tmp_path / "dev.db")
    _instancia(ruta, ambiente="homologacion", columnas={
        "certificado_path_homologacion": f"/viejo/data/arca_certs/{CERT_PROD}",
        "clave_path_homologacion": f"/viejo/data/arca_certs/{CLAVE_PROD}",
    })
    _migrar(ruta, certs)
    assert _revision(ruta) == REVISION

    assert (certs / CERT_HOMO).exists()
    assert os.path.basename(_fila(ruta)["certificado_path_homologacion"]) == CERT_HOMO


# -- Los controles negativos: a quién NO hay que tocarle nada ---------------

def test_la_instancia_de_produccion_NO_se_toca(tmp_path, certs):
    """🔴 El control que más importa. La instancia de cliente de Contalibra está
    en `produccion` con su par bajo `certificado.crt`, que es **su nombre
    correcto**. Moverlo le rompería la facturación real."""
    (certs / CERT_PROD).write_text("el certificado REAL del cliente")
    (certs / CLAVE_PROD).write_text("la clave REAL")
    ruta = str(tmp_path / "cliente.db")
    _instancia(ruta, ambiente="produccion", columnas={
        "certificado_path": str(certs / CERT_PROD),
        "clave_path": str(certs / CLAVE_PROD),
    })
    _migrar(ruta, certs)
    assert _revision(ruta) == REVISION

    fila = _fila(ruta)
    assert fila["certificado_path"] == str(certs / CERT_PROD)
    assert (certs / CERT_PROD).read_text() == "el certificado REAL del cliente"
    assert not (certs / CERT_HOMO).exists(), "le inventó un par de homologación"


def test_una_instancia_sin_arca_no_rompe(tmp_path, certs):
    """La mayoría del parque no tiene ARCA configurado."""
    ruta = str(tmp_path / "vacia.db")
    core._db_path = None
    core._database_url = None
    core.configure(db_path=ruta)
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    c.close()
    core._db_path = None

    r = _migrar(ruta, certs)
    assert _revision(ruta) == REVISION, r.stderr[-700:]


def test_un_archivo_que_no_esta_vacia_la_columna(tmp_path, certs):
    """🔴 Dejar la columna apuntando a un nombre ajeno es la trampa: el día que
    el otro ambiente suba su par, ese archivo aparece y esta columna empieza a
    leer el par del vecino."""
    ruta = str(tmp_path / "sin-archivo.db")
    _instancia(ruta, ambiente="homologacion", columnas={
        "certificado_path_homologacion": f"/no/existe/{CERT_PROD}",
        "clave_path_homologacion": f"/no/existe/{CLAVE_PROD}",
    })
    _migrar(ruta, certs)
    assert _revision(ruta) == REVISION

    fila = _fila(ruta)
    assert fila["certificado_path_homologacion"] == ""
    assert fila["clave_path_homologacion"] == ""


def test_no_pisa_un_destino_que_ya_existe(tmp_path, certs):
    """Si el operador ya subió su par de homologación por la pantalla, el suyo
    manda: la revisión sólo apunta la columna, no reemplaza el archivo."""
    (certs / CERT_HOMO).write_text("el que subio el operador")
    (certs / CERT_PROD).write_text("el viejo, con nombre de produccion")
    ruta = str(tmp_path / "ya-subido.db")
    _instancia(ruta, ambiente="homologacion", columnas={
        "certificado_path_homologacion": str(certs / CERT_PROD),
    })
    _migrar(ruta, certs)
    assert _revision(ruta) == REVISION

    assert (certs / CERT_HOMO).read_text() == "el que subio el operador"
    assert os.path.basename(_fila(ruta)["certificado_path_homologacion"]) == CERT_HOMO


def test_con_dos_duenos_no_adivina(tmp_path, certs):
    """Si las columnas de los dos ambientes apuntan al mismo archivo no hay
    forma de saber de quién es. Se deja como está, en vez de elegir."""
    (certs / CERT_PROD).write_text("de quien es?")
    ruta = str(tmp_path / "ambiguo.db")
    _instancia(ruta, ambiente="homologacion", columnas={
        "certificado_path": str(certs / CERT_PROD),
        "certificado_path_homologacion": str(certs / CERT_PROD),
    })
    _migrar(ruta, certs)
    assert _revision(ruta) == REVISION

    fila = _fila(ruta)
    assert fila["certificado_path_homologacion"] == str(certs / CERT_PROD)
    assert (certs / CERT_PROD).exists()


def test_correr_dos_veces_no_cambia_nada(tmp_path, certs):
    """Idempotencia sobre archivos, no sólo sobre DDL."""
    (certs / CERT_PROD).write_text("cert")
    ruta = str(tmp_path / "dos-veces.db")
    _instancia(ruta, ambiente="homologacion", columnas={
        "certificado_path_homologacion": str(certs / CERT_PROD),
    })
    _migrar(ruta, certs)
    primera = _fila(ruta)["certificado_path_homologacion"]

    c = sqlite3.connect(ruta)
    c.execute("UPDATE alembic_version SET version_num='0007_par_arca_por_ambiente'")
    c.commit()
    c.close()

    _migrar(ruta, certs)
    assert _revision(ruta) == REVISION
    assert _fila(ruta)["certificado_path_homologacion"] == primera
    assert (certs / CERT_HOMO).read_text() == "cert"
