"""Bloqueo exclusivo de un archivo entre procesos, en Linux y en Windows.

`fcntl` no existe en Windows, y el nodo de LibraEdge corre el motor ahí (ADR-010 de libraedge): importar `fcntl` al
cargar un módulo dejaba al nodo sin migraciones ni ARCA. Este módulo es la única costura con el sistema operativo; los
que necesitan un candado de archivo (`candados`, `arca_wsaa`) piden **sin bloquear** y esperan ellos (con `time.sleep`
o `asyncio.sleep`), así que acá sólo hay «intentar» y «soltar».

- **POSIX**: `flock(LOCK_EX | LOCK_NB)`. Dos `open` del mismo archivo, incluso en el mismo proceso, se excluyen.
- **Windows**: `msvcrt.locking(LK_NBLCK)` sobre el primer byte. También se excluyen dos `open` del mismo proceso, y el
  sistema lo suelta si el proceso muere, igual que `flock`.
"""
from __future__ import annotations

import os

if os.name == "nt":
    import msvcrt

    def intentar(fd: int) -> bool:
        """Toma el bloqueo exclusivo de `fd` sin esperar. `True` si lo tomó, `False` si lo tiene otro."""
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def soltar(fd: int) -> None:
        """Suelta el bloqueo que tomó `intentar` sobre `fd`."""
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def intentar(fd: int) -> bool:
        """Toma el bloqueo exclusivo de `fd` sin esperar. `True` si lo tomó, `False` si lo tiene otro."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def soltar(fd: int) -> None:
        """Suelta el bloqueo que tomó `intentar` sobre `fd`."""
        fcntl.flock(fd, fcntl.LOCK_UN)
