"""Las credenciales de máquina de una instancia: dónde viven, cómo se leen y cómo se cambian (ADR-071).

Dos credenciales, **una por instancia** y nunca compartidas:

| Variable                  | Header de libraauth  | Quién la presenta                                  |
|---------------------------|----------------------|----------------------------------------------------|
| `LIBRA_SERVICE_TOKEN`     | `x-internal-auth`    | el backoffice del proveedor, que la lee del disco  |
| `LIBRA_INTEGRACION_TOKEN` | `x-integracion-auth` | otra instancia que le habla (MedLibra → ContaLibra) |

Cada una tiene su `<VAR>_ANTERIOR` (libraauth v0.52.0, su ADR-025): mientras está, la instancia
acepta las dos, y así una rotación no corta a nadie. El ciclo es `--rotar` (la vigente pasa a
anterior y nace una nueva), repartir la nueva a quien la use, y `--cerrar` (se borra la anterior).

**Dónde vive cada valor.** En el `docker-compose.yml` de la instancia, en el `environment:` de la
app, de una de dos formas: literal (`- LIBRA_SERVICE_TOKEN=abc…`) o por referencia
(`- LIBRA_SERVICE_TOKEN=${LIBRA_SERVICE_TOKEN}`) contra el `.env` del mismo directorio. Este módulo
lee las dos y, al escribir, **respeta la que encuentra**: lo que estaba en `.env` se escribe en
`.env`, lo literal se escribe literal. Todo es texto sobre los dos archivos, igual que
`errores_env` y `mail_cuentas`: el motor no depende de PyYAML en producción.

🔴 **Ningún valor sale por acá a un log ni a la pantalla.** Lo único que se muestra es `huella()`
(8 hex de su SHA-256), que alcanza para comparar dos instancias sin poder reconstruir nada.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

SERVICIO = "LIBRA_SERVICE_TOKEN"
INTEGRACION = "LIBRA_INTEGRACION_TOKEN"
SUFIJO_ANTERIOR = "_ANTERIOR"

#: Las cuatro variables que no pueden venir del entorno del proceso que corre `docker compose`
#: (ver `entorno_para_compose`).
PROPIAS_DE_LA_INSTANCIA = (SERVICIO, SERVICIO + SUFIJO_ANTERIOR, INTEGRACION, INTEGRACION + SUFIJO_ANTERIOR)

COMPOSE = "docker-compose.yml"
DOTENV = ".env"

_NOMBRE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SERVICIO = re.compile(r"^  (?P<nombre>[A-Za-z0-9_.-]+):[ \t]*$")
_IMAGE = re.compile(r"^    image:[ \t]*(?P<ref>\S+)")
_ENVIRONMENT = re.compile(r"^    environment:[ \t]*$")
_ITEM_LISTA = re.compile(
    r"""^(?P<sangria>[ \t]+)-[ \t]*(?P<q>["']?)(?P<clave>[A-Za-z_][A-Za-z0-9_]*)=(?P<valor>.*?)(?P=q)[ \t]*$""")
_ITEM_MAPA = re.compile(r"^(?P<sangria>[ \t]{6,})(?P<clave>[A-Za-z_][A-Za-z0-9_]*):[ \t]*(?P<valor>.*?)[ \t]*$")
_REFERENCIA = re.compile(r"^\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)\}$")
_SUSTITUCION = re.compile(r"\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:?-)(?P<defecto>[^}]*))?\}"
                          r"|\$(?P<suelta>[A-Za-z_][A-Za-z0-9_]*)")
_LINEA_DOTENV = re.compile(r"^(?P<pre>[ \t]*(?:export[ \t]+)?)(?P<clave>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=(?P<valor>.*)$")
_SEGURO_SIN_COMILLAS = re.compile(r"^[A-Za-z0-9_.,:/@+%=-]*$")
#: Lo que el panel reconoce como «la app» cuando una variable todavía no está en ningún lado.
_MARCAS_DE_LA_APP = ("LIBRA_PANEL_TOKEN", SERVICIO, INTEGRACION, "LIBRA_INSTANCIA", "SECRET_KEY")


class TokenError(Exception):
    """No se pudo leer o escribir una credencial; el mensaje nunca trae un valor."""


def anterior_de(var: str) -> str:
    return var + SUFIJO_ANTERIOR


def nuevo_token() -> str:
    """64 hex, como `LIBRA_PANEL_TOKEN`: viaja en un header y no tiene caracteres que haya que escapar."""
    return secrets.token_hex(32)


def huella(valor: str | None) -> str:
    """Los primeros 8 hex del SHA-256 del valor (`""` si no hay): para mostrar sin revelar."""
    return hashlib.sha256(valor.encode("utf-8")).hexdigest()[:8] if valor else ""


def entorno_para_compose(base: dict[str, str] | None = None) -> dict[str, str]:
    """El entorno con el que correr `docker compose` sobre una instancia.

    🔴 `docker compose` resuelve los `${VAR}` **primero contra el entorno del proceso** y después
    contra el `.env`. El backoffice tiene su propio `LIBRA_SERVICE_TOKEN` en el entorno, así que un
    `up -d` lanzado desde él le pisaría a una instancia con referencia el valor de su `.env` por el
    del backoffice: la credencial volvería a ser una por producto sin que nadie lo vea. Por eso se
    sacan las cuatro de `PROPIAS_DE_LA_INSTANCIA`.
    """
    entorno = dict(os.environ if base is None else base)
    for var in PROPIAS_DE_LA_INSTANCIA:
        entorno.pop(var, None)
    return entorno


# ── lectura ──────────────────────────────────────────────────────────────────


@dataclass
class _Item:
    linea: int
    clave: str
    crudo: str


@dataclass
class _Servicio:
    nombre: str
    inicio: int
    fin: int
    image: str = ""
    env: int | None = None  # línea de `environment:`
    forma: str = "lista"  # `lista` (`- K=v`) o `mapa` (`K: v`)
    sangria: str = "      "
    items: dict[str, _Item] | None = None
    ultimo: int | None = None  # última línea con un ítem (o `env` si no hay)


def _servicios(lineas: list[str]) -> list[_Servicio]:
    """Los servicios de `services:` con su `environment:` (si lo tienen) ya partido en ítems."""
    servicios: list[_Servicio] = []
    en_services = False
    actual: _Servicio | None = None
    for i, linea in enumerate(lineas):
        if linea and not linea[0].isspace() and not linea.startswith("#"):
            en_services = linea.rstrip() == "services:"
            if actual:
                actual.fin = i
                actual = None
            continue
        if not en_services:
            continue
        s = _SERVICIO.match(linea)
        if s:
            if actual:
                actual.fin = i
            actual = _Servicio(s.group("nombre"), i, len(lineas), items={})
            servicios.append(actual)
            continue
        if actual is None:
            continue
        m = _IMAGE.match(linea)
        if m and not actual.image:
            actual.image = m.group("ref")
    if actual:
        actual.fin = len(lineas)

    for s in servicios:
        s.env = next((i for i in range(s.inicio, s.fin) if _ENVIRONMENT.match(lineas[i])), None)
        if s.env is None:
            continue
        s.ultimo = s.env
        i = s.env + 1
        while i < s.fin:
            linea = lineas[i]
            if not linea.strip() or linea.lstrip().startswith("#"):
                i += 1
                continue
            m = _ITEM_LISTA.match(linea)
            if m:
                s.forma = "lista"
            else:
                m = _ITEM_MAPA.match(linea)
                if not m:
                    break
                s.forma = "mapa"
            crudo = m.group("valor")
            if s.forma == "mapa" and len(crudo) >= 2 and crudo[0] == crudo[-1] and crudo[0] in "\"'":
                crudo = crudo[1:-1]
            s.sangria = m.group("sangria")
            s.items[m.group("clave")] = _Item(i, m.group("clave"), crudo)
            s.ultimo = i
            i += 1
    return servicios


def _leer_dotenv(texto: str) -> dict[str, str]:
    valores: dict[str, str] = {}
    for linea in texto.splitlines():
        if not linea.strip() or linea.lstrip().startswith("#"):
            continue
        m = _LINEA_DOTENV.match(linea)
        if not m:
            continue
        valor = m.group("valor").strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        else:
            valor = re.sub(r"[ \t]+#.*$", "", valor)
        valores[m.group("clave")] = valor
    return valores


def _resolver(crudo: str, dotenv: dict[str, str]) -> str:
    """El valor que vería el contenedor, con los `${VAR}` resueltos contra el `.env` (no contra el entorno)."""
    def _uno(m: re.Match) -> str:
        var = m.group("var") or m.group("suelta")
        valor = dotenv.get(var)
        op = m.group("op")
        if op is not None and (valor is None or (op == ":-" and valor == "")):
            return m.group("defecto") or ""
        return valor or ""
    return _SUSTITUCION.sub(_uno, crudo.replace("$$", "\0")).replace("\0", "$")


@dataclass
class _Archivos:
    dir: Path
    compose: str
    dotenv: str | None

    @classmethod
    def de(cls, dir_instancia) -> _Archivos:
        d = Path(dir_instancia)
        try:
            compose = (d / COMPOSE).read_text(encoding="utf-8")
        except OSError as e:
            raise TokenError(f"No se pudo leer {d / COMPOSE}: {e.strerror or e}") from None
        try:
            dotenv = (d / DOTENV).read_text(encoding="utf-8")
        except FileNotFoundError:
            dotenv = None
        except OSError as e:
            raise TokenError(f"No se pudo leer {d / DOTENV}: {e.strerror or e}") from None
        return cls(d, compose, dotenv)

    def valores_dotenv(self) -> dict[str, str]:
        return _leer_dotenv(self.dotenv or "")


def _buscar(servicios: list[_Servicio], var: str) -> tuple[_Servicio, _Item] | None:
    for s in servicios:
        if var in (s.items or {}):
            return s, s.items[var]
    return None


def leer(dir_instancia, var: str) -> str | None:
    """El valor de `var` en la instancia (compose literal o `${}` contra su `.env`); `None` si no tiene.

    `None` también para la variable declarada vacía: para libraauth es lo mismo que ausente.
    """
    archivos = _Archivos.de(dir_instancia)
    hallado = _buscar(_servicios(archivos.compose.split("\n")), var)
    if not hallado:
        return None
    valor = _resolver(hallado[1].crudo, archivos.valores_dotenv()).strip()
    return valor or None


def donde(dir_instancia, var: str) -> str | None:
    """`"compose"`, `".env"` o `None`: dónde vive hoy el valor de `var` (para mostrar, sin valores)."""
    archivos = _Archivos.de(dir_instancia)
    hallado = _buscar(_servicios(archivos.compose.split("\n")), var)
    if not hallado:
        return None
    return DOTENV if _REFERENCIA.match(hallado[1].crudo) else "compose"


# ── escritura ────────────────────────────────────────────────────────────────


def _servicio_de_la_app(servicios: list[_Servicio]) -> _Servicio:
    """El servicio donde poner una variable que todavía no está en ningún lado.

    El que ya tiene alguna de las credenciales de la familia; si ninguno, el único con
    `environment:` cuya imagen no es un PostgreSQL. Si no se puede saber, no se adivina.
    """
    con_env = [s for s in servicios if s.env is not None]
    for s in con_env:
        if any(m in s.items for m in _MARCAS_DE_LA_APP):
            return s
    candidatos = [s for s in con_env if not s.image.split("/")[-1].startswith("postgres")]
    if len(candidatos) == 1:
        return candidatos[0]
    raise TokenError("No se pudo identificar el servicio de la app (o no tiene un `environment:`); "
                     "no se toca nada.")


def _usa_dotenv(s: _Servicio, dotenv: dict[str, str]) -> bool:
    """Si la app ya toma algún valor del `.env` por referencia: entonces lo nuevo también va ahí."""
    for item in s.items.values():
        m = _REFERENCIA.match(item.crudo)
        if m and m.group("var") in dotenv:
            return True
    return False


def _linea_compose(s: _Servicio, clave: str, valor: str) -> str:
    if s.forma == "mapa":
        return f"{s.sangria}{clave}: {valor}" if _SEGURO_SIN_COMILLAS.match(valor) else f'{s.sangria}{clave}: "{valor}"'
    if _SEGURO_SIN_COMILLAS.match(valor) or _REFERENCIA.match(valor):
        return f"{s.sangria}- {clave}={valor}"
    return f'{s.sangria}- "{clave}={valor}"'


def _linea_dotenv(clave: str, valor: str) -> str:
    return f"{clave}={valor}" if _SEGURO_SIN_COMILLAS.match(valor) else f'{clave}="{valor}"'


def _dotenv_con(texto: str | None, clave: str, valor: str | None) -> str:
    """El `.env` con `clave` puesta (o sacada si `valor` es None), en su lugar si ya estaba."""
    lineas = (texto or "").split("\n")
    if lineas and lineas[-1] == "":
        lineas.pop()
    nuevas, puesta = [], False
    for linea in lineas:
        m = _LINEA_DOTENV.match(linea)
        if m and m.group("clave") == clave:
            if valor is not None and not puesta:
                nuevas.append(m.group("pre") + _linea_dotenv(clave, valor))
                puesta = True
            continue
        nuevas.append(linea)
    if valor is not None and not puesta:
        nuevas.append(_linea_dotenv(clave, valor))
    return "\n".join(nuevas) + "\n"


def _validar(var: str, valor: str | None) -> None:
    if not _NOMBRE.match(var):
        raise TokenError(f"`{var}` no es un nombre de variable válido.")
    if valor is not None and (not valor or any(c in valor for c in "$\n\r\"\\'")):
        raise TokenError(f"El valor para `{var}` está vacío o tiene caracteres que no se escriben.")


def _aplicar(archivos: _Archivos, var: str, valor: str | None, junto_a: str | None) -> None:
    lineas = archivos.compose.split("\n")
    servicios = _servicios(lineas)
    hallado = _buscar(servicios, var)
    dotenv = archivos.valores_dotenv()

    if hallado:
        s, item = hallado
        ref = _REFERENCIA.match(item.crudo)
        if ref:
            # Vive en el `.env`: se escribe ahí. Al borrar, sale de los dos lados.
            archivos.dotenv = _dotenv_con(archivos.dotenv, ref.group("var"), valor)
            if valor is None:
                del lineas[item.linea]
        elif valor is None:
            del lineas[item.linea]
        else:
            lineas[item.linea] = _linea_compose(s, var, valor)
        archivos.compose = "\n".join(lineas)
        return

    if valor is None:
        return  # borrar lo que no está: nada que hacer

    vecino = _buscar(servicios, junto_a) if junto_a else None
    if vecino:
        s, despues_de = vecino[0], vecino[1].linea
        en_dotenv = bool(_REFERENCIA.match(vecino[1].crudo))
    else:
        s = _servicio_de_la_app(servicios)
        despues_de = s.ultimo
        en_dotenv = _usa_dotenv(s, dotenv)
    if en_dotenv:
        archivos.dotenv = _dotenv_con(archivos.dotenv, var, valor)
        lineas.insert(despues_de + 1, _linea_compose(s, var, "${" + var + "}"))
    else:
        lineas.insert(despues_de + 1, _linea_compose(s, var, valor))
    archivos.compose = "\n".join(lineas)


def _escribir_600(ruta: Path, texto: str) -> None:
    tmp = ruta.with_name(ruta.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(texto)
        os.chmod(tmp, 0o600)
        tmp.replace(ruta)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _respaldar(ruta: Path, texto: str, sello: str) -> Path:
    destino = ruta.with_name(f"{ruta.name}.antes-token-{sello}")
    n = 2
    while destino.exists():
        destino = ruta.with_name(f"{ruta.name}.antes-token-{sello}-{n}")
        n += 1
    fd = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(texto)
    return destino


def escribir(dir_instancia, cambios: dict[str, str | None], *, sello: str,
             junto_a: dict[str, str] | None = None) -> list[Path]:
    """Pone (o saca, con `None`) cada variable de `cambios` en la instancia, en orden.

    - Lo que ya está se escribe donde está (`.env` si es `${}`, literal si es literal).
    - Lo nuevo va al lado de `junto_a[var]` si esa ya está, con su misma forma (así
      `<VAR>_ANTERIOR` queda pegada a `<VAR>`); si no, al final del `environment:` de la app, en
      `.env` si la app ya toma valores de ahí y literal si no.
    - Cada archivo que cambia se respalda antes como `<archivo>.antes-token-<sello>` y se reemplaza
      de forma atómica; los dos con modo 600. Lo que no cambia no se toca.

    Devuelve los respaldos escritos (vacío si no cambió nada). `TokenError` si no se puede, y en ese
    caso no se escribió nada.
    """
    for var, valor in cambios.items():
        _validar(var, valor)
    archivos = _Archivos.de(dir_instancia)
    compose_antes, dotenv_antes = archivos.compose, archivos.dotenv
    for var, valor in cambios.items():
        _aplicar(archivos, var, valor, (junto_a or {}).get(var))

    respaldos: list[Path] = []
    d = archivos.dir
    dotenv_escrito = False
    try:
        if archivos.dotenv != dotenv_antes and archivos.dotenv is not None:
            if dotenv_antes is not None:
                respaldos.append(_respaldar(d / DOTENV, dotenv_antes, sello))
            _escribir_600(d / DOTENV, archivos.dotenv)
            dotenv_escrito = True
        if archivos.compose != compose_antes:
            respaldos.append(_respaldar(d / COMPOSE, compose_antes, sello))
            _escribir_600(d / COMPOSE, archivos.compose)
    except OSError as e:
        # Los dos archivos van juntos: si el compose no se pudo escribir, el `.env` vuelve a como
        # estaba (o desaparece, si lo creamos recién).
        if dotenv_escrito:
            try:
                if dotenv_antes is None:
                    (d / DOTENV).unlink()
                else:
                    _escribir_600(d / DOTENV, dotenv_antes)
            except OSError:
                pass
        raise TokenError(f"No se pudo escribir en {d}: {e.strerror or e}") from None
    return respaldos
