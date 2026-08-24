"""Que el deploy corra las migraciones, y en el orden correcto.

🔴 **Tener el mecanismo no es tenerlo invocado.** LibraClub llegó a `main` el
2026-08-24 con la revisión `0008` adentro de la imagen y **ningún paso del
deploy la aplicaba**: los únicos `alembic upgrade` del repo estaban en
`semilla_dev.py`, `reset_demo.sh` y la suite. La instancia se habría
reconstruido con código que espera una columna que su base no tiene.

Lo que se fija acá es lo que no se ve leyendo la función:

1. que el comando se corra **antes** del `up -d` y no después —el orden es la
   mitad del arreglo—;
2. que una migración fallida **aborte** el deploy y repinee el compose;
3. que un producto **sin** migraciones declaradas se comporte exactamente
   como antes, que es el control de que esto no cambió a los otros cinco;
4. y que con **dos cadenas** —Gestiolibra y MedLibra tienen la de LibraGenda y
   la propia— corran en orden y la segunda no arranque si la primera falló.
"""
import json
import subprocess

import pytest

from libracore import provisioning
from libracore.provisioning import panel_admin as pa


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


@pytest.fixture(autouse=True)
def _contexto_falso(monkeypatch):
    """Igual que en `test_exit_code_del_deploy`: hay que parchear **los dos**
    nombres, porque `panel_admin` importó `contexto_de_build` con `from . import`
    y tiene su propia referencia."""
    from contextlib import contextmanager

    @contextmanager
    def _falso(repo_root, ref="main", *, from_checkout=False, log=print):
        yield repo_root, "abc1234", f"{ref} (prueba)"

    monkeypatch.setattr(provisioning, "contexto_de_build", _falso)
    monkeypatch.setattr(pa, "contexto_de_build", _falso)


def _armar(tmp_path, monkeypatch, *, migraciones=()):
    """Un producto con un cliente `demo` corriendo y docker/compose bajo control.

    Devuelve `(llamadas, estado)`: `llamadas` es la secuencia de invocaciones a
    `compose`, que es lo que permite afirmar sobre el ORDEN.
    """
    repo = tmp_path / "repo"
    (repo / "clientes" / "demo" / "data").mkdir(parents=True)
    (repo / "clientes" / "demo" / "cliente.json").write_text(json.dumps(
        {"nombre": "Demo", "slug": "demo", "port": 9000, "container": "testprod-demo"}))
    (repo / "clientes" / "demo" / "docker-compose.yml").write_text(
        "services:\n  testprod-demo:\n    image: testprod:v1\n"
        "    container_name: testprod-demo\n")
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo, base_port=9000, migraciones=migraciones,
    )

    llamadas: list[tuple] = []
    # `falla` permite reventar UN comando y no todos, que es lo que hace falta
    # para afirmar que el segundo no corre cuando el primero se cayó.
    estado = {"migracion_ok": True, "up_ok": True, "falla": None}

    def _compose(slug, *args):
        llamadas.append(args)
        if args[:2] == ("run", "--rm"):
            roto = not estado["migracion_ok"] or args[3:] == estado["falla"]
            return subprocess.CompletedProcess(args, 1 if roto else 0)
        return subprocess.CompletedProcess(args, 0 if estado["up_ok"] else 1)

    pineos: list[str] = []
    monkeypatch.setattr(pa, "build_image_tagged", lambda *a, **k: True)
    monkeypatch.setattr(pa, "container_status", lambda c: {"status": "running"})
    monkeypatch.setattr(pa, "compose", _compose)
    monkeypatch.setattr(pa, "podar_imagenes_viejas", lambda *a, **k: ([], []))
    monkeypatch.setattr(pa, "_guardar_meta", lambda *a, **k: None)
    monkeypatch.setattr(pa, "check_venv_sync", lambda *a, **k: None)

    def _pinear(slug, ref):
        pineos.append(ref)
        return "testprod:v1"

    monkeypatch.setattr(pa, "pinear_image", _pinear)
    return llamadas, estado, pineos


# ── El control: sin migraciones declaradas, nada cambia ──────────────────────


def test_un_producto_sin_migraciones_no_corre_nada(tmp_path, monkeypatch):
    """El control de no-regresión para los otros cinco productos.

    VentaLibra crea su esquema al conectar y Contalibra/Restolibra con
    `init_core_schema()`: no tienen nada que correr acá. Si este test se pusiera
    rojo, el cambio les habría agregado un paso que no pidieron.
    """
    llamadas, _, _ = _armar(tmp_path, monkeypatch)
    assert pa.cmd_actualizar(["demo"]) is True
    assert llamadas == [("up", "-d")], "sólo el arranque, sin `run`"


# ── Con migraciones: que corran, y ANTES ─────────────────────────────────────


def test_las_migraciones_corren_antes_del_up(tmp_path, monkeypatch):
    """🔑 **El orden es la mitad del arreglo.**

    Migrar después del `up -d` deja una ventana en la que el código nuevo
    consulta un esquema viejo. Con el compose ya pineado, el `run` levanta un
    contenedor efímero con el código NUEVO mientras la instancia sigue sirviendo
    el viejo.
    """
    llamadas, _, _ = _armar(
        tmp_path, monkeypatch, migraciones=(("alembic", "upgrade", "head"),))
    assert pa.cmd_actualizar(["demo"]) is True

    assert llamadas == [
        ("run", "--rm", "testprod-demo", "alembic", "upgrade", "head"),
        ("up", "-d"),
    ]
    # Explícito además del `==`: si mañana se agrega otra llamada al medio, el
    # `==` se cae por el motivo equivocado y esto dice cuál era la afirmación.
    assert llamadas.index(("up", "-d")) > 0, "el `up` va DESPUÉS de la migración"


def test_el_comando_se_pasa_como_argumentos_sueltos(tmp_path, monkeypatch):
    """Sin shell de por medio: la tupla viaja a `subprocess` argumento por
    argumento. Un string con espacios se ejecutaría como un binario inexistente
    llamado «alembic upgrade head»."""
    llamadas, _, _ = _armar(
        tmp_path, monkeypatch, migraciones=(("python", "-m", "app.migrar"),))
    pa.cmd_actualizar(["demo"])
    assert llamadas[0] == ("run", "--rm", "testprod-demo", "python", "-m", "app.migrar")


# ── Una migración que falla aborta el deploy ─────────────────────────────────


def test_una_migracion_fallida_no_mueve_la_instancia(tmp_path, monkeypatch):
    """🔴 Seguir sería mover la instancia a código que su base no soporta — el
    peor de los dos estados posibles."""
    llamadas, estado, _ = _armar(
        tmp_path, monkeypatch, migraciones=(("alembic", "upgrade", "head"),))
    estado["migracion_ok"] = False

    assert pa.cmd_actualizar(["demo"]) is False
    assert ("up", "-d") not in llamadas, "no se arrancó la imagen nueva"


def test_una_migracion_fallida_repinea_el_compose(tmp_path, monkeypatch):
    """Dejar el compose pineado a la imagen nueva con la base sin migrar es una
    bomba: el próximo `up -d` de cualquiera —un reinicio del host— la aplica."""
    _, estado, pineos = _armar(
        tmp_path, monkeypatch, migraciones=(("alembic", "upgrade", "head"),))
    estado["migracion_ok"] = False

    pa.cmd_actualizar(["demo"])
    assert pineos[-1] == "testprod:v1", "volvió a la versión anterior"


def test_el_control_de_que_el_fallo_viene_de_la_migracion(tmp_path, monkeypatch):
    """Control positivo de los dos de arriba: con la migración en verde y el
    `up` en rojo, el deploy también falla — pero por el otro motivo, y **sí**
    llegó a intentar el arranque. Sin esto, un `False` constante los pasaría
    todos."""
    llamadas, estado, _ = _armar(
        tmp_path, monkeypatch, migraciones=(("alembic", "upgrade", "head"),))
    estado["up_ok"] = False

    assert pa.cmd_actualizar(["demo"]) is False
    assert ("up", "-d") in llamadas, "acá sí se intentó arrancar"


# ── Dos cadenas: el caso de Gestiolibra y MedLibra ───────────────────────────


DOS_CADENAS = (("libragenda-migrar", "upgrade"), ("alembic", "upgrade", "head"))


def test_las_dos_cadenas_corren_en_orden(tmp_path, monkeypatch):
    """🔑 Gestiolibra y MedLibra tienen **dos cadenas de Alembic
    independientes**, cada una con su tabla de versión: la de LibraGenda
    (`alembic_version`) y la propia (`alembic_version_<producto>`).

    El orden no es estético: las revisiones del producto tienen FK contra
    tablas de LibraGenda (`branches`). Al revés, la primera revisión del
    producto que las toque falla con `relation "branches" does not exist`.
    """
    llamadas, _, _ = _armar(tmp_path, monkeypatch, migraciones=DOS_CADENAS)
    assert pa.cmd_actualizar(["demo"]) is True

    assert llamadas == [
        ("run", "--rm", "testprod-demo", "libragenda-migrar", "upgrade"),
        ("run", "--rm", "testprod-demo", "alembic", "upgrade", "head"),
        ("up", "-d"),
    ]


def test_la_segunda_cadena_no_corre_si_fallo_la_primera(tmp_path, monkeypatch):
    """Seguir con la segunda después de que se cayó la primera es garantía de un
    error que no nombra la causa: `alembic` fallaría por las tablas que la
    cadena anterior no llegó a crear, y el log culparía a la revisión del
    producto."""
    llamadas, estado, _ = _armar(tmp_path, monkeypatch, migraciones=DOS_CADENAS)
    estado["falla"] = ("libragenda-migrar", "upgrade")

    assert pa.cmd_actualizar(["demo"]) is False

    corridos = [l[3:] for l in llamadas if l[:2] == ("run", "--rm")]
    assert corridos == [("libragenda-migrar", "upgrade")], "la segunda no corrió"
    assert ("up", "-d") not in llamadas


def test_el_control_de_que_la_segunda_puede_fallar_sola(tmp_path, monkeypatch):
    """Control positivo del de arriba: si el que revienta es el **segundo**, el
    primero sí corrió. Sin esto, un doble que fallara siempre —o un bucle que
    nunca llegara al segundo comando— pasaría los dos."""
    llamadas, estado, _ = _armar(tmp_path, monkeypatch, migraciones=DOS_CADENAS)
    estado["falla"] = ("alembic", "upgrade", "head")

    assert pa.cmd_actualizar(["demo"]) is False

    corridos = [l[3:] for l in llamadas if l[:2] == ("run", "--rm")]
    assert corridos == [("libragenda-migrar", "upgrade"),
                        ("alembic", "upgrade", "head")]


def test_el_error_nombra_el_comando_que_fallo(tmp_path, monkeypatch, capsys):
    """Con dos cadenas, un "fallaron las migraciones" a secas manda a revisar
    las dos. El mensaje tiene que decir **cuál**."""
    _, estado, _ = _armar(tmp_path, monkeypatch, migraciones=DOS_CADENAS)
    estado["falla"] = ("libragenda-migrar", "upgrade")

    pa.cmd_actualizar(["demo"])

    salida = capsys.readouterr().out
    assert "libragenda-migrar upgrade" in salida
    assert "alembic upgrade head" not in salida.split("[ERROR]")[-1], (
        "el [ERROR] no puede nombrar el comando que ni llegó a correr")


# ── La forma plana, rechazada ────────────────────────────────────────────────


def test_la_forma_plana_se_rechaza(tmp_path):
    """🔴 `migraciones=("alembic", "upgrade", "head")` es lo que uno escribe por
    reflejo. Sin la guarda **no falla**: el bucle iteraría los tres strings y
    splatearía cada uno carácter por carácter — `run --rm app a l e m b i c`.
    """
    with pytest.raises(TypeError, match="secuencia de COMANDOS"):
        provisioning.configure(
            product_name="TESTPROD", image_name="testprod:latest",
            container_prefix="testprod", db_filename="testprod.db",
            repo_root=tmp_path, base_port=9000,
            migraciones=("alembic", "upgrade", "head"),
        )


def test_el_mensaje_de_la_guarda_muestra_la_forma_correcta(tmp_path):
    """Un error que dice «está mal» y no «así se escribe» obliga a ir a leer el
    fuente del motor. El texto trae la tupla anidada, lista para copiar."""
    with pytest.raises(TypeError) as e:
        provisioning.configure(
            product_name="TESTPROD", image_name="testprod:latest",
            container_prefix="testprod", db_filename="testprod.db",
            repo_root=tmp_path, base_port=9000,
            migraciones=("alembic", "upgrade", "head"),
        )
    assert "('alembic', 'upgrade', 'head')," in str(e.value)


def test_la_forma_anidada_con_un_solo_comando_se_acepta(tmp_path):
    """El control de que la guarda no es un «rechaza todo»: LibraClub y
    LibraDesk tienen UNA cadena y la declaran igual, anidada."""
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=tmp_path, base_port=9000,
        migraciones=(("alembic", "upgrade", "head"),),
    )
    assert provisioning.get_config().migraciones == (
        ("alembic", "upgrade", "head"),)
