"""Las tres variables con las que una instancia se identifica en el registro de errores (ADR-045).

`libracore.errores` escribe en cada línea de `errores.jsonl` quién es la instancia, de qué
entorno y qué versión corre. Esos tres datos los conoce el **panel**, no la aplicación:

| Variable          | Valor                                            | Quién la escribe          |
|-------------------|--------------------------------------------------|---------------------------|
| `LIBRA_INSTANCIA` | el contenedor (`libradesk-lagrace`)              | alta, y cada `actualizar` |
| `LIBRA_ENTORNO`   | `demo` si el slug es `demo`, si no `cliente`     | alta; `actualizar` no la pisa |
| `LIBRA_VERSION`   | el tag de la imagen pineada (`v2026.10.12-103000`) | alta, y **cada pin**    |

**La versión sale del tag que el panel pinea, no de un build-arg ni de la etiqueta de la
imagen.** Es la misma cadena que ya identifica el artefacto (`deploy_version`, el tag de la
imagen y `org.libra.version`), y el compose es el único lugar donde el panel y el contenedor
coinciden: viaja con el `up -d` que mueve la instancia. Un `ARG` por producto no la podría
tener —la imagen se construye *antes* de saber a qué instancias va— y la etiqueta de la imagen
no se ve desde adentro del contenedor. Como se reescribe en `pinear_image`, un `rollback` o el
repineo de un deploy fallido dejan la variable diciendo lo mismo que la imagen.

Todo es texto sobre el `docker-compose.yml`, igual que `pinear_image`, y **idempotente**.
"""
from __future__ import annotations

import re

VARIABLES = ("LIBRA_INSTANCIA", "LIBRA_ENTORNO", "LIBRA_VERSION")

#: La instancia que es la demo pública de un producto: `clientes/demo`.
SLUG_DEMO = "demo"

_SERVICIO = re.compile(r"^  [A-Za-z0-9_-]+:[ \t]*$")
_RAIZ = re.compile(r"^\S")
_ITEM_ENV = re.compile(r"^(?P<sangria>[ \t]+)-[ \t]*[\"']?(?P<clave>[A-Za-z_][A-Za-z0-9_]*)=")


def entorno_de(slug: str) -> str:
    """`demo` para la instancia `demo` de un producto y `cliente` para todas las demás."""
    return "demo" if slug == SLUG_DEMO else "cliente"


def valores(slug: str, contenedor: str, version: str) -> dict[str, str]:
    """Los valores por defecto de las tres variables para una instancia."""
    return {
        "LIBRA_INSTANCIA": contenedor,
        "LIBRA_ENTORNO": entorno_de(slug),
        "LIBRA_VERSION": version,
    }


def version_de(ref: str) -> str:
    """El tag de una referencia de imagen: `contalibra:v2026.10.12-103000` → `v2026.10.12-103000`."""
    return ref.rsplit(":", 1)[-1] if ":" in ref.rsplit("/", 1)[-1] else ref


def env_para_compose(slug: str, contenedor: str, version: str) -> str:
    """Las tres líneas de `environment:` para el compose de un alta (6 espacios, `\\n` al final)."""
    return "".join(f"      - {k}={v}\n" for k, v in valores(slug, contenedor, version).items())


def _bloque_del_servicio(lineas: list[str], indice_imagen: int) -> tuple[int, int]:
    """`[inicio, fin)` del servicio al que pertenece la línea `indice_imagen`."""
    inicio = indice_imagen
    while inicio > 0 and not _SERVICIO.match(lineas[inicio]):
        inicio -= 1
    fin = indice_imagen + 1
    while fin < len(lineas) and not _SERVICIO.match(lineas[fin]) and not _RAIZ.match(lineas[fin]):
        fin += 1
    return inicio, fin


def con_variables(contenido: str, indice_imagen_de_la_app: int, nuevos: dict[str, str]) -> str | None:
    """El compose con las variables de `nuevos` puestas en el `environment:` de la app.

    - `LIBRA_INSTANCIA` y `LIBRA_VERSION` se **reemplazan** si ya están; `LIBRA_ENTORNO` se
      **respeta** si ya está (quien la fijó a mano sabe algo que el panel no).
    - Las que faltan se agregan al final de la lista de `environment:`.
    - `None` si el servicio no tiene un `environment:` en forma de lista, que es la que genera el
      panel: no se adivina un formato que no se conoce. Quien llama deja el compose como estaba.
    - Aplicarla dos veces da el mismo texto que aplicarla una.

    `indice_imagen_de_la_app` es el número de línea (base 0) de la `image:` del servicio de la app.
    """
    lineas = contenido.split("\n")
    inicio, fin = _bloque_del_servicio(lineas, indice_imagen_de_la_app)
    env = next((i for i in range(inicio, fin) if re.match(r"^    environment:[ \t]*$", lineas[i])), None)
    if env is None:
        return None
    ultimo = env
    sangria = "      "
    presentes: dict[str, int] = {}
    i = env + 1
    while i < fin:
        linea = lineas[i]
        if not linea.strip() or linea.lstrip().startswith("#"):
            i += 1
            continue
        m = _ITEM_ENV.match(linea)
        if not m:
            break  # otra clave del servicio (o un formato de mapa): termina la lista
        sangria = m.group("sangria")
        presentes[m.group("clave")] = i
        ultimo = i
        i += 1
    if not presentes and i < fin and re.match(r"^\s+[A-Za-z_][A-Za-z0-9_]*:", lineas[i]):
        return None  # `environment:` en formato de mapa (`CLAVE: valor`): no se toca
    for clave in VARIABLES:
        if clave not in nuevos:
            continue
        if clave in presentes:
            if clave != "LIBRA_ENTORNO":
                lineas[presentes[clave]] = f"{sangria}- {clave}={nuevos[clave]}"
            continue
        lineas.insert(ultimo + 1, f"{sangria}- {clave}={nuevos[clave]}")
        ultimo += 1
    return "\n".join(lineas)
