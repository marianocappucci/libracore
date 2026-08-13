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
    # ⚠️ Los stubs devuelven ÉXITO, no fracaso.
    #
    # Hasta el 2026-08-13 devolvian False y la suite pasaba igual, porque una
    # base que nunca subia era un `[WARN]` y el alta seguia. O sea que **todos
    # estos tests corrian sobre el camino de fallo** sin que se notara. Desde
    # que eso lanza `AltaIncompleta`, el default tiene que ser el alta que sale
    # bien; el camino de fallo se ejercita explicitamente en los tests que lo
    # buscan.
    monkeypatch.setattr(nc, "_esperar_db_lista", lambda *a, **k: True)
    # El camino PostgreSQL espera por `docker exec` hasta 45s: sin stubear,
    # cada test del sidecar cuesta casi un minuto y la suite parece colgada.
    monkeypatch.setattr(nc, "_esperar_tabla_en_sidecar", lambda *a, **k: True)
    monkeypatch.setattr(nc, "_aplicar_plan_en_contenedor", lambda *a, **k: True)
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
    assert "TESTPROD_DATABASE_URL=postgresql+psycopg://testprod:" in texto
    assert "TESTPROD_LIBRACORE_DATABASE_URL=postgresql+psycopg://testprod:" in texto
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
    assert "TESTPROD_DATABASE_URL=postgresql+psycopg://testprod:" in texto
    assert "TESTPROD_LIBRACORE_DATABASE_URL=postgresql+psycopg://testprod:" in texto
    assert "/testprod\n" in texto or "/testprod\r\n" in texto
    assert "/testprod_core" in texto


def test_con_una_sola_base_no_se_monta_init(cfg_pg):
    """El init sobra donde hay una sola base, y montar un directorio que no
    existe rompe el `up`."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    d = cfg_pg.clientes_dir / "cliente-uno"
    assert not (d / "postgres-init").exists()
    assert "docker-entrypoint-initdb.d" not in _compose(cfg_pg)


def test_el_plan_se_aplica_contra_la_PRIMERA_base(cfg_pg_dos_bases, monkeypatch):
    """Medido el 2026-08-11: en los productos con dos bases, `modulos` existe en
    las DOS y la que tiene filas es la del dominio, que es la primera. Aplicar
    el plan contra la de LibraCore escribiria en una tabla que el producto no
    lee — y no fallaria.

    Se comprueba sobre el `docker exec`, que es por donde va el plan contra
    PostgreSQL: desde el host la URL no resuelve."""
    ejecutados = []

    def fake_run(args, **kwargs):
        ejecutados.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="1" if "psql" in args else "", stderr="")

    monkeypatch.setattr(nc, "_esperar_tabla_en_sidecar", _ESPERA_REAL)
    monkeypatch.setattr(nc, "_aplicar_plan_en_contenedor", _APLICAR_REAL)
    monkeypatch.setattr(nc.subprocess, "run", fake_run)
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)

    # la espera pregunta por la base de DOMINIO, no por la de core
    psql = [a for a in ejecutados if "psql" in a][0]
    assert "testprod" in psql and "testprod_core" not in " ".join(psql), psql

    plan = [a for a in ejecutados if "python3" in a and "-c" in a][0]
    codigo = plan[-1]
    assert "DATABASE_URL" in codigo
    assert "LIBRACORE" not in codigo, f"tiene que ser la de dominio, no la de core: {codigo}"
    assert "'basico'" in codigo


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
    assert "TESTPROD_DATABASE_URL=postgresql+psycopg://" in texto
    assert "TESTPROD_LIBRACORE_DATABASE_URL=postgresql+psycopg://" in texto

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


# Capturadas ANTES de que ningun fixture las reemplace: el `fake_docker` las
# stubea para que la suite no espere 90s por test, y este test necesita las de
# verdad para poder mirar los `docker exec` que emiten.
_ESPERA_REAL = nc._esperar_tabla_en_sidecar
_APLICAR_REAL = nc._aplicar_plan_en_contenedor


def test_contra_postgres_la_espera_y_el_plan_van_por_docker_exec(cfg_pg, monkeypatch):
    """🔴 El sidecar no publica puerto y su nombre es un alias de la red de
    Docker: desde el host NO resuelve. Hecho desde el host, la espera se agota
    siempre y el alta reporta que la base no estuvo lista sobre una instancia
    sana -- medido con un alta real el 2026-08-11, con 59 tablas ya creadas.
    """
    ejecutados = []

    def fake_run(args, **kwargs):
        ejecutados.append(args)
        salida = "1" if "psql" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout=salida, stderr="")

    monkeypatch.setattr(nc, "_esperar_tabla_en_sidecar", _ESPERA_REAL)
    monkeypatch.setattr(nc, "_aplicar_plan_en_contenedor", _APLICAR_REAL)
    monkeypatch.setattr(nc.subprocess, "run", fake_run)
    nc.crear_cliente(nombre="Uno", slug="uno", setup_npm=False)

    psql = [a for a in ejecutados if "psql" in a]
    assert psql, "la espera tiene que preguntar por la tabla DENTRO del sidecar"
    assert psql[0][:3] == ["docker", "exec", "testprod-uno-postgres"]
    assert "modulos" in " ".join(psql[0])

    plan = [a for a in ejecutados if "python3" in a and "-c" in a]
    assert plan, "el plan tiene que aplicarse DENTRO del contenedor de la app"
    assert plan[0][:3] == ["docker", "exec", "testprod-uno"]

    # y la URL NO puede ir por linea de comando: la leeria cualquiera en un `ps`
    #
    # ⚠️ Se busca "postgresql" a secas y no "postgresql://". Cuando el generador
    # pasó a emitir `postgresql+psycopg://`, la asercion vieja
    # (`"postgresql://" not in codigo`) quedo pasando por construccion: la
    # cadena `postgresql://` ya no aparece en NINGUN lado, asi que el test
    # habria seguido en verde con la URL entera —contrasena incluida— en la
    # linea de comando.
    codigo = plan[0][-1]
    assert "postgresql" not in codigo, "la URL con la contrasena no va en el comando"
    assert "@" not in codigo, "ni ninguna credencial embebida"
    assert "TESTPROD_DATABASE_URL" in codigo, "se pasa el NOMBRE de la variable"


def test_sin_postgres_el_plan_se_aplica_como_siempre(cfg, fake_plans, monkeypatch):
    """El camino SQLite no cambia: sigue yendo por `plans.aplicar_plan_en_db`
    con una ruta de archivo."""
    monkeypatch.setattr(nc, "_esperar_db_lista", lambda *a, **k: True)
    nc.crear_cliente(nombre="Uno", slug="uno", setup_npm=False)
    assert len(fake_plans.aplicar_plan_calls) == 1
    destino, _ = fake_plans.aplicar_plan_calls[0]
    assert destino.endswith("testprod.db")


# ————————————————————————————————————————————————————————————————
# El healthcheck del compose generado
#
# Hasta el 2026-08-12 esta plantilla estampaba
# `urlopen('http://localhost:8000/health')` para los seis productos, con dos
# defectos superpuestos:
#
#   1. La RUTA. LibraDesk sirve su endpoint en `/api/health`; `/health` no
#      existe ahi. Lo contestaba el catch-all de la SPA, asi que
#      `libradesk-demo` figuraba `healthy` midiendo el index.html.
#   2. La ASERCION, que es lo de fondo. Con estaticos servidos, CUALQUIER ruta
#      devuelve 200: un `urlopen()` a secas no puede fallar. Medido por
#      `docker exec` en los 21 contenedores del VPS, los 21 daban exit 0
#      contra una ruta inventada.
#
# Por eso los tests de aca abajo corren el comando generado contra un servidor
# que imita a la SPA (200 + HTML en todo) y exigen que se ponga en ROJO.
# ————————————————————————————————————————————————————————————————

@pytest.fixture
def cfg_health_api(tmp_path, fake_plans, fake_docker):
    """Producto que sirve la salud en otra ruta, como LibraDesk."""
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo_root, base_port=9000,
        health_path="/api/health",
    )
    return provisioning.get_config()


def _comando_del_healthcheck(cfg, slug="cliente-uno") -> str:
    """La fuente Python del healthcheck, tal cual quedo en el compose."""
    for linea in _compose(cfg, slug).splitlines():
        m = re.match(r"\s*test:\s*(\[.*\])\s*$", linea)
        if m and "python3" in linea:
            cmd = json.loads(m.group(1))
            assert cmd[:3] == ["CMD", "python3", "-c"], cmd[:3]
            return cmd[3]
    raise AssertionError("el compose no declara el healthcheck de la app")


def _servidor(cuerpo: bytes, tipo: str, ruta_viva: str | None = None):
    """Servidor de juguete. Con `ruta_viva=None` contesta lo mismo en TODAS las
    rutas, que es como se comporta un producto de esta familia con la SPA
    horneada."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if ruta_viva is not None and self.path != ruta_viva:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# 🔴 La referencia REAL, tomada en el import. `fake_docker` hace
# `monkeypatch.setattr(nc.subprocess, "run", ...)` y eso parchea el atributo
# del modulo `subprocess` ENTERO, no una copia: cualquier `subprocess.run` de
# este archivo —incluido el de aca abajo— recibiria el stub, que devuelve
# `returncode=0` siempre. O sea que estos tests darian verde sin ejecutar nada,
# que es exactamente el falso verde que vienen a cerrar. Lo delato el test que
# exige ROJO; los otros dos habrian pasado igual.
_RUN_SIN_STUB = subprocess.run


def _correr(fuente: str, puerto: int) -> int:
    fuente = fuente.replace("http://localhost:8000", f"http://127.0.0.1:{puerto}")
    return _RUN_SIN_STUB([sys.executable, "-c", fuente], capture_output=True, text=True).returncode


def test_el_healthcheck_usa_la_ruta_por_defecto(cfg):
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    assert "localhost:8000/health'" in _comando_del_healthcheck(cfg)


def test_el_producto_puede_declarar_otra_ruta(cfg_health_api):
    """LibraDesk. Sin esto, su instancia nace apuntada a una ruta inexistente."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    assert "localhost:8000/api/health'" in _comando_del_healthcheck(cfg_health_api)


def test_el_healthcheck_da_verde_contra_el_endpoint_de_verdad(cfg):
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    srv = _servidor(b'{"status": "ok"}', "application/json", ruta_viva="/health")
    try:
        assert _correr(_comando_del_healthcheck(cfg), srv.server_port) == 0
    finally:
        srv.shutdown()


def test_el_healthcheck_da_rojo_si_contesta_la_SPA(cfg):
    """La rotura: el backend caido con los estaticos en pie.

    Es el escenario real —200 + index.html en cualquier ruta— y el que el
    chequeo viejo no podia distinguir."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    srv = _servidor(b"<!doctype html><title>app</title>", "text/html")
    try:
        assert _correr(_comando_del_healthcheck(cfg), srv.server_port) != 0
    finally:
        srv.shutdown()


def test_un_chequeo_de_solo_codigo_http_no_habria_distinguido(cfg):
    """Contraprueba. Fija POR QUE el generado mira el cuerpo: el chequeo de
    antes da verde contra la SPA, o sea que no podia fallar."""
    viejo = "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"
    srv = _servidor(b"<!doctype html><title>app</title>", "text/html")
    try:
        assert _correr(viejo, srv.server_port) == 0
    finally:
        srv.shutdown()


# ————————————————————————————————————————————————————————————————
# El alta de `lagrace` (2026-08-13): lo que la dejo en crash loop
# ————————————————————————————————————————————————————————————————

def test_la_url_generada_trae_el_driver_psycopg(cfg_pg):
    """`postgresql://` a secas resuelve a psycopg2 en SQLAlchemy, y ninguna
    imagen de la familia lo instala: el driver es psycopg 3.

    LibraCore no lo notaba porque conecta con `psycopg.connect()`, que acepta la
    forma libpq. Pero un producto que le pase esta variable a `create_engine()`
    revienta al importar. Paso con `libradesk-lagrace`: 28 reinicios."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    texto = _compose(cfg_pg)

    urls = re.findall(r"=(postgresql[^\s@]*)://", texto)
    assert urls, "el compose tiene que traer alguna URL de PostgreSQL"
    assert set(urls) == {"postgresql+psycopg"}, urls


def test_ninguna_url_del_compose_queda_sin_driver(cfg_pg_dos_bases):
    """La contraprueba sobre el producto de DOS bases: el defecto estaba en el
    bucle que arma las lineas, asi que alcanzaba con que UNA quedara cruda."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    crudas = [ln.strip() for ln in _compose(cfg_pg_dos_bases).splitlines()
              if "=postgresql://" in ln]
    assert not crudas, crudas


def test_el_compose_trae_las_credenciales_con_el_nombre_que_la_app_lee(cfg):
    """`libraauth.bootstrap.ensure_default_admin(env_prefix=...)` lee
    `<PREFIJO>_ADMIN_USERNAME`/`<PREFIJO>_ADMIN_PASSWORD` y es **fail-closed**:
    sin la prefijada la app no arranca.

    Son cuatro los productos asi (libradesk, gestiolibra, medlibra, ventalibra).
    A cada instancia viva se le habia agregado a mano; `lagrace` no la recibio y
    quedaba en crash loop con `RuntimeError`."""
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno",
                     admin_user="admin", admin_password="secreto123",
                     setup_npm=False)
    texto = _compose(cfg)

    assert "TESTPROD_ADMIN_USERNAME=admin" in texto
    assert "TESTPROD_ADMIN_PASSWORD=secreto123" in texto
    # y la generica se conserva: la siguen leyendo los productos sin migrar
    assert "ADMIN_USER=admin" in texto
    assert "ADMIN_PASSWORD=secreto123" in texto


def test_el_token_de_servicio_sale_del_entorno(cfg, monkeypatch):
    """Sin `LIBRA_SERVICE_TOKEN` en la instancia, `token_de_servicio_valido()`
    devuelve False sin mirar el header y el backoffice recibe 401 en Usuarios y
    SMTP de la instancia que acaba de crear."""
    monkeypatch.setenv("LIBRA_SERVICE_TOKEN", "un-token-de-servicio")
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    assert "LIBRA_SERVICE_TOKEN=un-token-de-servicio" in _compose(cfg)


def test_sin_token_en_el_entorno_no_se_escribe_la_variable(cfg, monkeypatch):
    """Contraprueba: es opt-in por ausencia, igual que del lado de libraauth. Un
    alta desde la CLI no debe estampar una vacia — `LIBRA_SERVICE_TOKEN=` es
    peor que no tenerla, porque parece configurado."""
    monkeypatch.delenv("LIBRA_SERVICE_TOKEN", raising=False)
    nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)
    assert "LIBRA_SERVICE_TOKEN" not in _compose(cfg)


def test_la_espera_de_la_base_entra_en_el_presupuesto_del_proxy():
    """El `proxy_read_timeout` de Nginx Proxy Manager es 90s (su default global,
    el que usan los seis `admin.<producto>.com.ar`). Si esta espera sola vale
    90s, lo que viene despues —emitir el certificado, ~20s— cae siempre del otro
    lado y el navegador recibe `504` con el alta corriendo en el host.

    El margen se fija acá y no en un comentario para que subirlo de nuevo tenga
    que ser una decision explicita."""
    import inspect

    PROXY_READ_TIMEOUT = 90
    MARGEN_CERTIFICADO_Y_ARRANQUE = 30

    timeout = inspect.signature(
        nc._esperar_tabla_en_sidecar).parameters["timeout"].default
    assert timeout <= PROXY_READ_TIMEOUT - MARGEN_CERTIFICADO_Y_ARRANQUE, (
        f"la espera ({timeout}s) no deja margen para el certificado dentro de "
        f"los {PROXY_READ_TIMEOUT}s del proxy"
    )


def test_si_la_base_no_sube_el_alta_falla_y_NO_borra_la_instancia(cfg_pg, monkeypatch):
    """Dos cosas en un test porque son inseparables.

    **Falla**: cada producto siembra `modulos` con su default, y ese default es
    el plan mas alto con todo habilitado. Un `[WARN]` deja al cliente en premium
    sin que nadie lo decida y el alta devuelve exito igual.

    **No borra**: el rollback hace `docker compose down -v` —se lleva el
    volumen— y `rmtree` del directorio. Aplicado acá destruiria la evidencia y,
    si la base solo tardo mas que el timeout, una instancia sana."""
    monkeypatch.setattr(nc, "_esperar_tabla_en_sidecar", lambda *a, **k: False)

    with pytest.raises(nc.AltaIncompleta) as e:
        nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)

    assert "cliente-uno" in str(e.value)
    assert (cfg_pg.clientes_dir / "cliente-uno" / "cliente.json").exists(), \
        "el rollback se llevo puesta la instancia; hay que poder ir a mirarla"


def test_si_no_se_puede_aplicar_el_plan_el_alta_tampoco_reporta_exito(cfg_pg, monkeypatch):
    """La base subio pero el plan no se pudo aplicar: mismo riesgo, los modulos
    quedan en el default del producto."""
    monkeypatch.setattr(nc, "_esperar_tabla_en_sidecar", lambda *a, **k: True)
    monkeypatch.setattr(nc, "_aplicar_plan_en_contenedor", lambda *a, **k: False)

    with pytest.raises(nc.AltaIncompleta) as e:
        nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno",
                         plan="basico", setup_npm=False)
    assert "basico" in str(e.value)
    assert (cfg_pg.clientes_dir / "cliente-uno" / "cliente.json").exists()


def test_un_fallo_de_verdad_SI_dispara_el_rollback(cfg_pg, monkeypatch):
    """Contraprueba: `AltaIncompleta` es la UNICA excepcion que no limpia. Un
    `docker compose up` que falla tiene que seguir borrando, o queda un
    directorio con el slug tomado y sin contenedor detras."""
    def falla_el_up(args, **kwargs):
        if "compose" in args and "up" in args:
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="port is already allocated")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(nc.subprocess, "run", falla_el_up)

    with pytest.raises(nc.ClienteError):
        nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)

    assert not (cfg_pg.clientes_dir / "cliente-uno").exists(), \
        "un fallo de infraestructura si tiene que limpiar"


def test_el_diagnostico_dice_que_le_pasa_al_contenedor(cfg_pg, monkeypatch):
    """El mensaje tiene que traer el motivo leido del contenedor. Sin esto el
    operador recibe "la base no se armo" y tiene que ir al VPS a buscarlo — que
    es lo que hubo que hacer con `lagrace`, donde el motivo estaba en la primera
    linea de `docker logs`."""
    def fake_run(args, **kwargs):
        if "inspect" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="restarting reinicios=28\n", stderr="")
        if "logs" in args:
            return subprocess.CompletedProcess(
                args, 0,
                stdout="Traceback...\nModuleNotFoundError: No module named 'psycopg2'\n",
                stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(nc.subprocess, "run", fake_run)
    monkeypatch.setattr(nc, "_esperar_tabla_en_sidecar", lambda *a, **k: False)

    with pytest.raises(nc.AltaIncompleta) as e:
        nc.crear_cliente(nombre="Cliente Uno", slug="cliente-uno", setup_npm=False)

    mensaje = str(e.value)
    assert "reinicios=28" in mensaje, mensaje
    assert "psycopg2" in mensaje, mensaje
