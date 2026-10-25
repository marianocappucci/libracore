"""El alta de GestioLibra y MedLibra nace con UNA base (ADR-083).

Hasta el 2026-10-25 los dos llevaban el core en `<producto>_core` y el alta lo creaba aparte: `base_core_separada=True` en el
`configure()` del producto, un `10-bases-extra.sql` con el `CREATE DATABASE` y dos URL distintas en el compose. Desde que figuran en
`url_de_instancia._UNA_SOLA_BASE` esa lista manda sobre el argumento: es la misma que leen `libracore-migrar` y `libraauth-migrar`, y si el
alta creara una base que la migración no mira, la instancia nacería con un core vacío al lado.

Lo que se fija:
- con `base_core_separada=True` (lo que los productos todavía pasan hasta su PR 6) **o** con `False`, el alta crea una sola base y NO
  escribe el `CREATE DATABASE <producto>_core`;
- el compose lleva sólo las variables del dominio (`<P>_DATABASE_URL` y el histórico `DATABASE_URL`): **desde ADR-084 el alta no
  escribe más `<P>_LIBRACORE_DATABASE_URL` ni `<P>_LIBRACORE_DB_PATH`** para un producto de una sola base (apuntaban a la misma
  URL y no decían nada), salvo VentaLibra, cuya app todavía las exige;
- con ese entorno, `libracore-migrar` resuelve el core en la base del dominio y migra **ahí**, de verdad, contra PostgreSQL;
- un producto que NO figura en la lista y declara el core aparte sigue naciendo con dos bases (el control).
"""
import sys
import types
from pathlib import Path

import pytest
import yaml
from pg_descartable import conectar

from libracore import migrar, provisioning
from libracore.provisioning import nuevo_cliente as nc

CUIT = "30-71234567-8"
PRODUCTOS = ["gestiolibra", "medlibra"]


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


@pytest.fixture
def sin_docker(monkeypatch):
    import subprocess

    mod = types.ModuleType("plans")
    mod.PLANES = ["basico", "pro"]
    mod.aplicar_plan_en_db = lambda db_path, plan: None
    monkeypatch.setitem(sys.modules, "plans", mod)
    monkeypatch.setattr(nc.subprocess, "run", lambda args, **k: subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(nc, "_esperar_db_lista", lambda *a, **k: True)
    monkeypatch.setattr(nc, "_esperar_tabla_en_sidecar", lambda *a, **k: True)
    monkeypatch.setattr(nc, "_aplicar_plan_en_contenedor", lambda *a, **k: True)
    monkeypatch.setattr(nc, "migraciones_de_la_imagen",
                        lambda repo_root, image_ref, script=None: (provisioning.get_config().migraciones, "abc1234"))


def _alta(tmp_path, prefijo, **configure):
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True, exist_ok=True)
    provisioning.configure(product_name=prefijo.upper(), image_name=f"{prefijo}:latest", container_prefix=prefijo,
                           db_filename=f"{prefijo}.db", repo_root=repo_root, base_port=9000, postgres=True, **configure)
    cfg = provisioning.get_config()
    nc.crear_cliente(empresa_cuit=CUIT, nombre="Cliente Uno", slug="uno", setup_npm=False)
    carpeta = cfg.clientes_dir / "uno"
    doc = yaml.safe_load((carpeta / "docker-compose.yml").read_text())
    env = doc["services"][f"{prefijo}-uno"]["environment"]
    pares = dict(e.split("=", 1) for e in env if "=" in e)
    return cfg, carpeta, doc, pares


@pytest.mark.parametrize("separada", [True, False])
@pytest.mark.parametrize("prefijo", PRODUCTOS)
def test_el_alta_crea_una_sola_base_aunque_el_producto_siga_pidiendo_el_core_aparte(tmp_path, sin_docker, prefijo, separada):
    cfg, carpeta, doc, pares = _alta(tmp_path, prefijo, base_core_separada=separada)

    assert cfg.bases_postgres == (prefijo,), "una sola base: la del dominio"
    assert cfg.core_separado is False
    assert [base for _, base in cfg.db_urls] == [prefijo, prefijo]

    # el sidecar nace con esa base y no hay init que cree otra
    sidecar = doc["services"][f"{prefijo}-uno-postgres"]
    assert sidecar["environment"]["POSTGRES_DB"] == prefijo
    assert not (carpeta / "postgres-init").exists(), "no hay `CREATE DATABASE <producto>_core` que correr"
    assert not any("postgres-init" in v for v in sidecar["volumes"])
    assert f"{prefijo}_core" not in (carpeta / "docker-compose.yml").read_text()


@pytest.mark.parametrize("prefijo", PRODUCTOS)
def test_el_alta_ya_no_escribe_las_variables_del_core_y_si_las_del_dominio(tmp_path, sin_docker, prefijo):
    """ADR-084. Antes (ADR-083) las escribía apuntando a la misma URL que la del dominio. Un producto de una sola base no las
    necesita: sin ellas `libracore-migrar`, `libraauth-migrar --base core` y la app caen a la base del dominio."""
    cfg, carpeta, _, pares = _alta(tmp_path, prefijo, base_core_separada=True)
    p = prefijo.upper()
    dominio = pares[f"{p}_DATABASE_URL"]
    assert dominio.startswith("postgresql+psycopg://") and dominio.endswith(f"/{prefijo}")
    assert pares["DATABASE_URL"] == dominio, "el histórico del dominio sigue: lo lee migrations/env.py de los productos"
    assert not [k for k in pares if "LIBRACORE" in k], sorted(pares)
    assert "LIBRACORE" not in (carpeta / "docker-compose.yml").read_text()
    # `db_urls` sigue diciendo las dos (el plan de módulos usa la primera): lo que cambia es lo que se escribe
    assert [var for var, _ in cfg.db_urls] == [f"{p}_DATABASE_URL", f"{p}_LIBRACORE_DATABASE_URL"]


@pytest.mark.parametrize("prefijo", PRODUCTOS)
def test_con_el_entorno_del_alta_el_core_se_resuelve_en_la_base_del_dominio(tmp_path, sin_docker, prefijo):
    _, _, _, pares = _alta(tmp_path, prefijo, base_core_separada=True)
    assert not [k for k in pares if "LIBRACORE" in k], "el alta no escribe la variable del core (ADR-084)"
    assert migrar.url_de_core(prefijo, entorno=pares) == pares[f"{prefijo.upper()}_DATABASE_URL"]


def test_un_producto_fuera_de_la_lista_que_pide_el_core_aparte_sigue_naciendo_con_dos_bases(tmp_path, sin_docker):
    """El control: la lista manda sólo sobre los productos que nombra. Un producto nuevo declara lo que quiere."""
    cfg, carpeta, doc, pares = _alta(tmp_path, "productonuevo", base_core_separada=True)
    assert cfg.bases_postgres == ("productonuevo", "productonuevo_core")
    assert pares["PRODUCTONUEVO_DATABASE_URL"].endswith("/productonuevo")
    assert pares["PRODUCTONUEVO_LIBRACORE_DATABASE_URL"].endswith("/productonuevo_core")
    assert "CREATE DATABASE productonuevo_core" in (carpeta / "postgres-init" / "10-bases-extra.sql").read_text()


@pytest.mark.parametrize("prefijo", ["contalibra", "restolibra", "ventalibra", "libradesk", "libracargo", "libraclub"])
def test_los_que_ya_eran_de_una_sola_base_no_cambian(tmp_path, sin_docker, prefijo):
    cfg, carpeta, _, pares = _alta(tmp_path, prefijo)
    assert cfg.bases_postgres == (prefijo,)
    assert not (carpeta / "postgres-init").exists()
    assert cfg.tabla_modulos == "modulos"


@pytest.mark.parametrize("prefijo", ["contalibra", "restolibra", "libradesk", "libracargo", "libraclub", "gestiolibra", "medlibra"])
def test_ningun_producto_de_una_sola_base_que_arranca_sin_ella_recibe_la_variable_del_core(tmp_path, sin_docker, prefijo):
    """ADR-084. Los siete se midieron sobre `origin/main` (2026-10-25): sin la variable arrancan con el core en la base del
    dominio. El del dominio se escribe siempre."""
    _, _, _, pares = _alta(tmp_path, prefijo)
    assert [k for k in pares if "LIBRACORE" in k] == []
    assert pares[f"{prefijo.upper()}_DATABASE_URL"].endswith(f"/{prefijo}")


def test_ventalibra_sigue_recibiendo_la_variable_del_core_porque_su_app_todavia_la_exige(tmp_path, sin_docker):
    """`app/main.py` de VentaLibra resuelve el core con `url_de_instancia("ventalibra", core=True, default="./data/ventalibra_libracore.db")`:
    sin la variable le pasa una ruta SQLite a `billing.configure`, que la rechaza. Mientras sea así su alta las escribe, las dos
    (la normalizada y el histórico `_DB_PATH`), a la misma URL que la del dominio."""
    _, _, _, pares = _alta(tmp_path, "ventalibra")
    dominio = pares["VENTALIBRA_DATABASE_URL"]
    assert pares["VENTALIBRA_LIBRACORE_DATABASE_URL"] == dominio
    assert pares["VENTALIBRA_LIBRACORE_DB_PATH"] == dominio


def test_un_producto_nuevo_con_el_core_aparte_sigue_recibiendo_la_variable_del_core(tmp_path, sin_docker):
    """El control del otro lado: fuera de `_UNA_SOLA_BASE` la variable es la que dice cuál es la base del core."""
    for separada, base in ((True, "productonuevo_core"), (False, "productonuevo")):
        provisioning._cfg = None
        _, _, _, pares = _alta(tmp_path / str(separada), "productonuevo", base_core_separada=separada)
        assert pares["PRODUCTONUEVO_LIBRACORE_DATABASE_URL"].endswith(f"/{base}")


@pytest.mark.parametrize("prefijo", PRODUCTOS)
def test_el_alta_puede_declarar_la_tabla_de_modulos_del_producto(tmp_path, sin_docker, prefijo):
    cfg, *_ = _alta(tmp_path, prefijo, base_core_separada=True, tabla_modulos=f"{prefijo}_modulos")
    assert cfg.tabla_modulos == f"{prefijo}_modulos"


# ── contra PostgreSQL de verdad ───────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prefijo", PRODUCTOS)
def test_libracore_migrar_con_el_entorno_del_alta_migra_la_base_del_dominio_y_ninguna_otra(tmp_path, sin_docker, bases, prefijo):
    """La mitad que importa: no alcanza con que `url_de_core` devuelva el valor esperado, tiene que ser el que alembic termina usando.
    Dos bases de verdad: la del dominio (con una tabla del producto) y una `<producto>_core` vacía que NO debe tocarse."""
    _, _, _, pares = _alta(tmp_path, prefijo, base_core_separada=True)
    dominio = bases.nueva()
    core_viejo = bases.nueva(sufijo="_core")
    with conectar(dominio) as c:
        c.execute("CREATE TABLE tabla_del_producto (id SERIAL PRIMARY KEY)")
        c.commit()

    # el entorno del alta, con el host del sidecar reemplazado por las bases de prueba, base por base: lo que el alta
    # escribió para el dominio va a `dominio` y, si hubiera escrito un `<producto>_core`, iría a `core_viejo`
    destinos = {prefijo: dominio, f"{prefijo}_core": core_viejo}
    entorno = {k: destinos[v.rsplit("/", 1)[1]] for k, v in pares.items() if v.startswith("postgresql")}
    destino = migrar.url_de_core(prefijo, entorno=entorno)
    assert destino == dominio
    migrar.upgrade(destino)

    def tablas(url):
        with conectar(url) as c:
            return {r[0] for r in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()}

    del_dominio = tablas(dominio)
    assert {"tabla_del_producto", "clients", "modulos", "caja_movimientos", "alembic_version"} <= del_dominio, sorted(del_dominio)
    assert tablas(core_viejo) == set(), "la base `<producto>_core` no se creó ni se tocó"


def test_el_lugar_de_escritura_de_la_tabla_de_modulos_con_el_entorno_del_alta(tmp_path, sin_docker, bases):
    """GestioLibra, de punta a punta con lo que hace el alta DENTRO del contenedor: el motor migrado en la base del dominio (su
    `modulos` ahí), la tabla propia del producto al lado, y los adicionales prendidos en la del producto."""
    from libracore.provisioning import aplicar_modulos_de_adicionales

    _, _, _, pares = _alta(tmp_path, "gestiolibra", base_core_separada=True, tabla_modulos="gestiolibra_modulos")
    dominio = bases.nueva()
    entorno = {k: dominio for k, v in pares.items() if v.startswith("postgresql")}
    migrar.upgrade(migrar.url_de_core("gestiolibra", entorno=entorno))
    with conectar(dominio) as c:
        c.execute("CREATE TABLE gestiolibra_modulos (modulo TEXT PRIMARY KEY, habilitado BOOLEAN NOT NULL DEFAULT TRUE, "
                  "plan TEXT NOT NULL DEFAULT 'premium')")
        c.commit()

    aplicar_modulos_de_adicionales(dominio, {"offline": True}, tabla="gestiolibra_modulos")

    with conectar(dominio) as c:
        assert c.execute("SELECT modulo, habilitado, plan FROM gestiolibra_modulos").fetchall() == [("offline", True, "addon")]
        assert c.execute("SELECT count(*) FROM modulos").fetchone()[0] == 0, "la `modulos` del motor no es del producto"
