"""`ProductConfig.clientes_dir`: dónde viven las instancias, con una sola fuente.

Hasta v1.122 la ubicación era `repo_root / "clientes"`, fija, y cada producto
repetía `CLIENTES_DIR = REPO_ROOT / "clientes"` en sus scripts: el cron
(`panel_admin`) leía el valor del motor y el backoffice (`admin.services`) el de
la constante del producto. Para poder sacar los datos de los clientes del árbol
del repo hace falta que la ubicación sea configurable **y** que nadie más la
recomponga. Acá:

1. la precedencia (parámetro de `configure()` > `LIBRA_CLIENTES_DIR` > default);
2. que el default sea **idéntico** al de siempre;
3. que el panel y el backoffice vean el mismo directorio;
4. un barrido que falle si alguien vuelve a escribir `repo_root / "clientes"`.
"""
import ast
import pathlib
import sys

import pytest

from libracore import provisioning
from libracore.admin import services
from libracore.provisioning import panel_admin as pa

ENV = provisioning.CLIENTES_DIR_ENV


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


def _configurar(repo_root, **extra):
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo_root, **extra,
    )
    return provisioning.get_config()


# -- 1 y 2. Precedencia y default ------------------------------------------

def test_el_nombre_de_la_variable_es_el_documentado():
    assert ENV == "LIBRA_CLIENTES_DIR"


def test_default_identico_al_de_siempre(tmp_path):
    """Sin parámetro ni variable: exactamente `repo_root / "clientes"`."""
    cfg = _configurar(tmp_path)
    assert cfg.clientes_dir == tmp_path / "clientes"
    assert cfg.clientes_dir == cfg.repo_root / "clientes"
    assert cfg.clientes_dir_override is None


def test_clientes_dir_none_es_el_default(tmp_path):
    """`clientes_dir=None` explícito no es un valor: es "no lo declaro"."""
    assert _configurar(tmp_path, clientes_dir=None).clientes_dir == tmp_path / "clientes"


def test_la_variable_de_entorno_mueve_el_directorio(tmp_path, monkeypatch):
    destino = tmp_path / "afuera" / "clientes"
    monkeypatch.setenv(ENV, str(destino))
    assert _configurar(tmp_path / "repo").clientes_dir == destino


def test_variable_vacia_o_en_blanco_cuenta_como_no_definida(tmp_path, monkeypatch):
    """`LIBRA_CLIENTES_DIR=` (un `environment:` con la clave y sin valor) no
    puede resolver a `Path("")`, que es el directorio actual."""
    for vacio in ("", "   "):
        monkeypatch.setenv(ENV, vacio)
        assert _configurar(tmp_path).clientes_dir == tmp_path / "clientes"


def test_el_parametro_gana_sobre_la_variable(tmp_path, monkeypatch):
    del_param = tmp_path / "del-parametro"
    monkeypatch.setenv(ENV, str(tmp_path / "del-entorno"))
    assert _configurar(tmp_path, clientes_dir=del_param).clientes_dir == del_param


def test_el_parametro_acepta_str_y_path(tmp_path):
    destino = tmp_path / "x"
    assert _configurar(tmp_path, clientes_dir=str(destino)).clientes_dir == destino
    assert _configurar(tmp_path, clientes_dir=destino).clientes_dir == destino


def test_la_variable_se_lee_en_cada_acceso_no_al_configurar(tmp_path, monkeypatch):
    """Quien la define (cron, systemd, compose) no tiene que haberla exportado
    antes de importar el script del producto."""
    cfg = _configurar(tmp_path)
    assert cfg.clientes_dir == tmp_path / "clientes"
    monkeypatch.setenv(ENV, str(tmp_path / "despues"))
    assert cfg.clientes_dir == tmp_path / "despues"


def test_el_parametro_no_toca_el_resto_de_la_config(tmp_path):
    cfg = _configurar(tmp_path, clientes_dir=tmp_path / "otro")
    assert cfg.repo_root == tmp_path
    assert cfg.base_port == 8071


# -- 3. Una sola fuente de verdad ------------------------------------------

def _mkclient(clientes_dir, slug):
    cdir = clientes_dir / slug
    (cdir / "data").mkdir(parents=True)
    (cdir / "data" / "testprod.db").write_bytes(b"")
    (cdir / "cliente.json").write_text('{"nombre": "Uno", "port": 8080}', encoding="utf-8")
    return cdir


@pytest.mark.parametrize("como", ["parametro", "entorno"])
def test_panel_y_backoffice_ven_el_mismo_directorio(tmp_path, monkeypatch, como):
    """Con la ubicación movida, el cron (`panel_admin`) y el backoffice
    (`admin.services`) tienen que mirar la misma carpeta — era justo lo que no
    pasaba con la constante `CLIENTES_DIR` de cada producto."""
    repo, afuera = tmp_path / "repo", tmp_path / "afuera"
    (repo / "clientes").mkdir(parents=True)  # el viejo, vacío: no debe mirarse
    _mkclient(afuera, "cliente-uno")

    if como == "parametro":
        _configurar(repo, clientes_dir=afuera)
    else:
        monkeypatch.setenv(ENV, str(afuera))
        _configurar(repo)

    # `services` resuelve el `panel_admin` del producto por nombre: acá, el del motor.
    monkeypatch.setitem(sys.modules, "panel_admin", pa)
    services.configure(repo_root=repo, db_filename="testprod.db")

    assert [c["slug"] for c in pa.load_clients()] == ["cliente-uno"]
    assert pa.find_client("cliente-uno")["dir"] == afuera / "cliente-uno"
    assert services._clientes_dir() == afuera == provisioning.get_config().clientes_dir

    # Y el backoffice escribe su respaldo donde el panel purga los suyos.
    respaldo = pathlib.Path(services.backup_cliente("cliente-uno"))
    assert respaldo.parent == afuera
    assert not list((repo / "clientes").iterdir())


def test_services_ignora_la_constante_CLIENTES_DIR_del_producto(tmp_path, monkeypatch):
    """`panel_admin.CLIENTES_DIR` sigue existiendo en los scripts de los
    productos por compatibilidad, pero ya no manda."""
    import types
    _configurar(tmp_path, clientes_dir=tmp_path / "manda")
    producto = types.ModuleType("panel_admin")
    producto.CLIENTES_DIR = tmp_path / "no-manda"
    monkeypatch.setitem(sys.modules, "panel_admin", producto)
    services.configure(repo_root=tmp_path, db_filename="testprod.db")
    assert services._clientes_dir() == tmp_path / "manda"


# -- 4. El barrido ---------------------------------------------------------

RAIZ = pathlib.Path(__file__).resolve().parents[2] / "libracore"

#: Dónde SÍ puede nombrarse la carpeta por defecto: la propiedad que decide.
#: `(archivo relativo a libracore/, función que la contiene)`.
PERMITIDOS = {("provisioning/__init__.py", "clientes_dir")}

NOMBRE = "clientes"


def _es_nombre(nodo):
    return isinstance(nodo, ast.Constant) and nodo.value == NOMBRE


def _compone_la_carpeta(nodo):
    """¿Este nodo arma una ruta con el segmento literal `"clientes"`?

    Reconoce `x / "clientes"`, `Path(x, "clientes")`, `x.joinpath("clientes")`
    y `os.path.join(x, "clientes")` — o sea las formas en que se escribe la
    recomposición. No marca el string suelto (claves de dict, nombres de
    módulo del plan, plantillas `clientes/list.html`).
    """
    if isinstance(nodo, ast.BinOp) and isinstance(nodo.op, ast.Div):
        return _es_nombre(nodo.right)
    if isinstance(nodo, ast.Call):
        f = nodo.func
        nombre = getattr(f, "attr", None) or getattr(f, "id", None)
        if nombre in {"Path", "PurePath", "joinpath", "join"}:
            # El primer argumento de `os.path.join`/`Path` no es un segmento.
            return any(_es_nombre(a) for a in nodo.args[1:] or nodo.args)
    return False


def _hallazgos(fuente):
    """`[(línea, función que la contiene)]` de cada recomposición en `fuente`."""
    arbol = ast.parse(fuente)
    out = []

    def visitar(nodo, funcion):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcion = nodo.name
        if _compone_la_carpeta(nodo):
            out.append((nodo.lineno, funcion))
        for hijo in ast.iter_child_nodes(nodo):
            visitar(hijo, funcion)

    visitar(arbol, "<módulo>")
    return out


def test_nadie_recompone_repo_root_clientes_fuera_de_la_propiedad():
    """🔴 Mismo patrón que `test_el_par_en_disco_es_una_sola_llamada`: el defecto
    que busca es el archivo que **todavía no se escribió**. Un call site que
    arme `repo_root / "clientes"` por su cuenta vuelve a partir la fuente de
    verdad en dos, y el cron y el backoffice pasan a mirar carpetas distintas.
    """
    culpables, mirados = [], 0
    for f in RAIZ.rglob("*.py"):
        rel = f.relative_to(RAIZ).as_posix()
        mirados += 1
        for linea, funcion in _hallazgos(f.read_text(encoding="utf-8")):
            if (rel, funcion) not in PERMITIDOS:
                culpables.append(f"{rel}:{linea} (en {funcion})")

    assert mirados >= 50, f"el barrido sólo miró {mirados} archivos: ¿cambió la raíz?"
    assert not culpables, (
        "Estos arman la carpeta de clientes por su cuenta:\n  "
        + "\n  ".join(culpables)
        + "\nUsá `provisioning.get_config().clientes_dir`: es la única fuente"
          " (parámetro de configure() > LIBRA_CLIENTES_DIR > repo_root/'clientes')."
    )


def test_la_propiedad_sigue_siendo_el_unico_lugar_permitido():
    """Si la propiedad se renombrara o se moviera, el barrido de arriba pasaría
    a no permitir nada y el default dejaría de poder definirse: que avise acá, con
    un mensaje que lo diga, y no con un rojo confuso allá."""
    fuente = (RAIZ / "provisioning" / "__init__.py").read_text(encoding="utf-8")
    assert [funcion for _, funcion in _hallazgos(fuente)] == ["clientes_dir"]


@pytest.mark.parametrize("fuente", [
    'def f(cfg):\n    return cfg.repo_root / "clientes"\n',
    'def f(raiz):\n    return Path(raiz, "clientes")\n',
    'def f(raiz):\n    return raiz.joinpath("clientes")\n',
    'def f(raiz):\n    return os.path.join(raiz, "clientes")\n',
    'def f(raiz, slug):\n    return raiz / "clientes" / slug\n',
])
def test_el_barrido_reconoce_cada_forma_de_recomponer(fuente):
    """🔑 El control positivo. El barrido recorre un AST: con el patrón mal
    escrito daría verde para siempre sin encontrar nada."""
    assert _hallazgos(fuente), fuente


@pytest.mark.parametrize("fuente", [
    'x = {"clientes": []}\n',
    'def f(t):\n    return t.TemplateResponse(r, "clientes/list.html", {})\n',
    'def f(cfg, slug):\n    return cfg.clientes_dir / slug\n',
    'def f(r):\n    return r / "data"\n',
])
def test_el_barrido_no_marca_lo_que_no_es_la_carpeta(fuente):
    assert _hallazgos(fuente) == [], fuente
