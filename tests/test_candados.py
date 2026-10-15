"""`libracore.candados`: los candados que hacen falta para correr una instancia con varios workers.

SQLite se prueba siempre; PostgreSQL con `LIBRACORE_POSTGRES_URL` (en CI la pone el workflow). Cada test de PostgreSQL
usa una **base descartable propia** (`bases`): los candados asesores son por base, y así ninguno ve los de otro test.
"""
import multiprocessing
import os
import sqlite3
import threading
import time

import pytest

from libracore import candados
from libracore.candados import candado_de_base, esperar_candado_de_transaccion, una_vez_al_arrancar
from libracore.db import core
from libracore.db._postgres import ConnectionWrapper


@pytest.fixture(autouse=True)
def _archivos_de_candado_en_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


# ── Sin PostgreSQL ──────────────────────────────────────────────────────────

def test_sin_conexion_el_candado_es_del_proceso_y_se_suelta_al_salir():
    with candado_de_base(None, "a") as uno:
        assert uno is True
        with candado_de_base(None, "a") as otro:
            assert otro is False
        with candado_de_base(None, "b") as distinto:
            assert distinto is True
    with candado_de_base(None, "a") as de_nuevo:
        assert de_nuevo is True


def test_se_suelta_aunque_falle_lo_de_adentro():
    with pytest.raises(RuntimeError), candado_de_base(None, "a"):
        raise RuntimeError("ARCA no contesta")
    with candado_de_base(None, "a") as de_nuevo:
        assert de_nuevo is True


def test_con_sqlite_el_candado_es_un_flock_y_se_ve_entre_procesos(tmp_path):
    conn = sqlite3.connect(":memory:")
    with candado_de_base(conn, "nota"):
        assert any(f.name.startswith(".libracore-candado-") for f in tmp_path.iterdir()), "no creó su archivo"
        # Otro proceso que comparte DATA_DIR no lo obtiene.
        assert _otro_proceso_lo_obtiene(tmp_path, "nota") is False
    assert _otro_proceso_lo_obtiene(tmp_path, "nota") is True


def _intentar(data_dir, nombre, cola):
    os.environ["DATA_DIR"] = str(data_dir)
    with candado_de_base(sqlite3.connect(":memory:"), nombre) as obtenido:
        cola.put(obtenido)


def _otro_proceso_lo_obtiene(data_dir, nombre) -> bool:
    ctx = multiprocessing.get_context("spawn")
    cola = ctx.Queue()
    p = ctx.Process(target=_intentar, args=(data_dir, nombre, cola))
    p.start()
    p.join(30)
    return cola.get(timeout=5)


def test_un_arranque_en_sqlite_serializa_entre_hilos(tmp_path):
    """`una_vez_al_arrancar` sobre SQLite: de a uno por vez, aunque entren juntos."""
    core.configure(db_path=str(tmp_path / "x.db"))
    try:
        adentro, solapados = [], []

        def hook():
            with una_vez_al_arrancar("sembrar", espera=20):
                adentro.append(1)
                solapados.append(len(adentro))
                time.sleep(0.05)
                adentro.pop()

        hilos = [threading.Thread(target=hook) for _ in range(6)]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        assert solapados == [1] * 6
    finally:
        core._db_path = None


def test_un_arranque_que_no_consigue_el_candado_dice_por_que(tmp_path):
    core.configure(db_path=str(tmp_path / "x.db"))
    try:
        with una_vez_al_arrancar("sembrar"), pytest.raises(TimeoutError, match="sembrar"):
            with una_vez_al_arrancar("sembrar", espera=0.2):
                pass
    finally:
        core._db_path = None


def test_sin_configurar_y_sin_destino_avisa():
    estaba = core._db_path
    core._db_path = None
    try:
        with pytest.raises(RuntimeError, match="no está configurado"), una_vez_al_arrancar("x"):
            pass
    finally:
        core._db_path = estaba


def test_esperar_candado_de_transaccion_en_sqlite_no_hace_nada():
    """SQLite serializa a sus escritores solo: no se inventa un `flock`, y quien llama sigue sin esperar a nadie."""
    conn = sqlite3.connect(":memory:")
    assert esperar_candado_de_transaccion(conn, "esquema") is False
    assert esperar_candado_de_transaccion(conn, "esquema") is False


def test_una_conexion_desconocida_no_se_adivina():
    with pytest.raises(TypeError, match="no sé qué conexión"), candado_de_base(object(), "x"):
        pass


# ── Con PostgreSQL ──────────────────────────────────────────────────────────

@pytest.fixture
def url_pg(bases):
    return bases.nueva()


def _wrapper(url):
    return core.conectar(url)


def test_postgres_dos_conexiones_se_excluyen_hasta_el_fin_de_la_transaccion(url_pg):
    a, b = _wrapper(url_pg), _wrapper(url_pg)
    try:
        assert isinstance(a, ConnectionWrapper)
        with candado_de_base(a, "nota:1") as ganador:
            assert ganador is True
        # Salió el `with` pero la transacción de `a` sigue abierta: el candado sigue puesto.
        with candado_de_base(b, "nota:1") as perdedor:
            assert perdedor is False
        # Otro nombre no se pisa.
        with candado_de_base(b, "nota:2") as otro:
            assert otro is True
        a.commit()
        b.rollback()
        with candado_de_base(b, "nota:1") as despues:
            assert despues is True
    finally:
        a.close()
        b.close()


def test_postgres_el_rollback_tambien_lo_suelta(url_pg):
    a, b = _wrapper(url_pg), _wrapper(url_pg)
    try:
        with candado_de_base(a, "k"):
            pass
        a.rollback()
        with candado_de_base(b, "k") as obtenido:
            assert obtenido is True
    finally:
        a.close()
        b.close()


def test_postgres_acepta_psycopg_y_sqlalchemy(url_pg):
    import psycopg
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    crudo = url_pg.replace("postgresql+psycopg://", "postgresql://", 1)
    motor = create_engine(url_pg.replace("postgresql://", "postgresql+psycopg://", 1))
    with psycopg.connect(crudo) as pg, Session(motor) as sesion, _wrapper(url_pg) as wrapper:
        with candado_de_base(pg, "k") as uno:
            assert uno is True
        with candado_de_base(sesion, "k") as dos:
            assert dos is False, "la conexión de psycopg todavía tiene el candado"
        pg.commit()
        with candado_de_base(sesion, "k") as tres:
            assert tres is True
        with candado_de_base(wrapper, "k") as cuatro:
            assert cuatro is False, "la sesión de SQLAlchemy lo tiene hasta su commit"
        sesion.commit()
        with candado_de_base(wrapper, "k") as cinco:
            assert cinco is True
    motor.dispose()


def test_postgres_un_arranque_serializa_entre_conexiones(url_pg):
    """Seis hilos, cada uno con su conexión propia (como seis workers): nunca dos adentro a la vez."""
    adentro, solapados = [], []

    def hook():
        with una_vez_al_arrancar("sembrar", destino=url_pg, espera=30):
            adentro.append(1)
            solapados.append(len(adentro))
            time.sleep(0.05)
            adentro.pop()

    hilos = [threading.Thread(target=hook) for _ in range(6)]
    [h.start() for h in hilos]
    [h.join() for h in hilos]
    assert solapados == [1] * 6


def test_postgres_un_arranque_colgado_no_traba_para_siempre(url_pg):
    with una_vez_al_arrancar("sembrar", destino=url_pg), pytest.raises(TimeoutError, match="otro proceso"):
        with una_vez_al_arrancar("sembrar", destino=url_pg, espera=0.3):
            pass
    # Y al soltarse queda libre.
    with una_vez_al_arrancar("sembrar", destino=url_pg, espera=5):
        pass


def test_postgres_si_la_conexion_del_arranque_muere_el_candado_se_suelta(url_pg):
    """Un proceso que cae con el candado puesto no deja a los demás esperando: PostgreSQL lo suelta con la sesión."""
    import psycopg

    crudo = url_pg.replace("postgresql+psycopg://", "postgresql://", 1)
    muerta = psycopg.connect(crudo, autocommit=True)
    muerta.execute("SELECT pg_advisory_lock(%s)", (candados._clave("sembrar"),))
    with pytest.raises(TimeoutError):
        with una_vez_al_arrancar("sembrar", destino=url_pg, espera=0.3):
            pass
    muerta.close()
    with una_vez_al_arrancar("sembrar", destino=url_pg, espera=5):
        pass


def test_postgres_esperar_candado_de_transaccion_espera_al_commit_del_otro(url_pg):
    a, b = _wrapper(url_pg), _wrapper(url_pg)
    try:
        assert esperar_candado_de_transaccion(a, "esquema") is True
        # Reentrante en la misma conexión: no se espera a sí mismo.
        assert esperar_candado_de_transaccion(a, "esquema", espera=0.2) is True
        # Otra conexión no lo consigue mientras `a` no termine su transacción...
        with pytest.raises(TimeoutError, match="esquema"):
            esperar_candado_de_transaccion(b, "esquema", espera=0.3)
        # ...pero un nombre distinto no se pisa.
        assert esperar_candado_de_transaccion(b, "otro", espera=0.2) is True
        # Y espera de verdad: lo toma apenas `a` hace commit, sin que nadie reintente a mano.
        b.rollback()
        liberar = threading.Timer(0.5, a.commit)
        liberar.start()
        t0 = time.monotonic()
        assert esperar_candado_de_transaccion(b, "esquema", espera=10) is True
        assert 0.3 < time.monotonic() - t0 < 5, "no esperó a que el otro terminara (o no se enteró de que terminó)"
        liberar.join()
    finally:
        a.close()
        b.close()


def test_postgres_esperar_candado_de_transaccion_lo_suelta_el_rollback_y_acepta_sqlalchemy(url_pg):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    motor = create_engine(url_pg.replace("postgresql://", "postgresql+psycopg://", 1))
    a = _wrapper(url_pg)
    try:
        with Session(motor) as sesion:
            assert esperar_candado_de_transaccion(a, "esquema") is True
            with pytest.raises(TimeoutError):
                esperar_candado_de_transaccion(sesion, "esquema", espera=0.3)
            sesion.rollback()
            a.rollback()
            assert esperar_candado_de_transaccion(sesion, "esquema", espera=2) is True
    finally:
        a.close()
        motor.dispose()
