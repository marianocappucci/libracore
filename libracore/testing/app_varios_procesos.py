"""La app mínima con la que `libracore` prueba sus candados bajo `uvicorn --workers N`.

No es parte del motor: es el banco de pruebas de `tests/test_varios_procesos.py`, y un ejemplo copiable para el test del
mismo tipo de un producto (ver `libracore.testing.varios_procesos`). Se levanta así::

    PROCESOS_DB=<url postgresql> python -m uvicorn libracore.testing.app_varios_procesos:app --workers 2

Lo que expone:

- `GET /pid[?ocupado=s]`: el pid del worker que contestó (para saber cuántos procesos hay y que están vivos).
- `GET /semilla`: cuántas filas dejó el hook de arranque. Con `una_vez_al_arrancar` tiene que ser **1** aunque
  arranquen dos workers; con `PROCESOS_SIN_CANDADOS=<n workers>` el mismo hook corre sin candado y deja una por worker.
- `POST /nc/{id}`: una nota de crédito de verdad (`emitir_nota_de_credito`) con costuras de juguete que tardan lo que
  tarda ARCA. `?con_candado=0` no le pasa la conexión al candado: es la contraprueba (lo que había antes).
- `GET /notas/{id}`: cuántas notas quedaron emitidas para esa factura.

`preparar_base(url)` crea las dos tablas; lo llama el test **antes** de levantar los workers (dos workers haciendo
`CREATE TABLE` a la vez es otra carrera, y no es la que se está probando).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from libracore import notas_de_credito as nc
from libracore.candados import una_vez_al_arrancar
from libracore.db import core

_DDL = (
    "CREATE TABLE IF NOT EXISTS procesos_semilla (id SERIAL PRIMARY KEY, pid INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS procesos_barrera (pid INTEGER PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS procesos_notas (id SERIAL PRIMARY KEY, original INTEGER NOT NULL, "
    "tipo INTEGER NOT NULL, punto_venta INTEGER NOT NULL, numero INTEGER NOT NULL, cae TEXT, pid INTEGER NOT NULL)",
)

#: Cuánto tarda el «ARCA» de juguete en dar el número de la nota. Es la ventana en la que, sin candado de base, dos
#: procesos contestan los dos «esta factura no tiene notas».
DEMORA_DE_ARCA = 0.6

#: Cuánto tarda el hook de arranque entre mirar y escribir: la ventana de su carrera.
DEMORA_DEL_ARRANQUE = 0.3


def preparar_base(url: str) -> None:
    """Crea las tablas del banco de pruebas en `url` (una base PostgreSQL)."""
    conn = core.conectar(url)
    try:
        for ddl in _DDL:
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


def _sembrar(esperar_a: int = 0) -> None:
    """El hook de arranque de un producto: «si no existe, crear».

    `esperar_a` > 0 es **sólo para la contraprueba sin candado**: el worker avisa que ya miró y espera a que los demás
    también miren antes de escribir. Sin esa cita la contraprueba depende de qué tan juntos arranquen los workers
    (cada uno importa la app por su cuenta, y un arranque lento no solapa con el otro); con ella los dos están
    seguros adentro de la ventana y el duplicado es determinista.
    """
    conn = core.get_connection()
    try:
        if conn.execute("SELECT COUNT(*) FROM procesos_semilla").fetchone()[0] == 0:
            if esperar_a:
                conn.execute("INSERT INTO procesos_barrera (pid) VALUES (?)", (os.getpid(),))
                conn.commit()
                limite = time.monotonic() + 20
                while conn.execute("SELECT COUNT(*) FROM procesos_barrera").fetchone()[0] < esperar_a:
                    conn.commit()
                    if time.monotonic() > limite:
                        raise RuntimeError("el otro worker nunca llegó a la cita")
                    time.sleep(0.05)
            else:
                time.sleep(DEMORA_DEL_ARRANQUE)
            conn.execute("INSERT INTO procesos_semilla (pid) VALUES (?)", (os.getpid(),))
            conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def _vida(_app: FastAPI):
    core.configure(db_path=os.environ["PROCESOS_DB"])
    if os.environ.get("PROCESOS_SIN_CANDADOS"):
        _sembrar(esperar_a=int(os.environ["PROCESOS_SIN_CANDADOS"]))
    else:
        with una_vez_al_arrancar("sembrar-semilla"):
            _sembrar()
    yield


app = FastAPI(lifespan=_vida)


@app.get("/pid")
async def pid(ocupado: float = 0.0):
    """El pid del worker. `ocupado` lo deja **bloqueado** (a propósito, `time.sleep` en un `async def`) ese tiempo:
    mientras tanto no acepta más conexiones y el sistema operativo se las da al otro worker. Es lo que permite a un test
    ver los dos pids con pedidos simultáneos, en lugar de depender de cuál worker queda primero en la cola."""
    if ocupado:
        time.sleep(ocupado)
    return {"pid": os.getpid()}


@app.get("/semilla")
def semilla():
    with contextlib.closing(core.get_connection()) as conn:
        return {"filas": conn.execute("SELECT COUNT(*) FROM procesos_semilla").fetchone()[0], "pid": os.getpid()}


@app.get("/notas/{original}")
def notas(original: int):
    with contextlib.closing(core.get_connection()) as conn:
        n = conn.execute("SELECT COUNT(*) FROM procesos_notas WHERE original = ?", (original,)).fetchone()[0]
    return {"notas": n, "pid": os.getpid()}


def _factura(original: int) -> dict:
    return {
        "tipo": 11, "punto_venta": 1, "numero": original, "fecha": "2026-09-20",
        "cliente_cuit": "30709332852", "cliente_razon": "Agro Norte", "cliente_iva_cond": 1,
        "subtotal": 1000.0, "iva_amount": 210.0, "total": 1210.0, "concepto": 1,
        "items": [{"description": "Flete", "qty": 1, "unit_price": 1000.0}],
    }


@app.post("/nc/{original}")
async def nota_de_credito(original: int, con_candado: int = 1):
    """El mismo flujo que LibraCargo: una conexión, una transacción, `commit` al final."""
    conn = core.get_connection()
    try:
        def previas():
            filas = conn.execute(
                "SELECT tipo, punto_venta, numero, cae FROM procesos_notas WHERE original = ?", (original,)).fetchall()
            return [{"tipo": f[0], "punto_venta": f[1], "numero": f[2], "cae": f[3], "total": "1210.00"} for f in filas]

        async def numerar(_tipo, _punto_venta):
            await asyncio.sleep(DEMORA_DE_ARCA)
            return 1, None

        def registrar(nota, _contexto):
            return conn.execute(
                "INSERT INTO procesos_notas (original, tipo, punto_venta, numero, cae, pid) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (original, nota["tipo"], nota["punto_venta"], nota["numero"], os.getpid())).lastrowid

        async def pedir_cae(registro, _nota, _contexto):
            conn.execute("UPDATE procesos_notas SET cae = '75123456789012' WHERE id = ?", (registro,))
            return registro

        await nc.emitir_nota_de_credito(
            _factura(original), clave=("procesos", original), cargar_previas=previas,
            numerar=numerar, registrar=registrar, pedir_cae=pedir_cae,
            conn=conn if con_candado else None,
        )
        conn.commit()
        return {"ok": True, "pid": os.getpid()}
    except nc.NotaNoPermitida as e:
        conn.rollback()
        return JSONResponse({"ok": False, "codigo": e.codigo, "pid": os.getpid()}, status_code=409)
    finally:
        conn.close()
