"""
Panel de administración por línea de comandos: gestiona todos los
contenedores de clientes de un producto desde un menú interactivo o
comandos directos.

Requiere `libracore.provisioning.configure()` antes de usar cualquier
función de acá. `npm_api.py` (idéntico entre productos, pero vive en
`scripts/` de cada repo, no en LibraCore) se resuelve en tiempo de
ejecución vía import diferido — ver `libracore.provisioning._npm_api()`.
"""
import os
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
    LIBRACORE_SSH_KEY, LIBRACOMMERCE_SSH_KEY, LIBRA_UI_SSH_KEY, docker_build_ssh_args,
    check_venv_sync,
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


def cmd_backup(slug: str, quiet: bool = False):
    """Backup completo del directorio data (tar.gz) + copia WAL-safe de la DB
    (vía la API de backup online de sqlite3, no un copy2 crudo del archivo —
    en modo WAL un copy2 puede capturar el .db sin los cambios que todavía
    están en .db-wal si hay escrituras activas al mismo tiempo). Purga
    backups de más de BACKUP_RETENTION_DIAS días. Pensado para correr desde
    cron (ver `backup-all`) además de manual."""
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
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = cfg.clientes_dir / f"{slug}_backup_{ts}.tar.gz"
    _p(f"[*] Creando backup completo de {slug} ...")
    with tarfile.open(out_file, "w:gz") as tar:
        tar.add(data_dir, arcname=f"{slug}/data")
    size_mb = out_file.stat().st_size / 1_048_576
    _p(f"[OK] Backup tar.gz: {out_file}  ({size_mb:.1f} MB)")
    _purge_backups_viejos(cfg.clientes_dir, f"{slug}_backup_*.tar.gz")

    # Copia WAL-safe de la DB, vía sqlite3.Connection.backup() (Online Backup
    # API de SQLite) en vez de shutil.copy2 crudo del archivo.
    db_src = data_dir / cfg.db_filename
    if db_src.exists():
        bdir   = _backups_dir(c)
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
        print(f"[OK] Contenedor reiniciado.")


def cmd_actualizar(slugs: list[str] | None = None):
    """Reconstruye la imagen y reinicia los contenedores indicados (o todos)."""
    cfg = get_config()
    aviso = check_venv_sync(cfg.repo_root)
    if aviso:
        print(aviso)
    print(f"[*] Reconstruyendo imagen {cfg.image_name} ...")
    build_env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    r = subprocess.run(
        ["docker", "build", *docker_build_ssh_args(cfg.repo_root), "-t", cfg.image_name, "."],
        cwd=str(cfg.repo_root), env=build_env,
    )
    if r.returncode != 0:
        print("[ERROR] Falló el build.")
        return

    clients = load_clients()
    targets = [c for c in clients if (not slugs or c["slug"] in slugs)]
    if not targets:
        print("[INFO] Sin contenedores que actualizar.")
        return

    for c in targets:
        info = container_status(c["container"])
        if info["status"] == "running":
            print(f"[*] Actualizando {c['container']} ...")
            compose(c["slug"], "up", "-d")
        else:
            print(f"[SKIP] {c['container']} no está en ejecución.")
    print("[OK] Actualización completa.")


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
        print(f"  Estado: desconocido (config.json no encontrado)")
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
        print("DB:       list-backups <slug> | restore-db <slug> [archivo.db]")
        print("Servicio: activar <slug> | pausar <slug> | suspender <slug> | estado <slug>")
        print("NPM:      npm-listar | npm-crear <slug> | npm-eliminar <slug>")
        sys.exit(1)
