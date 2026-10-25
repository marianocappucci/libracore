"""El nombre de la tabla de módulos es configuración del producto (ADR-083).

🔴 **El defecto que cierra.** GestioLibra y MedLibra renombraron su `modulos` a `gestiolibra_modulos` y `medlibra_modulos` para poder
unir sus dos bases (la `modulos` del motor chocaba con la suya, ADR-078). El alta (`nuevo_cliente`) esperaba la tabla `modulos` en la base
del dominio y escribía los adicionales en `modulos`: con una sola base esa tabla **existe siempre** (es la del motor, que crea
`libracore-migrar`), así que la espera daba por lista una base cuyo `gestiolibra_modulos` todavía no estaba y los adicionales se prendían en
una tabla que el producto no lee —sin fallar—. Con dos bases, en cambio, la espera no veía nada y el alta terminaba en `AltaIncompleta`.

Lo que se fija:
- el nombre es un identificador SQL válido (se interpola en SQL: nada de comillas, puntos, mayúsculas ni 64 caracteres);
- sin declararlo, **ni un carácter cambia**: el comando de la espera, el de los adicionales y las llamadas son los de siempre;
- declarado, la espera pregunta por esa tabla y los adicionales viajan con `tabla=` dentro del contenedor;
- contra PostgreSQL de verdad, con la `modulos` del motor **al lado** (como en una base unida): se escribe en la del producto y la del
  motor no se toca, y cada una con el tipo de `habilitado` que tiene.
"""
import subprocess
import sys
import types

import pytest
from pg_descartable import conectar

from libracore import provisioning
from libracore.provisioning import (
    ProductConfig,
    _como_se_escribe_habilitado,
    aplicar_modulos_de_adicionales,
    apply_plan_modules,
    tabla_de_modulos_valida,
)
from libracore.provisioning import nuevo_cliente as nc

CUIT = "30-71234567-8"

# Capturadas antes de que ningún fixture las reemplace: el `fake_docker` las stubea.
_ESPERA_REAL = nc._esperar_tabla_en_sidecar
_APLICAR_PLAN_REAL = nc._aplicar_plan_en_contenedor


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


# ── el identificador ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("nombre", ["modulos", "gestiolibra_modulos", "medlibra_modulos", "_m", "m1", "a" * 63])
def test_los_nombres_validos_pasan_tal_cual(nombre):
    assert tabla_de_modulos_valida(nombre) == nombre


@pytest.mark.parametrize("nombre", [
    "", " ", None, 7, "Modulos", "GestioLibra_Modulos",   # minúsculas: PostgreSQL pliega y `information_schema` guarda el plegado
    "1modulos", "modulos ", " modulos", "mod ulos", "mod-ulos",
    "public.modulos", '"modulos"', "modulos;", "modulos; DROP TABLE clients", "modulos--", "modulos'", "módulos",
    "a" * 64,
])
def test_los_nombres_que_no_son_un_identificador_se_rechazan(nombre):
    """Se interpola en `INSERT INTO <tabla>` y en `table_name='<tabla>'`: los identificadores no se parametrizan."""
    with pytest.raises(ValueError, match="tabla de módulos"):
        tabla_de_modulos_valida(nombre)


def test_configure_sin_tabla_modulos_deja_modulos(tmp_path):
    provisioning.configure(product_name="X", image_name="x:latest", container_prefix="x", db_filename="x.db",
                           repo_root=tmp_path)
    assert provisioning.get_config().tabla_modulos == "modulos"


def test_configure_con_tabla_modulos_la_guarda(tmp_path):
    provisioning.configure(product_name="X", image_name="x:latest", container_prefix="x", db_filename="x.db",
                           repo_root=tmp_path, tabla_modulos="x_modulos")
    assert provisioning.get_config().tabla_modulos == "x_modulos"


def test_configure_rechaza_un_nombre_invalido_y_no_deja_configuracion_a_medias(tmp_path):
    with pytest.raises(ValueError, match="tabla de módulos"):
        provisioning.configure(product_name="X", image_name="x:latest", container_prefix="x", db_filename="x.db",
                               repo_root=tmp_path, tabla_modulos="modulos; drop table clients")
    assert provisioning._cfg is None


def test_construir_la_configuracion_a_mano_tambien_valida(tmp_path):
    with pytest.raises(ValueError, match="tabla de módulos"):
        ProductConfig(product_name="X", image_name="x:latest", container_prefix="x", db_filename="x.db",
                      repo_root=tmp_path, tabla_modulos="Modulos")


# ── el comando de la espera ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def _psql_emitido(monkeypatch, **kwargs):
    """El SQL que `_esperar_tabla_en_sidecar` manda por `docker exec`, sin Docker."""
    ejecutados = []

    def fake_run(args, **k):
        ejecutados.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="1", stderr="")

    monkeypatch.setattr(nc.subprocess, "run", fake_run)
    assert _ESPERA_REAL("sidecar", "base", "usuario", **kwargs) is True
    assert len(ejecutados) == 1
    return ejecutados[0]


def test_la_espera_por_defecto_pregunta_por_modulos_y_el_comando_es_el_de_siempre(monkeypatch):
    comando = _psql_emitido(monkeypatch)
    assert comando == [
        "docker", "exec", "sidecar", "psql", "-U", "usuario", "-d", "base", "-tAc",
        "select count(*) from information_schema.tables where table_schema='public' and table_name='modulos'",
    ]


def test_la_espera_con_tabla_propia_pregunta_por_esa(monkeypatch):
    comando = _psql_emitido(monkeypatch, tabla="gestiolibra_modulos")
    assert comando[-1].endswith("table_name='gestiolibra_modulos'")
    assert "table_name='modulos'" not in comando[-1]


def test_la_espera_no_llega_a_Docker_con_un_nombre_invalido(monkeypatch):
    llamadas = []
    monkeypatch.setattr(nc.subprocess, "run", lambda *a, **k: llamadas.append(a))
    with pytest.raises(ValueError):
        _ESPERA_REAL("sidecar", "base", "usuario", tabla="modulos' or '1'='1")
    assert llamadas == []


def test_la_espera_de_sqlite_mira_la_tabla_que_se_le_dice(tmp_path):
    import sqlite3

    ruta = tmp_path / "x.db"
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE modulos (modulo TEXT PRIMARY KEY)")
    con.commit()
    con.close()
    espera = nc._esperar_db_lista
    assert espera(ruta, timeout=1) is True
    assert espera(ruta, timeout=1, tabla="x_modulos") is False
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE x_modulos (modulo TEXT PRIMARY KEY)")
    con.commit()
    con.close()
    assert espera(ruta, timeout=1, tabla="x_modulos") is True


# ── el comando de los adicionales (corre DENTRO del contenedor, donde `configure()` no corrió) ─────────────────────────────────────

def _codigo_de_adicionales(monkeypatch, **kwargs):
    ejecutados = []
    monkeypatch.setattr(nc.subprocess, "run",
                        lambda args, **k: ejecutados.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    assert nc._aplicar_adicionales_en_contenedor("cont", "VAR_URL", {"offline": True}, **kwargs) is True
    return ejecutados[0][-1]


def test_los_adicionales_sin_tabla_propia_mandan_exactamente_el_codigo_de_siempre(monkeypatch):
    """Una imagen de un producto que no subió el pin no acepta `tabla=`: no se le manda si no hace falta."""
    assert _codigo_de_adicionales(monkeypatch) == (
        "import os; from libracore.provisioning import aplicar_modulos_de_adicionales; "
        "aplicar_modulos_de_adicionales(os.environ['VAR_URL'], {'offline': True})"
    )
    assert _codigo_de_adicionales(monkeypatch, tabla="modulos") == _codigo_de_adicionales(monkeypatch)


def test_los_adicionales_con_tabla_propia_la_mandan_en_el_codigo(monkeypatch):
    codigo = _codigo_de_adicionales(monkeypatch, tabla="gestiolibra_modulos")
    assert codigo.endswith("aplicar_modulos_de_adicionales(os.environ['VAR_URL'], {'offline': True}, tabla='gestiolibra_modulos')")
    # y la URL con la contraseña sigue sin ir por la línea de comandos: va el NOMBRE de la variable
    assert "postgresql" not in codigo


def test_los_adicionales_no_llegan_a_Docker_con_un_nombre_invalido(monkeypatch):
    llamadas = []
    monkeypatch.setattr(nc.subprocess, "run", lambda *a, **k: llamadas.append(a))
    with pytest.raises(ValueError):
        nc._aplicar_adicionales_en_contenedor("cont", "VAR_URL", {"offline": True}, tabla="x'); drop table clients; --")
    assert llamadas == []


# ── el alta entera, con Docker doblado ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_plans(monkeypatch):
    mod = types.ModuleType("plans")
    mod.PLANES = ["basico", "pro"]
    mod.PLAN_MODULOS = {"basico": {"ventas"}, "pro": {"ventas", "stock"}}
    mod.ADDONS = set()
    mod.ADICIONALES = {"offline": {"modulos": {"offline"}, "unidad": "sucursal", "limite": "sucursales_offline", "planes": {"pro"}}}
    mod.aplicar_plan_en_db = lambda db_path, plan: None
    monkeypatch.setitem(sys.modules, "plans", mod)
    return mod


@pytest.fixture
def alta(monkeypatch, tmp_path, fake_plans):
    """Un `crear_cliente` sin Docker que deja a la vista lo que habría hecho: la espera, el plan y los adicionales."""
    visto = {"esperas": [], "adicionales": [], "esperas_sqlite": [], "adicionales_sqlite": []}
    monkeypatch.setattr(nc.subprocess, "run", lambda args, **k: subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(nc, "_esperar_tabla_en_sidecar",
                        lambda sidecar, base, usuario, timeout=45, **k: visto["esperas"].append(k) or True)
    monkeypatch.setattr(nc, "_aplicar_plan_en_contenedor", lambda *a, **k: True)
    monkeypatch.setattr(nc, "_aplicar_adicionales_en_contenedor",
                        lambda container, variable, modulos, **k: visto["adicionales"].append((dict(modulos), k)) or True)
    monkeypatch.setattr(nc, "_esperar_db_lista", lambda db_path, timeout=25, **k: visto["esperas_sqlite"].append(k) or True)
    monkeypatch.setattr(provisioning, "aplicar_modulos_de_adicionales",
                        lambda db_path, m, **k: visto["adicionales_sqlite"].append((dict(m), k)))
    monkeypatch.setattr(nc, "migraciones_de_la_imagen",
                        lambda repo_root, image_ref, script=None: (provisioning.get_config().migraciones, "abc1234"))

    def configurar(*, prefijo="testprod", postgres=True, **extra):
        repo_root = tmp_path / "repo"
        (repo_root / "clientes").mkdir(parents=True, exist_ok=True)
        provisioning.configure(product_name=prefijo.upper(), image_name=f"{prefijo}:latest", container_prefix=prefijo,
                               db_filename=f"{prefijo}.db", repo_root=repo_root, base_port=9000, postgres=postgres, **extra)
        return provisioning.get_config()

    visto["configurar"] = configurar
    return visto


def test_un_producto_sin_tabla_propia_llama_a_la_espera_y_a_los_adicionales_como_siempre(alta):
    alta["configurar"]()
    nc.crear_cliente(empresa_cuit=CUIT, nombre="Uno", slug="uno", plan="pro", setup_npm=False, adicionales={"offline": 1})
    assert alta["esperas"] == [{}], "sin tabla propia no se manda ningún argumento nuevo"
    assert alta["adicionales"] == [({"offline": True}, {})]


def test_un_producto_con_tabla_propia_la_manda_a_la_espera_y_a_los_adicionales(alta):
    alta["configurar"](prefijo="gestiolibra", tabla_modulos="gestiolibra_modulos")
    nc.crear_cliente(empresa_cuit=CUIT, nombre="Uno", slug="uno", plan="pro", setup_npm=False, adicionales={"offline": 1})
    assert alta["esperas"] == [{"tabla": "gestiolibra_modulos"}]
    assert alta["adicionales"] == [({"offline": True}, {"tabla": "gestiolibra_modulos"})]


def test_en_sqlite_la_tabla_propia_tambien_llega(alta):
    alta["configurar"](prefijo="gestiolibra", postgres=False, tabla_modulos="gestiolibra_modulos")
    nc.crear_cliente(empresa_cuit=CUIT, nombre="Uno", slug="uno", plan="pro", setup_npm=False, adicionales={"offline": 1})
    assert alta["esperas_sqlite"] == [{"tabla": "gestiolibra_modulos"}]
    assert alta["adicionales_sqlite"] == [({"offline": True}, {"tabla": "gestiolibra_modulos"})]


def test_en_sqlite_sin_tabla_propia_no_cambia_nada(alta):
    alta["configurar"](postgres=False)
    nc.crear_cliente(empresa_cuit=CUIT, nombre="Uno", slug="uno", plan="pro", setup_npm=False, adicionales={"offline": 1})
    assert alta["esperas_sqlite"] == [{}]
    assert alta["adicionales_sqlite"] == [({"offline": True}, {})]


# ── PostgreSQL de verdad: la `modulos` del motor al lado de la del producto, como en una base unida ──────────────────────────────────

def _base_unida(bases):
    """Una base con el esquema del motor (su `modulos`: `id`, `modulo` único, `habilitado` INTEGER) y la `gestiolibra_modulos` del
    producto (ADR-034 de GestioLibra: clave `modulo`, sin `id`, `habilitado` BOOLEAN)."""
    from libracore.db.core import conectar as conectar_motor
    from libracore.db.schema import init_core_schema

    url = bases.nueva()
    con = conectar_motor(url)
    try:
        init_core_schema(con)
        con.commit()
    finally:
        con.close()
    with conectar(url) as c:
        c.execute("CREATE TABLE gestiolibra_modulos (modulo TEXT PRIMARY KEY, habilitado BOOLEAN NOT NULL DEFAULT TRUE, "
                  "plan TEXT NOT NULL DEFAULT 'premium')")
        c.commit()
    return url


def _filas(url, tabla):
    with conectar(url) as c:
        return {m: (bool(h), p) for m, h, p in c.execute(f"SELECT modulo, habilitado, plan FROM {tabla}").fetchall()}


def test_el_tipo_de_habilitado_se_pregunta_por_la_tabla_que_se_escribe(bases):
    """Las dos tablas conviven con tipos DISTINTOS: preguntar por `modulos` a secas le pasaría un `int` a la booleana."""
    url = _base_unida(bases)
    from libracore.db.core import conectar as conectar_motor

    con = conectar_motor(url)
    try:
        assert _como_se_escribe_habilitado(con) is int                              # la del motor
        assert _como_se_escribe_habilitado(con, "modulos") is int
        assert _como_se_escribe_habilitado(con, "gestiolibra_modulos") is bool      # la del producto
    finally:
        con.close()


def test_el_plan_se_escribe_en_la_tabla_del_producto_y_la_del_motor_no_se_toca(bases):
    url = _base_unida(bases)
    antes = _filas(url, "modulos")
    apply_plan_modules(url, active_modules={"ventas"}, all_modules={"ventas", "stock"}, plan="basico",
                       tabla="gestiolibra_modulos")
    assert _filas(url, "gestiolibra_modulos") == {"ventas": (True, "basico"), "stock": (False, "basico")}
    assert _filas(url, "modulos") == antes, "la `modulos` del motor no es del producto"


def test_un_plan_nuevo_actualiza_las_filas_de_la_tabla_del_producto(bases):
    url = _base_unida(bases)
    apply_plan_modules(url, active_modules=set(), all_modules={"a", "b"}, plan="basico", tabla="gestiolibra_modulos")
    apply_plan_modules(url, active_modules={"a", "b"}, all_modules={"a", "b"}, plan="pro", tabla="gestiolibra_modulos")
    assert _filas(url, "gestiolibra_modulos") == {"a": (True, "pro"), "b": (True, "pro")}


def test_los_adicionales_se_prenden_en_la_tabla_del_producto_sin_pisar_el_plan(bases):
    url = _base_unida(bases)
    apply_plan_modules(url, active_modules={"ventas"}, all_modules={"ventas"}, plan="basico", tabla="gestiolibra_modulos")
    antes = _filas(url, "modulos")
    aplicar_modulos_de_adicionales(url, {"offline": True, "ventas": True}, tabla="gestiolibra_modulos")
    assert _filas(url, "gestiolibra_modulos") == {"ventas": (True, "basico"), "offline": (True, "addon")}
    aplicar_modulos_de_adicionales(url, {"offline": False}, tabla="gestiolibra_modulos")
    assert _filas(url, "gestiolibra_modulos")["offline"] == (False, "addon")
    assert _filas(url, "modulos") == antes


def test_sin_tabla_se_escribe_en_modulos_como_siempre_y_la_del_producto_no_se_toca(bases):
    """Los productos que no declaran nada (Contalibra, Restolibra, VentaLibra, LibraDesk) no cambian."""
    url = _base_unida(bases)
    apply_plan_modules(url, active_modules={"ventas"}, all_modules={"ventas", "stock"}, plan="basico")
    aplicar_modulos_de_adicionales(url, {"offline": True})
    assert _filas(url, "modulos") == {"ventas": (True, "basico"), "stock": (False, "basico"), "offline": (True, "addon")}
    assert _filas(url, "gestiolibra_modulos") == {}


def test_un_nombre_invalido_falla_antes_de_abrir_la_base_y_no_escribe_nada(bases):
    url = _base_unida(bases)
    for mal in ("modulos; DELETE FROM clients", "Modulos", "public.modulos"):
        with pytest.raises(ValueError, match="tabla de módulos"):
            apply_plan_modules(url, active_modules={"a"}, all_modules={"a"}, plan="p", tabla=mal)
        with pytest.raises(ValueError, match="tabla de módulos"):
            aplicar_modulos_de_adicionales(url, {"a": True}, tabla=mal)
    assert _filas(url, "modulos") == {} and _filas(url, "gestiolibra_modulos") == {}


def test_una_tabla_que_no_existe_falla_en_voz_alta(bases):
    """Igual que con `modulos` ausente: el alta tiene que enterarse, no escribir en otra parte."""
    url = _base_unida(bases)
    with pytest.raises(Exception, match="medlibra_modulos"):
        apply_plan_modules(url, active_modules={"a"}, all_modules={"a"}, plan="p", tabla="medlibra_modulos")
    assert _filas(url, "modulos") == {}


def _psql_con_psycopg(url):
    """Un `subprocess.run` que atiende el `docker exec <sidecar> psql ... -tAc <SQL>` contra la base de prueba, con psycopg."""
    def fake_run(args, **k):
        if "psql" not in args:  # el resto del alta (network, build, compose...) no se prueba acá
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        with conectar(url) as c:
            valor = c.execute(args[-1]).fetchone()[0]
        return subprocess.CompletedProcess(args, 0, stdout=f"{valor}\n", stderr="")
    return fake_run


def test_la_espera_contra_postgres_distingue_la_tabla_del_producto_de_la_del_motor(bases, monkeypatch):
    """🔴 El caso que el nombre fijo escondía: en una base unida `modulos` (la del motor) existe desde `libracore-migrar`, así que
    esperar `modulos` daba por lista una base cuya tabla del producto todavía no estaba."""
    from libracore.db.core import conectar as conectar_motor
    from libracore.db.schema import init_core_schema

    url = bases.nueva()
    con = conectar_motor(url)
    try:
        init_core_schema(con)
        con.commit()
    finally:
        con.close()
    monkeypatch.setattr(nc.subprocess, "run", _psql_con_psycopg(url))
    monkeypatch.setattr("time.sleep", lambda s: None)

    assert _ESPERA_REAL("s", "b", "u", timeout=1) is True, "la `modulos` del motor está"
    assert _ESPERA_REAL("s", "b", "u", timeout=1, tabla="gestiolibra_modulos") is False, "la del producto todavía no"

    with conectar(url) as c:
        c.execute("CREATE TABLE gestiolibra_modulos (modulo TEXT PRIMARY KEY, habilitado BOOLEAN, plan TEXT)")
        c.commit()
    assert _ESPERA_REAL("s", "b", "u", timeout=1, tabla="gestiolibra_modulos") is True


def test_sin_la_tabla_del_producto_el_alta_termina_en_AltaIncompleta(bases, monkeypatch, alta):
    """El alta de punta a punta (con la espera REAL contra una base real y Docker doblado): si la tabla del producto no aparece, no se
    entrega como buena. Es lo que hoy pasa a una alta nueva de GestioLibra si el producto no declara su tabla."""
    url = bases.nueva()
    with conectar(url) as c:
        c.execute("CREATE TABLE modulos (id SERIAL PRIMARY KEY, modulo TEXT UNIQUE, habilitado INTEGER, plan TEXT)")
        c.commit()
    monkeypatch.setattr(nc, "_esperar_tabla_en_sidecar", _ESPERA_REAL)
    monkeypatch.setattr(nc.subprocess, "run", _psql_con_psycopg(url))
    monkeypatch.setattr("time.sleep", lambda s: None)
    # tiempo de espera corto: el default real son 45 s
    monkeypatch.setattr(nc._esperar_tabla_en_sidecar, "__defaults__", (1, "modulos"))

    alta["configurar"](prefijo="gestiolibra", tabla_modulos="gestiolibra_modulos")
    with pytest.raises(nc.AltaIncompleta, match="tabla de módulos no apareció"):
        nc.crear_cliente(empresa_cuit=CUIT, nombre="Uno", slug="uno", setup_npm=False)
