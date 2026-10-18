"""
Tests de libracore.admin.services. `panel_admin`/`nuevo_cliente`/`plans`
son específicos de cada producto (no del paquete LibraCore) — se
inyectan como módulos falsos en `sys.modules`, resueltos por los imports
diferidos de `services.py` vía `configure(repo_root=...)` (Fase 4 de
LibraCore, backoffice compartido — ver wiki/entities/libracore.md).
"""
import json
import sys
import types

import pytest

from libracore import provisioning
from libracore.admin import services


@pytest.fixture
def fake_scripts(monkeypatch, tmp_path):
    clientes_dir = tmp_path / "clientes"
    clientes_dir.mkdir()

    def _mkclient(nombre, slug, domain="", port=8080, admin_user="admin", plan="basico",
                  con_config=True):
        cdir = clientes_dir / slug
        (cdir / "data").mkdir(parents=True)
        (cdir / "data" / "test.db").write_bytes(b"")
        # `data/config.json` lo escribe la instancia en su primer arranque. Con
        # `con_config=False` se simula la instancia que nunca levantó, que es
        # donde el corte de servicio no tiene dónde escribir.
        if con_config:
            (cdir / "data" / "config.json").write_text(
                json.dumps({"servicio_estado": "activo", "servicio_mensaje": "",
                            "empresa_nombre": nombre}),
                encoding="utf-8")
        meta = {"nombre": nombre, "domain": domain, "port": port,
                "admin_user": admin_user, "plan": plan}
        (cdir / "cliente.json").write_text(json.dumps(meta), encoding="utf-8")
        return {"slug": slug, "nombre": nombre, "domain": domain, "port": port,
                "container": slug, "admin_user": admin_user, "plan": plan, "dir": cdir}

    def _find_client(slug):
        # Simula panel_admin.find_client real: relee cliente.json del disco
        # en cada llamada, no un registro en memoria — así los cambios que
        # services.py persiste (editar_cliente/set_plan) se reflejan.
        cdir = clientes_dir / slug
        meta_path = cdir / "cliente.json"
        if not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return {"slug": slug, "nombre": meta.get("nombre", ""), "domain": meta.get("domain", ""),
                "port": meta.get("port", 0), "container": slug,
                "admin_user": meta.get("admin_user", ""), "plan": meta.get("plan", ""), "dir": cdir}

    def _load_clients():
        return [_find_client(p.name) for p in clientes_dir.iterdir() if p.is_dir()]

    fake_pa = types.ModuleType("panel_admin")
    # Un señuelo a propósito: `services` ya no lee esta constante (la fuente es
    # `provisioning.get_config().clientes_dir`), y si volviera a leerla el backup
    # caería acá y `test_backup_cliente_crea_tar` se pondría rojo.
    fake_pa.CLIENTES_DIR = tmp_path / "clientes-del-producto-que-no-manda"
    fake_pa._NPM_AVAILABLE = False
    fake_pa.load_clients = _load_clients
    fake_pa.find_client = _find_client
    fake_pa.container_status = lambda container: {"status": "running", "started": "2026-07-14"}
    fake_pa.compose_calls = []
    fake_pa.compose = lambda slug, *args: fake_pa.compose_calls.append((slug, args))
    fake_pa.estado_calls = []

    def _set_servicio_estado(slug, estado, mensaje=""):
        # Réplica del real: relee `data/config.json`, pisa las dos claves de
        # servicio y reescribe el resto tal cual. Escribir de verdad es lo que
        # hace que `_enrich` pueda leer después lo que esta llamada dejó — con
        # un stub que sólo anota la llamada, el ida y vuelta no se ejercita.
        config_path = clientes_dir / slug / "data" / "config.json"
        fake_pa.estado_calls.append((slug, estado, mensaje))
        if not config_path.exists():
            return False
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        cfg["servicio_estado"] = estado
        cfg["servicio_mensaje"] = mensaje
        config_path.write_text(json.dumps(cfg), encoding="utf-8")
        return True

    fake_pa._set_servicio_estado = _set_servicio_estado

    fake_nc = types.ModuleType("nuevo_cliente")

    class ClienteError(Exception):
        pass

    fake_nc.ClienteError = ClienteError

    # La firma se copia de la real a proposito: este doble es lo unico que
    # atrapa un `services.crear_cliente` que deje de pasarle un argumento al
    # motor. Si se le agrega `**kwargs` para que "no moleste", deja de atrapar
    # nada — que es justo lo que hizo ruido al sumar la identidad fiscal.
    def crear_cliente(nombre, slug="", domain="", port=0, admin_user="admin",
                      admin_password="", plan="basico", empresa_cuit="",
                      empresa_nombre="", sin_identidad=False, setup_npm=True, **extra):
        slug = slug or nombre.lower().replace(" ", "-")
        if fake_pa.find_client(slug):
            raise ClienteError(f"Ya existe un cliente con slug '{slug}'.")
        _mkclient(nombre, slug, domain, port, admin_user, plan)
        fake_nc.ultima_alta = {
            "empresa_cuit": empresa_cuit, "empresa_nombre": empresa_nombre,
            "sin_identidad": sin_identidad,
            # lo que el servicio pasó de MÁS (ADR-041): vacío si no había nada que pasar
            "extra": extra,
        }
        return {"slug": slug, "admin_password": admin_password or "generated123"}

    fake_nc.crear_cliente = crear_cliente

    fake_plans = types.ModuleType("plans")
    fake_plans.PLANES = ["basico", "pro"]
    fake_plans.PLAN_LABELS = {"basico": "Básico", "pro": "Pro"}
    fake_plans.PLAN_PRECIOS = {"basico": 1000, "pro": 2000}
    fake_plans.modulos_de_plan = lambda p: {"clientes", "caja"} if p == "basico" else {"clientes", "caja", "reportes"}
    fake_plans.aplicar_plan_calls = []
    fake_plans.aplicar_plan_en_db = lambda db_path, plan: fake_plans.aplicar_plan_calls.append((db_path, plan))

    monkeypatch.setitem(sys.modules, "panel_admin", fake_pa)
    monkeypatch.setitem(sys.modules, "nuevo_cliente", fake_nc)
    monkeypatch.setitem(sys.modules, "plans", fake_plans)

    services.configure(repo_root=tmp_path, db_filename="test.db")
    # En producción lo configura el import del `panel_admin.py` del producto.
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="test.db",
        repo_root=tmp_path, clientes_dir=clientes_dir,
    )

    yield {"clientes_dir": clientes_dir, "mkclient": _mkclient,
           "pa": fake_pa, "plans": fake_plans}
    provisioning._cfg = None


def test_listar_clientes_vacio(fake_scripts):
    assert services.listar_clientes() == []


def test_crear_cliente_y_listar(fake_scripts):
    info = services.crear_cliente(nombre="Cliente Uno", slug="cliente-uno")
    assert info["slug"] == "cliente-uno"
    clientes = services.listar_clientes()
    assert len(clientes) == 1
    assert clientes[0]["slug"] == "cliente-uno"
    assert clientes[0]["estado"] == "running"


def test_crear_cliente_pasa_las_sucursales_contratadas_solo_si_hay(fake_scripts):
    """ADR-041: con un número viaja al alta; sin él el `nuevo_cliente` del producto no recibe ni el argumento (una versión que no lo conozca no se rompe)."""
    import nuevo_cliente as nc

    services.crear_cliente(nombre="Con Tope", slug="con-tope", sucursales_contratadas=2)
    assert nc.ultima_alta["extra"] == {"sucursales_contratadas": 2}
    services.crear_cliente(nombre="Sin Tope", slug="sin-tope")
    assert nc.ultima_alta["extra"] == {}


def test_crear_cliente_duplicado_lanza_service_error(fake_scripts):
    services.crear_cliente(nombre="Cliente Uno", slug="cliente-uno")
    with pytest.raises(services.ServiceError):
        services.crear_cliente(nombre="Cliente Uno Bis", slug="cliente-uno")


def test_un_alta_incompleta_no_se_confunde_con_un_rechazo(fake_scripts, monkeypatch):
    """`AltaIncompleta` tiene que llegar al backoffice como su propio tipo.

    Si se aplana en `ServiceError`, el router la devuelve como 422 y el frontend
    lee un 422 como "el motor rechazo el alta, no se creo nada" — o sea que
    invita a reintentar sobre un slug que YA esta tomado. La instancia existe:
    el estado correcto es "creada pero no entregable".

    El `except` genérico de `crear_cliente` la tapaba por orden, porque
    `AltaIncompleta` ES un `ClienteError`.

    ⚠️ La excepción sale de LibraCore y NO de `nc`. El shim de cada producto
    re-exporta una lista explícita de nombres que no incluye `AltaIncompleta`;
    este fixture reproduce esa forma, asi que `except nc.AltaIncompleta` daria
    `AttributeError` acá igual que en los seis productos."""
    from libracore.provisioning.nuevo_cliente import AltaIncompleta

    nc = services._nc()
    assert not hasattr(nc, "AltaIncompleta"), \
        "el shim del producto no re-exporta esta clase; es la premisa del test"
    monkeypatch.setattr(
        nc, "crear_cliente",
        lambda **kw: (_ for _ in ()).throw(AltaIncompleta("la base nunca subio")),
    )

    with pytest.raises(services.AltaIncompletaError, match="la base nunca subio"):
        services.crear_cliente(nombre="Cliente Uno", slug="cliente-uno")

    # y sigue siendo un ServiceError: el router no tiene que dejar de atraparla
    assert issubclass(services.AltaIncompletaError, services.ServiceError)


def test_get_cliente_inexistente_devuelve_none(fake_scripts):
    assert services.get_cliente("no-existe") is None


def test_editar_cliente_actualiza_metadata(fake_scripts):
    fake_scripts["mkclient"]("Cliente Dos", "cliente-dos", domain="")
    services.editar_cliente("cliente-dos", "Cliente Dos Editado", "nuevo.dominio.com")
    c = services.get_cliente("cliente-dos")
    assert c["nombre"] == "Cliente Dos Editado"
    assert c["domain"] == "nuevo.dominio.com"


def _npm_falso(fake_scripts, monkeypatch):
    """Prende NPM en el `panel_admin` falso y anota lo que se le pide crear."""
    creados = []

    class FakeNPM:
        def get_proxy_host_by_domain(self, domain):
            return None

        def create_proxy_host(self, **kwargs):
            creados.append(kwargs)
            return {"id": 1}

    pa = fake_scripts["pa"]
    monkeypatch.setattr(pa, "_NPM_AVAILABLE", True, raising=False)
    monkeypatch.setattr(pa, "client_from_config", lambda: FakeNPM(), raising=False)
    monkeypatch.setattr(pa, "le_email_from_config", lambda: "ssl@test.com", raising=False)
    # La puerta de enlace de la config: el proxy NO tiene que salir de acá.
    monkeypatch.setattr(pa, "forward_host_from_config", lambda: "10.0.0.1", raising=False)
    return creados


def test_editar_dominio_crea_el_proxy_por_nombre_de_contenedor(fake_scripts, monkeypatch):
    """🔑 Por nombre de contenedor y el puerto de la app (NPM y la instancia
    comparten `stack_stack-net`), no por la puerta de enlace más el puerto del
    host. El `cliente.json` del doble no trae `container`: el nombre sale de
    `find_client`, que es el respaldo para una instancia vieja."""
    creados = _npm_falso(fake_scripts, monkeypatch)
    fake_scripts["mkclient"]("Cliente Cinco", "cliente-cinco", domain="", port=8089)
    services.editar_cliente("cliente-cinco", "Cliente Cinco", "cinco.dominio.com")
    assert len(creados) == 1
    assert creados[0]["domain"] == "cinco.dominio.com"
    assert creados[0]["forward_host"] == "cliente-cinco"
    assert creados[0]["forward_port"] == 8000


def test_sin_nombre_de_contenedor_no_se_inventa_un_destino(fake_scripts, monkeypatch):
    creados = _npm_falso(fake_scripts, monkeypatch)
    assert services._npm_crear_proxy("x.dominio.com", "") is False
    assert creados == []


def test_set_plan_aplica_y_persiste(fake_scripts):
    fake_scripts["mkclient"]("Cliente Tres", "cliente-tres")
    services.set_plan("cliente-tres", "pro")
    assert fake_scripts["plans"].aplicar_plan_calls[-1][1] == "pro"
    c = services.get_cliente("cliente-tres")
    assert c["plan"] == "pro"


def test_set_plan_invalido_lanza_service_error(fake_scripts):
    fake_scripts["mkclient"]("Cliente Cuatro", "cliente-cuatro")
    with pytest.raises(services.ServiceError):
        services.set_plan("cliente-cuatro", "plan-inexistente")


def test_accion_estado_invalida_lanza_service_error(fake_scripts):
    fake_scripts["mkclient"]("Cliente Cinco", "cliente-cinco")
    with pytest.raises(services.ServiceError):
        services.accion_estado("cliente-cinco", "accion-invalida")


def test_accion_estado_pausar(fake_scripts):
    fake_scripts["mkclient"]("Cliente Seis", "cliente-seis")
    services.accion_estado("cliente-seis", "pausar")
    assert fake_scripts["pa"].estado_calls[-1] == ("cliente-seis", "pausado", "")


def test_el_inventario_expone_el_estado_de_servicio(fake_scripts):
    """El corte comercial es un eje distinto del estado del contenedor.

    Sin esto el backoffice puede suspender una instancia pero no mostrar que
    está suspendida: `estado` sigue diciendo `running`, porque el contenedor
    efectivamente corre y devuelve 503 a todo.
    """
    fake_scripts["mkclient"]("Cliente Ocho", "cliente-ocho")
    services.accion_estado("cliente-ocho", "suspender", mensaje="Falta de pago")

    c = services.get_cliente("cliente-ocho")
    assert c["servicio_estado"] == "suspendido"
    assert c["servicio_mensaje"] == "Falta de pago"
    assert c["estado"] == "running"

    listado = {i["slug"]: i for i in services.listar_clientes()}
    assert listado["cliente-ocho"]["servicio_estado"] == "suspendido"


def test_activar_limpia_el_mensaje(fake_scripts):
    """El cliente que ya pagó no puede seguir viendo "falta de pago"."""
    fake_scripts["mkclient"]("Cliente Nueve", "cliente-nueve")
    services.accion_estado("cliente-nueve", "suspender", mensaje="Falta de pago")
    services.accion_estado("cliente-nueve", "activar", mensaje="Falta de pago")

    c = services.get_cliente("cliente-nueve")
    assert c["servicio_estado"] == "activo"
    assert c["servicio_mensaje"] == ""


def test_el_corte_no_pisa_el_resto_de_la_configuracion(fake_scripts):
    """Cambiar el estado no puede tocar nada más de `config.json`."""
    fake_scripts["mkclient"]("Cliente Diez", "cliente-diez")
    config_path = fake_scripts["clientes_dir"] / "cliente-diez" / "data" / "config.json"
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cfg["mp_access_token"] = "TOKEN-DEL-CLIENTE"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")

    services.accion_estado("cliente-diez", "pausar", mensaje="Mantenimiento")

    despues = json.loads(config_path.read_text(encoding="utf-8"))
    assert despues["mp_access_token"] == "TOKEN-DEL-CLIENTE"
    assert despues["empresa_nombre"] == "Cliente Diez"


def test_cortar_el_servicio_de_una_instancia_que_nunca_arranco(fake_scripts):
    """Sin `config.json` no hay dónde escribir, y el error tiene que decirlo.

    Antes devolvía "No se pudo cambiar el estado del servicio", que no distingue
    esto de un fallo de escritura.
    """
    fake_scripts["mkclient"]("Cliente Once", "cliente-once", con_config=False)
    with pytest.raises(services.ServiceError, match="iniciarla al menos una vez"):
        services.accion_estado("cliente-once", "suspender", mensaje="Falta de pago")


def test_sin_config_el_inventario_reporta_activo(fake_scripts):
    """Es lo que hace la instancia: sin `config.json` levanta con los DEFAULTS."""
    fake_scripts["mkclient"]("Cliente Doce", "cliente-doce", con_config=False)
    c = services.get_cliente("cliente-doce")
    assert c["servicio_estado"] == "activo"
    assert c["servicio_mensaje"] == ""


def test_backup_cliente_crea_tar(fake_scripts):
    fake_scripts["mkclient"]("Cliente Siete", "cliente-siete")
    out_file = services.backup_cliente("cliente-siete")
    from pathlib import Path
    assert Path(out_file).exists()
    assert out_file.endswith(".tar.gz")


def test_backup_cliente_cae_en_el_directorio_del_motor_no_en_la_constante_del_producto(fake_scripts):
    """La única fuente es `provisioning.get_config().clientes_dir`.

    `panel_admin.CLIENTES_DIR` (la constante que cada producto calcula por su
    cuenta) es acá un señuelo: si `services` volviera a leerla, el respaldo
    quedaría en una carpeta que ni el panel ni el cron miran.
    """
    from pathlib import Path
    fake_scripts["mkclient"]("Cliente Siete", "cliente-siete")
    out = Path(services.backup_cliente("cliente-siete"))
    assert out.parent == fake_scripts["clientes_dir"]
    assert out.parent != fake_scripts["pa"].CLIENTES_DIR
    assert services._clientes_dir() == provisioning.get_config().clientes_dir


def test_eliminar_cliente_borra_directorio(fake_scripts):
    c = fake_scripts["mkclient"]("Cliente Ocho", "cliente-ocho")
    from pathlib import Path
    assert Path(c["dir"]).exists()
    services.eliminar_cliente("cliente-ocho", hacer_backup=False)
    assert not Path(c["dir"]).exists()


def test_la_baja_sin_servidor_de_correo_no_intenta_borrar_ninguna_casilla(
        fake_scripts, monkeypatch):
    monkeypatch.delenv("LIBRA_MAIL_ADMIN_SSH", raising=False)
    monkeypatch.delenv("LIBRA_MAIL_DOMINIO", raising=False)
    fake_scripts["mkclient"]("Cliente Nueve", "cliente-nueve")

    resultado = services.eliminar_cliente("cliente-nueve", hacer_backup=False)
    assert resultado["correo"] is None


def test_la_baja_borra_la_casilla_de_correo_de_la_instancia(fake_scripts, monkeypatch):
    """🔴 Una casilla huérfana es una credencial viva.

    El compose que la usaba se borra en esta misma función, pero cualquiera que
    tenga una copia —un backup, un archivo que alguien guardó— podría seguir
    mandando correo como esa instancia mientras la casilla exista.
    """
    from libracore.provisioning import mail_cuentas

    monkeypatch.setenv("LIBRA_MAIL_ADMIN_SSH", "libra-mail@mail.testprod.com.ar")
    monkeypatch.setenv("LIBRA_MAIL_DOMINIO", "testprod.com.ar")
    borradas = []
    monkeypatch.setattr(mail_cuentas, "borrar_cuenta", borradas.append)
    fake_scripts["mkclient"]("Cliente Diez", "cliente-diez")

    resultado = services.eliminar_cliente("cliente-diez", hacer_backup=False)

    assert borradas == ["cliente-diez@testprod.com.ar"]
    assert resultado["correo"] is True


def test_un_servidor_de_correo_caido_no_impide_dar_de_baja(fake_scripts, monkeypatch):
    """Best-effort como el proxy de NPM: la baja tiene que poder completarse
    con el servidor de correo caído, avisando que la casilla quedó."""
    from pathlib import Path

    from libracore.provisioning import mail_cuentas

    monkeypatch.setenv("LIBRA_MAIL_ADMIN_SSH", "libra-mail@mail.testprod.com.ar")
    monkeypatch.setenv("LIBRA_MAIL_DOMINIO", "testprod.com.ar")
    monkeypatch.setattr(
        mail_cuentas, "borrar_cuenta",
        lambda *a: (_ for _ in ()).throw(mail_cuentas.MailError("no contesta")),
    )
    c = fake_scripts["mkclient"]("Cliente Once", "cliente-once")

    resultado = services.eliminar_cliente("cliente-once", hacer_backup=False)

    assert resultado["correo"] is False
    assert not Path(c["dir"]).exists()


def test_planes_info(fake_scripts):
    info = services.planes_info()
    assert {p["key"] for p in info} == {"basico", "pro"}
    basico = next(p for p in info if p["key"] == "basico")
    assert basico["label"] == "Básico"
    assert basico["precio"] == 1000


def test_plan_vigente_resuelve_un_plan_retirado_y_deja_el_resto(fake_scripts):
    """Una instancia vieja guarda `basico` en su `cliente.json`; si el producto lo retiró, el plan que rige es su reemplazo (ADR-041)."""
    fake_scripts["plans"].PLANES_RETIRADOS = {"viejo": "pro"}
    assert services.plan_vigente("viejo") == "pro"
    assert services.plan_vigente("basico") == "basico"
    assert services.plan_vigente("desconocido") == "desconocido"
    assert services.plan_vigente("") == ""


def test_planes_info_sin_unidades_adicionales_dice_none(fake_scripts):
    """Un `plans.py` que no declara `PLAN_UNIDADES_ADICIONALES` (los otros productos) no rompe y no cobra nada de más (ADR-041)."""
    assert not hasattr(fake_scripts["plans"], "PLAN_UNIDADES_ADICIONALES")
    assert all(p["adicional"] is None for p in services.planes_info())


def test_planes_info_trae_la_unidad_adicional_del_plan_que_la_declara(fake_scripts):
    adicional = {"unidad": "sucursal", "incluidas": 1, "precio": 19950}
    fake_scripts["plans"].PLAN_UNIDADES_ADICIONALES = {"pro": adicional}
    info = {p["key"]: p for p in services.planes_info()}
    assert info["pro"]["adicional"] == adicional
    assert info["basico"]["adicional"] is None
    # una copia: quien la modifica no toca el `plans.py`
    info["pro"]["adicional"]["precio"] = 1
    assert fake_scripts["plans"].PLAN_UNIDADES_ADICIONALES["pro"]["precio"] == 19950


def test_configure_requerido_antes_de_usar():
    services._repo_root = None
    with pytest.raises(RuntimeError):
        services.listar_clientes()


# ── add-ons ──────────────────────────────────────────────────────────────────

class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_set_addon_corre_el_toggle_en_el_contenedor(fake_scripts, monkeypatch):
    fake_scripts["mkclient"]("Distri", "distri-1")
    fake_scripts["plans"].ADDONS = {"mayorista"}
    llamadas = []
    monkeypatch.setattr(services.subprocess, "run",
                        lambda cmd, **kw: llamadas.append(cmd) or _R())

    services.set_addon("distri-1", "mayorista", True)

    cmd = llamadas[0]
    assert cmd[:3] == ["docker", "exec", "distri-1"]   # container == slug en el doble
    assert cmd[3:5] == ["python3", "-c"]
    assert "from app.database import set_addon" in cmd[-1]
    assert "set_addon('mayorista', True)" in cmd[-1]


def test_set_addon_off_pasa_false(fake_scripts, monkeypatch):
    fake_scripts["mkclient"]("Distri", "distri-2")
    fake_scripts["plans"].ADDONS = {"mayorista"}
    codigos = []
    monkeypatch.setattr(services.subprocess, "run",
                        lambda cmd, **kw: codigos.append(cmd[-1]) or _R())
    services.set_addon("distri-2", "mayorista", False)
    assert "set_addon('mayorista', False)" in codigos[0]


def test_set_addon_desconocido_lanza(fake_scripts):
    fake_scripts["mkclient"]("Distri", "distri-3")
    fake_scripts["plans"].ADDONS = {"mayorista"}
    with pytest.raises(services.ServiceError):
        services.set_addon("distri-3", "inexistente", True)


def test_set_addon_error_de_docker_lanza(fake_scripts, monkeypatch):
    fake_scripts["mkclient"]("Distri", "distri-4")
    fake_scripts["plans"].ADDONS = {"mayorista"}
    monkeypatch.setattr(services.subprocess, "run",
                        lambda cmd, **kw: _R(returncode=1, stderr="boom"))
    with pytest.raises(services.ServiceError):
        services.set_addon("distri-4", "mayorista", True)


def test_addons_de_instancia_lee_el_estado(fake_scripts, monkeypatch):
    fake_scripts["mkclient"]("Distri", "distri-5")
    fake_scripts["plans"].ADDONS = {"mayorista"}
    monkeypatch.setattr(services.subprocess, "run",
                        lambda cmd, **kw: _R(stdout='{"mayorista": true, "caja": true}'))
    assert services.addons_de_instancia("distri-5") == {"mayorista": True}


def test_addons_de_instancia_sin_addons_da_vacio(fake_scripts):
    fake_scripts["mkclient"]("Distri", "distri-6")
    # fake_plans no define ADDONS → producto sin add-ons.
    assert services.addons_de_instancia("distri-6") == {}


# 🔑 Los tres que siguen fijan que **"no sé" no se reporta como "no"**.
#
# Hasta el 2026-09-09 acá había un solo test —`..._contenedor_caido_los_da_apagados`—
# que exigía `{"mayorista": False}` ante un `docker exec` fallido. O sea que el
# defecto estaba consagrado en verde: la función devolvía "apagado" cuando no
# había podido leer nada, y el backoffice mostraba el add-on destildado de una
# instancia que lo tenía prendido (`libradesk-lagrace`, `modo_simple=true`).
#
# El caso "contenedor caído" sigue existiendo; lo que cambia es qué se espera.


def test_addons_de_instancia_contenedor_caido_da_desconocido(fake_scripts, monkeypatch):
    fake_scripts["mkclient"]("Distri", "distri-7")
    fake_scripts["plans"].ADDONS = {"mayorista"}
    monkeypatch.setattr(services.subprocess, "run",
                        lambda cmd, **kw: _R(returncode=1, stderr="No such container"))
    assert services.addons_de_instancia("distri-7") == {"mayorista": None}


def test_addons_de_instancia_snippet_roto_da_desconocido(fake_scripts, monkeypatch):
    """El caso real de LibraDesk: el contenedor está vivo y el snippet no importa.

    `app.database` de LibraDesk es una engine factory de SQLAlchemy y no exporta
    `get_modulos`, así que el `docker exec` sale con `ImportError`. Es
    indistinguible de un contenedor caído desde acá, y las dos cosas son
    "no pude leer" — nunca "está apagado".
    """
    fake_scripts["mkclient"]("Distri", "distri-8")
    fake_scripts["plans"].ADDONS = {"mayorista"}
    monkeypatch.setattr(
        services.subprocess, "run",
        lambda cmd, **kw: _R(
            returncode=1,
            stderr="ImportError: cannot import name 'get_modulos' from 'app.database'",
        ),
    )
    assert services.addons_de_instancia("distri-8") == {"mayorista": None}


def test_addons_de_instancia_salida_no_json_da_desconocido(fake_scripts, monkeypatch):
    """Salida con código 0 pero que no parsea: tampoco es "apagado"."""
    fake_scripts["mkclient"]("Distri", "distri-9")
    fake_scripts["plans"].ADDONS = {"mayorista"}
    monkeypatch.setattr(services.subprocess, "run",
                        lambda cmd, **kw: _R(stdout="Traceback (most recent call last):"))
    assert services.addons_de_instancia("distri-9") == {"mayorista": None}


def test_addons_de_instancia_apagado_de_verdad_es_false(fake_scripts, monkeypatch):
    """El control que hace falta para que los tres de arriba prueben algo.

    Sin este, "devolver `None` siempre" también pasaría, y la pantalla nunca
    volvería a mostrar un add-on apagado. `False` y `None` tienen que salir de
    caminos distintos: uno lo dice la base, el otro es la ausencia de respuesta.
    """
    fake_scripts["mkclient"]("Distri", "distri-10")
    fake_scripts["plans"].ADDONS = {"mayorista"}
    monkeypatch.setattr(services.subprocess, "run",
                        lambda cmd, **kw: _R(stdout='{"mayorista": false, "caja": true}'))
    estado = services.addons_de_instancia("distri-10")
    assert estado == {"mayorista": False}
    assert estado["mayorista"] is False          # y no `None`
    assert estado["mayorista"] is not None


# ── adicionales fuera de la escalera (ADR-048) ───────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def con_adicionales(fake_scripts):
    """El producto de juguete con un sí/no (`mayorista`), offline por sucursal (sólo en `pro`) y sucursales con precio."""
    p = fake_scripts["plans"]
    p.PLAN_MODULOS = {"basico": {"clientes"}, "pro": {"clientes", "reportes"}}
    p.ADDONS = {"mayorista"}
    p.ADICIONALES = {
        "offline": {"label": "Modo offline", "modulos": {"offline"}, "unidad": "sucursal", "limite": "sucursales_offline", "planes": {"pro"}},
        "sucursal": {"label": "Sucursales", "precio": 19950, "unidad": "sucursal", "incluidas": {"basico": 1, "pro": 2}, "limite": "sucursales"},
    }
    return fake_scripts


def test_planes_info_trae_los_adicionales_de_cada_plan_con_sus_incluidas(con_adicionales):
    info = {p["key"]: p for p in services.planes_info()}
    basico = {a["clave"]: a for a in info["basico"]["adicionales"]}
    pro = {a["clave"]: a for a in info["pro"]["adicionales"]}
    assert sorted(basico) == ["mayorista", "sucursal"]          # offline es sólo de `pro`
    assert sorted(pro) == ["mayorista", "offline", "sucursal"]
    assert (basico["sucursal"]["incluidas"], pro["sucursal"]["incluidas"]) == (1, 2)
    assert basico["sucursal"]["precio"] == 19950 and pro["offline"]["precio"] is None
    assert pro["offline"]["modulos"] == ["offline"] and "planes" not in pro["offline"]


def test_planes_info_sin_adicionales_da_lista_vacia(fake_scripts):
    assert all(p["adicionales"] == [] for p in services.planes_info())


def test_adicionales_info_es_el_catalogo_para_la_pantalla(con_adicionales):
    assert [a["clave"] for a in services.adicionales_info()] == ["mayorista", "offline", "sucursal"]
    assert next(a for a in services.adicionales_info() if a["clave"] == "offline")["planes"] == ["pro"]


def test_set_plan_conserva_la_copia_de_adicionales_del_cliente_json(con_adicionales):
    con_adicionales["mkclient"]("C", "c-1", plan="basico")
    services.registrar_copia_de_adicionales("c-1", {"mayorista": True, "sucursal": 3})
    services.set_plan("c-1", "pro")
    meta = json.loads((con_adicionales["clientes_dir"] / "c-1" / "cliente.json").read_text(encoding="utf-8"))
    assert meta["plan"] == "pro" and meta["adicionales"] == {"mayorista": True, "sucursal": 3}


def test_set_plan_con_un_adicional_incompatible_es_409_con_la_lista_y_no_aplica_nada(con_adicionales):
    con_adicionales["mkclient"]("C", "c-2", plan="pro")
    with pytest.raises(services.AdicionalIncompatibleError) as e:
        services.set_plan("c-2", "basico", contratados={"offline": 2, "mayorista": True})
    assert e.value.incompatibles == ["offline"] and e.value.plan == "basico"
    assert isinstance(e.value, services.ServiceError)
    assert "offline" in str(e.value)
    assert con_adicionales["plans"].aplicar_plan_calls == []
    assert services.get_cliente("c-2")["plan"] == "pro"


def test_set_plan_sin_contratados_usa_la_copia_del_cliente_json(con_adicionales):
    con_adicionales["mkclient"]("C", "c-3", plan="pro")
    services.registrar_copia_de_adicionales("c-3", {"offline": 1})
    with pytest.raises(services.AdicionalIncompatibleError):
        services.set_plan("c-3", "basico")


def test_set_plan_con_el_incompatible_dado_de_baja_pasa(con_adicionales):
    con_adicionales["mkclient"]("C", "c-4", plan="pro")
    services.set_plan("c-4", "basico", contratados={"offline": None, "mayorista": True})
    assert services.get_cliente("c-4")["plan"] == "basico"


def test_la_copia_del_cliente_json_ilegible_no_rompe_el_cambio_de_plan(con_adicionales):
    con_adicionales["mkclient"]("C", "c-5", plan="basico")
    meta_path = con_adicionales["clientes_dir"] / "c-5" / "cliente.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["adicionales"] = "roto"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    services.set_plan("c-5", "pro")
    assert services.get_cliente("c-5")["plan"] == "pro"


def test_set_adicional_prende_el_modulo_por_docker_y_anota_la_copia(con_adicionales, monkeypatch):
    con_adicionales["mkclient"]("C", "c-6")
    llamadas = []
    monkeypatch.setattr(services.subprocess, "run", lambda cmd, **kw: llamadas.append(cmd[-1]) or _R())
    services.set_adicional("c-6", "mayorista", True)
    assert "set_addon('mayorista', True)" in llamadas[0]
    meta = json.loads((con_adicionales["clientes_dir"] / "c-6" / "cliente.json").read_text(encoding="utf-8"))
    assert meta["adicionales"] == {"mayorista": True}
    services.set_adicional("c-6", "mayorista", False)
    assert "set_addon('mayorista', False)" in llamadas[1]
    assert "adicionales" not in json.loads((con_adicionales["clientes_dir"] / "c-6" / "cliente.json").read_text(encoding="utf-8"))


def test_set_adicional_por_unidades_no_se_carga_por_docker(con_adicionales, monkeypatch):
    con_adicionales["mkclient"]("C", "c-7")
    monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: pytest.fail("no debería tocar Docker"))
    with pytest.raises(services.ServiceError, match="/api/adicionales"):
        services.set_adicional("c-7", "offline", True)


def test_set_adicional_desconocido_y_error_de_docker(con_adicionales, monkeypatch):
    con_adicionales["mkclient"]("C", "c-8")
    with pytest.raises(services.ServiceError, match="desconocido"):
        services.set_adicional("c-8", "cohetes", True)
    monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: _R(returncode=1, stderr="boom"))
    with pytest.raises(services.ServiceError, match="boom"):
        services.set_adicional("c-8", "mayorista", True)


def test_adicionales_de_instancia_lee_los_modulos_y_distingue_el_no_se(con_adicionales, monkeypatch):
    con_adicionales["mkclient"]("C", "c-9")
    monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: _R(stdout=json.dumps({"mayorista": True, "offline": False})))
    assert services.adicionales_de_instancia("c-9") == {"mayorista": True, "offline": False}
    monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: _R(returncode=1, stderr="x"))
    assert services.adicionales_de_instancia("c-9") == {"mayorista": None, "offline": None}
    monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: _R(stdout="<html>"))
    assert services.adicionales_de_instancia("c-9") == {"mayorista": None, "offline": None}


def test_adicionales_de_instancia_sin_adicionales_da_vacio(fake_scripts):
    assert services.adicionales_de_instancia("lo-que-sea") == {}


def test_la_copia_del_cliente_json_se_alinea_con_la_instancia_que_gana(con_adicionales):
    con_adicionales["mkclient"]("C", "c-10")
    path = con_adicionales["clientes_dir"] / "c-10" / "cliente.json"
    services.registrar_copia_de_adicionales("c-10", {"mayorista": True, "offline": 5})
    services.sincronizar_copia_de_adicionales("c-10", {"mayorista": None, "offline": 2, "sucursal": 3})
    assert json.loads(path.read_text(encoding="utf-8"))["adicionales"] == {"offline": 2, "sucursal": 3}
    antes = path.stat().st_mtime_ns
    services.sincronizar_copia_de_adicionales("c-10", {"offline": 2, "sucursal": 3})   # igual: no reescribe
    assert path.stat().st_mtime_ns == antes
    services.sincronizar_copia_de_adicionales("c-10", {})
    assert "adicionales" not in json.loads(path.read_text(encoding="utf-8"))


def test_crear_cliente_pasa_los_adicionales_al_motor_solo_si_hay(con_adicionales):
    services.crear_cliente(nombre="Con Extras", slug="con-extras", adicionales={"mayorista": True})
    import nuevo_cliente
    assert nuevo_cliente.ultima_alta["extra"] == {"adicionales": {"mayorista": True}}
    services.crear_cliente(nombre="Sin Extras", slug="sin-extras")
    assert nuevo_cliente.ultima_alta["extra"] == {}


def test_abono_por_lineas_es_el_anexo_i_del_plan_con_sus_adicionales(con_adicionales):
    """El plan más una línea por adicional contratado; el que no tiene precio sale «a cotizar» y no suma."""
    r = services.abono_por_lineas("pro", {"sucursal": 4, "offline": 2, "mayorista": True})
    assert r["plan"] == "pro" and r["base"] == 2000
    assert [ln["clave"] for ln in r["lineas"]] == ["mayorista", "offline", "sucursal"]
    assert r["extra"] == 2 * 19950 and r["total"] == 2000 + 2 * 19950  # 4 contratadas, 2 incluidas en `pro`
    assert r["a_cotizar"] == ["mayorista", "offline"] and r["incompatibles"] == []


def test_abono_por_lineas_resuelve_un_plan_retirado_y_marca_los_incompatibles(con_adicionales):
    con_adicionales["plans"].PLANES_RETIRADOS = {"viejo": "basico"}
    r = services.abono_por_lineas("viejo", {"offline": 1})
    assert r["plan"] == "basico" and r["incompatibles"] == ["offline"]


def test_abono_por_lineas_con_una_clave_que_no_se_vende_es_value_error(con_adicionales):
    with pytest.raises(ValueError, match="desconocido"):
        services.abono_por_lineas("pro", {"cohetes": True})


# ── token de servicio de cada instancia (ADR-071) ──────────────────────────────

TOKEN_FICTICIO = "f" * 64


def _con_compose(cdir, env_lineas, dotenv=None):
    (cdir / "docker-compose.yml").write_text(
        "services:\n  app:\n    image: testprod:v1\n    environment:\n" + env_lineas
        + "  app-postgres:\n    image: postgres:16\n    environment:\n      - POSTGRES_DB=x\n",
        encoding="utf-8")
    if dotenv is not None:
        (cdir / ".env").write_text(dotenv, encoding="utf-8")


def test_token_de_servicio_literal_en_el_compose(fake_scripts):
    c = fake_scripts["mkclient"]("Uno", "uno")
    _con_compose(c["dir"], f"      - LIBRA_SERVICE_TOKEN={TOKEN_FICTICIO}\n")
    assert services.token_de_servicio("uno") == TOKEN_FICTICIO


def test_token_de_servicio_por_referencia_al_env(fake_scripts):
    c = fake_scripts["mkclient"]("Uno", "uno")
    _con_compose(c["dir"], "      - LIBRA_SERVICE_TOKEN=${LIBRA_SERVICE_TOKEN}\n",
                 dotenv=f'LIBRA_SERVICE_TOKEN="{TOKEN_FICTICIO}"\n')
    assert services.token_de_servicio("uno") == TOKEN_FICTICIO


def test_token_de_servicio_ausente_o_instancia_inexistente(fake_scripts):
    c = fake_scripts["mkclient"]("Uno", "uno")
    _con_compose(c["dir"], "      - ADMIN_USER=admin\n")
    fake_scripts["mkclient"]("Dos", "dos")  # sin compose
    assert services.token_de_servicio("uno") is None
    assert services.token_de_servicio("dos") is None
    assert services.token_de_servicio("no-existe") is None
    assert services.token_de_servicio("../uno") is None
    assert services.token_de_servicio("") is None


def test_token_de_servicio_no_se_loguea(fake_scripts, caplog):
    c = fake_scripts["mkclient"]("Uno", "uno")
    _con_compose(c["dir"], f"      - LIBRA_SERVICE_TOKEN={TOKEN_FICTICIO}\n")
    with caplog.at_level("DEBUG"):
        services.token_de_servicio("uno")
        services.token_de_servicio("dos")
    assert TOKEN_FICTICIO not in caplog.text
