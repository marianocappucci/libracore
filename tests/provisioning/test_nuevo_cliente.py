"""
Tests de libracore.provisioning.nuevo_cliente. `plans.py` (planes reales de
cada producto) y `npm_api.py` (vive en scripts/ de cada repo, no en
LibraCore) se inyectan como módulos falsos en sys.modules, mismo patrón que
tests/admin/test_services.py. Docker/subprocess se mockean: crear_cliente()
no debe tocar Docker real.
"""
import json
import re
import subprocess
import sys
import types

import pytest
import yaml

from libracore import provisioning
from libracore.provisioning import nuevo_cliente as nc


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


@pytest.fixture
def fake_plans(monkeypatch):
    mod = types.ModuleType("plans")
    mod.PLANES = ["basico", "pro"]
    mod.aplicar_plan_calls = []
    mod.aplicar_plan_en_db = lambda db_path, plan: mod.aplicar_plan_calls.append((db_path, plan))
    monkeypatch.setitem(sys.modules, "plans", mod)
    return mod


@pytest.fixture
def fake_docker(monkeypatch):
    """Intercepta subprocess.run (docker build/network/compose) y
    _esperar_db_lista (poll de hasta 25s) para que crear_cliente() no
    dependa de Docker real ni sea lento en la suite."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(nc.subprocess, "run", fake_run)
    monkeypatch.setattr(nc, "_esperar_db_lista", lambda *a, **k: False)
    return calls


@pytest.fixture
def cfg(tmp_path, fake_plans, fake_docker):
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo_root, base_port=9000,
    )
    return provisioning.get_config()


def test_slugify_normaliza_acentos_y_espacios():
    assert nc.slugify("Café Cañón S.A.") == "cafe-canon-s-a"


def test_slugify_vacio_devuelve_cliente():
    assert nc.slugify("   ") == "cliente"


def test_crear_cliente_nombre_vacio_lanza_cliente_error(cfg):
    with pytest.raises(nc.ClienteError):
        nc.crear_cliente(nombre="", setup_npm=False)


def test_crear_cliente_plan_invalido_lanza_cliente_error(cfg):
    with pytest.raises(nc.ClienteError):
        nc.crear_cliente(nombre="Cliente Uno", plan="no-existe", setup_npm=False)


def test_crear_cliente_slug_duplicado_lanza_cliente_error(cfg):
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    with pytest.raises(nc.ClienteError):
        nc.crear_cliente(nombre="Cliente Uno Bis", slug="cliente-uno", setup_npm=False)


def test_crear_cliente_genera_compose_con_datos_del_producto(cfg):
    info = nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno",
                            admin_password="secreto123", setup_npm=False)
    assert info["container"] == "testprod-cliente-uno"
    assert info["port"] == 9000  # base_port del producto, sin puertos usados

    compose_text = (cfg.clientes_dir / "cliente-uno" / "docker-compose.yml").read_text()
    assert "container_name: testprod-cliente-uno" in compose_text
    assert "ADMIN_PASSWORD=secreto123" in compose_text

    meta = json.loads((cfg.clientes_dir / "cliente-uno" / "cliente.json").read_text())
    assert meta["container"] == "testprod-cliente-uno"
    assert meta["plan"] == "basico"


def test_el_proyecto_de_compose_lleva_el_prefijo_del_producto(cfg):
    """Sin `name:`, Compose usa el nombre del DIRECTORIO como proyecto — o sea
    el slug—, y dos productos con una instancia del mismo slug caen en el mismo
    proyecto. Ahi cada uno ve a los contenedores del otro como huerfanos, y un
    `docker compose --remove-orphans` se los lleva puestos.

    Paso de verdad: el 2026-08-02 quedaron cuatro instancias `prueba` de
    productos distintos compartiendo `project=prueba`."""
    nc.crear_cliente(nombre="Prueba", slug="prueba", setup_npm=False)
    compose_text = (cfg.clientes_dir / "prueba" / "docker-compose.yml").read_text()
    assert compose_text.startswith("name: testprod-prueba\n"), compose_text[:80]
    # El slug solo NO alcanza como nombre de proyecto: es justamente lo que
    # colisiona entre productos.
    assert "\nname: prueba\n" not in compose_text


def _claves_de_servicio(cfg, slug: str) -> list[str]:
    """Las claves del bloque `services:` de un compose de instancia.

    Sin PyYAML a proposito: no esta declarado en las dependencias de dev, asi
    que en CI puede no estar. El corte va del `services:` hasta la proxima
    clave de primer nivel (`networks:`, `volumes:`), que es lo que evita
    confundir `  stack-net:` con un servicio.
    """
    texto = (cfg.clientes_dir / slug / "docker-compose.yml").read_text()
    bloque = re.split(r"^services:$", texto, flags=re.MULTILINE)[1]
    bloque = re.split(r"^[a-z]", bloque, flags=re.MULTILINE)[0]
    return re.findall(r"^  ([a-z0-9][a-z0-9._-]*):$", bloque, flags=re.MULTILINE)


def test_dos_instancias_del_mismo_producto_no_comparten_clave_de_servicio(cfg):
    """Compose convierte la clave del servicio en ALIAS DE RED, y `stack-net`
    la comparten los seis productos. Con el prefijo del producto a secas, la
    segunda instancia reclama el mismo alias que la primera y el DNS interno
    las alterna por round-robin — sintoma intermitente, asi que un chequeo que
    da bien no descarta nada.

    Paso de verdad: el 2026-08-06 `sistema.contalibra.com.ar` —produccion de un
    cliente— sirvio a ratos el auto-login de la demo. Aquella vez se
    renombraron los composes afectados a mano y la plantilla quedo sin tocar,
    asi que cada instancia nueva reintroducia el defecto."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    nc.crear_cliente(nombre="Cliente Dos", slug="cliente-dos", setup_npm=False)

    uno = _claves_de_servicio(cfg, "cliente-uno")
    dos = _claves_de_servicio(cfg, "cliente-dos")

    # La asercion que vale es la del prefijo AUSENTE: `testprod` a secas es el
    # alias que colisiona, y es lo unico que cambia si la plantilla se rompe.
    # Que las dos claves sean distintas entre si tambien lo detecta, pero
    # pasaria igual con cualquier otro esquema de nombres.
    assert "testprod" not in uno, uno
    assert "testprod" not in dos, dos
    assert uno == ["testprod-cliente-uno"], uno
    assert dos == ["testprod-cliente-dos"], dos
    assert set(uno).isdisjoint(dos)


def test_crear_cliente_pinea_version_y_nunca_latest(cfg):
    """El compose de un cliente nuevo nace con una versión concreta: si
    naciera en `:latest`, el próximo build de otro cliente lo movería."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)

    compose_text = (cfg.clientes_dir / "cliente-uno" / "docker-compose.yml").read_text()
    assert "testprod:latest" not in compose_text
    assert re.search(r"^\s*image: testprod:v20\d\d\.\d\d\.\d\d-\d{4}$",
                     compose_text, re.MULTILINE)

    meta = json.loads((cfg.clientes_dir / "cliente-uno" / "cliente.json").read_text())
    assert meta["version_desplegada"].startswith("v20")


def test_crear_cliente_reusa_la_version_ya_construida(cfg, monkeypatch):
    """Un cliente nuevo se suma a la versión que ya corre la familia en vez
    de estrenar un artefacto propio por haberse creado más tarde."""
    monkeypatch.setattr(nc, "version_para_cliente_nuevo", lambda rebuild=False: "v2026.01.02-0304")

    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)

    compose_text = (cfg.clientes_dir / "cliente-uno" / "docker-compose.yml").read_text()
    assert "image: testprod:v2026.01.02-0304" in compose_text


def test_version_para_cliente_nuevo_toma_la_mas_reciente_disponible(cfg, monkeypatch):
    from libracore.provisioning import panel_admin as pa

    monkeypatch.setattr(pa, "versiones_disponibles",
                        lambda: ["v2026.05.05-1200", "v2026.01.01-0900"])
    monkeypatch.setattr(nc, "image_exists", lambda ref=None: True)
    llamadas = []
    monkeypatch.setattr(nc, "build_image", lambda *a, **k: llamadas.append(a) or "nueva")

    assert nc.version_para_cliente_nuevo() == "v2026.05.05-1200"
    assert llamadas == []  # no rebuildeó


def test_version_para_cliente_nuevo_buildea_si_no_hay_ninguna(cfg, monkeypatch):
    from libracore.provisioning import panel_admin as pa

    monkeypatch.setattr(pa, "versiones_disponibles", lambda: [])
    monkeypatch.setattr(nc, "build_image", lambda *a, **k: "v2026.09.09-0101")

    assert nc.version_para_cliente_nuevo() == "v2026.09.09-0101"


def test_version_para_cliente_nuevo_con_rebuild_siempre_construye(cfg, monkeypatch):
    from libracore.provisioning import panel_admin as pa

    monkeypatch.setattr(pa, "versiones_disponibles", lambda: ["v2026.05.05-1200"])
    monkeypatch.setattr(nc, "build_image", lambda *a, **k: "v2026.09.09-0101")

    assert nc.version_para_cliente_nuevo(rebuild=True) == "v2026.09.09-0101"


def test_crear_cliente_aplica_plan_cuando_db_lista(cfg, fake_plans, monkeypatch):
    monkeypatch.setattr(nc, "_esperar_db_lista", lambda *a, **k: True)
    nc.crear_cliente(nombre="Cliente Dos", slug="cliente-dos", setup_npm=False)
    assert fake_plans.aplicar_plan_calls[-1][1] == "basico"
    assert fake_plans.aplicar_plan_calls[-1][0].endswith("testprod.db")


def test_crear_cliente_sin_dominio_no_configura_proxy(cfg):
    info = nc.crear_cliente(nombre="Cliente Tres", slug="cliente-tres", setup_npm=True)
    assert info["proxy_ok"] is None


def test_crear_cliente_con_dominio_y_npm_crea_proxy(cfg, monkeypatch):
    created = {}

    class FakeNPMError(Exception):
        pass

    class FakeNPM:
        def get_proxy_host_by_domain(self, domain):
            return None

        def create_proxy_host(self, **kwargs):
            created.update(kwargs)
            return {"id": 1}

    npm_mod = types.ModuleType("npm_api")
    npm_mod.NPMError = FakeNPMError
    npm_mod.client_from_config = lambda: FakeNPM()
    npm_mod.forward_host_from_config = lambda: "10.0.0.1"
    npm_mod.le_email_from_config = lambda: "admin@test.com"
    monkeypatch.setitem(sys.modules, "npm_api", npm_mod)

    info = nc.crear_cliente(nombre="Cliente Cuatro", slug="cliente-cuatro",
                            domain="cliente-cuatro.test", setup_npm=True)
    assert info["proxy_ok"] is True
    assert created["domain"] == "cliente-cuatro.test"
    assert created["forward_host"] == "10.0.0.1"


def _build_cmd(calls):
    """El `docker build` entre las llamadas capturadas. No es la primera:
    el build ahora va precedido por el `git rev-parse` que resuelve el
    label del commit."""
    for cmd in calls:
        if list(cmd[:2]) == ["docker", "build"]:
            return cmd
    raise AssertionError(f"No hubo `docker build` en {calls}")


def test_build_image_sin_libracommerce_no_pasa_ese_ssh_id(cfg, fake_docker):
    (cfg.repo_root / "requirements.txt").write_text("fastapi\nlibracore @ git+ssh://...\n", encoding="utf-8")
    nc.build_image()

    build_cmd = _build_cmd(fake_docker)
    assert "default=" + provisioning.LIBRACORE_SSH_KEY in build_cmd
    assert "libracore=" + provisioning.LIBRACORE_SSH_KEY in build_cmd
    assert not any(a.startswith("libracommerce=") for a in build_cmd)


def test_build_image_con_libracommerce_agrega_su_ssh_id(cfg, fake_docker):
    (cfg.repo_root / "requirements.txt").write_text(
        "fastapi\nlibracore @ git+ssh://...\nlibracommerce @ git+ssh://...\n", encoding="utf-8"
    )
    nc.build_image()

    build_cmd = _build_cmd(fake_docker)
    assert "libracommerce=" + provisioning.LIBRACOMMERCE_SSH_KEY in build_cmd


def test_build_image_etiqueta_version_y_latest(cfg, fake_docker):
    version = nc.build_image("v2026.03.04-0506")

    build_cmd = _build_cmd(fake_docker)
    assert version == "v2026.03.04-0506"
    assert build_cmd[build_cmd.index("-t") + 1] == "testprod:v2026.03.04-0506"
    assert "testprod:latest" in build_cmd  # el puntero móvil se sigue moviendo
    assert "org.libra.version=v2026.03.04-0506" in build_cmd


def test_build_image_falla_corta_el_alta(cfg, monkeypatch):
    # `nc` importó el helper por nombre, así que el doble va sobre `nc`.
    monkeypatch.setattr(nc, "build_image_tagged", lambda *a, **k: False)
    with pytest.raises(SystemExit):
        nc.build_image()


# ── puertos del host ───────────────────────────────────────────────────────────

def _fake_docker_host(monkeypatch, ps_ports: str = "", port_bindings: str = "",
                      ids: str = "abc123\n"):
    """Doble de los tres comandos que consulta `used_ports()`."""
    def fake_run(args, **kwargs):
        if args[:3] == ["docker", "ps", "-aq"]:
            out = ids
        elif args[:2] == ["docker", "inspect"]:
            out = port_bindings
        elif args[:3] == ["docker", "ps", "-a"]:
            out = ps_ports
        else:
            out = ""
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(nc.subprocess, "run", fake_run)


def test_used_ports_cuenta_puertos_publicados_contra_cualquier_puerto_interno(monkeypatch):
    """Regresión del 2026-08-02: el alta eligió 8079, que `restolibra-web`
    publicaba contra el puerto **80** del contenedor y no contra el 8000, así
    que el filtro viejo (`:(\\d+)->8000`) no lo veía. `docker compose up` murió
    con `port is already allocated`."""
    _fake_docker_host(monkeypatch, ps_ports=(
        "0.0.0.0:8078->8000/tcp, [::]:8078->8000/tcp\n"
        "0.0.0.0:8079->80/tcp, [::]:8079->80/tcp\n"
    ))
    assert nc.used_ports() == {8078, 8079}


def test_used_ports_incluye_contenedores_de_otros_productos_parados(monkeypatch):
    """Un contenedor parado no publica nada en la columna PORTS, pero se queda
    con el puerto apenas alguien lo arranca — sale de `HostConfig.PortBindings`."""
    _fake_docker_host(monkeypatch, ps_ports="", port_bindings="8084 8085 \n")
    assert nc.used_ports() == {8084, 8085}


def test_used_ports_expande_rangos(monkeypatch):
    _fake_docker_host(monkeypatch, ps_ports="0.0.0.0:80-81->80-81/tcp\n")
    assert nc.used_ports() == {80, 81}


def test_used_ports_sin_docker_no_explota(monkeypatch):
    def fake_run(args, **kwargs):
        raise OSError("docker: command not found")

    monkeypatch.setattr(nc.subprocess, "run", fake_run)
    assert nc.used_ports() == set()


def test_next_port_saltea_el_puerto_de_otro_producto(cfg, monkeypatch):
    """El caso real: base_port 8071, los propios ocupan hasta 8078 y 8079 es de
    otro producto. El puerto elegido tiene que ser 8080, no 8079."""
    monkeypatch.setattr(nc, "used_ports", lambda: set(range(8071, 8079)) | {8079})
    assert nc.next_port(nc.used_ports()) != 8079


# ── rollback de un alta fallida ────────────────────────────────────────────────

def _falla_el_up(monkeypatch, stderr: str):
    """Hace fallar sólo el `docker compose up`; el resto de docker sigue OK."""
    ejecutados = []

    def fake_run(args, **kwargs):
        ejecutados.append(list(args))
        if args[:3] == ["docker", "compose", "up"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=stderr)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(nc.subprocess, "run", fake_run)
    return ejecutados


def test_alta_fallida_borra_el_directorio(cfg, monkeypatch):
    """Sin esto queda un cliente en el inventario del backoffice sin contenedor
    detrás, y el slug tomado para el reintento."""
    _falla_el_up(monkeypatch, "Bind for 0.0.0.0:8079 failed: port is already allocated")

    with pytest.raises(nc.ClienteError):
        nc.crear_cliente(nombre="Cliente Seis", slug="cliente-seis", setup_npm=False)

    assert not (cfg.clientes_dir / "cliente-seis").exists()


def test_alta_fallida_baja_el_contenedor_antes_de_borrar(cfg, monkeypatch):
    """Un `up` que falla al publicar el puerto deja el contenedor *creado*.
    Borrar sólo el directorio lo filtraría, con el nombre del slug puesto."""
    ejecutados = _falla_el_up(monkeypatch, "port is already allocated")

    with pytest.raises(nc.ClienteError):
        nc.crear_cliente(nombre="Cliente Siete", slug="cliente-siete", setup_npm=False)

    assert ["docker", "compose", "down", "-v"] in ejecutados
    idx_up = ejecutados.index(["docker", "compose", "up", "-d"])
    assert ejecutados.index(["docker", "compose", "down", "-v"]) > idx_up


def test_alta_fallida_reporta_el_motivo_real_de_docker(cfg, monkeypatch):
    """El 422 del backoffice decía sólo "no se pudo iniciar el contenedor"; el
    motivo quedaba en un log del host."""
    _falla_el_up(monkeypatch, "Error response from daemon: ...\n"
                              "Bind for 0.0.0.0:8079 failed: port is already allocated\n")

    with pytest.raises(nc.ClienteError) as exc:
        nc.crear_cliente(nombre="Cliente Ocho", slug="cliente-ocho", setup_npm=False)

    assert "port is already allocated" in str(exc.value)


def test_alta_fallida_libera_el_slug_para_reintentar(cfg, monkeypatch):
    """Tras el rollback, reintentar con el mismo slug tiene que funcionar en vez
    de chocar contra "ya existe un cliente con slug"."""
    _falla_el_up(monkeypatch, "port is already allocated")
    with pytest.raises(nc.ClienteError):
        nc.crear_cliente(nombre="Cliente Nueve", slug="cliente-nueve", setup_npm=False)

    # segundo intento, esta vez con docker sano
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(nc.subprocess, "run", fake_run)
    info = nc.crear_cliente(nombre="Cliente Nueve", slug="cliente-nueve", setup_npm=False)
    assert info["slug"] == "cliente-nueve"


def test_alta_fallida_por_el_plan_tambien_hace_rollback(cfg, fake_plans, monkeypatch):
    """El rollback no es sólo del `up`: cualquier paso posterior que reviente
    deja igual un cliente a medio crear."""
    monkeypatch.setattr(nc, "_esperar_db_lista", lambda *a, **k: True)

    def explota(db_path, plan):
        raise RuntimeError("la DB está corrupta")

    fake_plans.aplicar_plan_en_db = explota

    with pytest.raises(RuntimeError):
        nc.crear_cliente(nombre="Cliente Diez", slug="cliente-diez", setup_npm=False)

    assert not (cfg.clientes_dir / "cliente-diez").exists()


def test_alta_exitosa_no_borra_nada(cfg):
    nc.crear_cliente(nombre="Cliente Once", slug="cliente-once", setup_npm=False)
    assert (cfg.clientes_dir / "cliente-once" / "cliente.json").exists()


def test_slug_duplicado_no_borra_el_cliente_existente(cfg):
    """El rollback arranca *después* de la validación de slug — si arrancara
    antes, un alta duplicada borraría al cliente que ya estaba."""
    nc.crear_cliente(nombre="Cliente Doce", slug="cliente-doce", setup_npm=False)
    with pytest.raises(nc.ClienteError):
        nc.crear_cliente(nombre="Otro", slug="cliente-doce", setup_npm=False)
    assert (cfg.clientes_dir / "cliente-doce" / "cliente.json").exists()


def test_crear_cliente_proxy_existente_no_falla(cfg, monkeypatch):
    class FakeNPMError(Exception):
        pass

    class FakeNPM:
        def get_proxy_host_by_domain(self, domain):
            return {"id": 99}

    npm_mod = types.ModuleType("npm_api")
    npm_mod.NPMError = FakeNPMError
    npm_mod.client_from_config = lambda: FakeNPM()
    npm_mod.forward_host_from_config = lambda: "10.0.0.1"
    npm_mod.le_email_from_config = lambda: "admin@test.com"
    monkeypatch.setitem(sys.modules, "npm_api", npm_mod)

    info = nc.crear_cliente(nombre="Cliente Cinco", slug="cliente-cinco",
                            domain="cliente-cinco.test", setup_npm=True)
    assert info["proxy_ok"] is True




# ————————————————————————————————————————————————————————————————
# El sidecar de las instancias nuevas
# ————————————————————————————————————————————————————————————————

@pytest.fixture
def cfg_pg(tmp_path, fake_plans, fake_docker):
    """Producto con UNA base, como Contalibra/Restolibra/LibraDesk/VentaLibra."""
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo_root, base_port=9000,
        postgres=True,
    )
    return provisioning.get_config()


@pytest.fixture
def cfg_pg_dos_bases(tmp_path, fake_plans, fake_docker):
    """Producto con DOS bases, como Gestiolibra/MedLibra."""
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo_root, base_port=9000,
        postgres=True, base_core_separada=True,
    )
    return provisioning.get_config()


def _compose(cfg, slug="cliente-uno"):
    return (cfg.clientes_dir / slug / "docker-compose.yml").read_text()


def test_sin_postgres_la_instancia_sigue_naciendo_en_sqlite(cfg):
    """El producto que no declara `db_urls` no cambia en nada — es lo que
    permite migrarlos de a uno sin romper a los demas."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    texto = _compose(cfg)
    assert "postgres" not in texto
    assert "DATABASE_URL" not in texto


def test_la_instancia_nueva_nace_con_su_sidecar(cfg_pg):
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    texto = _compose(cfg_pg)
    assert "testprod-cliente-uno-postgres:" in texto
    assert "image: postgres:16-alpine" in texto
    assert "TESTPROD_DATABASE_URL=postgresql://testprod:" in texto
    assert "TESTPROD_LIBRACORE_DATABASE_URL=postgresql://testprod:" in texto
    assert "@testprod-cliente-uno-postgres:5432/testprod" in texto


def test_el_sidecar_no_publica_puerto(cfg_pg):
    """Publicar 5432 en un VPS es publicarlo a Internet. El unico `ports:` del
    compose tiene que ser el de la app."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    texto = _compose(cfg_pg)
    assert texto.count("ports:") == 1
    assert "5432:" not in texto


def test_el_sidecar_queda_fuera_de_la_red_compartida(cfg_pg, monkeypatch):
    """El corazon del cambio del 2026-08-11: hasta esa fecha las 15 instancias
    compartian `stack_stack-net` y el PostgreSQL de un cliente era alcanzable
    desde el contenedor de cualquier otro producto.

    Se comprueba sobre el YAML resuelto, no por substring: `- datos` aparece en
    los dos servicios y un `assert ... in texto` pasaria igual con el defecto.
    """
    monkeypatch.setattr(nc, "network_exists", lambda *a, **k: True)
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)

    doc = yaml.safe_load(_compose(cfg_pg))
    app = doc["services"]["testprod-cliente-uno"]
    base = doc["services"]["testprod-cliente-uno-postgres"]

    assert base["networks"] == ["datos"], "el sidecar NO puede estar en la red compartida"
    assert set(app["networks"]) == {"stack-net", "datos"}
    assert doc["networks"]["datos"]["name"] == "testprod-cliente-uno-datos"
    assert doc["networks"]["stack-net"]["external"] is True


def test_la_app_espera_a_que_la_base_este_healthy(cfg_pg):
    """Sin esto la app arranca contra un PostgreSQL que todavia no acepta
    conexiones, se cae y el contenedor entra en loop de reinicio. `depends_on`
    a secas no alcanza: solo espera a que el contenedor exista."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    doc = yaml.safe_load(_compose(cfg_pg))
    dep = doc["services"]["testprod-cliente-uno"]["depends_on"]
    assert dep["testprod-cliente-uno-postgres"]["condition"] == "service_healthy"


def test_cada_instancia_tiene_su_propia_clave(cfg_pg):
    """Dos instancias del mismo producto no pueden compartir contrasena: si una
    se filtra, se filtran las dos."""
    nc.crear_cliente(nombre="Uno", slug="uno", setup_npm=False)
    nc.crear_cliente(nombre="Dos", slug="dos", setup_npm=False)
    claves = []
    for slug in ("uno", "dos"):
        doc = yaml.safe_load(_compose(cfg_pg, slug))
        claves.append(doc["services"][f"testprod-{slug}-postgres"]["environment"]["POSTGRES_PASSWORD"])
    assert claves[0] != claves[1]
    assert all(len(c) >= 40 for c in claves), claves


def test_la_clave_de_la_base_no_va_a_cliente_json(cfg_pg):
    """`cliente.json` lo lee el backoffice y sale por su API. La clave de la
    base vive en el compose, que no se versiona."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    doc = yaml.safe_load(_compose(cfg_pg))
    clave = doc["services"]["testprod-cliente-uno-postgres"]["environment"]["POSTGRES_PASSWORD"]
    meta = (cfg_pg.clientes_dir / "cliente-uno" / "cliente.json").read_text()
    assert clave not in meta


def test_dos_bases_se_crean_con_un_init_montado(cfg_pg_dos_bases):
    """Gestiolibra y MedLibra necesitan DOS bases y no dos schemas: LibraCore y
    LibraGenda declaran los dos una tabla `clients` con `id` de tipos
    incompatibles."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    d = cfg_pg_dos_bases.clientes_dir / "cliente-uno"
    sql = (d / "postgres-init" / "10-bases-extra.sql").read_text()

    assert "CREATE DATABASE testprod_core OWNER testprod;" in sql
    # la principal la crea la imagen via POSTGRES_DB: crearla nnn el init la
    # duplicaria y el arranque fallaria
    assert "CREATE DATABASE testprod " not in sql
    assert "CREATE DATABASE testprod;" not in sql

    doc = yaml.safe_load((d / "docker-compose.yml").read_text())
    vols = doc["services"]["testprod-cliente-uno-postgres"]["volumes"]
    assert "./postgres-init:/docker-entrypoint-initdb.d:ro" in vols

    texto = (d / "docker-compose.yml").read_text()
    assert "TESTPROD_DATABASE_URL=postgresql://testprod:" in texto
    assert "TESTPROD_LIBRACORE_DATABASE_URL=postgresql://testprod:" in texto
    assert "/testprod\n" in texto or "/testprod\r\n" in texto
    assert "/testprod_core" in texto


def test_con_una_sola_base_no_se_monta_init(cfg_pg):
    """El init sobra donde hay una sola base, y montar un directorio que no
    existe rompe el `up`."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    d = cfg_pg.clientes_dir / "cliente-uno"
    assert not (d / "postgres-init").exists()
    assert "docker-entrypoint-initdb.d" not in _compose(cfg_pg)


def test_el_plan_se_aplica_contra_la_PRIMERA_base(cfg_pg_dos_bases, fake_plans, monkeypatch):
    """Medido el 2026-08-11: en los productos con dos bases, `modulos` existe en
    las DOS y la que tiene filas es la del dominio, que es la primera. Aplicar
    el plan contra la de LibraCore escribiria en una tabla que el producto no
    lee — y no fallaria."""
    monkeypatch.setattr(nc, "_esperar_db_lista", lambda *a, **k: True)
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)

    assert len(fake_plans.aplicar_plan_calls) == 1
    destino, plan = fake_plans.aplicar_plan_calls[0]
    assert destino.startswith("postgresql://")
    assert destino.endswith("/testprod"), f"tiene que ser la de dominio, no la de core: {destino}"
    assert plan == "basico"


def test_el_volumen_de_la_base_se_declara(cfg_pg):
    """Sin el volumen nombrado, Docker crea uno anonimo y un `down -v` del
    rollback se lleva los datos sin que nadie lo relacione."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    doc = yaml.safe_load(_compose(cfg_pg))
    assert "testprod-cliente-uno-postgres-data" in doc["volumes"]
    assert "testprod-cliente-uno-postgres-data:/var/lib/postgresql/data" in \
        doc["services"]["testprod-cliente-uno-postgres"]["volumes"]


def test_el_compose_escribe_TAMBIEN_el_nombre_historico(cfg_pg_dos_bases):
    """El fallback del resolvedor cubre "codigo nuevo + compose viejo". Falta el
    simetrico, y es el que rompe un alta: **compose nuevo + imagen vieja**.

    `crear_cliente` pinea la imagen que exista, asi que en un producto sin
    reconstruir la app lee el nombre historico, no lo encuentra, cae a su
    default de SQLite y crea un archivo al lado del PostgreSQL recien nacido.
    No falla: queda healthy y con la base vacia. Medido con un alta real el
    2026-08-11.
    """
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    texto = _compose(cfg_pg_dos_bases)

    # el vigente
    assert "TESTPROD_DATABASE_URL=postgresql://" in texto
    assert "TESTPROD_LIBRACORE_DATABASE_URL=postgresql://" in texto

    doc = yaml.safe_load(texto)
    env = doc["services"]["testprod-cliente-uno"]["environment"]
    pares = dict(e.split("=", 1) for e in env if "=" in e)

    # los dos nombres de la MISMA base tienen que traer la MISMA url, o una
    # imagen vieja y una nueva se conectarian a lugares distintos
    assert pares["TESTPROD_DATABASE_URL"].endswith("/testprod")
    assert pares["TESTPROD_LIBRACORE_DATABASE_URL"].endswith("/testprod_core")


def test_para_un_producto_sin_historicos_no_se_repiten_variables(cfg_pg):
    """TESTPROD no tiene nombres historicos registrados, asi que no tiene que
    aparecer ninguna variable duplicada."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    doc = yaml.safe_load(_compose(cfg_pg))
    env = doc["services"]["testprod-cliente-uno"]["environment"]
    claves = [e.split("=", 1)[0] for e in env if "=" in e]
    assert len(claves) == len(set(claves)), claves


def test_con_un_prefijo_REAL_el_compose_trae_el_nombre_viejo_y_el_nuevo(
        tmp_path, fake_plans, fake_docker):
    """El test de arriba usa `testprod`, que no tiene historicos registrados:
    para lo que dice comprobar es vacuo. Este usa `ventalibra`, que si los
    tiene, y es el producto donde el defecto se encontro con un alta real.
    """
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="VENTALIBRA", image_name="ventalibra:latest",
        container_prefix="ventalibra", db_filename="ventalibra.db",
        repo_root=repo_root, base_port=9000, postgres=True,
    )
    cfg = provisioning.get_config()
    nc.crear_cliente(nombre="Uno", slug="uno", setup_npm=False)

    doc = yaml.safe_load((cfg.clientes_dir / "uno" / "docker-compose.yml").read_text())
    env = doc["services"]["ventalibra-uno"]["environment"]
    pares = dict(e.split("=", 1) for e in env if "=" in e)

    # el vigente y el historico, los dos presentes
    assert "VENTALIBRA_DATABASE_URL" in pares
    assert "VENTALIBRA_DB_PATH" in pares, \
        "sin el nombre historico, una imagen vieja cae a SQLite y el sidecar queda vacio"
    assert "VENTALIBRA_LIBRACORE_DATABASE_URL" in pares
    assert "VENTALIBRA_LIBRACORE_DB_PATH" in pares

    # y apuntando a la MISMA base, o una imagen vieja y una nueva irian a
    # lugares distintos
    assert pares["VENTALIBRA_DATABASE_URL"] == pares["VENTALIBRA_DB_PATH"]
    assert pares["VENTALIBRA_LIBRACORE_DATABASE_URL"] == pares["VENTALIBRA_LIBRACORE_DB_PATH"]

    # ventalibra tiene UNA sola base: las dos variables van al mismo destino
    assert pares["VENTALIBRA_DATABASE_URL"] == pares["VENTALIBRA_LIBRACORE_DATABASE_URL"]
