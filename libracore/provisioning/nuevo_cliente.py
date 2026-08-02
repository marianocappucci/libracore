"""
Onboarding de un nuevo cliente: crea el directorio, genera
`docker-compose.yml` y `cliente.json`, buildea la imagen si falta, levanta
el contenedor, aplica el plan inicial y (si hay dominio + NPM) crea el
proxy con SSL. Modo interactivo (`main()`) y no interactivo
(`crear_cliente()`, usado por `libracore.admin.services`).

Requiere `libracore.provisioning.configure()` antes de usar cualquier
función de acá. `plans.py` (planes reales de cada producto) se resuelve en
tiempo de ejecución vía import diferido — ver `libracore.provisioning._plans()`.
"""
import re
import secrets
import shutil
import subprocess
import sys
import json
from pathlib import Path

from . import (
    get_config, _plans, _npm_api,
    client_from_config, forward_host_from_config, le_email_from_config, npm_available,
    check_venv_sync, build_image_tagged, deploy_version,
)


def slugify(name: str) -> str:
    s = name.lower().strip()
    for src, dst in [("áàäâ","a"),("éèëê","e"),("íìïî","i"),("óòöô","o"),("úùüû","u"),("ñ","n")]:
        for c in src:
            s = s.replace(c, dst)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "cliente"


def _docker_stdout(args: list) -> str:
    """stdout de un comando docker, o cadena vacía si falló o no hay Docker."""
    try:
        r = subprocess.run(args, capture_output=True, text=True)
    except Exception:
        return ""
    return r.stdout if r.returncode == 0 else ""


# Un mapping publicado sale como `0.0.0.0:8071->8000/tcp`, `[::]:8071->8000/tcp`
# o, en rangos, `0.0.0.0:80-81->80-81/tcp`. Lo único que importa es el lado
# izquierdo — el puerto del **host**. A qué puerto del contenedor va es
# irrelevante para saber si el host lo tiene tomado.
_PUBLICADO_RE = re.compile(r":(\d+)(?:-(\d+))?->")


def used_ports() -> set:
    """Puertos del **host** ya tomados por algún contenedor de este Docker.

    Mira todo el host, no sólo los clientes de este producto: un mismo VPS
    corre los seis productos de la familia y todos publican en el mismo rango.

    Dos fuentes, unidas porque cada una tapa el agujero de la otra:

    - `HostConfig.PortBindings` de cada contenedor — incluye los **parados**,
      que no aparecen en la columna PORTS de `docker ps` pero se quedan con el
      puerto apenas alguien los vuelve a arrancar.
    - La columna PORTS de `docker ps -a` — incluye los puertos efímeros que
      asigna `-P`, que no quedan declarados en `PortBindings`.

    > ⚠️ Esto matcheaba `:(\\d+)->8000`, con lo cual sólo veía instancias de
    > producto: los sitios `<producto>-web` publican contra el puerto **80** del
    > contenedor y eran invisibles. El 2026-08-02 un alta en Restolibra eligió
    > 8079 —de `restolibra-web`— y murió con `port is already allocated`. El
    > filtro por puerto de contenedor no aportaba nada y escondía la mitad del
    > rango.
    """
    ports = set()

    ids = _docker_stdout(["docker", "ps", "-aq"]).split()
    if ids:
        binds = _docker_stdout([
            "docker", "inspect", "--format",
            "{{range $port, $bindings := .HostConfig.PortBindings}}"
            "{{range $bindings}}{{.HostPort}} {{end}}{{end}}",
            *ids,
        ])
        ports.update(int(tok) for tok in binds.split() if tok.isdigit())

    publicados = _docker_stdout(["docker", "ps", "-a", "--format", "{{.Ports}}"])
    for m in _PUBLICADO_RE.finditer(publicados):
        desde = int(m.group(1))
        hasta = int(m.group(2) or desde)
        ports.update(range(desde, hasta + 1))

    return ports


def next_port(used: set) -> int:
    cfg = get_config()
    p = cfg.base_port
    while p in used:
        p += 1
    return p


def ask(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{msg}{suffix}: ").strip()
    return val if val else default


def build_image(version: str | None = None) -> str:
    """Construye la imagen del producto y devuelve **la versión** con la que
    quedó etiquetada, que es la que el cliente nuevo va a pinear en su
    compose."""
    cfg = get_config()
    aviso = check_venv_sync(cfg.repo_root)
    if aviso:
        print(aviso)
    version = version or deploy_version()
    if not build_image_tagged(version):
        sys.exit("[ERROR] Falló el build de la imagen.")
    print(f"[OK] Imagen {cfg.image_ref(version)} lista.")
    return version


def image_exists(ref: str | None = None) -> bool:
    cfg = get_config()
    return subprocess.run(["docker","image","inspect", ref or cfg.image_name],
                          capture_output=True).returncode == 0


def version_para_cliente_nuevo(rebuild: bool = False) -> str:
    """Versión que va a pinear un cliente recién creado.

    Reusa la más reciente ya construida en este host — así dar de alta un
    cliente no le mete un artefacto distinto al que corren sus hermanos por
    el solo hecho de haberse creado más tarde. Si no hay ninguna versionada
    (o se pidió `rebuild`), construye una nueva.
    """
    from .panel_admin import versiones_disponibles

    if not rebuild:
        disponibles = versiones_disponibles()
        if disponibles and image_exists(get_config().image_ref(disponibles[0])):
            return disponibles[0]
    return build_image()


def network_exists(name: str) -> bool:
    return subprocess.run(["docker","network","inspect",name],
                          capture_output=True).returncode == 0


def _setup_npm_proxy(npm, domain: str, port: int):
    fwd_host = forward_host_from_config()
    le_email = le_email_from_config()
    print(f"\n[*] Creando proxy en NPM: {domain} → {fwd_host}:{port} (SSL Let's Encrypt) ...")
    npm_mod = _npm_api()
    try:
        existing = npm.get_proxy_host_by_domain(domain)
        if existing:
            print(f"[WARN] Ya existe un proxy para {domain} (id={existing['id']}). Omitiendo.")
            return
        host = npm.create_proxy_host(
            domain=domain,
            forward_host=fwd_host,
            forward_port=port,
            ssl=True,
            le_email=le_email,
        )
        print(f"[OK]  Proxy creado en NPM (id={host['id']}) con certificado SSL.")
    except npm_mod.NPMError as e:
        print(f"[ERROR] NPM: {e}")
        print(f"[!]    Configurá el proxy manualmente: {domain} → {fwd_host}:{port}")


class ClienteError(Exception):
    """Error de alta de cliente (validación o infraestructura)."""


def _rollback_alta(client_dir: Path, log) -> None:
    """Deshace un alta que falló a mitad de camino.

    `crear_cliente` escribe el directorio, `config.json`, el compose y
    `cliente.json` **antes** de levantar el contenedor. Si el `up` falla —el
    caso típico es un puerto ya bindeado— y nadie limpia, queda un cliente en
    el inventario del backoffice (`load_clients()` lista `clientes/*/cliente.json`)
    sin contenedor detrás, y con el slug tomado para el reintento.

    El `compose down` va primero y no es opcional: un `up` que falla al publicar
    el puerto deja el contenedor **creado** aunque no arrancado, así que borrar
    sólo el directorio filtraría un contenedor con el nombre del slug puesto —
    y el siguiente intento con ese mismo slug chocaría contra él.
    """
    if (client_dir / "docker-compose.yml").exists():
        try:
            subprocess.run(["docker", "compose", "down", "-v"],
                           cwd=str(client_dir), capture_output=True)
        except Exception as e:  # noqa: BLE001
            log(f"[WARN] Rollback: no se pudo bajar el contenedor: {e}")
    try:
        shutil.rmtree(client_dir)
        log(f"[OK] Rollback: se borró {client_dir}")
    except Exception as e:  # noqa: BLE001
        log(f"[ERROR] Rollback incompleto — revisá {client_dir} a mano: {e}")


def _esperar_db_lista(db_path: Path, timeout: int = 25) -> bool:
    """Espera a que la instancia recién levantada cree su DB y la tabla `modulos`."""
    import sqlite3, time
    t0 = time.time()
    while time.time() - t0 < timeout:
        if db_path.exists():
            try:
                con = sqlite3.connect(str(db_path))
                row = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='modulos'"
                ).fetchone()
                con.close()
                if row:
                    return True
            except Exception:
                pass
        time.sleep(1)
    return False


def crear_cliente(nombre: str, slug: str = "", domain: str = "", port: int = 0,
                  admin_user: str = "admin", admin_password: str = "",
                  admin_nombre: str = "", plan: str = "basico",
                  setup_npm: bool = True, rebuild: bool = False, log=lambda *a: None) -> dict:
    """Da de alta un cliente de forma NO interactiva: crea el directorio, config,
    docker-compose y cliente.json, buildea la imagen si falta, levanta el contenedor,
    aplica el plan inicial y (si hay dominio + NPM) crea el proxy con SSL.

    Devuelve un dict con los datos del cliente (incluida la contraseña generada).
    Lanza ClienteError ante validaciones o fallos de infraestructura.
    """
    cfg = get_config()
    plans = _plans()

    nombre = (nombre or "").strip()
    if not nombre:
        raise ClienteError("El nombre es obligatorio.")

    slug = slugify(slug or nombre)
    client_dir = cfg.clientes_dir / slug
    if client_dir.exists():
        raise ClienteError(f"Ya existe un cliente con slug '{slug}'.")

    if plan not in plans.PLANES:
        raise ClienteError(f"Plan inválido: {plan!r}.")

    _used = used_ports()
    port = int(port) if port else next_port(_used)
    if port in _used:
        log(f"[WARN] El puerto {port} ya está en uso.")

    if not admin_password:
        admin_password = secrets.token_urlsafe(12)
    admin_nombre = admin_nombre or nombre
    secret_key = secrets.token_hex(32)

    # A partir de acá el alta escribe en disco, así que todo lo que sigue va
    # bajo rollback: si algo falla a mitad, `client_dir` se borra entero. Es
    # seguro borrarlo porque existe sólo porque lo creamos nosotros — el
    # chequeo de slug duplicado de más arriba garantiza que no había nada.
    try:
        # — directorios —
        data_dir = client_dir / "data"
        for sub in ["logos", "arca_certs", "facturas_pdf", "remitos_pdf", "presupuestos_pdf"]:
            (data_dir / sub).mkdir(parents=True, exist_ok=True)
        log(f"[OK] Directorios en {client_dir}")

        # — config.json — (claves deben coincidir con _DEFAULTS en config_manager.py)
        config = {
            "empresa_nombre": nombre, "empresa_direccion": "", "empresa_telefono": "",
            "empresa_email": "", "empresa_cuit": "",
            "empresa_iva_condition": "Responsable Inscripto",
        }
        (data_dir / "config.json").write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # — detectar red Docker —
        net_name    = "stack_stack-net"
        if network_exists(net_name):
            service_net = "    networks:\n      - stack-net\n"
            top_net     = (f"\nnetworks:\n  stack-net:\n    external: true\n"
                           f"    name: {net_name}\n")
        else:
            log(f"[WARN] Red '{net_name}' no encontrada — el contenedor usará la red por defecto.")
            service_net = ""
            top_net     = ""

        container = f"{cfg.container_prefix}-{slug}"

        # — versión de imagen — el compose nace pineado a una versión concreta,
        # nunca a `:latest` (ver panel_admin, sección "versión de imagen").
        version   = version_para_cliente_nuevo(rebuild)
        image_ref = cfg.image_ref(version)
        log(f"[OK] Imagen para este cliente: {image_ref}")

        # — docker-compose.yml —
        compose = f"""\
services:
  {cfg.container_prefix}:
    image: {image_ref}
    container_name: {container}
    restart: unless-stopped
    mem_limit: 768m
    mem_reservation: 256m
    cpus: 1.0
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    ports:
      - "{port}:8000"
    volumes:
      - ./data:/app/data
    environment:
      - DATA_DIR=/app/data
      - SECRET_KEY={secret_key}
      - ADMIN_USER={admin_user}
      - ADMIN_PASSWORD={admin_password}
      - ADMIN_NOMBRE={admin_nombre}
      - DOCS_AUTH_SECRET={cfg.docs_auth_secret}
{service_net}{top_net}"""
        (client_dir / "docker-compose.yml").write_text(compose)

        # — metadata del cliente —
        (client_dir / "cliente.json").write_text(
            json.dumps({
                "nombre": nombre, "slug": slug, "domain": domain,
                "port": port, "container": container,
                "admin_user": admin_user, "admin_password": admin_password,
                "plan": plan, "version_desplegada": version,
            }, indent=2, ensure_ascii=False)
        )

        # — levantar —
        log(f"[*] Iniciando {container} ...")
        r = subprocess.run(["docker", "compose", "up", "-d"], cwd=str(client_dir),
                           capture_output=True, text=True)
        if r.returncode != 0:
            # `docker compose` escribe el motivo real en stderr. Sin arrastrarlo
            # hasta acá, el backoffice devuelve un 422 que dice sólo "no se pudo
            # iniciar" y el `port is already allocated` queda enterrado en un log
            # del host que nadie va a mirar.
            detalle = [ln.strip() for ln in (r.stderr or r.stdout or "").splitlines() if ln.strip()]
            for linea in detalle:
                log(linea)
            motivo = detalle[-1] if detalle else ""
            raise ClienteError(
                "No se pudo iniciar el contenedor." + (f" {motivo}" if motivo else "")
            )

        # — aplicar plan inicial (tras esperar a que la instancia cree su DB) —
        db_path = data_dir / cfg.db_filename
        if _esperar_db_lista(db_path):
            plans.aplicar_plan_en_db(str(db_path), plan)
            log(f"[OK] Plan '{plan}' aplicado.")
        else:
            log("[WARN] La DB no estuvo lista a tiempo; aplicá el plan desde el backoffice.")

        # — proxy NPM (opcional) —
        proxy_ok = None
        if domain and setup_npm and npm_available():
            npm = client_from_config()
            if npm:
                try:
                    _setup_npm_proxy(npm, domain, port)
                    proxy_ok = True
                except Exception as e:  # noqa: BLE001
                    log(f"[ERROR] NPM: {e}")
                    proxy_ok = False
            else:
                log("[INFO] NPM no configurado; configurá el proxy manualmente.")

        return {
            "nombre": nombre, "slug": slug, "domain": domain, "port": port,
            "container": container, "admin_user": admin_user,
            "admin_password": admin_password, "plan": plan, "proxy_ok": proxy_ok,
            "dir": str(client_dir),
        }
    except Exception:
        # `Exception` y no `BaseException` a propósito: un Ctrl-C durante los
        # 25s que espera la DB llega con el contenedor ya arriba y sano, y
        # borrarlo ahí sería peor que dejarlo.
        _rollback_alta(client_dir, log)
        raise


def main():
    cfg = get_config()
    plans = _plans()

    print("=" * 60)
    print(f"  {cfg.product_name} — Alta de nuevo cliente")
    print("=" * 60)

    nombre = ask("Nombre del comercio / empresa")
    if not nombre:
        sys.exit("[ERROR] El nombre es obligatorio.")

    slug = slugify(ask("Identificador (slug)", slugify(nombre)))
    if (cfg.clientes_dir / slug).exists():
        sys.exit(f"[ERROR] Ya existe '{slug}' en {cfg.clientes_dir / slug}")

    domain = ask("Dominio (ej: mitienda.com, Enter para omitir)", "")

    _used = used_ports()
    port  = int(ask("Puerto HTTP", str(next_port(_used))))

    admin_user     = ask("Usuario admin", "admin")
    admin_password = ask("Contraseña admin (Enter = generar)", "")
    admin_nombre   = ask("Nombre completo del admin", nombre)

    plan = ask(f"Plan ({'/'.join(plans.PLANES)})", "basico")
    if plan not in plans.PLANES:
        sys.exit(f"[ERROR] Plan inválido: {plan}")

    print("\n" + "-" * 60)
    print(f"  Comercio:  {nombre}   Slug: {slug}   Puerto: {port}   Plan: {plan}")
    if domain:
        print(f"  Dominio:   {domain}")
    print("-" * 60)
    if ask("¿Confirmar? [S/n]", "s").lower() == "n":
        sys.exit("Cancelado.")

    setup_npm = True
    if domain and npm_available() and client_from_config():
        setup_npm = ask("¿Configurar proxy + SSL en NPM? [S/n]", "s").lower() != "n"

    try:
        info = crear_cliente(
            nombre=nombre, slug=slug, domain=domain, port=port,
            admin_user=admin_user, admin_password=admin_password,
            admin_nombre=admin_nombre, plan=plan, setup_npm=setup_npm, log=print,
        )
    except ClienteError as e:
        sys.exit(f"[ERROR] {e}")

    print("\n" + "=" * 60)
    print("  CLIENTE DADO DE ALTA EXITOSAMENTE")
    print("=" * 60)
    print(f"  Comercio:    {info['nombre']}")
    print(f"  URL local:   http://localhost:{info['port']}")
    if info["domain"]:
        print(f"  Dominio:     https://{info['domain']}")
    print(f"  Admin:       {info['admin_user']}  /  {info['admin_password']}")
    print(f"  Plan:        {info['plan']}")
    print("=" * 60)
    print("\n[!] Guardá las credenciales — no se volverán a mostrar.")
