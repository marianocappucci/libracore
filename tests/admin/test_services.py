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

    def _mkclient(nombre, slug, domain="", port=8080, admin_user="admin", plan="basico"):
        cdir = clientes_dir / slug
        (cdir / "data").mkdir(parents=True)
        (cdir / "data" / "test.db").write_bytes(b"")
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
    fake_pa._set_servicio_estado = lambda slug, estado: (
        fake_pa.estado_calls.append((slug, estado)) or True
    )

    fake_nc = types.ModuleType("nuevo_cliente")

    class ClienteError(Exception):
        pass

    fake_nc.ClienteError = ClienteError

    def crear_cliente(nombre, slug="", domain="", port=0, admin_user="admin",
                      admin_password="", plan="basico", setup_npm=True):
        slug = slug or nombre.lower().replace(" ", "-")
        if fake_pa.find_client(slug):
            raise ClienteError(f"Ya existe un cliente con slug '{slug}'.")
        _mkclient(nombre, slug, domain, port, admin_user, plan)
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
    assert fake_scripts["pa"].estado_calls[-1] == ("cliente-seis", "pausado")


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
