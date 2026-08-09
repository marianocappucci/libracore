"""Los PRAGMA sueltos contra PostgreSQL: cuáles se ignoran y cuáles frenan.

Por qué existe: `executescript()` saltea los PRAGMA desde siempre, pero un
`conn.execute("PRAGMA ...")` directo le llegaba crudo a psycopg. Eso hacía que
`init_schema()` de [[libracommerce]] no pudiera crear **ni una** tabla contra
PostgreSQL — su primera línea es `PRAGMA foreign_keys = ON`. Medido levantando
el schema contra un PostgreSQL real: 0 tablas creadas.

La parte que importa no es que se ignoren, es **cuál no**: `foreign_keys = OFF`
no es una preferencia, es "voy a violar la integridad referencial". Ignorarlo
dejaría al llamador creyendo algo falso.
"""
import os

import pytest

from libracore.db import core
from libracore.db._postgres import _revisar_pragma


def test_foreign_keys_off_no_se_ignora():
    with pytest.raises(NotImplementedError, match="foreign_keys = OFF"):
        _revisar_pragma("foreign_keys", "off")
    with pytest.raises(NotImplementedError):
        _revisar_pragma("foreign_keys", "0")


def test_foreign_keys_on_si_se_ignora():
    _revisar_pragma("foreign_keys", "on")  # PostgreSQL ya las aplica siempre


def test_un_pragma_desconocido_frena_en_vez_de_pasar_de_largo():
    """Ignorar por defecto sería peor: un PRAGMA que sí cambia el
    comportamiento pasaría inadvertido hasta que algo saliera mal lejos."""
    with pytest.raises(NotImplementedError, match="secure_delete"):
        _revisar_pragma("secure_delete", "on")


def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("LIBRACORE_POSTGRES_URL no está definida en CI")
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


def test_pragma_directo_contra_postgres_real():
    """El camino completo, ejecutado: antes moría con *syntax error at or near
    PRAGMA* y ahora es un no-op que no rompe la transacción en curso."""
    url = _url()
    core.configure(url)
    conn = core.get_connection()
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # La conexión sigue usable: si el PRAGMA hubiera llegado al motor, la
        # transacción quedaría abortada y esto también fallaría.
        assert conn.execute("SELECT 1").fetchone()[0] == 1

        cur = conn.execute("PRAGMA journal_mode=WAL")
        assert cur.fetchone() is None
        assert cur.fetchall() == []

        with pytest.raises(NotImplementedError):
            conn.execute("PRAGMA foreign_keys = OFF")
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


def test_un_execute_directo_no_rompe_lo_que_viene_despues():
    """Un DDL después del PRAGMA tiene que funcionar en la misma transacción.

    Es la forma exacta en la que fallaba: `init_schema()` de LibraCommerce hace
    el PRAGMA y **enseguida** el `executescript` con las tablas. Si el PRAGMA
    aborta la transacción, no se crea ninguna.
    """
    url = _url()
    core.configure(url)
    conn = core.get_connection()
    try:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE despues_del_pragma (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        conn.commit()

        existe = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='despues_del_pragma'"
        ).fetchone()
        assert existe is not None
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


# El caso que motivó todo esto —que el schema de LibraCommerce nazca entero
# contra PostgreSQL— se prueba **en el repo de LibraCommerce**, que es donde ese
# schema vive y donde su CI puede instalar los dos motores. Acá quedaría como
# `importorskip` y en CI se saltearía siempre: un test que nunca corre es peor
# que no tenerlo, porque figura en verde.
