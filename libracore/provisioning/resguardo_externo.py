"""Copia el backup de una instancia a la nube **del cliente**, con `rclone`.

## Por que existe

El backup nocturno vive en el mismo VPS que la instancia. Eso cubre el error
humano y el bug, pero **no cubre perder el servidor** — ni cubre que nos pase
algo a nosotros. El resguardo externo es una capacidad distinta, no "mas
seguridad de la misma", y por eso se vende aparte (ver
`wiki/analyses/resguardo-backup-familia-libra.md`).

## Por que a la cuenta del cliente y no a una nuestra

Decision del humano, 2026-08-12. El dato queda en poder de su responsable legal,
no consume almacenamiento nuestro, y resuelve el problema de MedLibra: mandar
documentos clinicos a una cuenta nuestra nos convertiria en custodios de datos
de salud de terceros.

## Por que corre en el HOST y no adentro del contenedor

Asi el contenedor **nunca ve la credencial de la nube del cliente**. Si el token
viviera adentro, comprometer la app de un cliente daria acceso a su Drive. El
ZIP ya esta en disco: subirlo es un paso posterior e independiente.

## Que NO hace este modulo

**No conecta la cuenta.** `rclone authorize` necesita un navegador y el consentimiento
del cliente, asi que el alta de cada remoto la hace una persona, una vez. Este
modulo asume que el remoto ya existe en la config de `rclone` y que
`cliente.json` lo nombra.
"""
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

# El estado vive en `libracore.resguardo_estado` porque lo escribe el host y lo
# lee la app: dejarlo aca obligaria al contenedor a importar el modulo que
# maneja Docker para leer un JSON. Se re-exportan para no romper a quien ya los
# importaba de aca.
from ..resguardo_estado import (  # noqa: F401
    ESTADO,
    escribir_estado,
    esta_al_dia,
    leer_estado,
)

#: Cuantos ZIP se conservan en el destino externo, por franja. Alla el disco no
#: es nuestro, asi que se puede guardar mas historia que en el VPS.
GFS_DIARIOS = 7
GFS_SEMANALES = 4
GFS_MENSUALES = 6

#: `backup_<motivo>_<YYYYmmdd>_<HHMMSS>[_n].zip`
_NOMBRE = re.compile(r"^backup_[a-z_]+_(\d{8})_(\d{6})(?:_\d+)?\.zip$")



class ResguardoExternoError(Exception):
    """Algo impidio dejar la copia afuera. El mensaje va al log del cron y al
    `.externo.json`, asi que dice **que** fallo."""


def destino_de(cliente: dict) -> dict | None:
    """La configuracion de resguardo externo de una instancia, o `None`.

    Vive en `cliente.json` —que es metadata por instancia y no esta en git—
    bajo la clave `resguardo_externo`:

        "resguardo_externo": {
            "remoto": "drive_compulibra:",
            "ruta": "libra-backups/contalibra/compulibra"
        }

    **Ausente significa "no contratado"**, y ese es el gate real del add-on: el
    subidor sólo corre para quien esta en la tabla. `plans.py` sólo decide que
    ve la pantalla.

    El `remoto` es un NOMBRE de la config de rclone, no una credencial: se puede
    loguear sin filtrar nada.
    """
    cfg = cliente.get("resguardo_externo")
    if not cfg:
        return None
    if not cfg.get("remoto"):
        raise ResguardoExternoError(
            f"'{cliente.get('slug')}' declara resguardo_externo sin 'remoto'"
        )
    return {"remoto": cfg["remoto"], "ruta": cfg.get("ruta", "").strip("/")}


def _fecha_de(nombre: str) -> datetime | None:
    """La fecha que dice el NOMBRE del backup, o None si no matchea.

    Se usa el nombre y no el mtime del remoto a proposito: subir un archivo lo
    fecha en el momento de la subida, asi que el mtime de alla no dice cuando se
    hizo el backup. Y un nombre que no matchea **no se puede fechar, asi que no
    se borra** — ver `a_borrar`.
    """
    m = _NOMBRE.match(nombre)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def a_borrar(nombres, ahora: datetime) -> list[str]:
    """Que sobra en el destino, con esquema abuelo-padre-hijo.

    Conserva los `GFS_DIARIOS` mas nuevos, mas uno por cada una de las ultimas
    `GFS_SEMANALES` semanas y uno por cada uno de los ultimos `GFS_MENSUALES`
    meses.

    🔴 **Dos cosas que no hace, y son las que evitan un borrado que duela:**

    - **Nunca borra el mas nuevo.** Aunque el calculo saliera mal, la ultima
      copia se queda.
    - **Nunca borra lo que no puede fechar.** Un archivo con otro nombre —algo
      que subio una persona, o un formato futuro— se conserva. Es preferible
      pagar unos MB de mas que borrar algo ajeno.
    """
    fechados = []
    for n in nombres:
        f = _fecha_de(n)
        if f is not None:
            fechados.append((f, n))
    if not fechados:
        return []
    fechados.sort(reverse=True)

    conservar = {n for _, n in fechados[:GFS_DIARIOS]}
    conservar.add(fechados[0][1])  # el mas nuevo, pase lo que pase

    def _primero_por(clave, cuantos):
        # El corte va ANTES de agregar: con `cuantos=0` tiene que devolver
        # vacio. Al reves agregaba uno igual, y eso hacia que la guarda de "el
        # mas nuevo" pareciera cubierta por un test que en realidad pasaba por
        # este off-by-one — lo delato el arnes de falla forzada.
        vistos = {}
        for f, n in fechados:
            if len(vistos) >= cuantos:
                break
            vistos.setdefault(clave(f), n)
        return set(vistos.values())

    conservar |= _primero_por(lambda f: f.isocalendar()[:2], GFS_SEMANALES)
    conservar |= _primero_por(lambda f: (f.year, f.month), GFS_MENSUALES)

    return sorted(n for _, n in fechados if n not in conservar)


def ultimo_zip(backups_dir) -> Path | None:
    """El backup mas nuevo que hay para subir, o None si no hay ninguno."""
    d = Path(backups_dir)
    if not d.is_dir():
        return None
    zips = sorted((f for f in d.iterdir() if f.suffix == ".zip"), reverse=True)
    return zips[0] if zips else None


def _rclone(*args, binario="rclone", timeout=1800):
    r = subprocess.run(
        [binario, *args], capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        detalle = (r.stderr or "").strip().splitlines()
        raise ResguardoExternoError(
            f"rclone {args[0]} termino con codigo {r.returncode}: "
            f"{detalle[-1] if detalle else 'sin detalle'}"
        )
    return r.stdout


def _listar_remoto(destino: str, binario="rclone") -> dict[str, int]:
    """`{nombre: bytes}` de lo que hay hoy en el destino."""
    try:
        salida = _rclone("lsjson", destino, binario=binario)
    except ResguardoExternoError as e:
        # Un destino que todavia no existe no es un error: la primera subida lo
        # crea. Cualquier otra cosa si.
        if "directory not found" in str(e).lower():
            return {}
        raise
    return {i["Name"]: i["Size"] for i in json.loads(salida or "[]") if not i["IsDir"]}




def subir(cliente: dict, backups_dir, *, binario="rclone", ahora=None, log=print) -> dict:
    """Sube el ZIP mas nuevo al destino del cliente y aplica retencion.

    Devuelve el mismo dict que deja en `.externo.json`.
    """
    ahora = ahora or datetime.now()
    slug = cliente.get("slug", "?")
    cfg = destino_de(cliente)
    if cfg is None:
        return {"ok": None, "motivo": "sin resguardo externo contratado"}

    destino = f"{cfg['remoto']}{cfg['ruta']}" if cfg["ruta"] else cfg["remoto"]
    estado = {
        "ok": False, "cuando": ahora.isoformat(timespec="seconds"),
        "destino": destino, "archivo": None, "bytes": 0, "error": None,
    }

    try:
        zip_local = ultimo_zip(backups_dir)
        if zip_local is None:
            raise ResguardoExternoError(
                f"no hay ningun backup en {backups_dir} para subir"
            )
        estado["archivo"] = zip_local.name
        estado["bytes"] = zip_local.stat().st_size

        log(f"[*] {slug}: subiendo {zip_local.name} "
            f"({estado['bytes'] / 1_048_576:.2f} MB) a {destino}")
        _rclone("copy", str(zip_local), destino, "--no-traverse", binario=binario)

        # 🔴 Que `rclone copy` no haya fallado NO alcanza. Es el mismo criterio
        # que `respaldo.verificar_backup`: se mira el producto, no el proceso.
        remoto = _listar_remoto(destino, binario=binario)
        if zip_local.name not in remoto:
            raise ResguardoExternoError(
                f"rclone dijo que copio pero {zip_local.name} no esta en el destino"
            )
        if remoto[zip_local.name] != estado["bytes"]:
            raise ResguardoExternoError(
                f"{zip_local.name} llego con {remoto[zip_local.name]} bytes y "
                f"pesa {estado['bytes']}"
            )
        log(f"[OK] {slug}: verificado en el destino")

        sobran = a_borrar(remoto, ahora)
        for nombre in sobran:
            _rclone("deletefile", f"{destino}/{nombre}", binario=binario)
        if sobran:
            log(f"[OK] {slug}: retencion, {len(sobran)} copia/s vieja/s borrada/s")
        estado["borrados"] = sobran
        estado["en_destino"] = len(remoto) - len(sobran)
        estado["ok"] = True
    except Exception as e:  # noqa: BLE001 — el error va al estado, no se traga
        estado["error"] = str(e)
        log(f"[ERROR] {slug}: {e}")
    finally:
        escribir_estado(backups_dir, estado)
    return estado


