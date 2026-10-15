"""Que la app corra sin root dentro del contenedor, y que el deploy le entregue sus datos.

Relevamiento de seguridad de la VPS del 2026-10-10, punto 8: seis de los ocho productos corrían
como `uid 0` dentro del contenedor. LibraCargo y LibraClub corren como `uid 10001` (`USER` en el
Dockerfile); este módulo es lo que el **motor** pone de su lado para que los demás puedan hacer
lo mismo sin romper a los clientes que ya tienen datos.

## El problema que esto resuelve

El `USER` del Dockerfile no alcanza. La carpeta de datos de cada instancia
(`clientes/<slug>/data`) la crea el host como root y se monta con `./data:/app/data`. Docker le
copia el dueño de la imagen a un volumen **nuevo y vacío**; un bind mount o un volumen que ya
tiene datos **conserva su dueño**. Con la imagen en 10001 y `data/` de root, la app no puede
escribir los PDF de comprobantes, el `arca_ta`, `errores/`, los logos ni los backups, y **ni
siquiera leer** lo que está en modo 600 (las claves de ARCA). Medido el 2026-10-15 con la imagen
sin root y un `data/` de root: `touch`, `mkdir`, `echo >` y `cat` de un 600 dan
`Permission denied`; con `chown -R 10001:10001` andan los cuatro.

LibraCargo lo resolvió a mano (`chown -R 10001:10001 clientes/*/data`, ver
`wiki/analyses/deploy-unificado-familia-libra.md`) y lo anotó como invariante: **el dueño del
DIRECTORIO es lo que importa, no el de los archivos** —para borrar un archivo alcanza con poder
escribir en la carpeta que lo contiene, así que los ZIP que el cron deja como root no molestan—.
Lo que no puede pasar es que `data/backups` vuelva a nacer de root.

## Qué hace el motor

- `entregar_datos`: `chown -R` de `data/` al uid de la app, sin seguir symlinks. Lo llama
  `panel_admin.cmd_actualizar` **justo antes del `up -d`** (después de respaldar y migrar) y sólo
  si la imagen que se despliega corre sin root: mientras el producto no haya migrado su imagen,
  el deploy no cambia nada. Es idempotente, y con el contenedor viejo (root) todavía corriendo es
  inocuo: root escribe igual en una carpeta ajena.
- `crear_cliente` lo llama también al dar de alta (la carpeta nace de root: la crea el
  backoffice o el panel).
- `asegurar_directorio`: lo que el host crea DENTRO de `data/` (`backups/`) nace con el dueño de
  su carpeta madre, no de root.
- `con_security_opt`: `security_opt: [no-new-privileges:true]` en el servicio de la app de los
  composes que ya existen. `crear_cliente` lo trae en la plantilla; `pinear_image` lo agrega a los
  anteriores, igual que `errores_env` hace con las variables de errores.

Todo es texto y syscalls sobre archivos del host: nada de esto necesita Docker salvo
`imagen_sin_root`, que le pregunta a Docker con qué `USER` corre la imagen.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .errores_env import _bloque_del_servicio

#: El uid/gid fijo de la app en toda la familia. **El mismo número está en el Dockerfile de cada
#: producto** (`useradd -m -u 10001`), y cada producto tiene un test que lo exige: si dos
#: números divergen, el deploy deja la carpeta de datos con un dueño que la app no usa.
UID_APP = 10001
GID_APP = 10001

SECURITY_OPT = "no-new-privileges:true"

#: Los `USER` que significan «root» en `Config.User` de una imagen. Vacío es root también.
_ROOT = {"", "root", "0", "0:0", "root:root", "root:0", "0:root"}


# ── la imagen ────────────────────────────────────────────────────────────────

def usuario_de_la_imagen(ref: str) -> str | None:
    """El `USER` con el que corre la imagen (`""` si no declara ninguno), o `None` si Docker no
    pudo decirlo (imagen inexistente, daemon caído)."""
    r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Config.User}}", ref],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def imagen_sin_root(ref: str) -> bool:
    """`True` si la imagen corre con un usuario que no es root **o si no se puede saber**.

    Sólo devuelve `False` cuando Docker confirma que no declara `USER` (o declara root). Ante la
    duda se entrega la carpeta: es inocuo para una imagen que corre como root, y lo contrario
    —no entregarla a una que no lo es— deja la instancia sin poder escribir.
    """
    usuario = usuario_de_la_imagen(ref)
    if usuario is None:
        return True
    return usuario.lower() not in _ROOT


# ── la carpeta de datos ──────────────────────────────────────────────────────

def _cambiar(ruta: str, uid: int, gid: int) -> int:
    """`lchown` si el dueño es otro (no sigue symlinks). Devuelve 1 si cambió algo."""
    st = os.lstat(ruta)
    if st.st_uid == uid and st.st_gid == gid:
        return 0
    os.lchown(ruta, uid, gid)
    return 1


def entregar_datos(data_dir: Path | str, uid: int = UID_APP, gid: int = GID_APP) -> int:
    """`chown -R uid:gid data_dir`, sin seguir symlinks. Devuelve cuántas entradas cambió.

    Es idempotente: sólo toca lo que tiene otro dueño. Cubre también los archivos en modo 600
    (las claves de ARCA, `arca_ta`), que de otro modo la app no podría ni leer.

    Es un `chown -R`, así que **se niega a correr sobre algo que no sea una carpeta de datos**:
    una ruta que no existe, un archivo, un symlink o la raíz son un `ValueError` y no se toca nada.
    `PermissionError` (el proceso no es root) se propaga: el que llama decide qué hacer.
    """
    raiz = Path(data_dir)
    if raiz.is_symlink() or not raiz.is_dir():
        raise ValueError(f"{raiz} no es una carpeta de datos (inexistente, archivo o symlink)")
    absoluta = raiz.resolve()
    if absoluta.parent == absoluta:
        raise ValueError("no se entrega la raíz del sistema de archivos")

    cambios = _cambiar(str(raiz), uid, gid)
    pendientes = [str(raiz)]
    while pendientes:
        carpeta = pendientes.pop()
        with os.scandir(carpeta) as it:
            for entrada in it:
                cambios += _cambiar(entrada.path, uid, gid)
                if entrada.is_dir(follow_symlinks=False):
                    pendientes.append(entrada.path)
    return cambios


def asegurar_directorio(ruta: Path | str) -> Path:
    """`mkdir -p` en el que cada carpeta NUEVA nace con el dueño de su carpeta madre.

    Es lo que usa el host para crear `data/backups`: si la instancia ya fue entregada (`data/` de
    10001), `backups/` nace de 10001 y la app puede escribir sus ZIP; si `data/` todavía es de root,
    nace de root y nada cambia. Si el proceso no puede cambiar dueños (no es root) lo deja como
    quedó: no es un error del host, sólo no hay nada que heredar.
    """
    destino = Path(ruta)
    faltantes = []
    actual = destino
    while not actual.exists():
        faltantes.append(actual)
        actual = actual.parent
    for carpeta in reversed(faltantes):
        carpeta.mkdir()
        madre = carpeta.parent.stat()
        try:
            _cambiar(str(carpeta), madre.st_uid, madre.st_gid)
        except PermissionError:
            pass
    return destino


# ── el compose ───────────────────────────────────────────────────────────────

_ITEM_SECURITY_OPT = re.compile(r"^(?P<sangria>[ \t]+)-[ \t]*\S")


def con_security_opt(contenido: str, indice_imagen_de_la_app: int) -> str | None:
    """El compose con `security_opt: [no-new-privileges:true]` en el servicio de la app.

    - Si ya está `no-new-privileges`, devuelve el texto sin cambios.
    - Si `security_opt:` ya existe con otras opciones (un perfil de seccomp, por ejemplo), suma la
      de acá al final de la lista y no pisa las que había.
    - Si el servicio no tiene `security_opt` lo inserta debajo de `image:`.
    - `None` si el `security_opt` del servicio está en un formato que no es la lista de siempre
      (`security_opt: [..]`, un mapa): no se adivina, quien llama deja el compose como estaba.
    - Aplicarla dos veces da el mismo texto que aplicarla una.

    `indice_imagen_de_la_app` es el número de línea (base 0) de la `image:` del servicio de la app,
    el mismo que usa `errores_env.con_variables`.
    """
    lineas = contenido.split("\n")
    inicio, fin = _bloque_del_servicio(lineas, indice_imagen_de_la_app)
    linea_imagen = lineas[indice_imagen_de_la_app]
    sangria = linea_imagen[: len(linea_imagen) - len(linea_imagen.lstrip())]

    clave = next((i for i in range(inicio, fin)
                  if re.match(rf"^{sangria}security_opt:", lineas[i])), None)
    if clave is None:
        lineas[indice_imagen_de_la_app + 1:indice_imagen_de_la_app + 1] = [
            f"{sangria}security_opt:", f"{sangria}  - {SECURITY_OPT}"]
        return "\n".join(lineas)

    if lineas[clave].split(":", 1)[1].strip():  # `security_opt: [..]` en una línea
        return None
    ultimo = clave
    sangria_item = f"{sangria}  "
    i = clave + 1
    while i < fin:
        linea = lineas[i]
        if not linea.strip() or linea.lstrip().startswith("#"):
            i += 1
            continue
        m = _ITEM_SECURITY_OPT.match(linea)
        if not m:
            break
        if linea.split("-", 1)[1].strip().strip("\"'") == SECURITY_OPT:
            return contenido
        sangria_item = m.group("sangria")
        ultimo = i
        i += 1
    if ultimo == clave:  # `security_opt:` sin ningún ítem: formato raro, no se toca
        return None
    lineas.insert(ultimo + 1, f"{sangria_item}- {SECURITY_OPT}")
    return "\n".join(lineas)
