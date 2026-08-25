"""Que las migraciones de LibraCore VIAJEN en el wheel y apunten a la base correcta.

🔴 **El motivo es un defecto medido, no una mejora teórica.** Hasta acá
`migrations/` vivía en la raíz del repo, fuera de `packages = ["libracore"]`, así
que no entraba al wheel. Un consumidor que instala LibraCore con pip adentro de
su imagen no tenía con qué aplicarlas: `scripts/run_migrations.sh` **clona el
repo**, y en un contenedor no hay repo.

Resultado medido el 2026-08-25 sobre las 14 bases del VPS con schema de
LibraCore: **7 sin `alembic_version` ninguna**, 2 en `0001_baseline` y sólo las 5
de dev en `0002`. Las cuatro columnas que la `0002` agrega a `clients` faltan en
9 de las 14, y no rompen sólo porque `libracore.db.clients` introspecta la tabla
en cada alta.

🔑 **El test que importa es el del wheel construido.** Afirmar sobre
`pyproject.toml` mediría la intención; afirmar sobre el árbol del repo mediría el
checkout. Lo único que contesta «¿el consumidor las recibe?» es abrir el `.whl`.

Y el otro que importa es el de la precedencia del destino, más abajo: es el que
distingue *migrar la base del core* de *migrar la del dominio y devolver éxito*.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from libracore import migrar

RAIZ = Path(__file__).resolve().parents[2]


# ── El paquete instalado sabe dónde están ────────────────────────────────


def test_el_directorio_de_migraciones_esta_dentro_del_paquete():
    assert migrar.DIRECTORIO.is_dir(), migrar.DIRECTORIO
    # Dentro del paquete y no al lado: es lo que hace que entre al wheel.
    assert migrar.DIRECTORIO.parent.name == "libracore"


def test_estan_las_revisiones_y_el_env():
    revisiones = sorted(p.name for p in (migrar.DIRECTORIO / "versions").glob("*.py"))
    # No se fija el número exacto —crece con cada migración nueva— pero sí que
    # la baseline esté: si `versions/` viajara vacío, el `upgrade` no haría nada
    # y **no fallaría**, que es el peor resultado posible.
    assert "0001_baseline_schema_core.py" in revisiones
    assert (migrar.DIRECTORIO / "env.py").is_file()


def test_la_configuracion_apunta_al_paquete_y_no_al_cwd(monkeypatch, tmp_path):
    """🔑 Se resuelve desde `__file__`, que es la diferencia entre andar en el
    repo y andar en site-packages."""
    monkeypatch.chdir(tmp_path)  # lejos de la raíz del repo
    cfg = migrar.configuracion("postgresql://u:p@h/db")
    ubicacion = Path(cfg.get_main_option("script_location"))

    assert ubicacion.is_absolute()
    assert (ubicacion / "versions" / "0001_baseline_schema_core.py").is_file()


def test_las_migraciones_viajan_en_el_wheel(tmp_path):
    """El único que contesta la pregunta real: se construye el wheel y se abre.

    `build` está en el extra `dev` a propósito, para que el CI **no** saltee
    esto: un skip acá se lee igual que un verde, y es justo el chequeo que
    faltaba cuando el directorio vivía en la raíz.
    """
    pytest.importorskip("build", reason="falta `build` en el extra dev")

    salida = tmp_path / "dist"
    proceso = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(salida), str(RAIZ)],
        capture_output=True,
        text=True,
    )
    assert proceso.returncode == 0, proceso.stderr[-2000:]

    wheels = list(salida.glob("*.whl"))
    assert len(wheels) == 1, wheels

    with zipfile.ZipFile(wheels[0]) as z:
        nombres = z.namelist()

    revisiones = [n for n in nombres if n.startswith("libracore/migrations/versions/")
                  and n.endswith(".py")]
    assert "libracore/migrations/env.py" in nombres, (
        "el `env.py` no viaja en el wheel: `libracore-migrar` no tiene entorno "
        "que ejecutar del lado del consumidor.")
    assert revisiones, (
        "el wheel no lleva ninguna revisión. El `upgrade` del consumidor saldría "
        "en VERDE sin aplicar nada, que es el peor resultado posible.")
    # Control de que no viaja basura: los `.pyc` quedan fuera por el `exclude`.
    assert not [n for n in nombres if n.endswith(".pyc")], "viajaron `.pyc`"


# ── Contra qué base migra, que es la mitad del módulo ─────────────────────

#: Las formas reales del entorno de cada producto, medidas en sus contenedores
#: el 2026-08-25. Los tres primeros llevan el schema del core en una base
#: **aparte**; los otros, en la misma que el dominio.
ENTORNOS = [
    # (prefijo, entorno, esperado, por qué)
    ("gestiolibra",
     {"DATABASE_URL": "postgresql://u:p@h/gestiolibra",
      "GESTIOLIBRA_LIBRACORE_DB_PATH": "postgresql://u:p@h/gestiolibra_core"},
     "postgresql://u:p@h/gestiolibra_core",
     "base separada, con el nombre HISTORICO de la variable"),
    ("medlibra",
     {"DATABASE_URL": "postgresql://u:p@h/medlibra",
      "MEDLIBRA_LIBRACORE_DATABASE_URL": "postgresql://u:p@h/medlibra_core"},
     "postgresql://u:p@h/medlibra_core",
     "base separada, con el nombre normalizado"),
    ("libraclub",
     {"DATABASE_URL": "postgresql://u:p@h/libraclub",
      "LIBRACLUB_LIBRACORE_DATABASE_URL": "postgresql://u:p@h/libraclub_core"},
     "postgresql://u:p@h/libraclub_core",
     "base separada"),
    ("contalibra",
     {"CONTALIBRA_DATABASE_URL": "postgresql://u:p@h/contalibra"},
     "postgresql://u:p@h/contalibra",
     "base unica: el core vive en la del dominio"),
    ("ventalibra",
     {"VENTALIBRA_DB_PATH": "postgresql://u:p@h/ventalibra"},
     "postgresql://u:p@h/ventalibra",
     "base unica, con el nombre historico que MIENTE (_DB_PATH con una URL)"),
]


@pytest.mark.parametrize("prefijo,entorno,esperado,motivo", ENTORNOS)
def test_url_de_core_elige_la_base_del_core(prefijo, entorno, esperado, motivo):
    """🔴 El error que esta función existe para evitar es **silencioso**.

    El comando corre adentro del contenedor del producto, donde `DATABASE_URL`
    es la base del dominio. En los tres productos con base separada, un
    `upgrade` que la tomara crearía las tablas del core al lado de las del
    dominio, dejaría la base real sin tocar y devolvería éxito.

    Los entornos de esta tabla son los **reales**, leídos de los contenedores:
    tres nombres distintos para lo mismo, dos de ellos históricos.
    """
    assert migrar.url_de_core(prefijo, entorno=entorno) == esperado, motivo


def test_url_de_core_no_cae_al_dominio_si_el_core_esta_declarado():
    """Control del caso 3: la caída al dominio es por AUSENCIA, no por default.

    Sin este test, una implementación que devolviera siempre la del dominio
    pasaría los dos casos de base única de la tabla de arriba.
    """
    entorno = {"DATABASE_URL": "postgresql://u:p@h/dominio",
               "GESTIOLIBRA_LIBRACORE_DATABASE_URL": "postgresql://u:p@h/core"}
    assert migrar.url_de_core("gestiolibra", entorno=entorno).endswith("/core")


def test_un_producto_fuera_de_la_convencion_FALLA_en_vez_de_adivinar():
    """🔴 **La mitad de los productos no usan `url_de_instancia`, y eso importa.**

    Medido el 2026-08-25 sobre los ocho: sólo LibraDesk, VentaLibra, Gestiolibra
    y MedLibra resuelven su base con `url_de_instancia`. LibraCargo, LibraClub,
    Contalibra y Restolibra la leen por su cuenta, así que sus nombres no están
    en el mapa de la convención — LibraCargo, por ejemplo, usa `DATABASE_URL` a
    secas y el mapa no lo lista.

    Lo que se aserta acá es que en ese caso **se frena**. La alternativa
    tentadora —caer a `DATABASE_URL` cuando el prefijo no resuelve— es
    exactamente el defecto que este módulo existe para evitar: adentro de un
    contenedor de Gestiolibra al que le faltara la variable del core, migraría
    la base del dominio y devolvería éxito. Para esos productos el destino tiene
    que llegar explícito, por `LIBRACORE_MIGRAR_URL`.
    """
    entorno = {"DATABASE_URL": "postgresql://u:p@h/libracargo"}
    with pytest.raises(migrar.SinURL, match="LIBRACARGO"):
        migrar.url_de_core("libracargo", entorno=entorno)

    # Y con el destino explícito, el mismo producto resuelve sin problema.
    entorno["LIBRACORE_MIGRAR_URL"] = "postgresql://u:p@h/libracargo"
    assert migrar.url_de_core("libracargo", entorno=entorno).endswith("/libracargo")


def test_url_de_core_sin_nada_falla_nombrando_las_variables():
    with pytest.raises(migrar.SinURL, match="LIBRACORE_DATABASE_URL"):
        migrar.url_de_core("gestiolibra", entorno={})


def test_la_salida_de_emergencia_gana_sobre_todo():
    entorno = {"LIBRACORE_MIGRAR_URL": "postgresql://u:p@h/elegida",
               "GESTIOLIBRA_LIBRACORE_DATABASE_URL": "postgresql://u:p@h/core"}
    assert migrar.url_de_core("gestiolibra", entorno=entorno).endswith("/elegida")


# ── El driver ────────────────────────────────────────────────────────────


def test_el_prefijo_pelado_se_manda_a_psycopg3():
    """SQLAlchemy resuelve `postgresql://` a psycopg2, y este repo instala
    psycopg 3."""
    cfg = migrar.configuracion("postgresql://u:p@h/db")
    assert cfg.get_main_option("libracore.url") == "postgresql+psycopg://u:p@h/db"


def test_un_driver_explicito_no_se_pisa():
    """Control del de arriba: sólo se toca el prefijo pelado."""
    cfg = migrar.configuracion("postgresql+psycopg2://u:p@h/db")
    assert cfg.get_main_option("libracore.url") == "postgresql+psycopg2://u:p@h/db"


def test_una_ruta_sqlite_no_se_traduce():
    """🔑 `env.py` acepta las dos formas y decide el bind. Traducir acá rompería
    el caso SQLite, que sigue vivo en LibraEdge y en instancias viejas."""
    cfg = migrar.configuracion("/root/contalibra/clientes/demo/data/contalibra.db")
    assert cfg.get_main_option("libracore.url").endswith("/contalibra.db")


# ── El que decide de verdad: migrar dos bases y ver cuál se tocó ──────────


def test_el_destino_explicito_gana_sobre_DATABASE_URL(tmp_path, monkeypatch):
    """🔴 **La regresión que ningún unitario ve, y la que más caro sale acá.**

    `env.py` leía `DATABASE_URL` del entorno y **no miraba** la opción que
    `configuracion()` pone en el `Config`. Adentro de un contenedor de
    Gestiolibra —donde la variable siempre está puesta y apunta al dominio—
    `upgrade(url_del_core)` habría migrado **la del dominio** y devuelto éxito.

    Los tests de `url_de_core` de arriba pasan igual con ese defecto, porque
    afirman sobre el valor que este módulo resuelve y no sobre el que alembic
    termina usando. Por eso acá se migran **dos bases** y se mide cuál quedó
    tocada.
    """
    core_db = tmp_path / "core.db"
    dominio_db = tmp_path / "dominio.db"
    # La del dominio existe y está vacía: si el upgrade se fuera contra ella, lo
    # que aparecería son las tablas del core, no un error.
    sqlite3.connect(dominio_db).close()
    monkeypatch.setenv("DATABASE_URL", str(dominio_db))

    migrar.upgrade(str(core_db))

    def tablas(ruta: Path) -> set[str]:
        with sqlite3.connect(ruta) as c:
            return {f[0] for f in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    del_core = tablas(core_db)
    del_dominio = tablas(dominio_db)

    assert "alembic_version" in del_core, (
        f"la base explícita no se migró: {sorted(del_core)}")
    assert "clients" in del_core, (
        "quedó la tabla de versiones pero no el schema: la baseline no corrió")
    # 🔑 La mitad que importa: la del entorno NO se tocó.
    assert del_dominio == set(), (
        "se migró la base de `DATABASE_URL` en vez de la explícita: "
        f"{sorted(del_dominio)}")


# ── La CLI ───────────────────────────────────────────────────────────────


def test_un_comando_que_no_existe_sale_distinto_de_cero():
    """Un typo en un pipeline de deploy no puede leerse como éxito."""
    assert migrar.main(["invento"]) == 2


def test_la_ayuda_sale_con_cero(capsys):
    assert migrar.main(["--help"]) == 0
    assert "migraciones en:" in capsys.readouterr().out


def test_sin_destino_la_cli_sale_1(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LIBRACORE_MIGRAR_URL", raising=False)
    assert migrar.main(["upgrade"]) == 1
    assert "prefijo" in capsys.readouterr().err


def test_la_cli_parsea_el_prefijo_en_las_dos_formas():
    assert migrar._parsear(["upgrade", "--prefijo", "gestiolibra"]) == (
        "upgrade", "gestiolibra", None)
    assert migrar._parsear(["upgrade", "--prefijo=medlibra"]) == (
        "upgrade", "medlibra", None)
    assert migrar._parsear(["stamp", "--prefijo=x", "0001_baseline"]) == (
        "stamp", "x", "0001_baseline")


def test_la_cli_migra_de_verdad_con_prefijo(tmp_path, monkeypatch, capsys):
    """El camino entero desde la línea de comandos, con el prefijo resolviendo.

    Control positivo del `sale_1` de arriba: sin esto, un `main` que devolviera
    1 siempre pasaría aquel test.
    """
    destino = tmp_path / "core.db"
    monkeypatch.delenv("LIBRACORE_MIGRAR_URL", raising=False)
    monkeypatch.setenv("VENTALIBRA_LIBRACORE_DATABASE_URL", str(destino))

    assert migrar.main(["upgrade", "--prefijo", "ventalibra"]) == 0

    with sqlite3.connect(destino) as c:
        revision = c.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision and revision[0], "no quedó revisión estampada"
