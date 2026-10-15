"""Un proceso que crea el esquema del core, para probar `init_core_schema` con varios procesos a la vez.

No es parte del motor: es el banco de pruebas de `tests/test_esquema_en_paralelo.py`, y se copia igual para el test del
mismo tipo de un producto. Se corre así, uno por proceso::

    python -m libracore.testing.esquema_en_paralelo <url postgresql> <t0>

`t0` es una hora (`time.time()`) a la que **todos** los procesos arrancan el esquema: cada uno tarda un rato en importar
el motor, y sin esa cita el primero termina antes de que el segundo empiece y la carrera no se ve. Sale con 0 si el
esquema quedó creado y con 1 —escribiendo el error en la salida— si no (un deadlock, una clave duplicada del catálogo).
"""
from __future__ import annotations

import sys
import time

from libracore.db import core
from libracore.db.schema import init_core_schema


def main(url: str, t0: float) -> int:
    core.configure(url)
    time.sleep(max(0.0, t0 - time.time()))
    conn = core.get_connection()
    try:
        init_core_schema(conn)
        conn.commit()
    except Exception as e:  # noqa: BLE001 - el test necesita ver QUÉ falló, sea lo que sea
        print(f"{type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], float(sys.argv[2])))
