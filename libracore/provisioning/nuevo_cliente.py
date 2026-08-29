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
from . import mail_cuentas


# Huso horario del ecosistema: Argentina, UTC-3 fijo, sin horario de verano
# (el país no aplica DST desde 2009). Ver `wiki/concepts/estandares-desarrollo.md`,
# sección "Fecha y hora".
#
# 🔴 Va en el compose que se GENERA, no sólo en el del repo del producto: las
# instancias de demo y de cada cliente salen de acá, no del compose versionado.
# Hasta el 2026-08-23 esta plantilla no lo ponía y las 18 instancias de los seis
# productos corrían en UTC — con la suite entera en verde, porque el defecto no
# da error: el reloj sale 3 h adelantado y entre las 21:00 y la medianoche
# `date.today()` devuelve directamente mañana.
#
# El sidecar de PostgreSQL lo lleva igual que la app: es el que define qué es
# "hoy" para un `CURRENT_DATE` o un default del schema.
#
# 🔴 Y en el sidecar va por `command:`, no sólo por `TZ`. La imagen de
# PostgreSQL escribe `timezone` en `postgresql.conf` UNA vez, en el `initdb`, y
# ese archivo vive en el volumen de datos: sobre un volumen que ya existe, `TZ`
# cambia el `date` del contenedor y **no cambia nada de lo que hace el
# servidor** — `now()` sigue devolviendo UTC. Medido el 2026-08-23 en las seis
# demos: `date` decía `-03` y `select now()` seguía dando la hora de Londres.
# Con `-c timezone=` se fija al arrancar el servidor, venga el volumen de donde
# venga.
_TZ = "America/Argentina/Buenos_Aires"


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


def cuit_valido(cuit: str) -> bool:
    """¿Tiene forma de CUIT? Once dígitos, con o sin guiones.

    No verifica el dígito verificador **a propósito**: el alta no es el lugar
    donde se descubre que un CUIT real está mal tipeado —eso lo dice ARCA la
    primera vez que se factura—, y un validador de más acá rechazaría también
    los CUIT de prueba con los que se arman las demos.

    Lo que sí ataja es el caso que motivó todo esto: la cadena vacía, y el
    "después lo cargo" escrito como `-` o `sin cuit`.
    """
    return len(re.sub(r"[^0-9]", "", cuit or "")) == 11


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


def _rollback_casilla(cuenta, log) -> None:
    """Borra la casilla de correo de un alta que falló. Nunca levanta.

    `cuenta` es `None` cuando el alta falló antes de crearla, o cuando este
    entorno no tiene servidor de correo configurado — los dos casos normales, y
    por eso el `return` temprano no es defensivo sino el camino habitual.

    Que no levante es la razón de que exista como función aparte: esto corre
    adentro de un `except` que va a re-lanzar el error REAL del alta, y una
    excepción escapándose de acá lo reemplazaría por una sobre el servidor de
    correo — el error de arriba se perdería y nadie sabría por qué falló el
    alta.
    """
    if cuenta is None:
        return
    try:
        mail_cuentas.borrar_cuenta(cuenta.direccion)
        log(f"[OK] Rollback: se borró la casilla {cuenta.direccion}")
    except Exception as e:  # noqa: BLE001
        # Queda una casilla con credencial viva y nadie la va a usar, pero el
        # compose que la usaba ya no existe. Se avisa con la dirección para
        # poder borrarla a mano.
        log(f"[ERROR] Rollback: quedó la casilla {cuenta.direccion} sin borrar: {e}")


class AltaIncompleta(ClienteError):
    """El alta creó la instancia pero no la dejó lista para entregar.

    Es un `ClienteError` para que el backoffice lo devuelva como error y la
    pantalla deje de mostrar el panel de credenciales como si todo hubiera
    salido bien. Pero es una subclase propia por una razón concreta: **no
    dispara el rollback.**

    El rollback borra el directorio, el contenedor y el volumen. Aplicado acá
    destruiría la evidencia de por qué falló y, peor, se llevaría puesta una
    instancia sana cuya base simplemente tardó más que el timeout. El estado
    correcto ante esto es "creada pero no entregable, andá a mirarla", no
    "borrada".
    """


def _diagnostico_contenedor(container: str) -> str:
    """Por qué no arrancó, leído del contenedor y no adivinado.

    Sin esto el operador recibe "la base no se armó" y tiene que ir al VPS a
    buscar el motivo a mano — que es exactamente lo que hubo que hacer con
    `lagrace`, donde la causa (`ModuleNotFoundError: No module named
    'psycopg2'`) estaba en la primera línea de `docker logs`.
    """
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f",
             "{{.State.Status}} reinicios={{.RestartCount}}", container],
            capture_output=True, text=True, timeout=10,
        )
        estado = r.stdout.strip() or "desconocido"
    except Exception:  # noqa: BLE001
        estado = "desconocido"

    ultima = ""
    try:
        r = subprocess.run(["docker", "logs", "--tail", "25", container],
                           capture_output=True, text=True, timeout=10)
        salida = (r.stdout or "") + (r.stderr or "")
        # La línea útil de un traceback de Python es la ÚLTIMA, no la primera:
        # arriba está el marco de uvicorn y abajo el error real.
        lineas = [ln.strip() for ln in salida.splitlines() if ln.strip()]
        if lineas:
            ultima = lineas[-1][:300]
    except Exception:  # noqa: BLE001
        pass

    detalle = f"Contenedor {container}: {estado}."
    if ultima:
        detalle += f" Último log: {ultima}"
    return detalle


def _esperar_tabla_en_sidecar(sidecar: str, base: str, usuario: str,
                              timeout: int = 45) -> bool:
    """Espera a que la app cree la tabla `modulos` DENTRO del sidecar.

    🔴 **El timeout tiene que entrar en el presupuesto del proxy.** Hasta el
    2026-08-13 eran 90 s, exactamente el `proxy_read_timeout` de Nginx Proxy
    Manager (su default global, el que usan los seis `admin.<producto>.com.ar`).
    O sea que esta espera sola podía consumir el presupuesto entero, y lo que
    viene después —emitir el certificado de Let's Encrypt, ~20 s— caía siempre
    del otro lado: el navegador recibía `504 Gateway Time-out` con el alta
    todavía corriendo en el host.

    Pasó con el alta de `libradesk-lagrace` el 2026-08-13. 45 s dejan margen
    para el certificado y el arranque del contenedor dentro de los 90 s. Un alta
    normal no se acerca: la medición del 2026-08-02 dio 24 s de punta a punta,
    certificado incluido.

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

    # 🔴 `postgresql+psycopg://` y NO `postgresql://` a secas.
    #
    # LibraCore conecta con `psycopg.connect()`, que acepta la forma libpq, así
    # que acá el driver nunca se notaba. Pero un producto puede pasarle esta
    # misma variable a SQLAlchemy, y SQLAlchemy resuelve `postgresql://` al
    # dialecto **psycopg2**, que ninguna imagen de la familia instala: el
    # driver es psycopg 3. La app revienta al importar con `ModuleNotFoundError:
    # No module named 'psycopg2'` y el contenedor queda en crash loop.
    #
    # Medido el 2026-08-13 con el alta real de `libradesk-lagrace`, la primera
    # instancia de LibraDesk creada por el backoffice: 28 reinicios. Las
    # instancias sanas del parque tenían la forma correcta **escrita a mano**,
    # que es por qué el defecto vivió acá sin que nadie lo viera.
    #
    # Es seguro para los consumidores de LibraCore: `db.core.conectar()`,
    # `panel_admin` y `respaldo` ya normalizan quitando el `+psycopg`, y
    # `es_url_postgres()` acepta las dos formas. Y `migrations/env.py` ya hacía
    # exactamente esta conversión, con este mismo comentario, sólo que del lado
    # del consumo — o sea que la trampa estaba documentada y el generador la
    # seguía pisando.
    lineas = []
    for (var, base), core in zip(cfg.db_urls, (False, True)):
        url = f"postgresql+psycopg://{usuario}:{clave}@{sidecar}:5432/{base}"
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
    command: postgres -c timezone={_TZ}
    container_name: {sidecar}
    restart: unless-stopped
    environment:
      POSTGRES_DB: {principal}
      POSTGRES_USER: {usuario}
      POSTGRES_PASSWORD: {clave}
      TZ: {_TZ}
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
    url_del_plan = (
        f"postgresql+psycopg://{usuario}:{clave}@{sidecar}:5432/{cfg.db_urls[0][1]}"
    )
    return env_lines, servicio_db, volumen, url_del_plan


def crear_cliente(nombre: str, slug: str = "", domain: str = "", port: int = 0,
                  admin_user: str = "admin", admin_password: str = "",
                  admin_nombre: str = "", plan: str = "basico",
                  empresa_cuit: str = "", empresa_nombre: str = "",
                  sin_identidad: bool = False,
                  setup_npm: bool = True, rebuild: bool = False, log=lambda *a: None) -> dict:
    """Da de alta un cliente de forma NO interactiva: crea el directorio, config,
    docker-compose y cliente.json, buildea la imagen si falta, levanta el contenedor,
    aplica el plan inicial y (si hay dominio + NPM) crea el proxy con SSL.

    `empresa_cuit` y `empresa_nombre` son la IDENTIDAD FISCAL de la instancia y
    van a su `config.json`, que es de donde el `identidad()` de cada producto
    saca lo que le contesta al panel del dueño. `empresa_nombre` es la razón
    social —que puede no ser el nombre comercial de `nombre`— y cae a `nombre`
    si no se pasa.

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

    # — identidad fiscal —
    #
    # 🔴 Se valida ACÁ, antes de tocar Docker y antes de escribir un solo
    # archivo, así que un alta sin CUIT no deja nada atrás y se puede reintentar
    # con el mismo slug. Es la diferencia con `AltaIncompleta`, que es para
    # cuando la instancia ya existe.
    #
    # El caso que esto cierra está vivo: `contalibra-demo` contesta
    # `instancia: {nombre: "", cuit: "", punto_venta: null}` porque nunca se le
    # cargó la empresa, y el panel no la puede agrupar por razón social. El
    # panel sabe defenderse —la muestra aparte como "sin identificar" en vez de
    # juntarla con cualquier otra vacía— pero eso es la red, no el objetivo:
    # sumar entre CUITs da un número de gestión y no uno declarable, así que una
    # sucursal sin CUIT es una sucursal que no entra en el único consolidado que
    # cierra contra los libros.
    #
    # `sin_identidad` existe porque las instancias de demo son legítimas y no
    # tienen CUIT. Es un opt-in EXPLÍCITO y no un default: inventar un CUIT para
    # pasar el chequeo sería peor que no tener ninguno —un CUIT falso agrupa, y
    # agrupa mal—, y un `[WARN]` en el log no lo lee nadie.
    empresa_nombre = (empresa_nombre or "").strip() or nombre
    empresa_cuit = (empresa_cuit or "").strip()
    if sin_identidad:
        if empresa_cuit and not cuit_valido(empresa_cuit):
            raise ClienteError(
                f"CUIT inválido: {empresa_cuit!r}. Son once dígitos, con o sin guiones."
            )
        log("[WARN] Instancia sin CUIT: el panel del dueño no la va a poder "
            "agrupar por razón social y la va a mostrar como «sin identificar». "
            "Cargale la empresa desde Configuración antes de entregarla.")
    elif not empresa_cuit:
        raise ClienteError(
            "Falta el CUIT de la empresa. Es lo que le permite al panel del "
            "dueño agrupar la sucursal por razón social; sin él la instancia "
            "queda sin identificar. Si es una demo, pedila con sin_identidad."
        )
    elif not cuit_valido(empresa_cuit):
        raise ClienteError(
            f"CUIT inválido: {empresa_cuit!r}. Son once dígitos, con o sin guiones."
        )

    _used = used_ports()
    port = int(port) if port else next_port(_used)
    if port in _used:
        log(f"[WARN] El puerto {port} ya está en uso.")

    if not admin_password:
        admin_password = secrets.token_urlsafe(12)
    admin_nombre = admin_nombre or nombre
    secret_key = secrets.token_hex(32)

    # — credencial del panel del cliente —
    #
    # 🔴 **Aleatoria y distinta en cada instancia, y ese es todo el punto.** No
    # sale del entorno como `LIBRA_SERVICE_TOKEN` (ver más abajo) ni se deriva
    # del `secret_key`: `LIBRA_SERVICE_TOKEN` es **por producto**. Medido el
    # 2026-08-20 en el VPS: `contalibra` y `contalibra-demo` comparten uno, y
    # `libradesk-lagrace` y `libradesk-compulibra` —dos CLIENTES distintos—
    # también. Correcto para lo que es, porque el backoffice del proveedor
    # administra todas las instancias de un producto; inservible acá, porque
    # esta credencial se le entrega al DUEÑO de unas sucursales y con un valor
    # compartido le abriría las de los demás.
    #
    # Se escribe SIEMPRE, no sólo si alguien la pidió. El guard de libraauth es
    # opt-in por ausencia: sin la variable, `/api/resumen` devuelve **401 sin
    # mirar el header**, y desde el panel eso se ve como "sin respuesta" —
    # indistinguible de una sucursal caída. Las dos instancias de Contalibra la
    # tienen porque se les puso a mano el 2026-08-20; esto es para que la
    # próxima no nazca necesitando esa visita. Que exista no expone nada: sin
    # el valor no se entra, y el valor sale de acá y del compose de la
    # instancia, de ningún otro lado.
    #
    # 64 hex y no `token_urlsafe`: viaja como header HTTP y los alfabetos con
    # `=` o `+` obligan a pensar en encoding cada vez que alguien la copia.
    panel_token = secrets.token_hex(32)

    # — contraseña de la casilla de correo saliente —
    #
    # Se genera acá, junto al resto de los secretos del alta, y no adentro de
    # `mail_cuentas`: que todas las credenciales de una instancia nueva salgan
    # del mismo lugar es lo que permite leer de un vistazo qué se le entrega.
    # `token_urlsafe` y no `token_hex` porque esta viaja como contraseña SASL,
    # no adentro de una URL ni de un header.
    smtp_password = secrets.token_urlsafe(24)

    # A partir de acá el alta escribe en disco, así que todo lo que sigue va
    # bajo rollback: si algo falla a mitad, `client_dir` se borra entero. Es
    # seguro borrarlo porque existe sólo porque lo creamos nosotros — el
    # chequeo de slug duplicado de más arriba garantiza que no había nada.
    #
    # La casilla de correo también entra al rollback, aunque no sea un archivo:
    # se lleva en `cuenta_de_correo` y el `except` de abajo la borra. Sin eso,
    # un alta que falla después de crearla deja una casilla huérfana con
    # credencial viva en el servidor de correo.
    cuenta_de_correo = None
    try:
        # — directorios —
        data_dir = client_dir / "data"
        for sub in ["logos", "arca_certs", "facturas_pdf", "remitos_pdf", "presupuestos_pdf"]:
            (data_dir / sub).mkdir(parents=True, exist_ok=True)
        log(f"[OK] Directorios en {client_dir}")

        # — config.json — (claves deben coincidir con _DEFAULTS en config_manager.py)
        #
        # El CUIT se guarda COMO LO ESCRIBIERON, con guiones o sin ellos. No se
        # normaliza acá porque cada consumidor ya lo hace a su manera y para lo
        # suyo (`arca_wsfe`, `pdf_generator` y `ticket_generator` le sacan los
        # guiones para el QR de ARCA), y el panel agrupa contra los dígitos
        # (`normalizar_cuit`). Normalizar en el alta no le ahorraría el paso a
        # ninguno y le cambiaría a la pantalla de Configuración lo que el humano
        # tipeó.
        config = {
            "empresa_nombre": empresa_nombre, "empresa_direccion": "",
            "empresa_telefono": "", "empresa_email": "",
            "empresa_cuit": empresa_cuit,
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

        # — credenciales del admin inicial, con el nombre que la app REALMENTE lee —
        #
        # 🔴 `ADMIN_USER`/`ADMIN_PASSWORD` no alcanzan. Los productos migrados a
        # `libraauth.bootstrap.ensure_default_admin(env_prefix=...)` leen
        # `<PREFIJO>_ADMIN_USERNAME` y `<PREFIJO>_ADMIN_PASSWORD`, y esa función
        # es **fail-closed**: sin la contraseña con el nombre prefijado la app no
        # arranca, tira `RuntimeError` y el contenedor entra en crash loop.
        #
        # Ya son cuatro los productos así (libradesk, gestiolibra, medlibra,
        # ventalibra). Las cinco instancias vivas de esos productos tienen las
        # dos formas —la genérica y la prefijada— porque **a cada alta se le
        # agregó la prefijada a mano**, sin que quedara escrito en ningún lado.
        # Encontrado el 2026-08-13 con el alta de `libradesk-lagrace`, que nadie
        # parchó y por eso no arrancaba.
        #
        # Se escriben las DOS: la genérica la siguen leyendo los productos que
        # todavía no migraron.
        prefijo_env = cfg.container_prefix.upper()
        admin_env = (
            f"      - {prefijo_env}_ADMIN_USERNAME={admin_user}\n"
            f"      - {prefijo_env}_ADMIN_PASSWORD={admin_password}\n"
        )

        # — token de servicio, para que el backoffice pueda administrar la instancia —
        #
        # Sin `LIBRA_SERVICE_TOKEN` en la instancia, `token_de_servicio_valido()`
        # de libraauth devuelve False **sin mirar el header** (es opt-in por
        # ausencia, a propósito), así que las pantallas de Usuarios y SMTP del
        # backoffice dan 401 contra una instancia recién creada.
        #
        # Sale del entorno del proceso que corre el alta —el backoffice, que ya
        # exige esta variable en sus settings— y por eso la instancia nace con el
        # mismo valor que él. Desde la CLI, sin la variable puesta, no se escribe
        # nada y la instancia se comporta como antes.
        token_servicio = os.environ.get("LIBRA_SERVICE_TOKEN", "").strip()
        token_env = (
            f"      - LIBRA_SERVICE_TOKEN={token_servicio}\n" if token_servicio else ""
        )
        # Sin `if`: a diferencia de la de servicio, esta no depende de que el
        # entorno del proceso que corre el alta tenga nada puesto.
        token_env += f"      - LIBRA_PANEL_TOKEN={panel_token}\n"

        # — casilla de correo saliente, una por instancia —
        #
        # Va ANTES de escribir el compose porque su credencial son seis
        # variables de ese archivo. La instancia nace mandando correo: sin
        # esto `/auth/forgot-password` contesta `503` hasta que alguien entre
        # al backoffice a cargarle un servidor, que es como está hoy el parque
        # entero.
        #
        # 🔴 **Un fallo acá NO aborta el alta**, igual que con NPM: una
        # instancia sin correo es una instancia a la que le falta una pantalla
        # de configuración, y la pantalla existe. Que el alta entera fallara
        # porque el servidor de correo no contesta sería peor que el problema.
        # `smtp_ok` viaja en la respuesta para que el backoffice lo pueda
        # mostrar en vez de que se descubra el día que un cliente pide un reset.
        smtp_env = ""
        smtp_ok = None
        if mail_cuentas.configurado():
            try:
                cuenta_de_correo = mail_cuentas.crear_cuenta(
                    slug, nombre, smtp_password
                )
                smtp_env = mail_cuentas.env_para_compose(cuenta_de_correo)
                smtp_ok = True
                # La dirección sí, la contraseña NO — este stream termina en la
                # respuesta del alta del backoffice y en los logs del contenedor.
                log(f"[OK] Casilla de correo: {cuenta_de_correo.direccion}")
            except mail_cuentas.MailError as e:
                log(f"[ERROR] Correo saliente: {e}")
                smtp_ok = False

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
        #
        # 🔴 EL HEALTHCHECK MIRA EL CUERPO, NO EL CÓDIGO HTTP, y la ruta sale de
        # `cfg.health_path`. Los seis productos sirven su SPA con un catch-all
        # (`/{full_path:path}` → index.html), así que **cualquier** ruta
        # devuelve 200 mientras uvicorn sirva estáticos: un `urlopen()` a secas
        # da `healthy` con la API muerta, y da `healthy` también apuntado a una
        # ruta inventada. Medido el 2026-08-12 por `docker exec` en los 21
        # contenedores del VPS: los 21 daban exit 0 contra una ruta que no
        # existe. Un chequeo que no puede fallar no es un chequeo.
        #
        # `json.load` sobre el index.html revienta, que es exactamente lo que se
        # busca. `isinstance(..., dict)` y no una clave concreta porque el
        # cuerpo difiere entre productos: `{"status": "ok"}` en Contalibra y
        # Restolibra, `{"ok": true, "product": …}` en el resto.
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
      test: ["CMD", "python3", "-c", "import json,urllib.request; assert isinstance(json.load(urllib.request.urlopen('http://localhost:8000{cfg.health_path}', timeout=3)), dict)"]
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
      - TZ={_TZ}
      - SECRET_KEY={secret_key}
      - ADMIN_USER={admin_user}
      - ADMIN_PASSWORD={admin_password}
{admin_env}      - ADMIN_NOMBRE={admin_nombre}
      - DOCS_AUTH_SECRET={cfg.docs_auth_secret}
{token_env}{smtp_env}{pg_env}{service_net}{pg_service}{top_net}{pg_volume}"""
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

        # — migraciones, ANTES del primer arranque —
        #
        # 🔴 Sin esto, el producto que declara `migraciones` planta una
        # bomba en cada alta. La instancia nueva nace con el esquema que arma
        # `Base.metadata.create_all()` al bootear —todas las tablas— y con la
        # tabla de versión de Alembic **vacía**. El primer
        # `panel_admin.py actualizar` que le toque arranca la cadena desde
        # `0001` y muere con `DuplicateTable` contra tablas que ella misma
        # creó, abortando el deploy.
        #
        # Correrlas acá hace que el esquema nazca de la MISMA fuente que lo va
        # a mantener, y deja la versión donde corresponde. El `create_all()`
        # del arranque pasa a ser un no-op para lo que las migraciones ya
        # crearon —sigue cubriendo lo que no está en ninguna cadena, como las
        # tablas de auth y auditoría—.
        #
        # Es la misma secuencia y el mismo orden que `cmd_actualizar`. Los
        # productos sin `migraciones` —cuatro de seis— no ven ningún paso
        # nuevo.
        for comando in cfg.migraciones:
            log(f"[*] Migraciones: {' '.join(comando)}")
            r = subprocess.run(
                ["docker", "compose", "run", "--rm", container, *comando],
                cwd=str(client_dir), capture_output=True, text=True)
            if r.returncode != 0:
                detalle = [ln.strip() for ln
                           in (r.stderr or r.stdout or "").splitlines() if ln.strip()]
                for linea in detalle:
                    log(linea)
                raise ClienteError(
                    f"Fallo `{' '.join(comando)}` al crear la instancia."
                    + (f" {detalle[-1]}" if detalle else "")
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
        # 🔴 **Que el plan no se aplique NO puede ser un `[WARN]`.** Cada producto
        # siembra su tabla `modulos` con un default, y ese default es el plan MÁS
        # ALTO con todo habilitado (`plan="premium"` en
        # `libradesk/app/services/modules.py`). O sea que cuando esta espera se
        # agota, la instancia no queda a medias: queda **en premium**, sin que
        # nadie lo haya decidido, y el alta devuelve éxito igual.
        #
        # Con `lagrace` no se notó porque el plan contratado ERA premium y
        # coincidió con el default. Un alta de plan básico que caiga por acá le
        # regala al cliente los diez módulos y no deja rastro.
        if cfg.usa_postgres:
            variable, base = cfg.db_urls[0]
            listo = _esperar_tabla_en_sidecar(
                f"{container}-postgres", base, cfg.container_prefix
            )
            if listo and _aplicar_plan_en_contenedor(container, variable, plan):
                log(f"[OK] Plan '{plan}' aplicado.")
            elif listo:
                raise AltaIncompleta(
                    f"La instancia '{slug}' se creó y su base está lista, pero no se "
                    f"pudo aplicar el plan '{plan}'. Los módulos quedaron en el "
                    "default del producto, que es el plan más alto con todo "
                    "habilitado. Aplicá el plan desde el backoffice antes de "
                    "entregarla."
                )
            else:
                raise AltaIncompleta(
                    f"La instancia '{slug}' se creó pero su base nunca se armó: "
                    f"la tabla de módulos no apareció. {_diagnostico_contenedor(container)}"
                )
        else:
            db_path = data_dir / cfg.db_filename
            if _esperar_db_lista(db_path):
                plans.aplicar_plan_en_db(str(db_path), plan)
                log(f"[OK] Plan '{plan}' aplicado.")
            else:
                raise AltaIncompleta(
                    f"La instancia '{slug}' se creó pero su base nunca se armó: "
                    f"no apareció {db_path}. {_diagnostico_contenedor(container)}"
                )

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
            "empresa_nombre": empresa_nombre, "empresa_cuit": empresa_cuit,
            # Misma semántica que `proxy_ok`: `None` = no se intentó (no hay
            # servidor de correo configurado en este entorno), `True` = la
            # casilla se creó y la instancia nace mandando correo, `False` = se
            # intentó y falló, así que la instancia existe pero no manda hasta
            # que alguien le cargue el SMTP por la pantalla del backoffice.
            "smtp_ok": smtp_ok,
            # La dirección no es secreta —es el remitente que va a ver todo el
            # que reciba un correo de esta instancia— y saberla es lo que
            # permite ir a buscar la casilla al servidor de correo. La
            # contraseña, en cambio, sale sólo por el compose, igual que
            # `panel_token`.
            "smtp_user": cuenta_de_correo.direccion if cuenta_de_correo else "",
            # 🔴 Vuelve por acá y **no se escribe en `cliente.json`**, a
            # diferencia de `admin_password`. Esa metadata la lee
            # `load_clients()` y viaja entera a quien pregunte por la instancia
            # —por eso el backoffice tiene un `response_model` que la filtra
            # campo por campo—; sumarle un secreto más es sumarle una forma de
            # filtrarse. Donde queda recuperable es en el `docker-compose.yml`
            # de la instancia, que es donde tiene que estar igual para que el
            # contenedor la lea.
            #
            # Y **nunca por `log()`**: en el backoffice ese stream termina en la
            # respuesta del alta y en los logs del contenedor. En la CLI la
            # imprime `main()`, en el mismo bloque final que la contraseña.
            "panel_token": panel_token,
        }
    except AltaIncompleta:
        # Sin rollback, a propósito — ver el docstring de `AltaIncompleta`. La
        # instancia queda creada y el error dice qué le falta. Va ANTES del
        # `except Exception` de abajo porque el orden de los `except` decide, y
        # `AltaIncompleta` ES un `ClienteError`.
        raise
    except Exception:
        # `Exception` y no `BaseException` a propósito: un Ctrl-C durante los
        # 25s que espera la DB llega con el contenedor ya arriba y sano, y
        # borrarlo ahí sería peor que dejarlo.
        _rollback_alta(client_dir, log)
        # La casilla se borra DESPUÉS del rollback del directorio, no antes: si
        # borrar la casilla fallara y esto se abortara ahí, quedaría el
        # directorio de un alta fallida con el slug tomado, que es el estado
        # peor. `_rollback_casilla` no levanta nada por su cuenta.
        _rollback_casilla(cuenta_de_correo, log)
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

    # La identidad fiscal, que es lo que el panel del dueño usa para agrupar por
    # razón social. Se pregunta acá y no "después, desde Configuración" porque
    # eso es exactamente lo que pasó con `contalibra-demo`, que hoy contesta
    # nombre y CUIT vacíos.
    empresa_nombre = ask("Razón social", nombre)
    empresa_cuit = ask("CUIT (Enter = es una demo, sin identidad fiscal)", "")
    sin_identidad = not empresa_cuit
    if sin_identidad and ask(
        "Sin CUIT el panel no la va a poder agrupar. ¿Seguir igual? [s/N]", "n"
    ).lower() != "s":
        sys.exit("Cancelado.")

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
    print(f"  Empresa:   {empresa_nombre}   CUIT: {empresa_cuit or '— sin identidad —'}")
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
            admin_nombre=admin_nombre, plan=plan,
            empresa_cuit=empresa_cuit, empresa_nombre=empresa_nombre,
            sin_identidad=sin_identidad,
            setup_npm=setup_npm, log=print,
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
    # La credencial con la que el panel del dueño le pide los números a esta
    # sucursal. Se imprime acá —y no por `log()`— porque este bloque es el canal
    # deliberado hacia el operador, el mismo por el que sale la contraseña.
    print(f"  Panel token: {info['panel_token']}")
    print("=" * 60)
    print("\n[!] Guardá las credenciales — no se volverán a mostrar.")
    print("[!] El panel token es el que va en el alta de esta sucursal en LibraPanel.")
