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


def _esperar_tabla_en_sidecar(sidecar: str, base: str, usuario: str,
                              timeout: int = 90) -> bool:
    """Espera a que la app cree la tabla `modulos` DENTRO del sidecar.

    🔴 **No se puede preguntar desde el host.** El sidecar no publica puerto —a
    propósito— y su nombre es un alias de la red de Docker, así que desde
    afuera no resuelve. Una espera hecha con `conectar(url)` desde el host no
    falla: **se agota siempre**, y el alta reporta *"la DB no estuvo lista a
    tiempo"* sobre una instancia que arrancó perfecta.

    Medido con un alta real el 2026-08-11: 59 tablas creadas en PostgreSQL y la
    espera igual vencida. Es la misma trampa que documenta
    `panel_admin._dump_postgres_por_docker`, encontrada por tercera vez.

    Se pregunta por la TABLA y no por `pg_isready`: el sidecar está healthy
    apenas acepta conexiones, y el schema lo construye la app después.
    """
    import time

    t0 = time.time()
    while time.time() - t0 < timeout:
        r = subprocess.run(
            ["docker", "exec", sidecar, "psql", "-U", usuario, "-d", base, "-tAc",
             "select count(*) from information_schema.tables "
             "where table_schema='public' and table_name='modulos'"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip() == "1":
            return True
        time.sleep(2)
    return False


def _aplicar_plan_en_contenedor(container: str, variable: str, plan: str) -> bool:
    """Aplica el plan corriendo el `plans.py` del producto DENTRO del contenedor.

    Mismo motivo que arriba: desde el host la URL no resuelve. Y además la
    **URL no se pasa por línea de comando** —la leería cualquiera en un `ps`,
    con la contraseña adentro—: se le pasa el NOMBRE de la variable y el
    contenedor la lee de su propio entorno.
    """
    codigo = (
        "import sys, os; sys.path.insert(0, '/app'); import plans; "
        f"plans.aplicar_plan_en_db(os.environ[{variable!r}], {plan!r})"
    )
    r = subprocess.run(["docker", "exec", container, "python3", "-c", codigo],
                       capture_output=True, text=True)
    return r.returncode == 0


def _esperar_db_lista(db_path, timeout: int = 25) -> bool:
    """Espera a que la instancia recién levantada cree su DB y la tabla `modulos`.

    Sólo para SQLite: contra PostgreSQL hay que preguntar desde adentro —ver
    `_esperar_tabla_en_sidecar`—.
    """
    import sqlite3, time

    t0 = time.time()
    while time.time() - t0 < timeout:
        if Path(db_path).exists():
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


def _bloques_postgres(cfg, slug: str, container: str, client_dir: Path):
    """Arma las piezas del compose que le dan un sidecar propio a la instancia.

    Devuelve `(env_lines, servicio_db, volumen, url_del_plan)`, o cuatro vacíos
    si el producto todavía no declara `db_urls`.

    Dos decisiones que no son de estilo:

    - **El sidecar NO publica puerto.** Publicar 5432 en un VPS es publicarlo a
      Internet. Se llega sólo desde la red de la instancia.
    - **El sidecar va SOLO en la red `datos`**, y la app en `datos` + la
      compartida. Hasta el 2026-08-11 las 15 instancias compartían una red
      plana y el PostgreSQL de un cliente era alcanzable desde el contenedor de
      cualquier otro producto: lo único que separaba era la contraseña. La app
      conserva la red compartida porque el proxy la routea por nombre.
    """
    if not cfg.usa_postgres:
        return "", "", "", None

    import secrets as _secrets

    sidecar = f"{container}-postgres"
    usuario = cfg.container_prefix
    # 48 hex, el mismo largo que las que ya están en el parque. `token_hex` y
    # no `token_urlsafe`: la clave viaja adentro de una URL y `urlsafe` mete
    # `-`/`_` que son seguros pero también `=` de padding en otros generadores.
    clave = _secrets.token_hex(24)

    bases = cfg.bases_postgres
    principal = bases[0]

    # 🔴 Se escriben TODOS los nombres aceptados, no sólo el vigente.
    #
    # El fallback de `url_de_instancia` cubre "código nuevo leyendo un compose
    # viejo". Falta el caso simétrico, y es el que rompe un alta: **compose
    # nuevo con imagen vieja.** `crear_cliente` pinea la imagen que exista, así
    # que en un producto que todavía no se reconstruyó la app lee el nombre
    # histórico, no lo encuentra, cae a su default de SQLite y crea un archivo
    # al lado del PostgreSQL que acaba de nacer vacío. **No falla**: el
    # contenedor queda healthy y el sidecar sin una tabla.
    #
    # Medido el 2026-08-11 con un alta real en VentaLibra, que es exactamente
    # como se encontró. Los dos nombres apuntan a la misma URL, así que sirven
    # las dos imágenes; se saca junto con `_HISTORICOS`, en el mismo momento.
    from ..db.url_de_instancia import nombres_aceptados

    lineas = []
    for (var, base), core in zip(cfg.db_urls, (False, True)):
        url = f"postgresql://{usuario}:{clave}@{sidecar}:5432/{base}"
        for nombre in nombres_aceptados(cfg.container_prefix, core=core):
            lineas.append(f"      - {nombre}={url}\n")
    env_lines = "".join(dict.fromkeys(lineas))  # sin repetir, conservando orden

    # Las bases extra las crea un init que la imagen corre UNA vez, al
    # inicializar el volumen. Si el volumen ya existe no se ejecuta — para una
    # instancia nueva siempre es la primera vez.
    monta_init = ""
    if len(bases) > 1:
        init_dir = client_dir / "postgres-init"
        init_dir.mkdir(parents=True, exist_ok=True)
        cuerpo = "\n".join(
            f"CREATE DATABASE {b} OWNER {usuario};" for b in bases[1:]
        )
        (init_dir / "10-bases-extra.sql").write_text(
            "-- Bases adicionales de esta instancia.\n"
            "--\n"
            "-- Son bases y no schemas porque LibraCore y el motor de dominio\n"
            "-- declaran los dos una tabla `clients` con `id` de tipos\n"
            "-- incompatibles: en un solo schema el segundo CREATE TABLE IF NOT\n"
            "-- EXISTS no hace nada y despues PostgreSQL rechaza las FK.\n"
            "--\n"
            "-- La imagen corre esto UNA sola vez, al inicializar el volumen.\n"
            f"{cuerpo}\n",
            encoding="utf-8",
        )
        monta_init = "      - ./postgres-init:/docker-entrypoint-initdb.d:ro\n"

    servicio_db = f"""
  {sidecar}:
    image: {cfg.postgres_image}
    container_name: {sidecar}
    restart: unless-stopped
    environment:
      POSTGRES_DB: {principal}
      POSTGRES_USER: {usuario}
      POSTGRES_PASSWORD: {clave}
    volumes:
      - {sidecar}-data:/var/lib/postgresql/data
{monta_init}    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U {usuario} -d {principal}"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
    networks:
      - datos
"""

    volumen = f"\nvolumes:\n  {sidecar}-data:\n"
    url_del_plan = f"postgresql://{usuario}:{clave}@{sidecar}:5432/{cfg.db_urls[0][1]}"
    return env_lines, servicio_db, volumen, url_del_plan


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

        container = f"{cfg.container_prefix}-{slug}"

        # — piezas del sidecar PostgreSQL (vacías si el producto no lo declara) —
        pg_env, pg_service, pg_volume, pg_url_plan = _bloques_postgres(
            cfg, slug, container, client_dir
        )

        # — redes —
        #
        # `datos` es de esta instancia y es donde vive su base. La compartida la
        # necesita SÓLO la app, porque el proxy la routea por nombre de
        # contenedor; el sidecar no entra ahí — ver `_bloques_postgres`.
        red_datos = f"{container}-datos"
        net_name  = "stack_stack-net"
        if network_exists(net_name):
            service_net = "    networks:\n      - stack-net\n"
            top_net     = (f"\nnetworks:\n  stack-net:\n    external: true\n"
                           f"    name: {net_name}\n")
        else:
            log(f"[WARN] Red '{net_name}' no encontrada — el contenedor usará la red por defecto.")
            service_net = ""
            top_net     = ""
        if cfg.usa_postgres:
            # La app necesita las dos. Si la compartida no existe, igual hace
            # falta declarar la propia o la app no llega a su base.
            service_net = (service_net or "    networks:\n") + "      - datos\n"
            top_net     = (top_net or "\nnetworks:\n") + f"  datos:\n    name: {red_datos}\n"

        # La app **no puede arrancar antes que su base**: el schema lo construye
        # ella al arrancar, y contra un PostgreSQL que todavía no acepta
        # conexiones se cae y el contenedor entra en loop de reinicio. Por eso
        # `service_healthy` y no un `depends_on` a secas, que sólo espera a que
        # el contenedor exista.
        pg_depends = (
            f"    depends_on:\n      {container}-postgres:\n"
            f"        condition: service_healthy\n"
            if cfg.usa_postgres else ""
        )

        # — versión de imagen — el compose nace pineado a una versión concreta,
        # nunca a `:latest` (ver panel_admin, sección "versión de imagen").
        version   = version_para_cliente_nuevo(rebuild)
        image_ref = cfg.image_ref(version)
        log(f"[OK] Imagen para este cliente: {image_ref}")

        # — docker-compose.yml —
        #
        # `name:` explícito y no el default de Compose, que es el nombre del
        # DIRECTORIO del compose — o sea el slug. Dos productos con una
        # instancia del mismo slug quedaban en el mismo proyecto, y ahí Compose
        # trata a la del otro producto como contenedor huérfano: cualquier
        # `docker compose --remove-orphans` corrido desde uno se lleva puestas
        # las del resto. Pasó de verdad: el 2026-08-02 quedaron cuatro
        # instancias `prueba` (restolibra, ventalibra, medlibra, gestiolibra)
        # compartiendo `project=prueba`. Con el prefijo del producto, el
        # proyecto es único aunque el slug se repita.
        #
        # La CLAVE DEL SERVICIO es `{prefijo}-{slug}` y no el prefijo a secas
        # por el mismo motivo, pero con una consecuencia peor: Compose la
        # convierte en ALIAS DE RED, y `stack-net` es compartida por los seis
        # productos. Con el prefijo a secas, la segunda instancia de un producto
        # reclama el mismo alias que la primera y el DNS interno las alterna por
        # round-robin. No es hipotético: el 2026-08-06
        # `sistema.contalibra.com.ar` —producción de un cliente— sirvió a ratos
        # el auto-login de la demo, justamente por esto. Aquella vez se
        # renombraron los composes afectados a mano y esta plantilla quedó sin
        # tocar, así que cada instancia nueva reintroducía el defecto.
        compose = f"""\
name: {cfg.container_prefix}-{slug}

services:
  {container}:
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
{pg_depends}    volumes:
      - ./data:/app/data
    environment:
      - DATA_DIR=/app/data
      - SECRET_KEY={secret_key}
      - ADMIN_USER={admin_user}
      - ADMIN_PASSWORD={admin_password}
      - ADMIN_NOMBRE={admin_nombre}
      - DOCS_AUTH_SECRET={cfg.docs_auth_secret}
{pg_env}{service_net}{pg_service}{top_net}{pg_volume}"""
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
        #
        # 🔴 Contra PostgreSQL las DOS cosas —esperar y aplicar— van por
        # `docker exec`, no desde el host: el sidecar no publica puerto y su
        # nombre es un alias de la red de Docker, así que desde afuera no
        # resuelve. Hecho desde el host, la espera se agota SIEMPRE y el alta
        # reporta que la base no estuvo lista sobre una instancia sana.
        #
        # El plan va contra la PRIMERA de `db_urls`: ahí vive el `modulos` que
        # el producto lee. En los productos con dos bases la tabla existe en
        # las dos y sólo la del dominio tiene filas.
        if cfg.usa_postgres:
            variable, base = cfg.db_urls[0]
            listo = _esperar_tabla_en_sidecar(
                f"{container}-postgres", base, cfg.container_prefix
            )
            if listo and _aplicar_plan_en_contenedor(container, variable, plan):
                log(f"[OK] Plan '{plan}' aplicado.")
            elif listo:
                log("[WARN] La DB está lista pero no se pudo aplicar el plan; "
                    "aplicalo desde el backoffice.")
            else:
                log("[WARN] La DB no estuvo lista a tiempo; aplicá el plan "
                    "desde el backoffice.")
        else:
            db_path = data_dir / cfg.db_filename
            if _esperar_db_lista(db_path):
                plans.aplicar_plan_en_db(str(db_path), plan)
                log(f"[OK] Plan '{plan}' aplicado.")
            else:
                log("[WARN] La DB no estuvo lista a tiempo; aplicá el plan "
                    "desde el backoffice.")

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
