"""Conservar los códigos de acceso de una demo a través del reset nocturno.

**El problema.** `demo_codigos` (de libraauth: los códigos que se le entregan a
un cliente potencial, válidos 7 días y 10 usos) vive en la misma base que la
demo, y `scripts/reset_demo.sh` de cada producto la recrea todas las noches con
`DROP SCHEMA`. Medido en `/var/log/demo_reset.log` del 2026-10-09: en seis demos
el reset se llevaba todos los códigos entregados esa misma noche. Sólo
LibraCargo y LibraClub tenían un bloque bash propio que la guardaba y la
devolvía; era una copia a mano que las otras seis no tenían. Acá vive una sola
vez (ADR-039).

**Cómo se usa** (desde el `.venv-scripts` del producto, en el VPS):

    libracore-demo-codigos guardar  --sidecar S --archivo F [--base B]   # antes del DROP SCHEMA
    libracore-demo-codigos devolver --sidecar S --archivo F [--base B]   # con la app ya arriba

`devolver` va **después** de que la app arrancó porque es libraauth quien crea
la tabla al arrancar; el volcado es `--data-only`.

**Códigos de salida** (el script llamador decide qué hacer con cada uno):

- `0`: todo bien. Incluye «todavía no existe `demo_codigos`: nada que
  preservar» (`guardar`) y «no había archivo» (`devolver`).
- `1`: `devolver` no pudo volver a cargar el volcado. **El archivo queda** para
  reintentar a mano. Un reset que sigue de largo con esto pierde los códigos, no
  la demo: en general conviene loguear y seguir.
- `2`: Docker o `psql`/`pg_dump` fallaron en `guardar` (o al contar), o los
  argumentos no sirven. En `guardar` esto significa que **no se sabe** si había
  códigos: abortar el reset antes del `DROP SCHEMA` es lo prudente.

**Secretos.** `$POSTGRES_USER` y `$POSTGRES_DB` se resuelven *dentro* del
sidecar (el `sh -c` va con comillas simples y sin expandir en el host), así la
contraseña y el usuario nunca pasan por la línea de comandos del host. El
volcado se escribe con permisos 600: son códigos de acceso vivos.
"""
import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

TABLA = "demo_codigos"

#: Segundos por orden. Un `pg_dump` de esta tabla (decenas de filas) tarda
#: menos de uno: si Docker no contesta en un minuto, no contesta.
_TIMEOUT = 60

# La base y el sidecar acaban dentro de un comando (el primero dentro de un
# `sh -c`), así que se aceptan sólo identificadores simples. Nada de espacios,
# comillas, `;` ni `-` al principio (sería una opción de docker).
_BASE_VALIDA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SIDECAR_VALIDO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

MSG_PRESERVAR = "codigos de acceso a preservar: {n}"
MSG_NADA = "todavia no existe demo_codigos: nada que preservar"
MSG_DEVUELTOS = "codigos de acceso devueltos: {n}"
MSG_OJO = "OJO: no se pudieron devolver los codigos de acceso. Hay que emitir uno nuevo."

EXIT_OK = 0
EXIT_NO_DEVUELTOS = 1
EXIT_DOCKER = 2


class DockerFalla(Exception):
    """Docker, `psql` o `pg_dump` no contestaron lo esperado."""


class NoSePudieronDevolver(Exception):
    """`psql` rechazó el volcado. El archivo **no se borra**, para reintentar a mano."""


def _correr_subprocess(args: Sequence[str], *, entrada: str | None = None, timeout: int = _TIMEOUT):
    """Ejecutor por defecto: `subprocess.run` sin shell, con texto UTF-8."""
    return subprocess.run(
        list(args), input=entrada, capture_output=True, text=True,
        encoding="utf-8", timeout=timeout, check=False,
    )


Correr = Callable[..., "subprocess.CompletedProcess"]


def _validar_base(base: str | None) -> str | None:
    if base is not None and not _BASE_VALIDA.match(base):
        raise ValueError(f"nombre de base invalido: {base!r} (solo letras, digitos y _)")
    return base


def _validar_sidecar(sidecar: str) -> str:
    if not _SIDECAR_VALIDO.match(sidecar or ""):
        raise ValueError(f"nombre de sidecar invalido: {sidecar!r}")
    return sidecar


def _bd(base: str | None) -> str:
    """La base tal como va en el `sh -c`: la variable sin expandir o el nombre validado."""
    return '"$POSTGRES_DB"' if base is None else _validar_base(base)


def _docker(sidecar: str, script: str, *, interactivo: bool = False) -> list[str]:
    return ["docker", "exec", *(["-i"] if interactivo else []), _validar_sidecar(sidecar), "sh", "-c", script]


def _psql(base: str | None, opciones: str) -> str:
    return f'psql {opciones} -U "$POSTGRES_USER" -d {_bd(base)}'


def _ejecutar(correr: Correr, args: list[str], *, entrada: str | None = None):
    try:
        r = correr(args, entrada=entrada)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DockerFalla(f"no se pudo ejecutar docker: {e}") from e
    if r.returncode != 0:
        detalle = (r.stderr or "").strip().splitlines()
        raise DockerFalla(f"docker exec termino con codigo {r.returncode}: "
                          f"{detalle[-1] if detalle else 'sin detalle'}")
    return r


def _contar(sidecar: str, base: str | None, correr: Correr) -> int:
    script = f'{_psql(base, "-tA")} -c "SELECT COUNT(*) FROM {TABLA}"'
    r = _ejecutar(correr, _docker(sidecar, script))
    try:
        return int((r.stdout or "").strip())
    except ValueError as e:
        raise DockerFalla(f"el conteo de {TABLA} no es un numero: {r.stdout!r}") from e


def _existe_tabla(sidecar: str, base: str | None, correr: Correr) -> bool:
    script = (f'{_psql(base, "-tA")} -c "SELECT 1 FROM information_schema.tables '
              f"WHERE table_name = '{TABLA}'\"")
    r = _ejecutar(correr, _docker(sidecar, script))
    return (r.stdout or "").strip() == "1"


def _escribir_600(archivo: Path, texto: str) -> None:
    """Crea `archivo` con 0600 desde el primer instante (sin ventana de carrera).

    Si ya existía se lo borra antes: `O_TRUNC` sobre un archivo previo
    conservaría sus permisos viejos. `O_EXCL` hace que, si alguien lo recrea en
    el medio, falle en vez de escribir en un archivo ajeno.
    """
    archivo.unlink(missing_ok=True)
    fd = os.open(archivo, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(texto)


def guardar(sidecar: str, archivo: Path, base: str | None = None, *, correr: Correr = _correr_subprocess) -> int | None:
    """Vuelca `demo_codigos` a `archivo` (0600) y devuelve cuántas filas tiene.

    `None` si la tabla no existe todavía: no escribe nada. `base=None` usa
    `$POSTGRES_DB` del sidecar. Levanta `DockerFalla` si algo falla, y en ese
    caso no deja un volcado a medias.
    """
    archivo = Path(archivo)
    _validar_base(base)
    _validar_sidecar(sidecar)
    if not _existe_tabla(sidecar, base, correr):
        return None
    script = f'pg_dump -U "$POSTGRES_USER" -d {_bd(base)} --data-only --table={TABLA}'
    volcado = _ejecutar(correr, _docker(sidecar, script)).stdout or ""
    n = _contar(sidecar, base, correr)
    _escribir_600(archivo, volcado)
    return n


def devolver(sidecar: str, archivo: Path, base: str | None = None, *, correr: Correr = _correr_subprocess) -> int | None:
    """Carga el volcado de `archivo` en la base nueva y devuelve el conteo final.

    `None` si el archivo no existe o está vacío (no hace nada). Si `psql`
    rechaza el volcado levanta `NoSePudieronDevolver` y **deja el archivo**.
    Cuando devuelve bien, borra el archivo.
    """
    archivo = Path(archivo)
    _validar_base(base)
    _validar_sidecar(sidecar)
    if not archivo.is_file() or archivo.stat().st_size == 0:
        return None
    script = _psql(base, "-q -v ON_ERROR_STOP=1")
    try:
        _ejecutar(correr, _docker(sidecar, script, interactivo=True),
                  entrada=archivo.read_text(encoding="utf-8"))
    except DockerFalla as e:
        raise NoSePudieronDevolver(str(e)) from e
    archivo.unlink()
    return _contar(sidecar, base, correr)


def _base_arg(valor: str) -> str:
    try:
        return _validar_base(valor)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _sidecar_arg(valor: str) -> str:
    try:
        return _validar_sidecar(valor)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="libracore-demo-codigos",
        description="Guarda y devuelve los codigos de acceso de una demo a traves del reset nocturno.",
        epilog="Salida: 0 ok (incluye 'nada que preservar'); 1 devolver fallo (el archivo queda); "
               "2 fallo Docker/psql o argumentos invalidos.",
    )
    sub = p.add_subparsers(dest="accion", required=True)
    for nombre, ayuda in (("guardar", "volcar demo_codigos antes del DROP SCHEMA"),
                          ("devolver", "volver a cargar el volcado con la app ya arriba")):
        s = sub.add_parser(nombre, help=ayuda)
        s.add_argument("--sidecar", required=True, type=_sidecar_arg, help="contenedor PostgreSQL sidecar")
        s.add_argument("--archivo", required=True, type=Path, help="archivo del volcado (se crea 0600)")
        s.add_argument("--base", type=_base_arg, default=None,
                       help="nombre de la base; por omision $POSTGRES_DB del sidecar")
    return p


def main(argv: list[str] | None = None, *, correr: Correr = _correr_subprocess) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.accion == "guardar":
            n = guardar(args.sidecar, args.archivo, args.base, correr=correr)
            print(MSG_NADA if n is None else MSG_PRESERVAR.format(n=n))
        else:
            n = devolver(args.sidecar, args.archivo, args.base, correr=correr)
            if n is not None:
                print(MSG_DEVUELTOS.format(n=n))
    except NoSePudieronDevolver as e:
        print(MSG_OJO)
        print(f"[ERROR] {e}", file=sys.stderr)
        return EXIT_NO_DEVUELTOS
    except DockerFalla as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return EXIT_DOCKER
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
