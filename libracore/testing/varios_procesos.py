"""Levantar uvicorn con **varios workers de verdad** para probar lo que sólo se rompe con varios procesos.

Una suite normal corre la app en un solo proceso (un `TestClient`), y por eso los tests no ven lo que falla cuando dos
workers de la misma instancia pisan lo mismo: la guarda en memoria que no se ve entre procesos, el «si no existe,
crear» de un arranque que corre dos veces, el archivo que otro lee a medio escribir. Este módulo levanta
`python -m uvicorn <app> --workers N` como subproceso, contra una base PostgreSQL de verdad, y deja pegarle con
pedidos simultáneos.

Uso, desde el test de un producto (con su `app` y su base de test)::

    from libracore.testing.varios_procesos import en_paralelo, pedir, servidor

    with servidor("app.main:app", workers=2, env={"DATABASE_URL": url}) as base:
        respuestas = en_paralelo(20, lambda i: pedir(f"{base}/api/lo-que-sea", metodo="POST"))

`libracore.testing.app_varios_procesos` es la app mínima con la que `libracore` prueba sus propios candados.

Requiere `uvicorn` en el entorno (los productos ya lo tienen; en `libracore` es una dependencia de desarrollo).
Cuesta unos 3 s de arranque por servidor: levantar uno por test, no uno por pedido.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pedir(url: str, *, metodo: str = "GET", cuerpo: Any = None, espera: float = 30.0) -> tuple[int, Any]:
    """Un pedido con **conexión propia** (`Connection: close`), para que el sistema operativo reparta cada uno entre
    los workers. Devuelve `(estado, json)`; un cuerpo que no es JSON vuelve como texto."""
    datos = None if cuerpo is None else json.dumps(cuerpo).encode()
    req = urllib.request.Request(url, data=datos, method=metodo,
                                 headers={"Connection": "close", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=espera) as r:
            estado, crudo = r.status, r.read()
    except urllib.error.HTTPError as e:
        estado, crudo = e.code, e.read()
    try:
        return estado, json.loads(crudo)
    except ValueError:
        return estado, crudo.decode(errors="replace")


def en_paralelo(cantidad: int, trabajo: Callable[[int], Any]) -> list[Any]:
    """Corre `trabajo(i)` en `cantidad` hilos que arrancan **juntos** (una barrera) y devuelve los resultados en orden."""
    resultados: list[Any] = [None] * cantidad
    barrera = threading.Barrier(cantidad)

    def correr(i: int) -> None:
        barrera.wait()
        try:
            resultados[i] = trabajo(i)
        except BaseException as e:  # noqa: BLE001 - se devuelve para que el test lo vea, no se pierde en el hilo
            resultados[i] = e

    hilos = [threading.Thread(target=correr, args=(i,)) for i in range(cantidad)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    return resultados


@contextlib.contextmanager
def servidor(
    app: str, *, workers: int = 2, env: dict[str, str] | None = None, puerto: int | None = None,
    salud: str = "/pid", arranque: float = 30.0, cwd: str | None = None,
) -> Iterator[str]:
    """Levanta `uvicorn <app> --workers <workers>` y entrega su URL base (`http://127.0.0.1:<puerto>`).

    Espera a que `GET <salud>` conteste 200 (por defecto `/pid`, de la app mínima). Al salir lo apaga con `SIGTERM`
    —uvicorn baja a sus workers— y, si no se va, lo mata. Si no llega a levantar, el error trae lo que escribió
    el proceso en su stderr.
    """
    puerto = puerto or puerto_libre()
    log = tempfile.TemporaryFile()
    proceso = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(puerto),
         "--workers", str(workers), "--log-level", "warning"],
        env={**os.environ, **(env or {})}, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{puerto}"
    try:
        limite = time.monotonic() + arranque
        listo = False
        while not listo and proceso.poll() is None and time.monotonic() < limite:
            try:
                listo = pedir(base + salud, espera=2)[0] == 200
            except OSError:
                pass
            if not listo:
                time.sleep(0.1)
        if not listo:
            log.seek(0)
            raise RuntimeError(f"uvicorn no levantó ({app}, {workers} workers):\n{log.read().decode(errors='replace')}")
        yield base
    finally:
        proceso.terminate()
        try:
            proceso.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proceso.kill()
            proceso.wait()
        log.close()


def pids_vistos(base: str, *, cuantos: int = 2, tandas: int = 20, ruta: str = "/pid") -> set[int]:
    """Los `pid` distintos que contestan en `base + ruta`, hasta juntar `cuantos` (o agotar las `tandas`).

    Es la prueba de que los workers están **vivos y son procesos distintos**, y no uno solo que contesta por todos.
    Pega con 8 pedidos simultáneos por tanda y le pide a cada uno `?ocupado=0.2`: pedidos de a uno los contesta
    siempre el mismo worker (el que queda libre primero), y sin esa ocupación una tanda cae entera en uno.
    """
    vistos: set[int] = set()
    for _ in range(tandas):
        for estado, cuerpo in en_paralelo(8, lambda _i: pedir(f"{base}{ruta}?ocupado=0.2")):
            if estado == 200:
                vistos.add(int(cuerpo["pid"]))
        if len(vistos) >= cuantos:
            break
    return vistos
