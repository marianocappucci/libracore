"""La casilla de correo de una instancia que ya existe (ADR-046).

Dos cosas: de dónde lee `mail_cuentas` su configuración cuando el proceso es la
CLI del host (que no tiene el `environment:` del backoffice), y el comando
`panel_admin.py casilla <slug>` que crea o repara la casilla de una instancia
existente. El servidor de correo es simulado: se intercepta `subprocess.run`,
que es la frontera real de `mail_cuentas`, y el doble recuerda qué casillas
existen para poder contestar «ya existe» como el wrapper de verdad.
"""
import json
import subprocess

import pytest
import yaml

from libracore import provisioning
from libracore.provisioning import mail_cuentas as mc
from libracore.provisioning import panel_admin as pa

from .test_panel_admin import _reset_config, fake_docker  # noqa: F401

CLAVE_SECRETA_DE_LA_SUITE = "no-debe-salir-por-pantalla"


# ── el archivo del host ──────────────────────────────────────────────────────


@pytest.fixture
def archivo(tmp_path, monkeypatch):
    ruta = tmp_path / "correo.env"
    monkeypatch.setenv("LIBRA_MAIL_ENV_FILE", str(ruta))
    return ruta


def test_el_archivo_del_host_configura_cuando_el_entorno_no_esta(archivo):
    archivo.write_text(
        "# correo de testprod\n"
        "LIBRA_MAIL_ADMIN_SSH=vps-mail\n"
        'LIBRA_MAIL_DOMINIO="testprod.com.ar"\n'
        "export LIBRA_MAIL_SMTP_HOST='mail.testprod.com.ar'\n",
        encoding="utf-8",
    )
    assert mc.configurado() is True
    assert mc.direccion_de("lagrace") == "lagrace@testprod.com.ar"
    assert mc._smtp_host() == "mail.testprod.com.ar"
    assert mc.motivo_sin_configurar() == ""


def test_el_entorno_del_proceso_gana_sobre_el_archivo(archivo, monkeypatch):
    archivo.write_text(
        "LIBRA_MAIL_ADMIN_SSH=del-archivo\nLIBRA_MAIL_DOMINIO=archivo.com.ar\n", encoding="utf-8")
    monkeypatch.setenv("LIBRA_MAIL_DOMINIO", "entorno.com.ar")
    assert mc.direccion_de("x") == "x@entorno.com.ar"
    assert mc._entorno("LIBRA_MAIL_ADMIN_SSH") == "del-archivo"


def test_del_archivo_solo_se_leen_las_tres_variables(archivo):
    archivo.write_text(
        "LIBRA_MAIL_ADMIN_SSH=vps-mail\nLIBRA_MAIL_DOMINIO=testprod.com.ar\n"
        f"SECRET_KEY={CLAVE_SECRETA_DE_LA_SUITE}\n", encoding="utf-8")
    assert mc._entorno("SECRET_KEY") == ""
    assert CLAVE_SECRETA_DE_LA_SUITE not in repr(mc._leer_archivo())


def test_sin_archivo_ni_entorno_dice_que_falta_y_donde_se_declara(archivo):
    assert mc.configurado() is False
    motivo = mc.motivo_sin_configurar()
    assert "LIBRA_MAIL_ADMIN_SSH" in motivo and "LIBRA_MAIL_DOMINIO" in motivo
    assert str(archivo) in motivo


def test_una_sola_variable_en_el_archivo_no_alcanza(archivo):
    archivo.write_text("LIBRA_MAIL_ADMIN_SSH=vps-mail\n", encoding="utf-8")
    assert mc.configurado() is False
    assert "LIBRA_MAIL_DOMINIO" in mc.motivo_sin_configurar()
    assert "LIBRA_MAIL_ADMIN_SSH" not in mc.motivo_sin_configurar()


def test_el_archivo_por_defecto_es_uno_por_producto(tmp_path, monkeypatch):
    monkeypatch.delenv("LIBRA_MAIL_ENV_FILE", raising=False)
    provisioning._cfg = None
    try:
        provisioning.configure(
            product_name="TESTPROD", image_name="testprod:latest",
            container_prefix="testprod", db_filename="testprod.db", repo_root=tmp_path)
        assert str(mc.archivo_del_host()) == "/root/secretos/correo-testprod.env"
    finally:
        provisioning._cfg = None


# ── el compose de una instancia existente ────────────────────────────────────


def _cuenta(nombre="La Grace"):
    return mc.CuentaDeCorreo(direccion="lagrace@testprod.com.ar", password="pw-Secreta_1",
                             host="mail.testprod.com.ar", port=587, from_name=nombre)


def _compose(extra_env="", sidecar_primero=False):
    app = (
        "  testprod-lagrace:\n"
        "    image: testprod:v1\n"
        "    container_name: testprod-lagrace\n"
        "    environment:\n"
        "      - ADMIN_USER=admin\n"
        f"{extra_env}"
        "    ports:\n"
        '      - "9000:8000"\n'
    )
    db = (
        "  testprod-lagrace-db:\n"
        "    image: postgres:16-alpine\n"
        "    container_name: testprod-lagrace-db\n"
        "    environment:\n"
        "      - POSTGRES_DB=cosas\n"
    )
    return "services:\n" + (db + "\n" + app if sidecar_primero else app + "\n" + db)


def _env_de(texto, servicio):
    doc = yaml.safe_load(texto)
    return dict(i.partition("=")[::2] for i in doc["services"][servicio]["environment"])


@pytest.mark.parametrize("sidecar_primero", [False, True])
def test_con_cuenta_pone_las_seis_variables_en_la_app_y_no_en_el_sidecar(sidecar_primero):
    texto = _compose(sidecar_primero=sidecar_primero)
    idx = texto.split("\n").index("    image: testprod:v1")

    nuevo = mc.con_cuenta_en_compose(texto, idx, _cuenta("Casa # 5"))

    app = _env_de(nuevo, "testprod-lagrace")
    assert app["ADMIN_USER"] == "admin"
    assert app["LIBRAAUTH_SMTP_USER"] == "lagrace@testprod.com.ar"
    assert app["LIBRAAUTH_SMTP_PASSWORD"] == "pw-Secreta_1"
    assert app["LIBRAAUTH_SMTP_FROM_NAME"] == "Casa # 5"
    assert app["LIBRAAUTH_SMTP_HOST"] == "mail.testprod.com.ar"
    assert _env_de(nuevo, "testprod-lagrace-db") == {"POSTGRES_DB": "cosas"}
    assert mc.smtp_en_el_compose(nuevo) is True
    assert mc.smtp_en_el_compose(texto) is False


def test_con_cuenta_es_igual_al_compose_del_alta():
    """Lo que escribe la reparación es lo mismo que escribe el alta."""
    texto = _compose()
    idx = texto.split("\n").index("    image: testprod:v1")
    del_alta = _compose(extra_env=mc.env_para_compose(_cuenta()))
    assert mc.con_cuenta_en_compose(texto, idx, _cuenta()) == del_alta


def test_con_cuenta_es_idempotente_y_reemplaza_la_contrasena():
    texto = _compose()
    idx = texto.split("\n").index("    image: testprod:v1")
    una = mc.con_cuenta_en_compose(texto, idx, _cuenta())
    assert mc.con_cuenta_en_compose(una, idx, _cuenta()) == una

    otra = mc.CuentaDeCorreo(**{**_cuenta().__dict__, "password": "otra-clave"})
    cambiada = mc.con_cuenta_en_compose(una, idx, otra)
    assert _env_de(cambiada, "testprod-lagrace")["LIBRAAUTH_SMTP_PASSWORD"] == "otra-clave"
    assert cambiada.count("LIBRAAUTH_SMTP_PASSWORD") == 1


def test_con_cuenta_no_adivina_un_environment_en_formato_de_mapa():
    texto = (
        "services:\n  testprod-lagrace:\n    image: testprod:v1\n"
        "    environment:\n      ADMIN_USER: admin\n"
    )
    assert mc.con_cuenta_en_compose(texto, 3 - 1, _cuenta()) is None
    sin_env = "services:\n  testprod-lagrace:\n    image: testprod:v1\n"
    assert mc.con_cuenta_en_compose(sin_env, 2, _cuenta()) is None


# ── panel_admin.py casilla <slug> ────────────────────────────────────────────


@pytest.fixture
def correo(archivo):
    archivo.write_text(
        "LIBRA_MAIL_ADMIN_SSH=libra-mail@mail.testprod.com.ar\n"
        "LIBRA_MAIL_DOMINIO=testprod.com.ar\n", encoding="utf-8")


@pytest.fixture
def servidor(monkeypatch):
    """Un servidor de correo simulado que recuerda sus casillas.

    Contesta como el wrapper real: `crear` sobre una existente falla con
    «La casilla ya existe», `borrar` la saca. Guarda cada orden con su stdin
    para asertar que la contraseña nunca viaja como argumento.
    """
    class Servidor:
        casillas: dict[str, str] = {}
        ordenes: list[tuple[list[str], str]] = []
        caido = False

    srv = Servidor()
    srv.casillas, srv.ordenes = {}, []

    def run(args, **kwargs):
        if args and args[0] == "ssh":
            # ssh -o BatchMode=yes -o ConnectTimeout=N <destino> <orden...>
            orden = args[6:]
            srv.ordenes.append((orden, kwargs.get("input", "")))
            if srv.caido:
                return subprocess.CompletedProcess(args, 255, stdout="",
                                                   stderr="ssh: connect to host: Connection refused")
            accion, direccion = orden[0], orden[1]
            if accion == "crear":
                if direccion in srv.casillas:
                    return subprocess.CompletedProcess(
                        args, 1, stdout="", stderr=f"La casilla ya existe: {direccion}")
                srv.casillas[direccion] = kwargs.get("input", "").strip()
            elif accion == "borrar":
                srv.casillas.pop(direccion, None)
            return subprocess.CompletedProcess(args, 0, stdout="OK", stderr="")
        raise AssertionError(f"orden inesperada: {args}")

    monkeypatch.setattr(mc.subprocess, "run", run)
    return srv


@pytest.fixture
def instancia(tmp_path, fake_docker):  # noqa: F811
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db", repo_root=repo_root)
    cfg = provisioning.get_config()
    cdir = cfg.clientes_dir / "lagrace"
    cdir.mkdir(parents=True)
    (cdir / "cliente.json").write_text(json.dumps({
        "nombre": "La Grace", "slug": "lagrace", "container": "testprod-lagrace"}), encoding="utf-8")
    (cdir / "docker-compose.yml").write_text(_compose(), encoding="utf-8")
    return cdir


def _env_del_compose(cdir):
    return _env_de((cdir / "docker-compose.yml").read_text(encoding="utf-8"), "testprod-lagrace")


def test_la_casilla_se_crea_se_carga_en_el_compose_y_se_recrea_la_instancia(
        instancia, correo, servidor, fake_docker, capsys):  # noqa: F811
    assert pa.cmd_casilla("lagrace") is True

    env = _env_del_compose(instancia)
    assert env["LIBRAAUTH_SMTP_USER"] == "lagrace@testprod.com.ar"
    assert env["LIBRAAUTH_SMTP_FROM_NAME"] == "La Grace"
    # La contraseña del compose es la que quedó en el servidor.
    assert servidor.casillas["lagrace@testprod.com.ar"] == env["LIBRAAUTH_SMTP_PASSWORD"]
    # Viajó por stdin, no por argumento.
    (orden, stdin), = servidor.ordenes
    assert orden == ["crear", "lagrace@testprod.com.ar"]
    assert stdin == env["LIBRAAUTH_SMTP_PASSWORD"]
    # Y la instancia se recreó para tomarla.
    assert fake_docker["compose_calls"] == [("lagrace", ("up", "-d"))]
    # Nada de secretos por pantalla.
    salida = capsys.readouterr()
    assert env["LIBRAAUTH_SMTP_PASSWORD"] not in salida.out + salida.err
    assert "lagrace@testprod.com.ar" in salida.out


def test_repetirlo_no_hace_nada(instancia, correo, servidor, fake_docker, capsys):  # noqa: F811
    assert pa.cmd_casilla("lagrace") is True
    antes = (instancia / "docker-compose.yml").read_text(encoding="utf-8")
    servidor.ordenes.clear()
    fake_docker["compose_calls"].clear()

    assert pa.cmd_casilla("lagrace") is True

    assert (instancia / "docker-compose.yml").read_text(encoding="utf-8") == antes
    assert servidor.ordenes == []
    assert fake_docker["compose_calls"] == []
    assert "nada que hacer" in capsys.readouterr().out


def test_si_la_casilla_ya_existe_y_el_compose_no_la_tiene_no_borra_nada(
        instancia, correo, servidor, fake_docker, capsys):  # noqa: F811
    """La contraseña no se lee de vuelta; recrear borra el buzón: no se decide solo."""
    servidor.casillas["lagrace@testprod.com.ar"] = "la-de-otro-lado"
    antes = (instancia / "docker-compose.yml").read_text(encoding="utf-8")

    assert pa.cmd_casilla("lagrace") is False

    assert (instancia / "docker-compose.yml").read_text(encoding="utf-8") == antes
    assert servidor.casillas["lagrace@testprod.com.ar"] == "la-de-otro-lado"
    assert [o[0][0] for o in servidor.ordenes] == ["crear"]
    assert fake_docker["compose_calls"] == []
    assert "--recrear" in capsys.readouterr().out


def test_recrear_borra_y_crea_de_nuevo(instancia, correo, servidor, fake_docker):  # noqa: F811
    servidor.casillas["lagrace@testprod.com.ar"] = "la-de-otro-lado"

    assert pa.cmd_casilla("lagrace", recrear=True) is True

    assert [o[0][0] for o in servidor.ordenes] == ["crear", "borrar", "crear"]
    env = _env_del_compose(instancia)
    assert servidor.casillas["lagrace@testprod.com.ar"] == env["LIBRAAUTH_SMTP_PASSWORD"] != "la-de-otro-lado"


def test_recrear_con_el_compose_ya_cargado_rota_la_contrasena(
        instancia, correo, servidor):  # noqa: F811
    assert pa.cmd_casilla("lagrace") is True
    vieja = _env_del_compose(instancia)["LIBRAAUTH_SMTP_PASSWORD"]

    assert pa.cmd_casilla("lagrace", recrear=True) is True

    nueva = _env_del_compose(instancia)["LIBRAAUTH_SMTP_PASSWORD"]
    assert nueva != vieja
    assert servidor.casillas["lagrace@testprod.com.ar"] == nueva
    assert (instancia / "docker-compose.yml").read_text().count("LIBRAAUTH_SMTP_PASSWORD") == 1


def test_sin_servidor_de_correo_falla_y_dice_que_falta(instancia, archivo, servidor, capsys):
    assert pa.cmd_casilla("lagrace") is False
    salida = capsys.readouterr().out
    assert "[ERROR]" in salida and "LIBRA_MAIL_ADMIN_SSH" in salida
    assert servidor.ordenes == []


def test_con_el_servidor_caido_falla_y_no_toca_el_compose(
        instancia, correo, servidor, fake_docker, capsys):  # noqa: F811
    servidor.caido = True
    antes = (instancia / "docker-compose.yml").read_text(encoding="utf-8")

    assert pa.cmd_casilla("lagrace") is False

    assert (instancia / "docker-compose.yml").read_text(encoding="utf-8") == antes
    assert fake_docker["compose_calls"] == []
    assert "Connection refused" in capsys.readouterr().out


def test_si_el_compose_no_se_puede_modificar_borra_la_casilla_que_acaba_de_crear(
        instancia, correo, servidor, fake_docker, capsys):  # noqa: F811
    (instancia / "docker-compose.yml").write_text(
        "services:\n  testprod-lagrace:\n    image: testprod:v1\n"
        "    container_name: testprod-lagrace\n", encoding="utf-8")

    assert pa.cmd_casilla("lagrace") is False

    assert servidor.casillas == {}, "no quedó una casilla con credencial que nadie conoce"
    assert [o[0][0] for o in servidor.ordenes] == ["crear", "borrar"]
    assert fake_docker["compose_calls"] == []


def test_slug_inexistente(instancia, correo, servidor, capsys):
    assert pa.cmd_casilla("no-existe") is False
    assert "no encontrado" in capsys.readouterr().out


def test_up_fallido_se_informa(instancia, correo, servidor, monkeypatch, capsys):
    monkeypatch.setattr(pa, "compose", lambda slug, *a: subprocess.CompletedProcess([], 1))
    assert pa.cmd_casilla("lagrace") is False
    assert "up -d" in capsys.readouterr().out
    # El compose quedó escrito: la casilla sigue siendo la del archivo.
    assert _env_del_compose(instancia)["LIBRAAUTH_SMTP_USER"] == "lagrace@testprod.com.ar"


def test_el_comando_esta_en_el_cli(instancia, correo, servidor, monkeypatch):
    monkeypatch.setattr(pa.sys, "argv", ["panel_admin.py", "casilla", "lagrace"])
    pa.cli()
    assert "LIBRAAUTH_SMTP_PASSWORD" in _env_del_compose(instancia)

    monkeypatch.setattr(pa.sys, "argv", ["panel_admin.py", "casilla", "no-existe"])
    with pytest.raises(SystemExit) as salida:
        pa.cli()
    assert salida.value.code == 1
