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

## Por que lo que sale va cifrado (2026-09-17)

El ZIP lleva la **clave privada de ARCA** del cliente —la que permite facturar en
su nombre— porque sin ella un restore deja una instancia que no factura. Hasta
el 2026-09-17 subia en claro al Drive del cliente. Se decidio que el ZIP local y
la descarga del admin no cambian, y que **lo que sale a un tercero va cifrado**
con `rclone crypt`. Ver `wiki/analyses/resguardo-backup-familia-libra.md`.

Tres decisiones que no son de gusto:

- 🔴 **Sin clave no se sube.** No hay camino que caiga a claro: si falta la
  passphrase, el error va al estado y `estado-externo` se pone rojo. Un
  resguardo que no sube se ve; uno que sube en claro sin avisar, no.
- **La passphrase vive solo en el host** y el remoto cifrado se arma por
  variables de entorno en cada llamada. No entra al `rclone.conf` del enlace
  —que lo escribe y lo lee la app— ni al argv de ningun proceso.
- **Es una del parque, fuera del ciclo de rotacion.** Rotarla deja ilegible todo
  lo ya subido, al reves que los secretos de `libraauth`: misma sonda, distinto
  ciclo. Por eso el estado guarda su **huella** —si cambia entre dos subidas,
  parte del historial del cliente ya no se abre con la clave de hoy— y **no
  puede derivarse de `SECRET_KEY`**, que se rota.
"""
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
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

#: Nombre del remoto `crypt` que se arma por entorno. Distintivo a proposito: una
#: variable `RCLONE_CONFIG_<NOMBRE>_*` pisa a un remoto del mismo nombre en el
#: `rclone.conf`, y ninguno deberia llamarse asi.
REMOTO_CIFRADO = "resguardocifrado"

#: Donde vive la passphrase en el host. `RESGUARDO_CIFRADO_CLAVE_ARCHIVO` la
#: cambia —los tests, o un host con otro layout—.
CLAVE_ARCHIVO_DEFAULT = "/root/secretos/resguardo_cifrado.key"
CLAVE_ARCHIVO_ENV = "RESGUARDO_CIFRADO_CLAVE_ARCHIVO"

#: Largo minimo aceptado. Detras hay un ZIP con la clave para facturar en nombre
#: del cliente.
CLAVE_MINIMO = 32



class ResguardoExternoError(Exception):
    """Algo impidio dejar la copia afuera. El mensaje va al log del cron y al
    `.externo.json`, asi que dice **que** fallo."""


def destino_de(cliente: dict, backups_dir=None) -> dict | None:
    """La configuracion de resguardo externo de una instancia, o `None`.

    Sale de uno de dos lugares, en este orden:

    1. **`cliente.json`**, bajo la clave `resguardo_externo` — el alta hecha a
       mano por una persona, con un remoto de la config global de rclone:

           "resguardo_externo": {
               "remoto": "drive_compulibra:",
               "ruta": "libra-backups/contalibra/compulibra"
           }

       Gana sobre el enlace a proposito: es lo que escribe el operador, y le
       tiene que servir para pisar lo que haya hecho la pantalla.

    2. **El enlace que hizo el cliente desde su pantalla**
       (`libracore.resguardo_enlace`), si se pasa `backups_dir`. Trae su propio
       `rclone.conf`, que va en `config`.

    **Ninguno de los dos significa "no contratado"**: el subidor sólo corre para
    quien tiene destino. Quien decide si la pantalla deja enlazar es el modulo
    `resguardo_externo` del plan.

    El `remoto` es un NOMBRE de la config de rclone, no una credencial: se puede
    loguear sin filtrar nada. `config` es una ruta, tampoco.
    """
    cfg = cliente.get("resguardo_externo")
    if not cfg:
        if backups_dir is None:
            return None
        from ..resguardo_enlace import REMOTO, config_rclone, enlace_de
        conf = config_rclone(backups_dir)
        if conf is None:
            return None
        return {
            "remoto": f"{REMOTO}:",
            "ruta": str(enlace_de(backups_dir).get("carpeta") or "").strip("/"),
            "config": str(conf),
        }
    if not cfg.get("remoto"):
        raise ResguardoExternoError(
            f"'{cliente.get('slug')}' declara resguardo_externo sin 'remoto'"
        )
    return {"remoto": cfg["remoto"], "ruta": cfg.get("ruta", "").strip("/"), "config": None}


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


def _rclone(*args, binario="rclone", timeout=1800, env=None):
    r = subprocess.run(
        [binario, *args], capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **env} if env else None,
    )
    if r.returncode != 0:
        detalle = (r.stderr or "").strip().splitlines()
        raise ResguardoExternoError(
            f"rclone {args[0]} termino con codigo {r.returncode}: "
            f"{detalle[-1] if detalle else 'sin detalle'}"
        )
    return r.stdout


def _listar_remoto(destino: str, *extra, binario="rclone", env=None) -> dict[str, int]:
    """`{nombre: bytes}` de lo que hay hoy en el destino.

    A traves del remoto cifrado, nombre y tamaño son los **originales**: `crypt`
    descuenta su sobrecarga. Por eso la verificacion de abajo no cambio.
    """
    try:
        salida = _rclone("lsjson", destino, *extra, binario=binario, env=env)
    except ResguardoExternoError as e:
        # Un destino que todavia no existe no es un error: la primera subida lo
        # crea. Cualquier otra cosa si.
        if "directory not found" in str(e).lower():
            return {}
        raise
    return {i["Name"]: i["Size"] for i in json.loads(salida or "[]") if not i["IsDir"]}




def leer_clave(ruta=None) -> str:
    """La passphrase del cifrado, o `ResguardoExternoError`.

    Los mensajes dicen **que** falta y **donde**, nunca el valor.
    """
    ruta = Path(ruta or os.environ.get(CLAVE_ARCHIVO_ENV) or CLAVE_ARCHIVO_DEFAULT)
    try:
        info = ruta.stat()
    except FileNotFoundError:
        raise ResguardoExternoError(
            f"no hay clave de cifrado en {ruta}: no se sube en claro"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise ResguardoExternoError(f"{ruta} no es un archivo: no se sube en claro")
    if info.st_mode & 0o077:
        raise ResguardoExternoError(
            f"{ruta} tiene permisos {stat.S_IMODE(info.st_mode):04o}; "
            "tiene que ser 0600: no se sube"
        )
    clave = ruta.read_text(encoding="utf-8").strip()
    if len(clave) < CLAVE_MINIMO:
        raise ResguardoExternoError(
            f"la clave de cifrado en {ruta} tiene menos de {CLAVE_MINIMO} caracteres: no se sube"
        )
    return clave


def huella_de(clave: str) -> str:
    """Identifica la clave sin revelarla. Si cambia entre dos subidas, parte del
    historial del cliente quedo cifrado con otra y ya no se abre con esta."""
    return hashlib.sha256(clave.encode("utf-8")).hexdigest()[:8]


def _obscurecer(clave: str, binario="rclone") -> str:
    """La forma en que `rclone` espera una password de config.

    Por **stdin** y no como argumento: el argv de un proceso lo ve cualquiera que
    liste procesos.
    """
    r = subprocess.run(
        [binario, "obscure", "-"], input=clave, capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise ResguardoExternoError("rclone obscure no devolvio la clave preparada")
    return r.stdout.strip()


def _entorno_cifrado(destino_plano: str, clave_obscura: str) -> dict[str, str]:
    """Las variables que definen el remoto `crypt` sobre el destino de siempre.

    `filename_encryption = off` no es comodidad: la verificacion compara por
    nombre y la retencion fecha por nombre. Con los nombres cifrados las dos
    dejarian de funcionar. El contenido va cifrado igual.
    """
    p = f"RCLONE_CONFIG_{REMOTO_CIFRADO.upper()}_"
    return {
        f"{p}TYPE": "crypt",
        f"{p}REMOTE": destino_plano,
        f"{p}PASSWORD": clave_obscura,
        f"{p}FILENAME_ENCRYPTION": "off",
        f"{p}DIRECTORY_NAME_ENCRYPTION": "false",
    }


def _en(destino: str, nombre: str) -> str:
    """`destino` + `nombre`, sin la barra de mas cuando el destino es la raiz de
    un remoto (`resguardocifrado:`)."""
    return f"{destino}{nombre}" if destino.endswith(":") else f"{destino}/{nombre}"


def _verificar_contenido(zip_local: Path, destino: str, *extra, binario, env) -> None:
    """🔴 Que el objeto de alla sea ESTE archivo, cifrado — no sólo que tenga su
    nombre y su tamaño.

    `cryptcheck` cifra el local con el nonce del remoto y compara hashes, sin
    bajar nada. Y no alcanza con su codigo de salida: con un filtro que no
    matchea compara **cero** archivos y sale 0. Por eso la lista va por
    `--files-from` —literal, sin globs— y se exige que `--match` nombre al
    archivo.
    """
    with tempfile.TemporaryDirectory() as tmp:
        lista = Path(tmp) / "archivos"
        coinciden = Path(tmp) / "coinciden"
        lista.write_text(zip_local.name + "\n", encoding="utf-8")
        _rclone(
            "cryptcheck", str(zip_local.parent), destino, "--one-way",
            "--files-from", str(lista), "--match", str(coinciden),
            *extra, binario=binario, env=env,
        )
        vistos = (
            coinciden.read_text(encoding="utf-8").splitlines() if coinciden.exists() else []
        )
    if vistos != [zip_local.name]:
        raise ResguardoExternoError(
            f"cryptcheck no confirmo {zip_local.name} en el destino (coincidieron: {vistos})"
        )


def subir(cliente: dict, backups_dir, *, binario="rclone", ahora=None, log=print) -> dict:
    """Sube el ZIP mas nuevo al destino del cliente y aplica retencion.

    Devuelve el mismo dict que deja en `.externo.json`.
    """
    ahora = ahora or datetime.now()
    slug = cliente.get("slug", "?")
    cfg = destino_de(cliente, backups_dir)
    if cfg is None:
        return {"ok": None, "motivo": "sin resguardo externo contratado"}

    # El destino de siempre, legible: es el que se muestra y el que envuelve el
    # remoto cifrado. A rclone se le habla SOLO por el cifrado.
    destino_plano = f"{cfg['remoto']}{cfg['ruta']}" if cfg["ruta"] else cfg["remoto"]
    destino = f"{REMOTO_CIFRADO}:"
    # Un enlace hecho desde la pantalla trae su propio `rclone.conf`. Va como
    # flag al final y no como parametro de `_rclone`, para que `args[0]` siga
    # siendo el subcomando que nombra el mensaje de error.
    extra = ("--config", cfg["config"]) if cfg.get("config") else ()
    estado = {
        "ok": False, "cuando": ahora.isoformat(timespec="seconds"),
        "destino": destino_plano, "cifrado": True, "huella_clave": None,
        "archivo": None, "bytes": 0, "error": None,
    }

    try:
        # Lo primero, antes de tocar la red: sin clave no hay subida posible, y
        # el error tiene que decir eso y no otra cosa.
        clave = leer_clave()
        estado["huella_clave"] = huella_de(clave)
        env = _entorno_cifrado(destino_plano, _obscurecer(clave, binario=binario))

        zip_local = ultimo_zip(backups_dir)
        if zip_local is None:
            raise ResguardoExternoError(
                f"no hay ningun backup en {backups_dir} para subir"
            )
        estado["archivo"] = zip_local.name
        estado["bytes"] = zip_local.stat().st_size

        log(f"[*] {slug}: subiendo {zip_local.name} "
            f"({estado['bytes'] / 1_048_576:.2f} MB) cifrado a {destino_plano}")
        _rclone("copy", str(zip_local), destino, "--no-traverse", *extra,
                binario=binario, env=env)

        # 🔴 Que `rclone copy` no haya fallado NO alcanza. Es el mismo criterio
        # que `respaldo.verificar_backup`: se mira el producto, no el proceso.
        remoto = _listar_remoto(destino, *extra, binario=binario, env=env)
        if zip_local.name not in remoto:
            raise ResguardoExternoError(
                f"rclone dijo que copio pero {zip_local.name} no esta en el destino"
            )
        if remoto[zip_local.name] != estado["bytes"]:
            raise ResguardoExternoError(
                f"{zip_local.name} llego con {remoto[zip_local.name]} bytes y "
                f"pesa {estado['bytes']}"
            )
        _verificar_contenido(zip_local, destino, *extra, binario=binario, env=env)
        log(f"[OK] {slug}: verificado en el destino, cifrado")

        sobran = a_borrar(remoto, ahora)
        for nombre in sobran:
            _rclone("deletefile", _en(destino, nombre), *extra, binario=binario, env=env)
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


