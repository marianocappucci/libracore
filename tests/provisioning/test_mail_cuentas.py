"""
Tests de `libracore.provisioning.mail_cuentas`: la casilla de correo saliente
que el alta le crea a cada instancia.

El SSH contra el servidor de correo se intercepta en `subprocess.run`, que es
la frontera real del módulo — así los tests ejercitan el armado del comando y
no una capa intermedia inventada para poder testear.
"""
import subprocess

import pytest
import yaml

from libracore.provisioning import mail_cuentas as mc


@pytest.fixture
def configurado(monkeypatch):
    monkeypatch.setenv("LIBRA_MAIL_ADMIN_SSH", "libra-mail@mail.testprod.com.ar")
    monkeypatch.setenv("LIBRA_MAIL_DOMINIO", "testprod.com.ar")


@pytest.fixture
def ssh(monkeypatch):
    """Registra cada `subprocess.run` y contesta éxito.

    Guarda los kwargs enteros, no sólo los args: la propiedad que más importa
    de este módulo —que la contraseña viaje por stdin— vive en `input=`, y un
    doble que sólo mirara `args` no podría distinguirla de una que va como
    argumento.
    """
    llamadas = []

    def fake_run(args, **kwargs):
        llamadas.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(mc.subprocess, "run", fake_run)
    return llamadas


# ── configurado(): el opt-in por ausencia ───────────────────────────────────


def test_sin_variables_no_esta_configurado(monkeypatch):
    monkeypatch.delenv("LIBRA_MAIL_ADMIN_SSH", raising=False)
    monkeypatch.delenv("LIBRA_MAIL_DOMINIO", raising=False)
    assert mc.configurado() is False


@pytest.mark.parametrize("presente", ["LIBRA_MAIL_ADMIN_SSH", "LIBRA_MAIL_DOMINIO"])
def test_una_sola_variable_no_alcanza(monkeypatch, presente):
    """Media configuración es peor que ninguna: haría fallar cada alta con un
    `MailError` en vez de comportarse como el parque de hoy."""
    monkeypatch.delenv("LIBRA_MAIL_ADMIN_SSH", raising=False)
    monkeypatch.delenv("LIBRA_MAIL_DOMINIO", raising=False)
    monkeypatch.setenv(presente, "algo")
    assert mc.configurado() is False


def test_con_las_dos_esta_configurado(configurado):
    assert mc.configurado() is True


# ── la dirección ────────────────────────────────────────────────────────────


def test_la_direccion_es_slug_arroba_dominio_del_producto(configurado):
    assert mc.direccion_de("lagrace") == "lagrace@testprod.com.ar"


def test_una_direccion_mal_formada_no_llega_al_ssh(monkeypatch, ssh):
    """El dominio sale del entorno y el slug de un formulario. Si el resultado
    no tiene forma de dirección, se corta ACÁ: del otro lado del SSH hay un
    shell, y no es el lugar donde descubrir que `LIBRA_MAIL_DOMINIO` quedó con
    un espacio."""
    monkeypatch.setenv("LIBRA_MAIL_ADMIN_SSH", "libra-mail@mail.testprod.com.ar")
    monkeypatch.setenv("LIBRA_MAIL_DOMINIO", "no es un dominio")
    with pytest.raises(mc.MailError):
        mc.crear_cuenta("lagrace", "La Grace", "unaclave")
    assert ssh == [], "no tenía que salir ninguna orden al servidor de correo"


# ── crear_cuenta ────────────────────────────────────────────────────────────


def test_crear_cuenta_manda_la_contrasena_por_stdin_y_no_como_argumento(
    configurado, ssh
):
    """🔴 La propiedad de seguridad del módulo.

    Como argumento, la contraseña queda visible en el `ps` del servidor de
    correo mientras dura la orden, y además llega del otro lado adentro de
    `SSH_ORIGINAL_COMMAND` — una variable de entorno, o sea cualquier log que
    vuelque el entorno.
    """
    mc.crear_cuenta("lagrace", "La Grace", "clave-secreta-123")

    (args, kwargs), = ssh
    assert kwargs["input"] == "clave-secreta-123"
    assert "clave-secreta-123" not in args
    # Y tampoco escondida adentro de otro argumento.
    assert not any("clave-secreta-123" in str(a) for a in args)


def test_crear_cuenta_arma_la_orden_contra_el_destino_ssh(configurado, ssh):
    mc.crear_cuenta("lagrace", "La Grace", "unaclave")

    (args, kwargs), = ssh
    assert args[0] == "ssh"
    assert "libra-mail@mail.testprod.com.ar" in args
    # El verbo y la dirección, en ese orden, al final de la orden.
    assert args[-2:] == ["crear", "lagrace@testprod.com.ar"]
    # Sin BatchMode un problema de clave abre un prompt y el alta se cuelga.
    assert "BatchMode=yes" in args


def test_crear_cuenta_devuelve_con_que_conectarse(configurado, ssh):
    cuenta = mc.crear_cuenta("lagrace", "La Grace", "unaclave")
    assert cuenta.direccion == "lagrace@testprod.com.ar"
    assert cuenta.password == "unaclave"
    assert cuenta.host == "mail.testprod.com.ar"
    assert cuenta.port == 587
    assert cuenta.from_name == "La Grace"


def test_sin_contrasena_no_se_crea_la_casilla(configurado, ssh):
    with pytest.raises(mc.MailError):
        mc.crear_cuenta("lagrace", "La Grace", "")
    assert ssh == []


# ── el host y el puerto SMTP ────────────────────────────────────────────────


def test_el_host_smtp_se_puede_declarar_aparte_del_destino_ssh(configurado, monkeypatch, ssh):
    """Al SSH se le puede pasar un alias de `~/.ssh/config` o una IP; el host
    SMTP tiene que ser el nombre que matchea el certificado TLS."""
    monkeypatch.setenv("LIBRA_MAIL_ADMIN_SSH", "alias-interno")
    monkeypatch.setenv("LIBRA_MAIL_SMTP_HOST", "mail.testprod.com.ar")
    assert mc.crear_cuenta("x", "X", "c").host == "mail.testprod.com.ar"


def test_sin_host_declarado_cae_al_destino_ssh_sin_el_usuario(configurado, ssh):
    assert mc.crear_cuenta("x", "X", "c").host == "mail.testprod.com.ar"


def test_un_puerto_que_no_es_numero_es_un_error_explicito(configurado, monkeypatch, ssh):
    monkeypatch.setenv("LIBRA_MAIL_SMTP_PORT", "quinientos")
    with pytest.raises(mc.MailError, match="puerto"):
        mc.crear_cuenta("x", "X", "c")


# ── errores del servidor de correo ──────────────────────────────────────────


def test_un_rechazo_del_servidor_llega_con_su_motivo(configurado, monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr="la casilla ya existe\n"
        )

    monkeypatch.setattr(mc.subprocess, "run", fake_run)
    with pytest.raises(mc.MailError, match="la casilla ya existe"):
        mc.crear_cuenta("lagrace", "La Grace", "unaclave")


def test_un_servidor_que_no_contesta_no_cuelga_el_alta(configurado, monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, mc._TIMEOUT)

    monkeypatch.setattr(mc.subprocess, "run", fake_run)
    with pytest.raises(mc.MailError, match="no contestó"):
        mc.crear_cuenta("lagrace", "La Grace", "unaclave")


def test_sin_destino_ssh_es_un_error_y_no_un_ssh_a_la_nada(monkeypatch, ssh):
    monkeypatch.delenv("LIBRA_MAIL_ADMIN_SSH", raising=False)
    monkeypatch.setenv("LIBRA_MAIL_DOMINIO", "testprod.com.ar")
    with pytest.raises(mc.MailError):
        mc.borrar_cuenta("lagrace@testprod.com.ar")
    assert ssh == []


# ── borrar_cuenta ───────────────────────────────────────────────────────────


def test_borrar_cuenta_manda_el_verbo_borrar(configurado, ssh):
    mc.borrar_cuenta("lagrace@testprod.com.ar")
    (args, _), = ssh
    assert args[-2:] == ["borrar", "lagrace@testprod.com.ar"]


def test_borrar_una_direccion_mal_formada_no_llega_al_ssh(configurado, ssh):
    with pytest.raises(mc.MailError):
        mc.borrar_cuenta("; rm -rf /")
    assert ssh == []


# ── env_para_compose ────────────────────────────────────────────────────────


def _variables(bloque: str) -> dict:
    """Lo que le va a llegar al contenedor, no lo que dice el texto.

    🔴 Los tests de este bloque parsean con YAML a propósito. Asertar sobre el
    string es lo que dejó pasar el `shlex.quote`: el texto se veía razonable
    (`- K='La Grace'`) y el valor que llegaba tenía las comillas adentro. La
    pregunta que hay que hacerle a esto no es "¿qué línea escribí?" sino "¿qué
    variable de entorno queda?".
    """
    doc = yaml.safe_load("environment:\n" + bloque)
    return dict(item.partition("=")[::2] for item in doc["environment"])


def test_env_para_compose_escribe_las_seis_variables(configurado, ssh):
    cuenta = mc.crear_cuenta("lagrace", "La Grace", "unaclave")

    assert _variables(mc.env_para_compose(cuenta)) == {
        "LIBRAAUTH_SMTP_HOST": "mail.testprod.com.ar",
        "LIBRAAUTH_SMTP_PORT": "587",
        "LIBRAAUTH_SMTP_USER": "lagrace@testprod.com.ar",
        "LIBRAAUTH_SMTP_PASSWORD": "unaclave",
        "LIBRAAUTH_SMTP_FROM_EMAIL": "lagrace@testprod.com.ar",
        "LIBRAAUTH_SMTP_FROM_NAME": "La Grace",
    }
    # Termina en salto de línea: se concatena con los otros bloques del compose.
    assert mc.env_para_compose(cuenta).endswith("\n")


def test_un_nombre_con_espacios_no_llega_entrecomillado(configurado, ssh):
    """🔴 El defecto que tuvo este módulo antes de existir en `main`.

    `shlex.quote` cita para un shell; acá el consumidor es YAML + el split de
    Compose, así que las comillas terminaban **adentro del valor** y cada
    correo habría salido con el remitente entre comillas.
    """
    cuenta = mc.crear_cuenta("x", "La Grace", "unaclave")
    assert _variables(mc.env_para_compose(cuenta))["LIBRAAUTH_SMTP_FROM_NAME"] == "La Grace"


def test_un_numeral_en_el_nombre_no_trunca_el_valor(configurado, ssh):
    """Sin comillas en el ítem, YAML lee ` #` como comentario y «Casa # 5»
    llega como «Casa». Un cliente con un número de local en el nombre no es un
    caso raro."""
    cuenta = mc.crear_cuenta("x", "Casa # 5", "unaclave")
    assert _variables(mc.env_para_compose(cuenta))["LIBRAAUTH_SMTP_FROM_NAME"] == "Casa # 5"


def test_comillas_y_barras_en_el_nombre_no_rompen_el_compose(configurado, ssh):
    cuenta = mc.crear_cuenta("x", 'Cami\\no "El Alto"', "unaclave")
    assert _variables(mc.env_para_compose(cuenta))["LIBRAAUTH_SMTP_FROM_NAME"] == 'Cami\\no "El Alto"'


def test_un_nombre_con_salto_de_linea_no_agrega_una_variable(configurado, ssh):
    """🔴 El nombre sale de un formulario y el compose se arma como texto. Un
    `\\n` ahí adentro escribiría un ítem propio en el bloque `environment`.

    **Las dos capas lo cubren por separado**, verificado por mutación: sacando
    el saneado de `_from_name` el ítem sigue siendo uno solo (YAML pliega el
    salto adentro del escalar entre comillas), y sacando las comillas también
    (el nombre ya llega sin saltos). Por eso este test no muere con ninguna de
    las dos mutaciones sola — lo que cada capa sostiene en exclusiva está en
    `…_no_lleva_un_salto_de_linea_adentro_del_valor` y en
    `…_un_numeral_en_el_nombre_no_trunca_el_valor`.
    """
    cuenta = mc.crear_cuenta(
        "x", "La Grace\n      - ADMIN_PASSWORD=colado", "unaclave"
    )
    variables = _variables(mc.env_para_compose(cuenta))
    assert set(variables) == {
        "LIBRAAUTH_SMTP_HOST", "LIBRAAUTH_SMTP_PORT", "LIBRAAUTH_SMTP_USER",
        "LIBRAAUTH_SMTP_PASSWORD", "LIBRAAUTH_SMTP_FROM_EMAIL",
        "LIBRAAUTH_SMTP_FROM_NAME",
    }
    assert "ADMIN_PASSWORD" not in variables


def test_el_nombre_nunca_lleva_un_salto_de_linea_adentro_del_valor(configurado, ssh):
    """La segunda capa, y la que las comillas NO cubren.

    🔴 Se prueba con **dos** saltos seguidos, no con uno. Medido: YAML pliega
    un salto suelto a un espacio —así que con uno solo este test pasaría
    aunque `_from_name` no hiciera nada, que es exactamente como estaba
    escrito antes—, pero una línea en blanco se pliega a un `\\n` literal y
    queda adentro del valor de la variable de entorno.
    """
    cuenta = mc.crear_cuenta("x", "La Grace\n\nSucursal Centro", "unaclave")
    nombre = _variables(mc.env_para_compose(cuenta))["LIBRAAUTH_SMTP_FROM_NAME"]
    assert "\n" not in nombre
    assert nombre == "La Grace Sucursal Centro"
