"""`esperar_candado_de_transaccion` dentro de un ciclo de bloqueos: PostgreSQL lo ve y falla en segundos (ADR-081).

🔴 **Por qué existe.** La espera era un bucle de `pg_try_advisory_xact_lock` más `time.sleep`: el que espera no está
esperando ningún bloqueo (duerme), así que si el que tiene el candado espera algo que tiene él —una tabla, por
ejemplo— PostgreSQL **no ve el ciclo** y los dos quedan callados hasta que el tope (120 s) mata a uno con `TimeoutError`.
Medido el 2026-10-24 en LibraClub: un worker murió con `TimeoutError: 'libracommerce.esquema' ... 120 s`.

Ahora la espera es un `pg_advisory_xact_lock` bloqueante con `lock_timeout`: el que espera está en la cola de bloqueos
y PostgreSQL contesta `deadlock detected` a ~1 s (el `deadlock_timeout` por defecto). Estos tests arman el ciclo con
dos conexiones y dos hilos, y comprueban que **termina rápido y con un error claro**, que el `TimeoutError` de siempre
sigue siendo el de siempre y que la espera no deja huellas en la sesión (ni transacción rota ni `lock_timeout` cambiado).

Se saltea sin `LIBRACORE_POSTGRES_URL`. Cada test usa una base descartable propia (`bases`).
"""
import threading
import time

import psycopg
import pytest

from libracore import candados
from libracore.candados import esperar_candado_de_transaccion
from libracore.db import core
from libracore.db.schema import init_core_schema

#: Cuánto dura como máximo la espera de los tests. El ciclo real tiene que resolverse mucho antes (`deadlock_timeout`
#: es 1 s); si no, lo que cortó la espera fue este tope y no el detector de PostgreSQL.
ESPERA = 20.0
#: «En segundos»: lo que tarda el detector (1 s) más margen para una máquina cargada. Sin el arreglo son `ESPERA`.
EN_SEGUNDOS = 8.0


@pytest.fixture
def url_pg(bases):
    return bases.nueva()


def _sqlstate(error: BaseException) -> str | None:
    """El `SQLSTATE` del error: el `ConnectionWrapper` lo traduce a una excepción de `sqlite3` y deja el original en `__cause__`."""
    return getattr(error.__cause__, "sqlstate", None) or getattr(error, "sqlstate", None)


def _esta_esperando_un_bloqueo(url: str, pid: int, tope: float = 10.0) -> None:
    """Espera a que el backend `pid` esté dormido en la cola de bloqueos de PostgreSQL (no en un `sleep` de Python)."""
    limite = time.monotonic() + tope
    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True) as admin:
        while time.monotonic() < limite:
            fila = admin.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()
            if fila and fila[0] == "Lock":
                return
            time.sleep(0.05)
    raise AssertionError(f"el backend {pid} no llegó a esperar un bloqueo en {tope:.0f} s")


class _Hilo(threading.Thread):
    """Corre `trabajo()`; guarda el error (si hubo) y cuánto tardó. Si falló, hace `rollback` para soltar lo que tenía."""

    def __init__(self, conn, trabajo):
        super().__init__(daemon=True)
        self.conn, self.trabajo = conn, trabajo
        self.error: BaseException | None = None
        self.segundos = 0.0

    def run(self):
        t0 = time.monotonic()
        try:
            self.trabajo()
            self.conn.commit()
        except BaseException as e:  # noqa: BLE001 - se devuelve al hilo principal
            self.error = e
            self.conn.rollback()
        finally:
            self.segundos = time.monotonic() - t0


@pytest.mark.parametrize("espera_primero", ["candado", "tabla"])
def test_un_ciclo_entre_un_candado_y_una_tabla_da_deadlock_en_segundos(url_pg, espera_primero):
    """P tiene la tabla y espera el candado; Q tiene el candado y espera la tabla. Antes: 120 s callados y `TimeoutError`.

    Se prueba con cada uno esperando primero, porque PostgreSQL aborta al que detecta el ciclo antes (el que lleva más
    esperando): una vez es el que espera el candado (y el error trae la nota que dice cuál era) y otra el que espera
    la tabla. En los dos casos tiene que ser `deadlock detected`, a ~1 s, y el otro tiene que terminar bien.
    """
    p, q = core.conectar(url_pg), core.conectar(url_pg)
    try:
        p.execute("CREATE TABLE ciclo (x INTEGER)")
        p.commit()
        pid_p = p.execute("SELECT pg_backend_pid()").fetchone()[0]
        pid_q = q.execute("SELECT pg_backend_pid()").fetchone()[0]

        p.execute("INSERT INTO ciclo VALUES (1)")                       # P: ROW EXCLUSIVE sobre la tabla
        assert esperar_candado_de_transaccion(q, "ciclo") is True       # Q: el candado

        quiere_el_candado = _Hilo(p, lambda: esperar_candado_de_transaccion(p, "ciclo", espera=ESPERA))
        quiere_la_tabla = _Hilo(q, lambda: q.execute("LOCK TABLE ciclo IN SHARE MODE"))
        primero, segundo, pid_primero = (
            (quiere_el_candado, quiere_la_tabla, pid_p) if espera_primero == "candado"
            else (quiere_la_tabla, quiere_el_candado, pid_q)
        )
        t0 = time.monotonic()
        primero.start()
        _esta_esperando_un_bloqueo(url_pg, pid_primero)
        segundo.start()
        for hilo in (primero, segundo):
            hilo.join(timeout=ESPERA + 5)
            assert not hilo.is_alive(), "el ciclo no se resolvió: ¿sigue esperando en silencio?"
        total = time.monotonic() - t0

        assert total < EN_SEGUNDOS, f"tardó {total:.1f} s: lo cortó el tope y no el detector de PostgreSQL"
        errores = [h.error for h in (primero, segundo) if h.error is not None]
        assert len(errores) == 1, f"tenía que fallar exactamente uno (el que aborta PostgreSQL): {errores}"
        assert _sqlstate(errores[0]) == "40P01", f"no es `deadlock detected`: {errores[0]!r}"
        assert not isinstance(errores[0], TimeoutError)
        if quiere_el_candado.error is not None:
            notas = " ".join(getattr(quiere_el_candado.error, "__notes__", []))
            assert "'ciclo'" in notas and "rollback" in notas, f"el error no dice qué candado esperaba: {notas!r}"
    finally:
        p.close()
        q.close()


def test_sin_ciclo_el_tope_sigue_siendo_un_timeout_con_el_mismo_mensaje_y_la_transaccion_sana(url_pg):
    """El `TimeoutError` es el de siempre, y la sesión queda como estaba: transacción usable y `lock_timeout` intacto."""
    a, b = core.conectar(url_pg), core.conectar(url_pg)
    try:
        b.execute("SET lock_timeout = '45s'")                           # lo de la sesión de quien llama
        assert esperar_candado_de_transaccion(a, "ocupado") is True
        b.execute("CREATE TABLE sana (x INTEGER)")                      # b ya hizo algo en su transacción

        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match=r"'ocupado': no se pudo tomar el candado en 1 s"):
            esperar_candado_de_transaccion(b, "ocupado", espera=1.0)
        assert 0.8 < time.monotonic() - t0 < 5, "no esperó lo que se le pidió"

        b.execute("INSERT INTO sana VALUES (1)")                        # la transacción de `b` no quedó abortada...
        assert b.execute("SHOW lock_timeout").fetchone()[0] == "45s"    # ...ni cambió el tope de la sesión
        assert esperar_candado_de_transaccion(b, "libre") is True

        # Y cuando espera y lo consigue, tampoco deja huellas.
        liberar = threading.Timer(0.5, a.commit)
        liberar.start()
        assert esperar_candado_de_transaccion(b, "ocupado", espera=10) is True
        liberar.join()
        assert b.execute("SHOW lock_timeout").fetchone()[0] == "45s"
        # Lo tomó dentro de un SAVEPOINT, pero es de la transacción de `b`: otra conexión sigue sin poder tomarlo.
        tercera = core.conectar(url_pg)
        try:
            with pytest.raises(TimeoutError):
                esperar_candado_de_transaccion(tercera, "ocupado", espera=0.3)
        finally:
            tercera.close()
        b.execute("INSERT INTO sana VALUES (2)")
        b.commit()
        assert b.execute("SELECT count(*) FROM sana").fetchone()[0] == 2
    finally:
        a.close()
        b.close()


def test_con_sqlalchemy_el_timeout_tambien_deja_la_sesion_sana(url_pg):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    motor = create_engine(url_pg.replace("postgresql://", "postgresql+psycopg://", 1))
    a = core.conectar(url_pg)
    try:
        esperar_candado_de_transaccion(a, "ocupado")
        with Session(motor) as sesion:
            sesion.execute(text("SELECT 1"))
            with pytest.raises(TimeoutError):
                esperar_candado_de_transaccion(sesion, "ocupado", espera=0.5)
            assert sesion.execute(text("SELECT 1")).scalar() == 1           # sin rollback: la transacción sigue sana
            assert sesion.execute(text("SHOW lock_timeout")).scalar() == "0"
    finally:
        a.close()
        motor.dispose()


def test_espera_cero_no_espera_y_levanta_timeout(url_pg):
    """`espera=0` en PostgreSQL es `lock_timeout = 0`, que es **sin tope**: no debe colgarse."""
    a, b = core.conectar(url_pg), core.conectar(url_pg)
    try:
        esperar_candado_de_transaccion(a, "x")
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            esperar_candado_de_transaccion(b, "x", espera=0)
        assert time.monotonic() - t0 < 2
    finally:
        a.close()
        b.close()


def test_el_sqlstate_se_lee_de_psycopg_de_sqlalchemy_y_del_wrapper():
    class ConEstado(Exception):
        sqlstate = "40P01"

    class DeSqlalchemy(Exception):
        orig = ConEstado()

    traducido = RuntimeError("traducido")
    traducido.__cause__ = ConEstado()

    assert candados._sqlstate(ConEstado()) == "40P01"
    assert candados._sqlstate(DeSqlalchemy()) == "40P01"
    assert candados._sqlstate(traducido) == "40P01"
    assert candados._sqlstate(ValueError("otra cosa")) is None


# ── init_core_schema: el relleno de la caja ya no toma ROW EXCLUSIVE de más ─────────────────────────────────────────


def _locks_de_tabla(conn, tablas):
    return {
        (r[0], r[1]) for r in conn.execute(
            "SELECT relation::regclass::text, mode FROM pg_locks WHERE pid = pg_backend_pid() "
            "AND locktype = 'relation' AND relation::regclass::text = ANY(?)", (list(tablas),),
        ).fetchall()
    }


def test_init_core_schema_sobre_una_base_armada_no_toma_row_exclusive_de_la_caja(url_pg):
    """El `UPDATE ... WHERE caja_id IS NULL` incondicional dejaba un `ROW EXCLUSIVE` hasta el commit aunque no tocara ninguna fila."""
    armada = core.conectar(url_pg)
    init_core_schema(armada)
    armada.commit()
    armada.close()

    conn = core.conectar(url_pg)
    try:
        init_core_schema(conn)
        modos = _locks_de_tabla(conn, ["caja_movimientos", "turnos_caja"])
        assert modos, "no se vio ningún candado de tabla: ¿se perdió la transacción?"
        assert not [m for m in modos if m[1] == "RowExclusiveLock"], modos
    finally:
        conn.close()


def test_init_core_schema_rellena_la_caja_de_las_filas_viejas(url_pg):
    """Y lo condicional no pierde el relleno: las filas con `caja_id` NULL siguen quedando con la caja por defecto."""
    conn = core.conectar(url_pg)
    try:
        init_core_schema(conn)
        conn.commit()
        conn.execute("INSERT INTO usuarios (username, nombre, password_hash) VALUES ('viejo', 'Viejo', 'x')")
        usuario = conn.execute("SELECT id FROM usuarios WHERE username = 'viejo'").fetchone()[0]
        conn.execute("INSERT INTO turnos_caja (usuario_id, apertura) VALUES (?, '2026-01-01')", (usuario,))
        conn.execute("INSERT INTO caja_movimientos (fecha, tipo, concepto, monto) VALUES ('2026-01-01', 'ingreso', 'viejo', 10)")
        conn.commit()
        assert conn.execute("SELECT count(*) FROM turnos_caja WHERE caja_id IS NULL").fetchone()[0] == 1

        init_core_schema(conn)
        conn.commit()
        assert conn.execute("SELECT count(*) FROM turnos_caja WHERE caja_id IS NULL").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM caja_movimientos WHERE caja_id IS NULL").fetchone()[0] == 0
    finally:
        conn.close()
