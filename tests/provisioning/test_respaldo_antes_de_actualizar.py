"""Que `actualizar` respalde cada instancia antes de tocarla.

🔴 **Hasta el 2026-09-10 no había respaldo en ningún punto del deploy.** Lo
tomaba a mano el script de cada sesión, y se lo dio por hecho una vez por un
`backup_zip=True` que sólo fija el formato del cron: el zip más nuevo era el
mismo antes y después de migrar. Con una migración que reescriba datos, esa es
la diferencia entre poder volver atrás o no.

Lo que se fija acá:

1. que el respaldo vaya **antes de pinear y de migrar**;
2. que corra **aunque no haya migraciones declaradas** —Contalibra, Restolibra
   y VentaLibra cambian su esquema al arrancar—;
3. que un respaldo fallido **no toque la instancia**;
4. que `--sin-respaldo` exista, lo saltee y lo diga;
5. y que `cmd_backup` informe su resultado, que es lo que lo hace exigible.
"""
import json
import subprocess

import pytest

from libracore import provisioning
from libracore.provisioning import panel_admin as pa

# Tomada al importar, antes de que el `conftest` la reemplace por el doble.
_RESPALDO_PREVIO_REAL = pa._respaldo_previo


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


@pytest.fixture(autouse=True)
def _contexto_falso(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _falso(repo_root, ref="main", *, from_checkout=False, log=print):
        yield repo_root, "abc1234", f"{ref} (prueba)"

    monkeypatch.setattr(provisioning, "contexto_de_build", _falso)
    monkeypatch.setattr(pa, "contexto_de_build", _falso)


def _configurar(tmp_path, *, migraciones=()):
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
    return repo


def _armar(tmp_path, monkeypatch, *, migraciones=(), respaldo_ok=True,
           corriendo=True):
    """Un cliente `demo` y **una sola línea de tiempo** para respaldo, pineo y
    compose: es lo que permite afirmar el ORDEN entre los tres."""
    _configurar(tmp_path, migraciones=migraciones)
    eventos: list[tuple] = []
    estado = {"up_ok": True}

    def _respaldo(slug):
        eventos.append(("respaldo", slug))
        return respaldo_ok

    def _compose(slug, *args):
        eventos.append(args)
        ok = args[0] != "up" or estado["up_ok"]
        return subprocess.CompletedProcess(args, 0 if ok else 1)

    def _pinear(slug, ref):
        eventos.append(("pinear", ref))
        return "testprod:v1"

    monkeypatch.setattr(pa, "_respaldo_previo", _respaldo)
    monkeypatch.setattr(pa, "build_image_tagged", lambda *a, **k: True)
    monkeypatch.setattr(
        pa, "container_status",
        lambda c: {"status": "running" if corriendo else "exited"})
    monkeypatch.setattr(pa, "compose", _compose)
    monkeypatch.setattr(pa, "pinear_image", _pinear)
    monkeypatch.setattr(pa, "leer_image_pineada", lambda slug: "testprod:v1")
    monkeypatch.setattr(pa, "podar_imagenes_viejas", lambda *a, **k: ([], []))
    monkeypatch.setattr(pa, "_guardar_meta", lambda *a, **k: None)
    monkeypatch.setattr(pa, "check_venv_sync", lambda *a, **k: None)
    return eventos, estado


def _tipos(eventos):
    return [e[0] for e in eventos]


# ── El orden ─────────────────────────────────────────────────────────────────


def test_respalda_antes_de_pinear_y_de_migrar(tmp_path, monkeypatch):
    """🔑 Antes de pinear, para que un respaldo fallido no deje nada que
    deshacer; y antes de migrar, que es lo que el respaldo viene a cubrir."""
    eventos, _ = _armar(
        tmp_path, monkeypatch, migraciones=(("alembic", "upgrade", "head"),))

    assert pa.cmd_actualizar(["demo"]) is True
    assert _tipos(eventos) == ["respaldo", "pinear", "run", "up"]
    assert eventos[0] == ("respaldo", "demo")


def test_respalda_aunque_no_haya_migraciones_declaradas(tmp_path, monkeypatch):
    """Contalibra, Restolibra y VentaLibra no declaran `migraciones`: cambian
    su esquema **al arrancar** (`init_core_schema()`), o sea en el `up -d`. Un
    respaldo atado a que haya migraciones los dejaría afuera justo a ellos."""
    eventos, _ = _armar(tmp_path, monkeypatch)

    assert pa.cmd_actualizar(["demo"]) is True
    assert _tipos(eventos) == ["respaldo", "pinear", "up"]


# ── Un respaldo que falla no toca la instancia ───────────────────────────────


def test_si_el_respaldo_falla_la_instancia_queda_como_estaba(
        tmp_path, monkeypatch, capsys):
    """🔴 Ni pineo, ni migración, ni arranque: nada que deshacer. Y el deploy
    sale en rojo, que es lo que `cli()` convierte en código 1."""
    eventos, _ = _armar(
        tmp_path, monkeypatch, migraciones=(("alembic", "upgrade", "head"),),
        respaldo_ok=False)

    assert pa.cmd_actualizar(["demo"]) is False
    assert eventos == [("respaldo", "demo")], "no se tocó nada más"
    salida = capsys.readouterr().out
    assert "sin respaldo" in salida and "--sin-respaldo" in salida


def test_el_control_de_que_el_rojo_viene_del_respaldo(tmp_path, monkeypatch):
    """Control positivo del de arriba: con el respaldo en verde y el arranque en
    rojo, el deploy también falla — pero **sí** llegó a pinear y arrancar. Sin
    esto, un `cmd_actualizar` que no hiciera nada pasaría el test anterior."""
    eventos, estado = _armar(tmp_path, monkeypatch)
    estado["up_ok"] = False

    assert pa.cmd_actualizar(["demo"]) is False
    assert "pinear" in _tipos(eventos) and "up" in _tipos(eventos)


def test_una_instancia_apagada_no_se_respalda(tmp_path, monkeypatch):
    """La que no está corriendo se saltea sin repinear; respaldarla sería
    trabajo sobre algo que el deploy no toca."""
    eventos, _ = _armar(tmp_path, monkeypatch, corriendo=False)

    assert pa.cmd_actualizar(["demo"]) is True
    assert eventos == []


# ── --sin-respaldo ───────────────────────────────────────────────────────────


def test_sin_respaldo_lo_saltea_y_lo_dice(tmp_path, monkeypatch, capsys):
    """La salida de emergencia —disco lleno, un sidecar que no dumpea— existe,
    pero no es silenciosa: el log del deploy dice que se fue sin respaldo."""
    eventos, _ = _armar(tmp_path, monkeypatch)

    assert pa.cmd_actualizar(["demo"], respaldar=False) is True
    assert "respaldo" not in _tipos(eventos)
    assert "SIN respaldo previo" in capsys.readouterr().out


def test_la_opcion_del_cli_se_saca_de_los_argumentos():
    restantes, opciones = pa._sacar_opciones_de_build(
        ["actualizar", "--sin-respaldo", "demo"])
    assert restantes == ["actualizar", "demo"], "el slug sigue en args[1]"
    assert opciones["respaldar"] is False


def test_por_defecto_se_respalda():
    _, opciones = pa._sacar_opciones_de_build(["actualizar", "demo"])
    assert opciones["respaldar"] is True


# ── _respaldo_previo exige el resultado ──────────────────────────────────────


@pytest.mark.parametrize("resultado, esperado", [(True, True), (False, False)])
def test_el_respaldo_previo_sigue_a_cmd_backup(monkeypatch, resultado, esperado):
    monkeypatch.setattr(pa, "cmd_backup", lambda slug, **k: resultado)
    assert _RESPALDO_PREVIO_REAL("demo") is esperado


def test_una_excepcion_del_backup_es_un_fallo_y_no_se_propaga(monkeypatch, capsys):
    """El dump de PostgreSQL falla con excepción (`BackupInvalido`). Si se
    propagara, cortaría el deploy de las instancias que quedan en la lista."""
    def _explota(slug, **k):
        raise RuntimeError("pg_dump termino con codigo 1")

    monkeypatch.setattr(pa, "cmd_backup", _explota)
    assert _RESPALDO_PREVIO_REAL("demo") is False
    assert "pg_dump termino con codigo 1" in capsys.readouterr().out


# ── cmd_backup informa su resultado ──────────────────────────────────────────


def test_cmd_backup_sin_base_devuelve_false(tmp_path, monkeypatch):
    """Sin archivo SQLite y sin URL PostgreSQL no hay base que respaldar. Antes
    escribía el `[ERROR]` y devolvía `None`, igual que cuando salía bien."""
    _configurar(tmp_path)
    monkeypatch.setattr(pa, "_urls_postgres_del_contenedor", lambda c: [])
    assert pa.cmd_backup("demo", quiet=True) is False


def test_cmd_backup_con_base_devuelve_true(tmp_path, monkeypatch):
    """Control positivo del de arriba: con la base presente, `True`."""
    import sqlite3

    repo = _configurar(tmp_path)
    sqlite3.connect(repo / "clientes" / "demo" / "data" / "testprod.db").close()
    monkeypatch.setattr(pa, "_urls_postgres_del_contenedor", lambda c: [])
    assert pa.cmd_backup("demo", quiet=True) is True


def test_cmd_backup_de_un_cliente_inexistente_devuelve_false(tmp_path):
    _configurar(tmp_path)
    assert pa.cmd_backup("no-existe", quiet=True) is False


def test_backup_all_no_dice_ok_de_lo_que_no_respaldo(monkeypatch, capsys):
    """🔴 El cron imprimía `[OK] respaldado` a continuación del `[ERROR]`."""
    monkeypatch.setattr(pa, "load_clients", lambda: [{"slug": "uno"}, {"slug": "dos"}])
    monkeypatch.setattr(pa, "cmd_backup", lambda slug, **k: slug == "dos")

    pa.cmd_backup_all()
    salida = capsys.readouterr().out
    assert "[OK] 'uno'" not in salida
    assert "'uno' NO quedó respaldado" in salida
    assert "[OK] 'dos' respaldado." in salida
