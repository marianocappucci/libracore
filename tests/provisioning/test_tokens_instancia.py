"""Credenciales de máquina por instancia (ADR-071): lectura, escritura y los comandos del panel.

Todo sobre directorios de `tmp_path` con composes ficticios y tokens ficticios; Docker no se toca
(`compose_en` se reemplaza por un doble que anota las llamadas).
"""
import os
import stat
import subprocess

import pytest
import yaml

from libracore import provisioning
from libracore.provisioning import panel_admin as pa
from libracore.provisioning import tokens_instancia as ti

SERVICIO = ti.SERVICIO
ANTERIOR = ti.SERVICIO + ti.SUFIJO_ANTERIOR
INTEGRACION = ti.INTEGRACION

TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


def _compose_app(env_lineas: str, *, sidecar_primero=False, nombre="testprod-uno") -> str:
    app = (
        f"  {nombre}:\n"
        "    image: testprod:v1\n"
        f"    container_name: {nombre}\n"
        "    environment:\n"
        f"{env_lineas}"
        "    ports:\n"
        '      - "9000:8000"\n'
    )
    db = (
        f"  {nombre}-postgres:\n"
        "    image: postgres:16-alpine\n"
        "    environment:\n"
        "      - POSTGRES_DB=cosas\n"
    )
    cuerpo = db + app if sidecar_primero else app + db
    return "services:\n" + cuerpo + "\nvolumes:\n  datos:\n"


def _env(texto: str, servicio: str = "testprod-uno") -> dict:
    env = yaml.safe_load(texto)["services"][servicio]["environment"]
    if isinstance(env, dict):
        return {k: str(v) for k, v in env.items()}
    return dict(i.partition("=")[::2] for i in env)


def _instancia(tmp_path, compose: str, dotenv: str | None = None, nombre="uno"):
    d = tmp_path / nombre
    d.mkdir(parents=True)
    (d / "docker-compose.yml").write_text(compose, encoding="utf-8")
    if dotenv is not None:
        (d / ".env").write_text(dotenv, encoding="utf-8")
    return d


def _modo(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


# ── lectura ──────────────────────────────────────────────────────────────────


def test_lee_el_valor_literal_del_compose(tmp_path):
    d = _instancia(tmp_path, _compose_app(f"      - ADMIN_USER=admin\n      - {SERVICIO}={TOKEN_A}\n"))
    assert ti.leer(d, SERVICIO) == TOKEN_A
    assert ti.donde(d, SERVICIO) == "compose"


def test_lee_el_valor_entre_comillas(tmp_path):
    d = _instancia(tmp_path, _compose_app(f'      - "{SERVICIO}={TOKEN_A}"\n'))
    assert ti.leer(d, SERVICIO) == TOKEN_A


def test_resuelve_la_referencia_contra_el_env(tmp_path):
    d = _instancia(tmp_path, _compose_app(f"      - {SERVICIO}=${{TOKEN_DEL_ENV}}\n"),
                   dotenv=f"# secretos\nexport TOKEN_DEL_ENV='{TOKEN_A}'\nOTRA=x\n")
    assert ti.leer(d, SERVICIO) == TOKEN_A
    assert ti.donde(d, SERVICIO) == ".env"


def test_lee_el_environment_en_forma_de_mapa(tmp_path):
    d = _instancia(tmp_path, _compose_app(f'      ADMIN_USER: admin\n      {SERVICIO}: "{TOKEN_A}"\n'))
    assert ti.leer(d, SERVICIO) == TOKEN_A


def test_sin_la_variable_o_vacia_es_none(tmp_path):
    d = _instancia(tmp_path, _compose_app(f"      - ADMIN_USER=admin\n      - {INTEGRACION}=${{NO_ESTA}}\n"))
    assert ti.leer(d, SERVICIO) is None
    assert ti.leer(d, INTEGRACION) is None  # la referencia no resuelve: vacía


def test_el_entorno_del_proceso_no_cuenta_para_la_lectura(tmp_path, monkeypatch):
    """`docker compose` lo usaría, pero `entorno_para_compose` lo saca: lo que vale es el `.env`."""
    monkeypatch.setenv("TOKEN_DEL_ENV", TOKEN_B)
    d = _instancia(tmp_path, _compose_app(f"      - {SERVICIO}=${{TOKEN_DEL_ENV}}\n"),
                   dotenv=f"TOKEN_DEL_ENV={TOKEN_A}\n")
    assert ti.leer(d, SERVICIO) == TOKEN_A


def test_el_compose_de_docker_va_sin_las_credenciales_del_proceso():
    entorno = ti.entorno_para_compose({"PATH": "/bin", SERVICIO: TOKEN_A, ANTERIOR: TOKEN_B,
                                       INTEGRACION: TOKEN_A, "LIBRA_PANEL_TOKEN": "p"})
    assert entorno == {"PATH": "/bin", "LIBRA_PANEL_TOKEN": "p"}


def test_la_huella_no_es_el_valor():
    assert len(ti.huella(TOKEN_A)) == 8
    assert ti.huella(TOKEN_A) not in TOKEN_A
    assert ti.huella(None) == ""


# ── escritura ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sidecar_primero", [False, True])
def test_una_variable_nueva_va_al_environment_de_la_app_y_no_al_sidecar(tmp_path, sidecar_primero):
    d = _instancia(tmp_path, _compose_app("      - LIBRA_PANEL_TOKEN=p\n", sidecar_primero=sidecar_primero))
    ti.escribir(d, {SERVICIO: TOKEN_A}, sello="s1")
    texto = (d / "docker-compose.yml").read_text()
    assert _env(texto)[SERVICIO] == TOKEN_A
    assert _env(texto, "testprod-uno-postgres") == {"POSTGRES_DB": "cosas"}


def test_rotar_en_el_compose_deja_la_anterior_al_lado_y_respalda(tmp_path):
    original = _compose_app(f"      - ADMIN_USER=admin\n      - {SERVICIO}={TOKEN_A}\n      - OTRA=1\n")
    d = _instancia(tmp_path, original)

    respaldos = ti.escribir(d, {SERVICIO: TOKEN_B, ANTERIOR: TOKEN_A}, sello="s1", junto_a={ANTERIOR: SERVICIO})

    texto = (d / "docker-compose.yml").read_text()
    env = _env(texto)
    assert env[SERVICIO] == TOKEN_B and env[ANTERIOR] == TOKEN_A and env["OTRA"] == "1"
    lineas = texto.split("\n")
    assert lineas.index(f"      - {ANTERIOR}={TOKEN_A}") == lineas.index(f"      - {SERVICIO}={TOKEN_B}") + 1
    assert not (d / ".env").exists()  # estaba literal: se queda literal
    assert [p.name for p in respaldos] == ["docker-compose.yml.antes-token-s1"]
    assert respaldos[0].read_text() == original
    assert _modo(respaldos[0]) == 0o600 and _modo(d / "docker-compose.yml") == 0o600
    assert not list(d.glob("*.tmp"))


def test_lo_que_vive_en_el_env_se_escribe_en_el_env(tmp_path):
    compose = _compose_app(f"      - {SERVICIO}=${{{SERVICIO}}}\n")
    d = _instancia(tmp_path, compose, dotenv=f"{SERVICIO}={TOKEN_A}\nSECRET_KEY=x\n")

    respaldos = ti.escribir(d, {SERVICIO: TOKEN_B, ANTERIOR: TOKEN_A}, sello="s1", junto_a={ANTERIOR: SERVICIO})

    dotenv = (d / ".env").read_text()
    assert dotenv == f"{SERVICIO}={TOKEN_B}\nSECRET_KEY=x\n{ANTERIOR}={TOKEN_A}\n"
    texto = (d / "docker-compose.yml").read_text()
    assert TOKEN_A not in texto and TOKEN_B not in texto
    assert _env(texto)[ANTERIOR] == f"${{{ANTERIOR}}}"
    assert ti.leer(d, SERVICIO) == TOKEN_B and ti.leer(d, ANTERIOR) == TOKEN_A
    assert sorted(p.name for p in respaldos) == [".env.antes-token-s1", "docker-compose.yml.antes-token-s1"]
    assert _modo(d / ".env") == 0o600


@pytest.mark.parametrize("modificador", [":?falta en el .env", "?falta", ":-", "-x"])
def test_la_referencia_con_modificador_de_compose_se_lee_del_env(tmp_path, modificador):
    """LibraDesk, LibraCargo y LibraClub declaran `${LIBRA_SERVICE_TOKEN:?falta en el .env}`: el texto
    del modificador es el mensaje de error de compose, nunca el valor (antes se leía como literal)."""
    compose = _compose_app(f"      - {SERVICIO}=${{{SERVICIO}{modificador}}}\n")
    d = _instancia(tmp_path, compose, dotenv=f"{SERVICIO}={TOKEN_A}\n")
    assert ti.leer(d, SERVICIO) == TOKEN_A
    assert ti.donde(d, SERVICIO) == ti.DOTENV


def test_la_referencia_con_error_sin_valor_en_el_env_es_none(tmp_path):
    d = _instancia(tmp_path, _compose_app(f"      - {SERVICIO}=${{{SERVICIO}:?falta}}\n"), dotenv="OTRA=1\n")
    assert ti.leer(d, SERVICIO) is None


def test_rotar_con_referencia_con_error_escribe_el_env_y_no_toca_la_linea(tmp_path):
    linea = f"      - {SERVICIO}=${{{SERVICIO}:?falta en el .env}}\n"
    d = _instancia(tmp_path, _compose_app(linea), dotenv=f"{SERVICIO}={TOKEN_A}\nSECRET_KEY=x\n")

    ti.escribir(d, {SERVICIO: TOKEN_B, ANTERIOR: TOKEN_A}, sello="s1", junto_a={ANTERIOR: SERVICIO})

    texto = (d / "docker-compose.yml").read_text()
    assert linea in texto
    assert TOKEN_A not in texto and TOKEN_B not in texto
    assert ti.leer(d, SERVICIO) == TOKEN_B and ti.leer(d, ANTERIOR) == TOKEN_A


def test_cerrar_saca_la_anterior_de_los_dos_lados(tmp_path):
    d = _instancia(tmp_path, _compose_app(f"      - {SERVICIO}=${{{SERVICIO}}}\n      - {ANTERIOR}=${{{ANTERIOR}}}\n"),
                   dotenv=f"{SERVICIO}={TOKEN_B}\n{ANTERIOR}={TOKEN_A}\n")
    ti.escribir(d, {ANTERIOR: None}, sello="s1")
    assert ANTERIOR not in (d / ".env").read_text()
    assert ANTERIOR not in (d / "docker-compose.yml").read_text()
    assert ti.leer(d, SERVICIO) == TOKEN_B


def test_si_la_app_toma_valores_del_env_lo_nuevo_tambien_va_ahi(tmp_path):
    d = _instancia(tmp_path, _compose_app("      - SECRET_KEY=${SECRET_KEY}\n"), dotenv="SECRET_KEY=x\n")
    ti.escribir(d, {INTEGRACION: TOKEN_A}, sello="s1")
    assert f"{INTEGRACION}={TOKEN_A}" in (d / ".env").read_text()
    assert TOKEN_A not in (d / "docker-compose.yml").read_text()


def test_en_forma_de_mapa_escribe_en_forma_de_mapa(tmp_path):
    d = _instancia(tmp_path, _compose_app(f"      ADMIN_USER: admin\n      {SERVICIO}: {TOKEN_A}\n"))
    ti.escribir(d, {SERVICIO: TOKEN_B, ANTERIOR: TOKEN_A}, sello="s1", junto_a={ANTERIOR: SERVICIO})
    assert _env((d / "docker-compose.yml").read_text()) == {
        "ADMIN_USER": "admin", SERVICIO: TOKEN_B, ANTERIOR: TOKEN_A}


def test_sin_cambios_no_escribe_ni_respalda(tmp_path):
    d = _instancia(tmp_path, _compose_app(f"      - {SERVICIO}={TOKEN_A}\n"))
    assert ti.escribir(d, {SERVICIO: TOKEN_A, ANTERIOR: None}, sello="s1") == []
    assert sorted(p.name for p in d.iterdir()) == ["docker-compose.yml"]


def test_sin_servicio_de_la_app_identificable_no_toca_nada(tmp_path):
    compose = ("services:\n  a:\n    image: x:1\n    environment:\n      - A=1\n"
               "  b:\n    image: y:1\n    environment:\n      - B=1\n")
    d = _instancia(tmp_path, compose)
    with pytest.raises(ti.TokenError):
        ti.escribir(d, {SERVICIO: TOKEN_A}, sello="s1")
    assert (d / "docker-compose.yml").read_text() == compose
    assert sorted(p.name for p in d.iterdir()) == ["docker-compose.yml"]


def test_un_valor_con_caracteres_raros_no_se_escribe(tmp_path):
    d = _instancia(tmp_path, _compose_app("      - LIBRA_PANEL_TOKEN=p\n"))
    for malo in ("", "con$dolar", "con\nsalto", 'con"comilla'):
        with pytest.raises(ti.TokenError) as e:
            ti.escribir(d, {SERVICIO: malo}, sello="s1")
        assert malo not in str(e.value) or not malo


# ── comandos del panel ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


@pytest.fixture
def recreados(monkeypatch):
    llamadas = []
    monkeypatch.setattr(pa, "compose_en", lambda d, *args: llamadas.append((os.fspath(d), args))
                        or subprocess.CompletedProcess([], 0))
    return llamadas


@pytest.fixture
def cfg(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(product_name="TESTPROD", image_name="testprod:latest",
                           container_prefix="testprod", db_filename="testprod.db", repo_root=repo_root)
    return provisioning.get_config()


def _cliente(cfg, slug, env_lineas, dotenv=None):
    d = _instancia(cfg.clientes_dir, _compose_app(env_lineas, nombre=f"testprod-{slug}"), dotenv, nombre=slug)
    (d / "cliente.json").write_text('{"nombre": "Uno", "plan": "basico"}', encoding="utf-8")
    return d


def test_token_servicio_sin_flags_muestra_estado_sin_valores(cfg, capsys, recreados, monkeypatch):
    monkeypatch.setenv(SERVICIO, TOKEN_A)  # el del producto: la instancia todavía lo comparte
    _cliente(cfg, "uno", f"      - {SERVICIO}={TOKEN_A}\n")
    assert pa.cmd_token_servicio("uno") is True
    out = capsys.readouterr().out
    assert TOKEN_A not in out
    assert ti.huella(TOKEN_A) in out and "propio" in out and "entorno de este proceso" in out
    assert f"{ANTERIOR}: no" in out
    assert recreados == []


def test_token_servicio_ausente(cfg, capsys, recreados):
    _cliente(cfg, "uno", "      - ADMIN_USER=admin\n")
    assert pa.cmd_token_servicio("uno") is True
    assert f"{SERVICIO}: ausente" in capsys.readouterr().out


def test_rotar_y_cerrar_el_token_de_servicio(cfg, capsys, recreados):
    d = _cliente(cfg, "uno", f"      - {SERVICIO}={TOKEN_A}\n")

    assert pa.cmd_token_servicio("uno", rotar=True) is True
    nuevo = ti.leer(d, SERVICIO)
    assert nuevo not in (None, TOKEN_A) and len(nuevo) == 64
    assert ti.leer(d, ANTERIOR) == TOKEN_A
    assert list(d.glob("docker-compose.yml.antes-token-*"))
    assert recreados == [(os.fspath(d), ("up", "-d"))]

    # con la rotación abierta, rotar de nuevo dejaría afuera a quien usa la anterior
    assert pa.cmd_token_servicio("uno", rotar=True) is False
    assert ti.leer(d, SERVICIO) == nuevo

    assert pa.cmd_token_servicio("uno", cerrar=True) is True
    assert ti.leer(d, ANTERIOR) is None and ti.leer(d, SERVICIO) == nuevo
    assert len(recreados) == 2

    out = capsys.readouterr().out
    assert TOKEN_A not in out and nuevo not in out


def test_rotar_sin_token_previo_crea_uno_sin_anterior(cfg, recreados):
    d = _cliente(cfg, "uno", "      - LIBRA_PANEL_TOKEN=p\n")
    assert pa.cmd_token_servicio("uno", rotar=True) is True
    assert ti.leer(d, SERVICIO) and ti.leer(d, ANTERIOR) is None


def test_cerrar_sin_rotacion_abierta_no_toca_nada(cfg, recreados):
    d = _cliente(cfg, "uno", f"      - {SERVICIO}={TOKEN_A}\n")
    assert pa.cmd_token_servicio("uno", cerrar=True) is True
    assert recreados == [] and not list(d.glob("*.antes-token-*"))


def test_token_integracion_rota_en_el_env(cfg, recreados):
    d = _cliente(cfg, "uno", f"      - {INTEGRACION}=${{{INTEGRACION}}}\n", dotenv=f"{INTEGRACION}={TOKEN_A}\n")
    assert pa.cmd_token_integracion("uno", rotar=True) is True
    assert ti.leer(d, INTEGRACION + ti.SUFIJO_ANTERIOR) == TOKEN_A
    assert TOKEN_A not in (d / "docker-compose.yml").read_text()
    assert ti.leer(d, SERVICIO) is None  # la otra credencial no se toca


def test_rotar_y_cerrar_juntos_es_un_error(cfg, recreados):
    _cliente(cfg, "uno", f"      - {SERVICIO}={TOKEN_A}\n")
    assert pa.cmd_token_servicio("uno", rotar=True, cerrar=True) is False
    assert recreados == []


def test_cliente_inexistente(cfg, recreados):
    assert pa.cmd_token_servicio("no-existe", rotar=True) is False


def test_vincular_integracion_copia_la_credencial_del_destino(tmp_path, capsys, recreados):
    destino = _instancia(tmp_path, _compose_app(f"      - {INTEGRACION}={TOKEN_A}\n", nombre="contalibra-demo"),
                         nombre="contalibra-demo")
    origen = _instancia(tmp_path, _compose_app("      - LIBRA_PANEL_TOKEN=p\n", nombre="medlibra-demo"),
                        nombre="medlibra-demo")

    ok = pa.cmd_vincular_integracion(str(origen), str(destino), "CONTALIBRA_URL", "CONTALIBRA_SERVICE_TOKEN",
                                     "http://contalibra-demo:8000")
    assert ok is True
    env = _env((origen / "docker-compose.yml").read_text(), "medlibra-demo")
    assert env["CONTALIBRA_URL"] == "http://contalibra-demo:8000"
    assert env["CONTALIBRA_SERVICE_TOKEN"] == TOKEN_A
    assert list(origen.glob("docker-compose.yml.antes-token-*"))
    assert _modo(origen / "docker-compose.yml") == 0o600
    assert recreados == [(os.fspath(origen), ("up", "-d"))]
    assert TOKEN_A not in capsys.readouterr().out

    # idempotente
    assert pa.cmd_vincular_integracion(str(origen), str(destino), "CONTALIBRA_URL", "CONTALIBRA_SERVICE_TOKEN",
                                       "http://contalibra-demo:8000") is True
    assert len(recreados) == 1


def test_vincular_respeta_donde_vivian_las_variables_del_origen(tmp_path, recreados):
    destino = _instancia(tmp_path, _compose_app(f"      - {INTEGRACION}={TOKEN_B}\n"), nombre="destino")
    origen = _instancia(tmp_path, _compose_app("      - CONTALIBRA_URL=http://vieja\n"
                                               "      - CONTALIBRA_SERVICE_TOKEN=${CONTALIBRA_SERVICE_TOKEN}\n"),
                        dotenv=f"CONTALIBRA_SERVICE_TOKEN={TOKEN_A}\n", nombre="origen")

    assert pa.cmd_vincular_integracion(str(origen), str(destino), "CONTALIBRA_URL", "CONTALIBRA_SERVICE_TOKEN",
                                       "http://nueva:8000") is True
    assert (origen / ".env").read_text() == f"CONTALIBRA_SERVICE_TOKEN={TOKEN_B}\n"
    texto = (origen / "docker-compose.yml").read_text()
    assert "- CONTALIBRA_URL=http://nueva:8000" in texto and TOKEN_B not in texto


def test_vincular_sin_credencial_en_el_destino_no_toca_el_origen(tmp_path, capsys, recreados):
    destino = _instancia(tmp_path, _compose_app("      - LIBRA_PANEL_TOKEN=p\n"), nombre="destino")
    compose = _compose_app("      - LIBRA_PANEL_TOKEN=q\n")
    origen = _instancia(tmp_path, compose, nombre="origen")
    assert pa.cmd_vincular_integracion(str(origen), str(destino), "U", "T", "http://x") is False
    assert (origen / "docker-compose.yml").read_text() == compose
    assert "token-integracion" in capsys.readouterr().out
    assert recreados == []


@pytest.mark.parametrize("url_var,token_var,url", [
    ("1MAL", "T", "http://x"), ("U", "T-MAL", "http://x"), ("U", "U", "http://x"), ("U", "T", "ftp://x"),
    ("U", "T", "http://x/$y"),
])
def test_vincular_valida_los_argumentos(tmp_path, recreados, url_var, token_var, url):
    destino = _instancia(tmp_path, _compose_app(f"      - {INTEGRACION}={TOKEN_A}\n"), nombre="destino")
    origen = _instancia(tmp_path, _compose_app("      - LIBRA_PANEL_TOKEN=q\n"), nombre="origen")
    assert pa.cmd_vincular_integracion(str(origen), str(destino), url_var, token_var, url) is False
    assert recreados == []


def test_vincular_por_la_cli(tmp_path, monkeypatch, recreados):
    destino = _instancia(tmp_path, _compose_app(f"      - {INTEGRACION}={TOKEN_A}\n"), nombre="destino")
    origen = _instancia(tmp_path, _compose_app("      - LIBRA_PANEL_TOKEN=q\n"), nombre="origen")
    monkeypatch.setattr("sys.argv", ["panel_admin.py", "vincular-integracion", "--origen", str(origen),
                                     f"--destino={destino}", "--url-var", "U", "--token-var", "T",
                                     "--url", "http://destino:8000"])
    pa.cli()
    assert ti.leer(origen, "T") == TOKEN_A


def test_la_cli_sale_1_con_un_uso_incompleto(monkeypatch, recreados):
    monkeypatch.setattr("sys.argv", ["panel_admin.py", "vincular-integracion", "--origen", "x"])
    with pytest.raises(SystemExit) as e:
        pa.cli()
    assert e.value.code == 1
    monkeypatch.setattr("sys.argv", ["panel_admin.py", "token-servicio", "uno", "--borrar"])
    with pytest.raises(SystemExit):
        pa.cli()
