"""`libracore.testing.pg_por_worker`: una base por worker y plantillas restauradas con `CREATE DATABASE ... TEMPLATE`.

Contra PostgreSQL real (`LIBRACORE_POSTGRES_URL`): lo que se prueba es `CREATE
DATABASE`, `FORCE` y `pg_database`, y un doble no diría nada de eso. Cada test usa
una `clave` propia y borra lo que creó, así que no se pisan entre sí ni con la base
compartida del CI.
"""
import os
import uuid

import psycopg
import pytest
from pg_descartable import conectar, existe, url_del_servidor

from libracore.testing.pg_por_worker import base_por_worker


@pytest.fixture
def pg(monkeypatch):
    url = url_del_servidor()
    clave = f"t{uuid.uuid4().hex[:8]}"
    base = base_por_worker(clave, url)
    assert base is not None
    yield base
    base.soltar_todo()
    for k in [k for k in os.environ if k.startswith(f"_LIBRACORE_PG_{clave.upper()}")]:
        monkeypatch.delenv(k, raising=False)


def _filas(url: str) -> int:
    with conectar(url) as c:
        return c.execute("SELECT count(*) FROM t").fetchone()[0]


def _construir_con_una_fila(url: str, llamadas: list) -> None:
    llamadas.append(url)
    with conectar(url, autocommit=True) as c:
        c.execute("CREATE TABLE t (n int)")
        c.execute("INSERT INTO t VALUES (1)")


def test_sin_url_no_hace_nada():
    assert base_por_worker("vacia", "") is None


def test_crea_su_propia_base_y_la_borra(pg):
    assert pg.url != pg.url_original
    assert existe(pg.url_original, pg.nombre)
    pg.soltar_todo()
    assert not existe(pg.url_original, pg.nombre)


def test_llamarla_de_nuevo_no_recrea_la_base(pg, monkeypatch):
    # Un segundo import del conftest (`tests.motor` y `motor` son dos modulos) vuelve
    # a llamarla: si recreara la base, le borraria el piso a un test en curso.
    with conectar(pg.url, autocommit=True) as c:
        c.execute("CREATE TABLE marca (n int)")
    clave = next(k for k in os.environ if k.startswith("_LIBRACORE_PG_T") and k.endswith("_ORIGINAL"))
    otra = base_por_worker(clave[len("_LIBRACORE_PG_"):-len("_ORIGINAL")].lower(), pg.url_original)
    assert otra.url == pg.url
    with conectar(pg.url) as c:
        assert c.execute("SELECT to_regclass('marca')").fetchone()[0] is not None


def test_la_plantilla_se_arma_una_sola_vez_y_cada_restauracion_sale_limpia(pg):
    llamadas: list = []
    pg.restaurar("armada", lambda u: _construir_con_una_fila(u, llamadas))
    assert _filas(pg.url) == 1
    with conectar(pg.url, autocommit=True) as c:
        c.execute("INSERT INTO t VALUES (2), (3)")
    assert _filas(pg.url) == 3

    pg.restaurar("armada", lambda u: _construir_con_una_fila(u, llamadas))

    assert _filas(pg.url) == 1  # lo que escribio el test anterior ya no esta
    assert len(llamadas) == 1  # y no se reconstruyo la plantilla


def test_restaurar_echa_a_las_conexiones_vivas_del_test_anterior(pg):
    pg.restaurar("armada", lambda u: _construir_con_una_fila(u, []))
    colgada = psycopg.connect(pg.url.replace("postgresql+psycopg://", "postgresql://", 1))
    colgada.execute("SELECT 1")  # abre una transaccion y la deja
    try:
        pg.restaurar("armada", lambda u: _construir_con_una_fila(u, []))  # no se cuelga ni falla
        assert _filas(pg.url) == 1
    finally:
        colgada.close()


def test_si_armar_la_plantilla_falla_no_queda_una_a_medias(pg):
    def roto(url):
        with conectar(url, autocommit=True) as c:
            c.execute("CREATE TABLE t (n int)")
        raise RuntimeError("fallo a mitad")

    with pytest.raises(RuntimeError):
        pg.restaurar("armada", roto)
    assert not existe(pg.url_original, f"{pg.nombre}_t_armada")

    pg.restaurar("armada", lambda u: _construir_con_una_fila(u, []))  # la proxima vez se arma bien
    assert _filas(pg.url) == 1


def test_plantillas_distintas_dejan_estados_distintos(pg):
    pg.restaurar("vacia", lambda u: conectar(u, autocommit=True).execute("CREATE TABLE t (n int)"))
    assert _filas(pg.url) == 0
    pg.restaurar("armada", lambda u: _construir_con_una_fila(u, []))
    assert _filas(pg.url) == 1


def test_soltar_todo_borra_tambien_las_plantillas(pg):
    pg.restaurar("armada", lambda u: _construir_con_una_fila(u, []))
    assert existe(pg.url_original, f"{pg.nombre}_t_armada")
    pg.soltar_todo()
    assert not existe(pg.url_original, f"{pg.nombre}_t_armada")


@pytest.mark.parametrize("nombre", ["Armada", "con espacio", "x;drop", "a-b", ""])
def test_rechaza_nombres_de_plantilla_peligrosos(pg, nombre):
    with pytest.raises(ValueError):
        pg.restaurar(nombre, lambda u: None)
