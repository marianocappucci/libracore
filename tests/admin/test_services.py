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
    fake_pa.CLIENTES_DIR = clientes_dir
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
                      empresa_nombre="", sin_identidad=False, setup_npm=True):
        slug = slug or nombre.lower().replace(" ", "-")
        if fake_pa.find_client(slug):
            raise ClienteError(f"Ya existe un cliente con slug '{slug}'.")
        _mkclient(nombre, slug, domain, port, admin_user, plan)
        fake_nc.ultima_alta = {
            "empresa_cuit": empresa_cuit, "empresa_nombre": empresa_nombre,
            "sin_identidad": sin_identidad,
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

    return {"clientes_dir": clientes_dir, "mkclient": _mkclient,
            "pa": fake_pa, "plans": fake_plans}


def test_listar_clientes_vacio(fake_scripts):
    assert services.listar_clientes() == []


def test_crear_cliente_y_listar(fake_scripts):
    info = services.crear_cliente(nombre="Cliente Uno", slug="cliente-uno")
    assert info["slug"] == "cliente-uno"
    clientes = services.listar_clientes()
    assert len(clientes) == 1
    assert clientes[0]["slug"] == "cliente-uno"
    assert clientes[0]["estado"] == "running"


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


def test_addons_de_instancia_contenedor_caido_los_da_apagados(fake_scripts, monkeypatch):
    fake_scripts["mkclient"]("Distri", "distri-7")
    fake_scripts["plans"].ADDONS = {"mayorista"}
    monkeypatch.setattr(services.subprocess, "run",
                        lambda cmd, **kw: _R(returncode=1))
    assert services.addons_de_instancia("distri-7") == {"mayorista": False}
