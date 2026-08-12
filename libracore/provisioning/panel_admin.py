"""
Panel de administración por línea de comandos: gestiona todos los
contenedores de clientes de un producto desde un menú interactivo o
comandos directos.

Requiere `libracore.provisioning.configure()` antes de usar cualquier
función de acá. `npm_api.py` (idéntico entre productos, pero vive en
`scripts/` de cada repo, no en LibraCore) se resuelve en tiempo de
ejecución vía import diferido — ver `libracore.provisioning._npm_api()`.
"""
import re
import sys
import json
import sqlite3
import subprocess
import tarfile
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import (
    get_config, _npm_api,
    check_venv_sync, build_image_tagged, deploy_version,
)

BACKUP_RETENTION_DIAS = 14


# ── helpers Docker ────────────────────────────────────────────────────────────

def docker(*args, capture=False, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args],
                          capture_output=capture, text=True, cwd=cwd)


def compose(slug: str, *args) -> subprocess.CompletedProcess:
    cfg = get_config()
    compose_file = cfg.clientes_dir / slug / "docker-compose.yml"
    return subprocess.run(
        ["docker", "compose", "-f", str(compose_file), *args],
        cwd=str(cfg.clientes_dir / slug),
    )


def container_status(container: str) -> dict:
    r = docker("inspect", container,
               "--format", "{{.State.Status}}|{{.State.StartedAt}}",
               capture=True)
    if r.returncode != 0:
        return {"status": "no encontrado", "started": ""}
    parts = r.stdout.strip().split("|")
    status  = parts[0] if parts else "?"
    started = parts[1][:19].replace("T", " ") if len(parts) > 1 else ""
    return {"status": status, "started": started}


# ── lectura de clientes ───────────────────────────────────────────────────────

def load_clients() -> list[dict]:
    cfg = get_config()
    clients = []
    if not cfg.clientes_dir.exists():
        return clients
    for d in sorted(cfg.clientes_dir.iterdir()):
        if not d.is_dir():
            continue
        meta_file = d / "cliente.json"
        if not meta_file.exists():
            continue
        meta = json.loads(meta_file.read_text())
        meta["slug"]      = d.name
        meta["container"] = meta.get("container", f"{cfg.container_prefix}-{d.name}")
        meta["dir"]       = d
        clients.append(meta)
    return clients


def find_client(slug: str) -> dict | None:
    for c in load_clients():
        if c["slug"] == slug:
            return c
    return None


# ── versión de imagen por cliente ─────────────────────────────────────────────
#
# Cada cliente pinea en su `docker-compose.yml` una versión concreta de la
# imagen (`contalibra:v2026.07.30-2110`), no `:latest`. El motivo es que
# `:latest` es un tag mutable: con dos clientes del mismo producto,
# actualizar a uno reconstruye la imagen que el otro también nombra, y el
# segundo se salta a ese código en su próximo `up -d` — sin que nadie haya
# pedido un deploy suyo. Con el pin, cada instancia se mueve solo cuando se
# la nombra, y volver atrás es repinear el tag anterior (ver `cmd_rollback`).

_IMAGE_LINE = re.compile(r"^(?P<pre>[ \t]*image:[ \t]*)(?P<ref>\S+)", re.MULTILINE)


def _compose_path(slug: str) -> Path:
    return get_config().clientes_dir / slug / "docker-compose.yml"


def leer_image_pineada(slug: str) -> str | None:
    """Referencia de imagen que declara hoy el compose del cliente, o None
    si el archivo no existe o no tiene una línea `image:`."""
    path = _compose_path(slug)
    try:
        contenido = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _IMAGE_LINE.search(contenido)
    return m.group("ref") if m else None


def pinear_image(slug: str, ref: str) -> str | None:
    """Reescribe la línea `image:` del compose del cliente y devuelve la
    referencia que había antes (None si no se pudo tocar el archivo).

    Se reemplaza solo la **primera** ocurrencia: el compose de un cliente
    declara un único servicio, y limitarlo evita que un `image:` que
    aparezca más abajo (un sidecar futuro) se pise sin querer."""
    path = _compose_path(slug)
    try:
        contenido = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _IMAGE_LINE.search(contenido)
    if not m:
        return None
    anterior = m.group("ref")
    if anterior != ref:
        nuevo = contenido[:m.start()] + m.group("pre") + ref + contenido[m.end():]
        path.write_text(nuevo, encoding="utf-8")
    return anterior


def _guardar_meta(slug: str, **campos):
    """Merge de campos en `cliente.json` (versión desplegada, anterior,
    fecha) conservando lo que ya estaba."""
    meta_file = get_config().clientes_dir / slug / "cliente.json"
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    meta.update(campos)
    meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def container_image(container: str) -> str | None:
    """Referencia de imagen con la que se creó el contenedor (el string,
    tal como lo nombró su compose)."""
    r = docker("inspect", container, "--format", "{{.Config.Image}}", capture=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def container_image_id(container: str) -> str | None:
    """ID (sha256) de la imagen que el contenedor está corriendo.

    Es lo que hace verificable el pin, y el string de `container_image()`
    no alcanza: un contenedor creado desde `:latest` sigue diciendo
    `producto:latest` aunque ese tag ya apunte a otra imagen. Comparar
    nombres ahí da un falso "todo en orden" — exactamente el caso que este
    cambio existe para evitar."""
    r = docker("inspect", container, "--format", "{{.Image}}", capture=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def image_id(ref: str) -> str | None:
    """ID (sha256) de una referencia de imagen presente en este host."""
    r = docker("image", "inspect", ref, "--format", "{{.Id}}", capture=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def versiones_disponibles() -> list[str]:
    """Tags versionados de la imagen del producto presentes en este host,
    del más nuevo al más viejo. `latest` queda afuera a propósito: no es
    una versión a la que se pueda volver, es un puntero móvil."""
    cfg = get_config()
    r = docker("images", cfg.image_repo, "--format", "{{.Tag}}", capture=True)
    if r.returncode != 0:
        return []
    tags = [t.strip() for t in r.stdout.splitlines() if t.strip()]
    return sorted({t for t in tags if t not in ("latest", "<none>")}, reverse=True)


# ── poda de imágenes de deploy ────────────────────────────────────────────────

# Tags de deploy que se conservan además del que corre. Dos alcanzan para
# volver atrás; el tercero es margen para cuando el rollback también falla.
IMAGE_RETENTION = 3

_TAG_DEPLOY = re.compile(r"^v\d{4}\.\d{2}\.\d{2}-\d{4}$")


def _es_tag_de_deploy(tag: str) -> bool:
    """Si el tag lo acuñó `deploy_version()` (`vYYYY.MM.DD-HHMM`).

    Los que no matchean se pusieron a mano —hitos de migración y puntos de
    rollback: `p7`, `pre-p8-cutover-rollback`, `pre-recibos-20260805-074659`—
    y la poda **no los toca**. Nadie los va a volver a generar: son el
    artefacto de un corte que ya pasó."""
    return bool(_TAG_DEPLOY.match(tag))


def _ids_de_imagen_en_uso() -> set[str]:
    """IDs de imagen que referencia algún contenedor, corriendo **o parado**.

    Un contenedor parado también retiene su imagen: si se la borráramos, un
    cliente pausado o suspendido se quedaría sin con qué arrancar."""
    r = docker("ps", "-a", "--format", "{{.Image}}", capture=True)
    if r.returncode != 0:
        return set()
    ids = set()
    for ref in {l.strip() for l in r.stdout.splitlines() if l.strip()}:
        iid = image_id(ref)
        if iid:
            ids.add(iid)
    return ids


def podar_imagenes_viejas(keep: int = IMAGE_RETENTION, *, dry_run: bool = False,
                          log=print) -> tuple[list[str], list[str]]:
    """Borra los tags de deploy viejos del producto activo. Devuelve
    `(borrados, conservados)`; con `dry_run` devuelve `(candidatos, conservados)`
    sin tocar nada.

    Existe porque **nada los borraba**: `deploy_version()` acuña un tag nuevo
    por deploy y ninguno se retiraba nunca. Medido en el VPS el 2026-08-07:
    24 tags de contalibra y 21 de restolibra, de 566 MB a 1 GB cada uno, con
    el disco al 75% — de los 75 GB usados, 63 eran imágenes y build cache.

    Conserva:
      - `latest`, que `build_image_tagged()` sigue moviendo a propósito;
      - los tags que no acuñó `deploy_version()` (hitos y rollbacks a mano);
      - los `keep` más nuevos;
      - los pineados en el compose de cualquier cliente, **aunque el cliente
        esté parado** — es justo el caso en que el pin es lo único que queda;
      - los que referencia algún contenedor.

    Usa `docker rmi` **sin** `-f`: si algo quedó reteniendo la imagen, Docker
    se niega y se reporta como conservada, en vez de romper por la fuerza."""
    cfg = get_config()
    pineados = {ref for ref in
                (leer_image_pineada(c["slug"]) for c in load_clients()) if ref}
    en_uso = _ids_de_imagen_en_uso()

    conservados: list[str] = []
    candidatos: list[str] = []
    recientes = 0
    for tag in versiones_disponibles():      # ya viene del más nuevo al más viejo
        ref = cfg.image_ref(tag)
        if not _es_tag_de_deploy(tag):
            conservados.append(f"{ref} (tag a mano: hito o rollback)")
        elif recientes < keep:
            recientes += 1
            conservados.append(f"{ref} (entre los {keep} más nuevos)")
        elif ref in pineados:
            conservados.append(f"{ref} (pineado en el compose de un cliente)")
        elif (iid := image_id(ref)) and iid in en_uso:
            conservados.append(f"{ref} (en uso por un contenedor)")
        else:
            candidatos.append(ref)

    if dry_run:
        return candidatos, conservados

    borrados: list[str] = []
    for ref in candidatos:
        if docker("rmi", ref, capture=True).returncode == 0:
            borrados.append(ref)
        else:
            conservados.append(f"{ref} (Docker se negó a borrarla)")
    return borrados, conservados


def cmd_podar_imagenes(keep: int = IMAGE_RETENTION, dry_run: bool = False):
    """Poda manual, para recuperar espacio sin esperar al próximo deploy."""
    candidatos, conservados = podar_imagenes_viejas(keep, dry_run=dry_run)
    for c in conservados:
        print(f"  conserva  {c}")
    if not candidatos:
        print(f"[OK] Nada que podar (se conservan los {keep} tags más nuevos).")
        return
    verbo = "Se borrarían" if dry_run else "Borrados"
    for ref in candidatos:
        print(f"  {'(simulacro)' if dry_run else 'borrado'}   {ref}")
    print(f"[OK] {verbo} {len(candidatos)} tag/s de deploy.")


# ── display ───────────────────────────────────────────────────────────────────

STATUS_COLOR = {
    "running":      "\033[32m●\033[0m",   # verde
    "exited":       "\033[31m●\033[0m",   # rojo
    "paused":       "\033[33m●\033[0m",   # amarillo
    "no encontrado":"\033[90m○\033[0m",   # gris
}

def _col(status: str) -> str:
    return STATUS_COLOR.get(status, "○")


def cmd_listar():
    clients = load_clients()
    if not clients:
        print("No hay clientes. Creá uno con: python3 scripts/nuevo_cliente.py")
        return
    fmt = "{:<3}  {}  {:<18}  {:>5}  {:<12}  {}"
    print(fmt.format("#", " ", "SLUG", "PORT", "ESTADO", "NOMBRE"))
    print("-" * 68)
    for i, c in enumerate(clients, 1):
        info   = container_status(c["container"])
        status = info["status"]
        print(fmt.format(
            i,
            _col(status),
            c["slug"][:18],
            c.get("port", "—"),
            status[:12],
            c.get("nombre", "")[:30],
        ))
    print()


def cmd_info(slug: str):
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return
    info = container_status(c["container"])
    print(f"\n  Nombre:      {c.get('nombre','')}")
    print(f"  Slug:        {c['slug']}")
    print(f"  Contenedor:  {c['container']}  [{info['status']}]")
    print(f"  Puerto:      {c.get('port','')}")
    print(f"  Dominio:     {c.get('domain','—') or '—'}")
    print(f"  Admin:       {c.get('admin_user','')}  /  {c.get('admin_password','')}")
    print(f"  Iniciado:    {info['started'] or '—'}")
    print(f"  Datos:       {c['dir'] / 'data'}\n")


def cmd_start(slug: str):
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return
    print(f"[*] Iniciando {c['container']} ...")
    compose(slug, "up", "-d")


def cmd_stop(slug: str):
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return
    print(f"[*] Deteniendo {c['container']} ...")
    compose(slug, "stop")


def cmd_restart(slug: str):
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return
    print(f"[*] Reiniciando {c['container']} ...")
    compose(slug, "restart")


def cmd_logs(slug: str, lines: int = 50):
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return
    print(f"[*] Últimas {lines} líneas de {c['container']} (Ctrl+C para salir):\n")
    try:
        subprocess.run(["docker", "logs", "--tail", str(lines), "-f", c["container"]])
    except KeyboardInterrupt:
        pass


def _backups_dir(c: dict) -> Path:
    d = c["dir"] / "backups"
    d.mkdir(exist_ok=True)
    return d


def _purge_backups_viejos(bdir: Path, patron: str, dias: int = BACKUP_RETENTION_DIAS):
    corte = datetime.now() - timedelta(days=dias)
    for f in bdir.glob(patron):
        if datetime.fromtimestamp(f.stat().st_mtime) < corte:
            f.unlink()


def _urls_postgres_del_contenedor(c: dict) -> list[str]:
    """**Todas** las bases PostgreSQL de una instancia, leidas de su contenedor.

    Es la unica fuente confiable: el compose de cada cliente vive solo en el VPS
    y puede nombrar la variable de distintas formas segun el producto
    (`DATABASE_URL`, `<PRODUCTO>_DATABASE_URL`, `<PRODUCTO>_DB_PATH`). Se busca
    por el VALOR -- que empiece con el esquema-- y no por el nombre.

    🔴 **Devuelve una lista, no la primera.** Gestiolibra y MedLibra tienen DOS
    bases: el dominio y LibraCore no pueden compartir schema porque los dos
    declaran una tabla `clients` con `id` de tipos incompatibles. Con la version
    anterior, que devolvia la primera, el backup del cron se llevaba una sola
    mitad -- y un backup con una mitad no se puede restaurar: o volves el
    dominio y te quedan usuarios de otro momento, o al reves.

    Se deduplica conservando el orden: la principal es la primera que aparece.
    """
    import subprocess

    r = subprocess.run(
        ["docker", "inspect", c["container"],
         "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    urls: list[str] = []
    for linea in r.stdout.splitlines():
        _, _, valor = linea.partition("=")
        if valor.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
            normal = valor.replace("postgresql+psycopg://", "postgresql://", 1)
            if normal not in urls:
                urls.append(normal)
    return urls


def _dump_postgres_por_docker(url: str, destino) -> None:
    """`pg_dump` corrido DENTRO del sidecar, y el archivo traido con `docker cp`.

    🔴 **No se puede dumpear desde el host.** El sidecar no publica puerto -- a
    proposito, publicar 5432 en un VPS es publicarlo a Internet-- y su nombre es
    un alias de la red de Docker, asi que desde afuera no resuelve:
    *"could not translate host name ... to address"*. Medido el 2026-08-10 con
    la demo de Contalibra.

    Ademas el host no tiene por que tener `pg_dump`, y menos con la version
    correcta; adentro del sidecar es la misma que sirve la base, por definicion.

    **El destino se escribe recien cuando el dump salio bien.** El primer
    intento dejo un `.dump` de 0 bytes al fallar: un archivo con nombre de
    backup y sin nada adentro es peor que ningun archivo, porque la rotacion lo
    cuenta y alguien puede creer que tiene una copia.
    """
    import tempfile

    from ..respaldo import BackupInvalido

    sin_usuario = url.split("@", 1)[-1]
    sidecar = sin_usuario.split(":", 1)[0].split("/", 1)[0]
    base = url.rsplit("/", 1)[-1].split("?")[0]

    dentro = f"/tmp/backup_{base}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.dump"
    r = docker(
        "exec", sidecar, "sh", "-c",
        f'pg_dump --format=custom --no-owner --no-privileges '
        f'-U "$POSTGRES_USER" -d "{base}" --file "{dentro}"',
        capture=True,
    )
    if r.returncode != 0:
        raise BackupInvalido(
            f"No se pudo hacer el backup de la base PostgreSQL: pg_dump dentro de "
            f"'{sidecar}' termino con codigo {r.returncode}. {(r.stderr or '').strip()[:300]}"
        )

    with tempfile.TemporaryDirectory() as tmp:
        provisorio = Path(tmp) / "dump"
        r = docker("cp", f"{sidecar}:{dentro}", str(provisorio), capture=True)
        docker("exec", sidecar, "rm", "-f", dentro, capture=True)
        if r.returncode != 0 or not provisorio.exists():
            raise BackupInvalido(
                f"El dump se hizo pero no se pudo traer del contenedor: "
                f"{(r.stderr or '').strip()[:300]}"
            )
        if provisorio.stat().st_size == 0:
            raise BackupInvalido("El dump salio vacio (0 bytes). No se guarda.")
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(provisorio), str(destino))


def _directorios_de_datos(data_dir: Path) -> list[Path]:
    """Las carpetas de `data/` que entran al ZIP: todas menos `backups/`.

    Todas y no una lista fija (`logos`, `arca_certs`, ...) porque este código es
    de los seis productos y cada uno guarda cosas distintas ahí — MedLibra
    documentos clínicos, Contalibra los certificados de ARCA. Una lista fija se
    desactualiza en silencio el día que un producto agrega una carpeta, y el
    backup sale sin ella sin que nada falle.

    🔴 **`backups/` afuera, y no es un detalle de prolijidad**: es donde queda
    este mismo ZIP. Incluirla haría que cada backup se llevara adentro a los
    diez anteriores, creciendo en cascada. Es la versión ordenada del problema
    que ya tenía el `tar.gz`, que empaquetaba `data/` entero.
    """
    return sorted(
        d for d in data_dir.iterdir() if d.is_dir() and d.name != "backups"
    )


def _instancia_del_cliente(c: dict, cfg, urls_postgres: list[str]) -> "Instancia":
    """Traduce un cliente del panel a lo que `libracore.respaldo` sabe respaldar.

    El `nombre` sale de `cfg.container_prefix` **a propósito**: es el mismo que
    el producto le pasa a `Instancia(nombre=...)` en su `main.py`, y de él sale
    el nombre del dump dentro del ZIP. Si los dos no coincidieran, el ZIP del
    cron no pasaría la validación de la pantalla al restaurar — diría que "el
    backup es de otro sistema" — y esa es justamente la única cosa que este
    cambio vino a garantizar.
    """
    from ..respaldo import Instancia

    data_dir = c["dir"] / "data"
    directorios = _directorios_de_datos(data_dir)
    if urls_postgres:
        return Instancia(
            nombre=cfg.container_prefix,
            postgres_url=urls_postgres[0],
            postgres_extra=urls_postgres[1:],
            directorios=directorios,
        )
    return Instancia(
        nombre=cfg.container_prefix,
        bases=[data_dir / cfg.db_filename],
        directorios=directorios,
    )


def _backup_zip(c: dict, cfg, urls_postgres: list[str], _p) -> None:
    """El backup del cron, en el MISMO formato que el de la pantalla.

    Ver `ProductConfig.backup_zip` para por qué esto convive con el camino
    viejo en vez de reemplazarlo de una.
    """
    from ..respaldo import crear_backup, verificar_backup

    instancia = _instancia_del_cliente(c, cfg, urls_postgres)
    destino_dir = c["dir"] / "data" / "backups"
    destino = crear_backup(
        instancia, destino_dir, motivo="automatico",
        # Desde el host `pg_dump` no llega al sidecar: no publica puerto y su
        # nombre es un alias de la red de Docker. Ver `_dump_postgres_por_docker`.
        dump_fn=_dump_postgres_por_docker if urls_postgres else None,
    )
    # Que `crear_backup` no haya fallado NO alcanza — ver `verificar_backup`.
    detalle = verificar_backup(destino, instancia)
    _p(f"[OK] Backup: {destino}  ({detalle['tamano_mb']} MB)")
    for nombre, tam in sorted(detalle["bases"].items()):
        _p(f"     base {nombre}: {tam / 1024:.0f} KB")


def cmd_backup(slug: str, quiet: bool = False):
    """Backup de una instancia. Pensado para el cron (ver `backup-all`).

    **Con `backup_zip=True` arma el mismo ZIP que la pantalla de Backups del
    producto**, en `data/backups/`. Ese es el camino nuevo y el que deberían
    terminar usando los seis: un solo artefacto, una sola retención (la de
    `respaldo.MAX_BACKUPS`), y lo que se respalda de noche es exactamente lo que
    el cliente puede listar, bajar y restaurar solo.

    🔴 **Sin ese flag hace lo de siempre, que tiene un problema medido.** El
    `tar.gz` empaqueta `data/`, pero el dump de PostgreSQL se escribe en
    `clientes/<slug>/backups/`, que está **afuera**. Medido el 2026-08-12 sobre
    las nueve instancias del VPS: en cinco de los seis productos el tar nocturno
    traía los `.db` de SQLite congelados en el corte a PostgreSQL y **ningún
    dump**. Los datos no estaban en riesgo —el dump existe, al lado— pero el
    archivo que parece el backup de la instancia no lo es.

    El camino viejo (tar.gz + copia WAL-safe vía la Online Backup API de
    sqlite3, purgando a los `BACKUP_RETENTION_DIAS`) se conserva mientras
    Contalibra y Restolibra no migren su pantalla al motor."""
    cfg = get_config()

    def _p(*a):
        if not quiet:
            print(*a)

    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return
    data_dir = c["dir"] / "data"
    if not data_dir.exists():
        print(f"[ERROR] No existe {data_dir}")
        return
    # `docker inspect` una sola vez: lo necesitan los dos caminos.
    urls_postgres = _urls_postgres_del_contenedor(c)

    if cfg.backup_zip:
        _p(f"[*] Creando backup de {slug} ...")
        _backup_zip(c, cfg, urls_postgres, _p)
        return

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = cfg.clientes_dir / f"{slug}_backup_{ts}.tar.gz"
    _p(f"[*] Creando backup completo de {slug} ...")
    with tarfile.open(out_file, "w:gz") as tar:
        tar.add(data_dir, arcname=f"{slug}/data")
    size_mb = out_file.stat().st_size / 1_048_576
    _p(f"[OK] Backup tar.gz: {out_file}  ({size_mb:.1f} MB)")
    _purge_backups_viejos(cfg.clientes_dir, f"{slug}_backup_*.tar.gz")

    # --- La base -----------------------------------------------------------
    #
    # 🔴 Hasta el 2026-08-10 esto era `if db_src.exists():` y nada mas. Con la
    # instancia migrada a PostgreSQL ese archivo NO existe, asi que el `if` se
    # saltaba la base **en silencio**: el cron nocturno dejaba un `tar.gz` con
    # los logos y los adjuntos, sin datos, y escribia `[OK]`. Un backup que
    # miente es peor que un backup que falta, y este corre todas las noches
    # sobre instancias de clientes.
    #
    # Ahora hay tres caminos y ninguno es silencioso: PostgreSQL -> `pg_dump`,
    # archivo SQLite -> copia WAL-safe, y **nada de eso -> error**.
    db_src = data_dir / cfg.db_filename
    bdir = _backups_dir(c)
    if urls_postgres:
        # Una por base. Gestiolibra y MedLibra tienen dos, y con una sola el
        # backup no se puede restaurar.
        for i, url in enumerate(urls_postgres):
            base = url.rsplit("/", 1)[-1].split("?")[0]
            sufijo = "" if i == 0 else f"_{base}"
            db_dst = bdir / f"{cfg.container_prefix}{sufijo}_{ts}.dump"
            _dump_postgres_por_docker(url, db_dst)
            _p(f"[OK] Dump PostgreSQL ({base}): {db_dst}  "
               f"({db_dst.stat().st_size/1_048_576:.1f} MB)")
        _purge_backups_viejos(bdir, f"{cfg.container_prefix}_*.dump")
    elif db_src.exists():
        # Copia WAL-safe de la DB, vía sqlite3.Connection.backup() (Online
        # Backup API de SQLite) en vez de shutil.copy2 crudo del archivo.
        db_dst = bdir / f"{cfg.container_prefix}_{ts}.db"
        src_conn = sqlite3.connect(str(db_src))
        dst_conn = sqlite3.connect(str(db_dst))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            src_conn.close()
            dst_conn.close()
        _p(f"[OK] Copia DB (WAL-safe): {db_dst}  ({db_dst.stat().st_size/1_048_576:.1f} MB)")
        _purge_backups_viejos(bdir, f"{cfg.container_prefix}_*.db")
    else:
        print(f"[ERROR] {slug}: no hay base que respaldar. No existe "
              f"{db_src} y el contenedor no declara una URL PostgreSQL. "
              f"El tar.gz quedo hecho pero **no tiene la base**.")


def cmd_backup_all():
    """Backup de todos los clientes activos — pensado para cron diario."""
    clientes = load_clients()
    if not clientes:
        print("[*] Sin clientes para respaldar.")
        return
    for c in clientes:
        print(f"[*] Backup de '{c['slug']}' ...")
        try:
            cmd_backup(c["slug"], quiet=True)
            print(f"[OK] '{c['slug']}' respaldado.")
        except Exception as e:
            print(f"[ERROR] Falló el backup de '{c['slug']}': {e}")


def cmd_list_backups(slug: str):
    """Lista los backups de DB disponibles para el cliente."""
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return
    bdir = _backups_dir(c)
    dbs  = sorted(bdir.glob("*.db"), reverse=True)
    if not dbs:
        print(f"  Sin backups de DB en {bdir}")
        return
    print(f"\n  Backups disponibles para '{slug}':")
    print(f"  {'#':<3}  {'ARCHIVO':<35}  {'TAMAÑO':>8}  FECHA")
    print("  " + "-" * 70)
    for i, f in enumerate(dbs, 1):
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        size  = f"{f.stat().st_size/1_048_576:.1f} MB"
        print(f"  {i:<3}  {f.name:<35}  {size:>8}  {mtime}")
    print()


def cmd_restore_db(slug: str, backup_file: str | None = None):
    """Restaura la DB de un cliente desde un backup. Para el contenedor durante el proceso."""
    cfg = get_config()
    import sqlite3 as _sq3
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return

    # Si no se indicó archivo, mostrar lista y pedir selección
    if not backup_file:
        bdir = _backups_dir(c)
        dbs  = sorted(bdir.glob("*.db"), reverse=True)
        if not dbs:
            print(f"[ERROR] No hay backups disponibles en {bdir}")
            print(f"  Creá uno con: python3 scripts/panel_admin.py backup {slug}")
            return
        cmd_list_backups(slug)
        sel = input("Número de backup a restaurar (Enter para cancelar): ").strip()
        if not sel or not sel.isdigit():
            print("Cancelado.")
            return
        idx = int(sel) - 1
        if not (0 <= idx < len(dbs)):
            print("[ERROR] Número fuera de rango.")
            return
        backup_path = dbs[idx]
    else:
        backup_path = Path(backup_file)
        if not backup_path.exists():
            # Buscar por nombre en el directorio de backups
            backup_path = _backups_dir(c) / backup_file
        if not backup_path.exists():
            print(f"[ERROR] No se encontró el archivo: {backup_file}")
            return

    # Validar que es SQLite
    try:
        with open(backup_path, "rb") as f:
            magic = f.read(16)
        if not magic.startswith(b"SQLite format 3\x00"):
            print("[ERROR] El archivo no es una base de datos SQLite válida.")
            return
        conn = _sq3.connect(str(backup_path))
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        if result != "ok":
            print(f"[ERROR] Integridad fallida: {result}")
            return
    except Exception as e:
        print(f"[ERROR] No se pudo validar el backup: {e}")
        return

    confirm = input(f"¿Restaurar '{backup_path.name}' en '{slug}'? Se reemplazarán TODOS los datos. [S/n]: ").strip().lower()
    if confirm == "n":
        print("Cancelado.")
        return

    db_dest = c["dir"] / "data" / cfg.db_filename

    # Parar contenedor
    info = container_status(c["container"])
    was_running = info["status"] == "running"
    if was_running:
        print(f"[*] Deteniendo {c['container']} ...")
        compose(slug, "stop")

    # Backup automático de la DB actual
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = _backups_dir(c)
    auto = bdir / f"antes_restore_{ts}.db"
    if db_dest.exists():
        shutil.copy2(db_dest, auto)
        print(f"[OK] Backup automático guardado: {auto.name}")

    # Reemplazar DB y limpiar WAL
    shutil.copy2(backup_path, db_dest)
    for ext in ("-wal", "-shm"):
        wal = Path(str(db_dest) + ext)
        if wal.exists():
            wal.unlink()
    print(f"[OK] DB restaurada desde: {backup_path.name}")

    # Reiniciar si estaba corriendo
    if was_running:
        print(f"[*] Reiniciando {c['container']} ...")
        compose(slug, "up", "-d")
        print("[OK] Contenedor reiniciado.")


def cmd_actualizar(slugs: list[str] | None = None, version: str | None = None):
    """Construye una **versión nueva** de la imagen y mueve a ella los
    contenedores indicados (o todos), repineando el compose de cada uno.

    Un cliente que no esté corriendo se saltea **sin repinear**: queda en
    la versión que ya tenía, así que arrancarlo más tarde no lo salta a
    código que no se desplegó para él."""
    cfg = get_config()
    aviso = check_venv_sync(cfg.repo_root)
    if aviso:
        print(aviso)

    version = version or deploy_version()
    ref = cfg.image_ref(version)
    if not build_image_tagged(version):
        print("[ERROR] Falló el build.")
        return
    print(f"[OK] Imagen {ref} construida.")

    clients = load_clients()
    targets = [c for c in clients if (not slugs or c["slug"] in slugs)]
    if not targets:
        print(f"[INFO] Sin contenedores que actualizar (imagen {ref} disponible).")
        return

    for c in targets:
        slug = c["slug"]
        info = container_status(c["container"])
        if info["status"] != "running":
            print(f"[SKIP] {c['container']} no está en ejecución — sigue pineado "
                  f"en {leer_image_pineada(slug) or '?'}.")
            continue

        anterior = pinear_image(slug, ref)
        if anterior is None:
            print(f"[WARN] No se pudo pinear la versión en el compose de '{slug}' "
                  "(sin línea `image:`) — se actualiza igual, pero sin pin.")
        print(f"[*] Actualizando {c['container']} → {ref} ...")
        r = compose(slug, "up", "-d")
        if r.returncode != 0:
            if anterior:
                pinear_image(slug, anterior)
                print(f"[ERROR] Falló el arranque de {c['container']}. "
                      f"Compose repineado a {anterior} (no se aplicó el cambio).")
            else:
                print(f"[ERROR] Falló el arranque de {c['container']}.")
            continue
        _guardar_meta(slug, version_desplegada=version,
                      version_anterior=anterior, desplegado_at=datetime.now().isoformat(timespec="seconds"))

    # Se poda al final y nunca antes: si el deploy falló, la imagen vieja es
    # justo a la que hay que poder volver.
    borrados, _ = podar_imagenes_viejas()
    if borrados:
        print(f"[OK] Poda: {len(borrados)} tag/s de deploy viejos borrados "
              f"(se conservan los {IMAGE_RETENTION} más nuevos, los pineados "
              "y los de rollback).")
    print("[OK] Actualización completa.")


def cmd_versiones():
    """Qué versión tiene pineada cada cliente y cuál está corriendo de
    verdad. Las dos columnas existen porque son cosas distintas: el compose
    es la intención, `docker inspect` es el hecho."""
    cfg = get_config()
    clients = load_clients()
    if not clients:
        print("No hay clientes.")
        return
    fmt = "{:<18}  {:<24}  {:<24}  {}"
    print(fmt.format("SLUG", "PINEADO (compose)", "CORRIENDO", "ESTADO"))
    print("-" * 88)
    sin_pin, desfasados = [], []
    for c in clients:
        slug      = c["slug"]
        pineado   = leer_image_pineada(slug) or "—"
        corriendo = container_image(c["container"]) or "—"
        estado    = container_status(c["container"])["status"]
        marca     = ""
        # El desfasaje se decide por ID, no por nombre: dos contenedores
        # pueden decir `producto:latest` y estar corriendo imágenes
        # distintas.
        id_pineado   = image_id(pineado) if pineado != "—" else None
        id_corriendo = container_image_id(c["container"])
        if pineado.endswith(":latest"):
            marca = "  ⚠ sin pin"
            sin_pin.append(slug)
        elif id_pineado and id_corriendo and id_pineado != id_corriendo:
            marca = "  ⚠ desfasado"
            desfasados.append(slug)
        print(fmt.format(slug[:18], pineado[:24], corriendo[:24], estado + marca))
    print()
    for slug in sin_pin:
        print(f"  ⚠ '{slug}': sigue en `:latest` — corré `actualizar {slug}` para pinearlo.")
    for slug in desfasados:
        print(f"  ⚠ '{slug}': el compose pinea una versión que el contenedor no está "
              "corriendo — le falta un `up -d`.")
    disponibles = versiones_disponibles()
    if disponibles:
        print(f"\n  Versiones de {cfg.image_repo} en este host: {', '.join(disponibles[:8])}"
              + (" ..." if len(disponibles) > 8 else ""))
    print()


def cmd_rollback(slug: str, version: str | None = None):
    """Devuelve un cliente a una versión anterior de la imagen: repinea su
    compose y lo levanta. No toca sus datos — si el deploy que se está
    revirtiendo migró la base, hay que restaurar el backup aparte
    (`restore-db`)."""
    cfg = get_config()
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return

    disponibles = versiones_disponibles()
    if not version:
        version = c.get("version_anterior") or ""
        # `version_anterior` se guarda como referencia completa
        # (`contalibra:v...`), que es lo que se leyó del compose.
        if version.startswith(f"{cfg.image_repo}:"):
            version = version.split(":", 1)[1]
        if not version:
            if not disponibles:
                print(f"[ERROR] No hay versiones de {cfg.image_repo} en este host.")
                return
            print(f"\n  Versiones disponibles de {cfg.image_repo}:")
            for i, v in enumerate(disponibles, 1):
                print(f"  {i:<3}  {v}")
            sel = input("\nNúmero o versión (Enter para cancelar): ").strip()
            if not sel:
                print("Cancelado.")
                return
            version = disponibles[int(sel) - 1] if sel.isdigit() and 1 <= int(sel) <= len(disponibles) else sel

    if disponibles and version not in disponibles:
        print(f"[ERROR] La versión '{version}' no está construida en este host.")
        print(f"  Disponibles: {', '.join(disponibles)}")
        return

    ref    = cfg.image_ref(version)
    actual = leer_image_pineada(slug) or "?"
    if actual == ref:
        print(f"[INFO] '{slug}' ya está en {ref}.")
        return

    confirm = input(f"¿Volver '{slug}' de {actual} a {ref}? [S/n]: ").strip().lower()
    if confirm == "n":
        print("Cancelado.")
        return

    anterior = pinear_image(slug, ref)
    if anterior is None:
        print(f"[ERROR] No se pudo reescribir el compose de '{slug}'.")
        return
    r = compose(slug, "up", "-d")
    if r.returncode != 0:
        pinear_image(slug, anterior)
        print(f"[ERROR] Falló el arranque. Compose repineado a {anterior}.")
        return
    _guardar_meta(slug, version_desplegada=version, version_anterior=anterior,
                  desplegado_at=datetime.now().isoformat(timespec="seconds"))
    print(f"[OK] '{slug}' corriendo en {ref}.")


def _set_servicio_estado(slug: str, estado: str, mensaje: str = ""):
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return False
    config_path = c["dir"] / "data" / "config.json"
    if not config_path.exists():
        print(f"[ERROR] No existe {config_path}. ¿El contenedor fue iniciado alguna vez?")
        return False
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cfg["servicio_estado"]   = estado
    cfg["servicio_mensaje"]  = mensaje
    config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def cmd_activar(slug: str):
    if _set_servicio_estado(slug, "activo", ""):
        print(f"[OK] Servicio de '{slug}' → ACTIVO.")


def cmd_pausar(slug: str):
    mensaje = input("Mensaje para el cliente (Enter para omitir): ").strip()
    if _set_servicio_estado(slug, "pausado", mensaje):
        print(f"[OK] Servicio de '{slug}' → PAUSADO (banner de aviso visible).")


def cmd_suspender(slug: str):
    mensaje = input("Mensaje para el cliente (Enter para usar el predeterminado): ").strip()
    confirm = input(f"¿Suspender acceso completo a '{slug}'? [S/n]: ").strip().lower()
    if confirm == "n":
        print("Cancelado.")
        return
    if _set_servicio_estado(slug, "suspendido", mensaje):
        print(f"[OK] Servicio de '{slug}' → SUSPENDIDO (sin acceso al sistema).")


def cmd_estado_servicio(slug: str):
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return
    config_path = c["dir"] / "data" / "config.json"
    if not config_path.exists():
        print("  Estado: desconocido (config.json no encontrado)")
        return
    cfg    = json.loads(config_path.read_text(encoding="utf-8"))
    estado = cfg.get("servicio_estado", "activo")
    msg    = cfg.get("servicio_mensaje", "")
    color  = {"activo": "\033[32m", "pausado": "\033[33m", "suspendido": "\033[31m"}.get(estado, "")
    reset  = "\033[0m"
    print(f"\n  Estado del servicio:  {color}{estado.upper()}{reset}")
    if msg:
        print(f"  Mensaje:              {msg}")
    print()


def cmd_eliminar(slug: str):
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return
    confirm = input(f"¿Eliminar PERMANENTEMENTE al cliente '{slug}' y todos sus datos? [escribí el slug para confirmar]: ").strip()
    if confirm != slug:
        print("Cancelado.")
        return
    print(f"[*] Deteniendo y eliminando {c['container']} ...")
    compose(slug, "down", "-v")
    shutil.rmtree(c["dir"])
    print(f"[OK] Cliente '{slug}' eliminado.")


# ── NPM proxy ─────────────────────────────────────────────────────────────────

def _npm_client():
    npm_mod = _npm_api()
    if not npm_mod:
        print("[ERROR] npm_api.py no disponible.")
        return None, None
    npm = npm_mod.client_from_config()
    if not npm:
        print("[ERROR] NPM no configurado. Ejecutá: python3 scripts/npm_setup.py")
        return None, None
    return npm, npm_mod


def cmd_npm_crear(slug: str):
    cfg = get_config()
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return
    domain = c.get("domain", "")
    if not domain:
        domain = input("Dominio para este cliente: ").strip()
        if not domain:
            print("Cancelado.")
            return
    npm, npm_mod = _npm_client()
    if not npm:
        return
    fwd_host = npm_mod.forward_host_from_config()
    port     = c.get("port", cfg.base_port)
    le_email = npm_mod.le_email_from_config()
    print(f"[*] Creando proxy: {domain} → {fwd_host}:{port} ...")
    try:
        existing = npm.get_proxy_host_by_domain(domain)
        if existing:
            print(f"[WARN] Ya existe proxy para {domain} (id={existing['id']}).")
            return
        host = npm.create_proxy_host(
            domain=domain, forward_host=fwd_host,
            forward_port=port, ssl=True, le_email=le_email,
        )
        print(f"[OK] Proxy creado (id={host['id']}) con SSL Let's Encrypt.")
    except npm_mod.NPMError as e:
        print(f"[ERROR] {e}")


def cmd_npm_eliminar(slug: str):
    c = find_client(slug)
    if not c:
        print(f"[ERROR] Cliente '{slug}' no encontrado.")
        return
    domain = c.get("domain", "")
    if not domain:
        print("[INFO] Este cliente no tiene dominio registrado.")
        return
    npm, npm_mod = _npm_client()
    if not npm:
        return
    try:
        host = npm.get_proxy_host_by_domain(domain)
        if not host:
            print(f"[INFO] No se encontró proxy para {domain} en NPM.")
            return
        ok = npm.delete_proxy_host(host["id"])
        print(f"[OK] Proxy eliminado (id={host['id']})." if ok else "[ERROR] No se pudo eliminar.")
    except npm_mod.NPMError as e:
        print(f"[ERROR] {e}")


def cmd_npm_listar():
    npm, npm_mod = _npm_client()
    if not npm:
        return
    try:
        hosts = npm.list_proxy_hosts()
        if not hosts:
            print("No hay proxy hosts en NPM.")
            return
        fmt = "{:<5}  {:<35}  {:>6}  {}"
        print(fmt.format("ID", "DOMINIO", "PORT", "SSL"))
        print("-" * 60)
        for h in hosts:
            domains   = ", ".join(h.get("domain_names", []))
            port      = h.get("forward_port", "")
            ssl_id    = h.get("certificate_id", 0)
            ssl_label = "✓" if ssl_id and ssl_id != 0 else "—"
            print(fmt.format(h["id"], domains[:35], port, ssl_label))
    except npm_mod.NPMError as e:
        print(f"[ERROR] {e}")


# ── menú interactivo ──────────────────────────────────────────────────────────

def pick_client(prompt: str) -> str | None:
    clients = load_clients()
    if not clients:
        print("No hay clientes registrados.")
        return None
    cmd_listar()
    val = input(f"{prompt} (número o slug): ").strip()
    if not val:
        return None
    if val.isdigit():
        idx = int(val) - 1
        if 0 <= idx < len(clients):
            return clients[idx]["slug"]
        print("[ERROR] Número fuera de rango.")
        return None
    if any(c["slug"] == val for c in clients):
        return val
    print(f"[ERROR] Slug '{val}' no encontrado.")
    return None


def _menu() -> str:
    cfg = get_config()
    # Ancho interior de la caja = 30 (mismo tamaño que la versión original,
    # de un único producto); "  " son los 2 espacios iniciales fijos.
    titulo = f"{cfg.product_name} — Panel Admin"
    return f"""
╔══════════════════════════════╗
║  {titulo:<28}║
╠══════════════════════════════╣
║  1  Listar clientes          ║
║  2  Info de un cliente       ║
║  3  Iniciar contenedor       ║
║  4  Detener contenedor       ║
║  5  Reiniciar contenedor     ║
║  6  Ver logs                 ║
║  7  Backup completo          ║
║  8  Actualizar imagen        ║
║  9  Eliminar cliente         ║
╠══════════════════════════════╣
║  rb Restaurar DB             ║
║  lb Listar backups DB        ║
╠══════════════════════════════╣
║  v  Versiones desplegadas    ║
║  rv Rollback de versión      ║
╠══════════════════════════════╣
║  sa Activar servicio         ║
║  sp Pausar servicio          ║
║  ss Suspender servicio       ║
║  se Estado del servicio      ║
╠══════════════════════════════╣
║  p  Proxies NPM (listar)     ║
║  pa Crear proxy NPM          ║
║  pd Eliminar proxy NPM       ║
╠══════════════════════════════╣
║  0  Salir                    ║
╚══════════════════════════════╝"""


def interactive():
    while True:
        print(_menu())
        opt = input("Opción: ").strip()
        print()

        if opt == "0":
            break
        elif opt == "1":
            cmd_listar()
        elif opt == "2":
            slug = pick_client("Cliente")
            if slug:
                cmd_info(slug)
        elif opt == "3":
            slug = pick_client("Iniciar cliente")
            if slug:
                cmd_start(slug)
        elif opt == "4":
            slug = pick_client("Detener cliente")
            if slug:
                cmd_stop(slug)
        elif opt == "5":
            slug = pick_client("Reiniciar cliente")
            if slug:
                cmd_restart(slug)
        elif opt == "6":
            slug = pick_client("Ver logs de")
            if slug:
                lines = input("Últimas N líneas [50]: ").strip()
                cmd_logs(slug, int(lines) if lines.isdigit() else 50)
        elif opt == "7":
            slug = pick_client("Backup de")
            if slug:
                cmd_backup(slug)
        elif opt == "rb":
            slug = pick_client("Restaurar DB de")
            if slug:
                cmd_restore_db(slug)
        elif opt == "lb":
            slug = pick_client("Listar backups de")
            if slug:
                cmd_list_backups(slug)
        elif opt == "v":
            cmd_versiones()
        elif opt == "rv":
            slug = pick_client("Rollback de")
            if slug:
                cmd_rollback(slug)
        elif opt == "8":
            slugs_input = input("Slugs a actualizar (Enter = todos): ").strip()
            slugs = slugs_input.split() if slugs_input else None
            cmd_actualizar(slugs)
        elif opt == "9":
            slug = pick_client("Eliminar cliente")
            if slug:
                cmd_eliminar(slug)
        elif opt == "sa":
            slug = pick_client("Activar servicio de")
            if slug:
                cmd_activar(slug)
        elif opt == "sp":
            slug = pick_client("Pausar servicio de")
            if slug:
                cmd_pausar(slug)
        elif opt == "ss":
            slug = pick_client("Suspender servicio de")
            if slug:
                cmd_suspender(slug)
        elif opt == "se":
            slug = pick_client("Ver estado de")
            if slug:
                cmd_estado_servicio(slug)
        elif opt == "p":
            cmd_npm_listar()
        elif opt == "pa":
            slug = pick_client("Crear proxy para cliente")
            if slug:
                cmd_npm_crear(slug)
        elif opt == "pd":
            slug = pick_client("Eliminar proxy de cliente")
            if slug:
                cmd_npm_eliminar(slug)
        else:
            print("Opción no válida.")

        input("\n[Enter para continuar]")


# ── CLI directo ───────────────────────────────────────────────────────────────

def cli():
    args = sys.argv[1:]
    if not args:
        interactive()
        return

    cmd  = args[0]
    slug = args[1] if len(args) > 1 else None

    dispatch = {
        "listar":     lambda: cmd_listar(),
        "info":       lambda: cmd_info(slug) if slug else print("Uso: panel_admin.py info <slug>"),
        "start":      lambda: cmd_start(slug) if slug else print("Uso: panel_admin.py start <slug>"),
        "stop":       lambda: cmd_stop(slug) if slug else print("Uso: panel_admin.py stop <slug>"),
        "restart":    lambda: cmd_restart(slug) if slug else print("Uso: panel_admin.py restart <slug>"),
        "logs":       lambda: cmd_logs(slug) if slug else print("Uso: panel_admin.py logs <slug>"),
        "backup":       lambda: cmd_backup(slug) if slug else print("Uso: panel_admin.py backup <slug>"),
        "backup-all":   lambda: cmd_backup_all(),
        "list-backups": lambda: cmd_list_backups(slug) if slug else print("Uso: panel_admin.py list-backups <slug>"),
        "restore-db":   lambda: cmd_restore_db(slug, args[2] if len(args) > 2 else None) if slug else print("Uso: panel_admin.py restore-db <slug> [archivo.db]"),
        "actualizar":  lambda: cmd_actualizar([slug] if slug else None),
        "versiones":   lambda: cmd_versiones(),
        "podar-imagenes": lambda: cmd_podar_imagenes(dry_run=(slug == "--dry-run")),
        "rollback":    lambda: cmd_rollback(slug, args[2] if len(args) > 2 else None) if slug else print("Uso: panel_admin.py rollback <slug> [version]"),
        "eliminar":    lambda: cmd_eliminar(slug) if slug else print("Uso: panel_admin.py eliminar <slug>"),
        "npm-listar":  lambda: cmd_npm_listar(),
        "npm-crear":   lambda: cmd_npm_crear(slug) if slug else print("Uso: panel_admin.py npm-crear <slug>"),
        "npm-eliminar":lambda: cmd_npm_eliminar(slug) if slug else print("Uso: panel_admin.py npm-eliminar <slug>"),
        "activar":     lambda: cmd_activar(slug) if slug else print("Uso: panel_admin.py activar <slug>"),
        "pausar":      lambda: cmd_pausar(slug) if slug else print("Uso: panel_admin.py pausar <slug>"),
        "suspender":   lambda: cmd_suspender(slug) if slug else print("Uso: panel_admin.py suspender <slug>"),
        "estado":      lambda: cmd_estado_servicio(slug) if slug else print("Uso: panel_admin.py estado <slug>"),
    }

    fn = dispatch.get(cmd)
    if fn:
        fn()
    else:
        print(f"Comando desconocido: {cmd}")
        print("Comandos: listar | info | start | stop | restart | logs | backup | actualizar | eliminar")
        print("Versión:  versiones | rollback <slug> [version] | podar-imagenes [--dry-run]")
        print("DB:       list-backups <slug> | restore-db <slug> [archivo.db]")
        print("Servicio: activar <slug> | pausar <slug> | suspender <slug> | estado <slug>")
        print("NPM:      npm-listar | npm-crear <slug> | npm-eliminar <slug>")
        sys.exit(1)
