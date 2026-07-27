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
import os
import re
import secrets
import subprocess
import sys
import json
from pathlib import Path

from . import (
    get_config, _plans, _npm_api,
    client_from_config, forward_host_from_config, le_email_from_config, npm_available,
    docker_build_ssh_args, check_venv_sync,
)


def slugify(name: str) -> str:
    s = name.lower().strip()
    for src, dst in [("áàäâ","a"),("éèëê","e"),("íìïî","i"),("óòöô","o"),("úùüû","u"),("ñ","n")]:
        for c in src:
            s = s.replace(c, dst)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "cliente"


def used_ports() -> set:
    try:
        out = subprocess.run(["docker","ps","-a","--format","{{.Ports}}"],
                             capture_output=True, text=True).stdout
        return {int(m.group(1)) for m in re.finditer(r":(\d+)->8000", out)}
    except Exception:
        return set()


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


def build_image():
    cfg = get_config()
    aviso = check_venv_sync(cfg.repo_root)
    if aviso:
        print(aviso)
    print(f"\n[*] Construyendo imagen {cfg.image_name} ...")
    build_env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    r = subprocess.run(
        ["docker", "build", *docker_build_ssh_args(cfg.repo_root), "-t", cfg.image_name, "."],
        cwd=str(cfg.repo_root), env=build_env,
    )
    if r.returncode != 0:
        sys.exit("[ERROR] Falló el build de la imagen.")
    print("[OK] Imagen lista.")


def image_exists() -> bool:
    cfg = get_config()
    return subprocess.run(["docker","image","inspect",cfg.image_name],
                          capture_output=True).returncode == 0


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

    # — docker-compose.yml —
    compose = f"""\
services:
  {cfg.container_prefix}:
    image: {cfg.image_name}
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
            "plan": plan,
        }, indent=2, ensure_ascii=False)
    )

    # — imagen Docker —
    if rebuild or not image_exists():
        build_image()

    # — levantar —
    log(f"[*] Iniciando {container} ...")
    r = subprocess.run(["docker", "compose", "up", "-d"], cwd=str(client_dir))
    if r.returncode != 0:
        raise ClienteError("No se pudo iniciar el contenedor.")

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
