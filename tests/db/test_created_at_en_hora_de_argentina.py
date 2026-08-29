"""El `created_at` que estampa el schema va en hora de Argentina, no en UTC.

🔴 **El defecto no daba error y estuvo desde siempre.** El DEFAULT de las 29
columnas `created_at`/`updated_at` del schema era `datetime('now')`, que en
SQLite es UTC — y que el adaptador de PostgreSQL traduce a UTC **a propósito**,
para que las dos bases guarden el mismo texto. O sea que las dos guardaban la
hora equivocada, y de la misma manera.

Medido en la instancia `compulibra` de [[contalibra]] el 2026-08-29: las 81
filas de `facturas` y las 112 de `caja_movimientos`, 3 h adelantadas. **No sólo
las del cron nocturno**: también las escritas a mano, que pasaban desapercibidas
porque una compra de las 12:56 guardada como `15:56` sigue pareciendo un horario
de trabajo. Lo que sí se ve es el borde: un comprobante creado a las 22:00 de
Argentina quedaba fechado el día siguiente.

Las cuatro guardas de acá fallan por motivos distintos, y por eso están
separadas:

1. que el control sepa distinguir UTC de hora de Argentina — sin esto las otras
   tres podrían cumplirse por la razón equivocada;
2. el valor que termina guardado, en SQLite y en PostgreSQL;
3. que una tabla **nueva** no nazca con el DEFAULT viejo;
4. que el valor no dependa de la zona de la sesión del servidor, que es
   justamente lo que `'localtime'` no garantiza.
"""
import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest

from libracore.db import core
from libracore.db.core import _ar_now
from libracore.db.schema import (
    alters_para_hora_ar,
    defaults_con_reloj,
    defaults_fuera_de_hora_ar,
    init_core_schema,
)

#: Margen entre el reloj del test y el de la base. Generoso a propósito: lo que
#: se busca distinguir son 3 horas, no segundos.
TOLERANCIA = datetime.timedelta(seconds=30)

TRES_HORAS = datetime.timedelta(hours=3)

SCHEMA_PY = Path(init_core_schema.__code__.co_filename)


def _leer(ts: str) -> datetime.datetime:
    """Parsea el texto que guardan estas columnas ('YYYY-MM-DD HH:MM:SS')."""
    return datetime.datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")


def _desfasaje(ts: str) -> datetime.timedelta:
    """Cuánto se aparta el valor guardado de la hora de Argentina de ahora."""
    return abs(_leer(ts) - _leer(_ar_now()))


# ── El control ───────────────────────────────────────────────────────────────

def test_el_control_distingue_utc_de_hora_de_argentina(conn_sqlite):
    """Control positivo de todo este archivo.

    Si `_ar_now()` y el DEFAULT estuvieran mal de la misma forma, los tests de
    abajo pasarían igual y no dirían nada. Acá se mide que las dos expresiones
    que están en juego —la vieja y la nueva— **dan valores distintos**, y que la
    diferencia es exactamente las 3 h que el defecto metía.
    """
    utc = conn_sqlite.execute("SELECT datetime('now')").fetchone()[0]
    ar = conn_sqlite.execute("SELECT datetime('now','-3 hours')").fetchone()[0]

    assert abs((_leer(utc) - _leer(ar)) - TRES_HORAS) <= TOLERANCIA
    assert _desfasaje(utc) >= TRES_HORAS - TOLERANCIA
    assert _desfasaje(ar) <= TOLERANCIA


# ── El valor que se guarda ───────────────────────────────────────────────────

@pytest.fixture
def conn_sqlite(tmp_path):
    core.configure(db_path=str(tmp_path / "hora.db"))
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


#: Dos tablas de las 29, elegidas porque son las que el incidente tocó y porque
#: el alta no pide media docena de FKs. Que las otras 27 no se queden atrás no
#: lo cubre esta prueba sino la estructural de más abajo, que las mira a todas.
ALTAS = {
    "clients": ("INSERT INTO clients (name) VALUES (?)", ("Municipalidad",)),
    "caja_movimientos": (
        "INSERT INTO caja_movimientos (fecha, tipo, concepto, monto) VALUES (?,?,?,?)",
        ("2026-08-29", "ingreso", "Cobro de prueba", 1000.0),
    ),
}


@pytest.mark.parametrize("tabla", sorted(ALTAS))
def test_el_default_guarda_hora_de_argentina_en_sqlite(conn_sqlite, tabla):
    sql, valores = ALTAS[tabla]
    conn_sqlite.execute(sql, valores)
    conn_sqlite.commit()

    guardado = conn_sqlite.execute(f"SELECT created_at FROM {tabla}").fetchone()[0]
    assert _desfasaje(guardado) <= TOLERANCIA, (
        f"{tabla}.created_at guardó {guardado!r} y en Argentina son {_ar_now()!r}"
    )


# ── Que ninguna tabla nueva nazca con el DEFAULT viejo ───────────────────────

def test_ninguna_columna_del_schema_estampa_utc():
    """🔴 Se mira la **propiedad final** —"ninguna columna con reloj queda fuera
    de la hora de Argentina"— y no el patrón viejo.

    Buscar `datetime('now')` sería repetir el patrón original: una columna nueva
    escrita como `DEFAULT CURRENT_TIMESTAMP` (que es lo que usa LibraCommerce, y
    que en PostgreSQL además arrastra microsegundos y offset) pasaría por limpia.

    El barrido vive en `db/schema.py` y no acá porque los cuatro productos con
    DDL propio corren esta misma comprobación sobre el suyo. Copiarlo en cada
    repo es la forma conocida de que empiecen a decir cosas distintas — pasó con
    las cinco definiciones de "hoy" del frontend.
    """
    fuente = SCHEMA_PY.read_text(encoding="utf-8")

    # Control: si el barrido dejara de encontrar columnas, la lista vacía de
    # abajo pasaría por verde y este test no diría nada nunca más.
    assert len(defaults_con_reloj(fuente)) >= 25

    assert defaults_fuera_de_hora_ar(fuente) == []


def test_el_barrido_encuentra_las_dos_formas_del_defecto():
    """Control positivo del barrido mismo: que sepa fallar.

    Las dos formas que la familia tiene escritas —la de LibraCore y la de
    LibraCommerce— y una sana, para que no pase por el atajo de reportar todo.
    """
    assert defaults_fuera_de_hora_ar("a TEXT DEFAULT (datetime('now'))") != []
    assert defaults_fuera_de_hora_ar("a TEXT DEFAULT CURRENT_TIMESTAMP") != []
    assert defaults_fuera_de_hora_ar("a TEXT DEFAULT (datetime('now','-3 hours'))") == []


# ── PostgreSQL: el valor no sale de la zona de la sesión ─────────────────────

def _url():
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    return url


@pytest.fixture
def conn_postgres():
    core.configure(_url())
    c = core.get_connection()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None


def test_el_default_guarda_hora_de_argentina_en_postgres(conn_postgres):
    conn_postgres.execute("INSERT INTO clients (name) VALUES (?)", ("Municipalidad",))
    conn_postgres.commit()

    guardado = conn_postgres.execute("SELECT created_at FROM clients").fetchone()[0]
    assert _desfasaje(guardado) <= TOLERANCIA


@pytest.mark.parametrize("zona", ["UTC", "Asia/Tokyo", "America/Argentina/Buenos_Aires"])
def test_la_hora_guardada_no_depende_de_la_zona_de_la_sesion(conn_postgres, zona):
    """🔴 Ésta es la razón de que el DEFAULT sea `-3 hours` y no `'localtime'`.

    `LOCALTIMESTAMP` sale de la zona de la sesión del servidor, que se escribe
    en el `initdb` y que `TZ` **no** mueve (2026-08-23, seis demos medidas). Un
    volumen nacido sin `command: postgres -c timezone=...` devolvería a esta
    familia al mismo defecto, y en silencio. Con el offset fijo la zona de la
    sesión es indiferente — que es lo que se mide acá.
    """
    conn_postgres.execute(f"SET TIME ZONE '{zona}'")
    conn_postgres.execute("INSERT INTO clients (name) VALUES (?)", (f"Cliente {zona}",))
    conn_postgres.commit()

    guardado = conn_postgres.execute(
        "SELECT created_at FROM clients ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert _desfasaje(guardado) <= TOLERANCIA, (
        f"con la sesión en {zona} se guardó {guardado!r}; en Argentina son {_ar_now()!r}"
    )


# ── La instancia que YA existe: la revisión de Alembic ───────────────────────

#: El DEFAULT que tenían las 29 columnas antes del arreglo, en el dialecto que
#: PostgreSQL guarda. Es lo que hay hoy en las bases de producción.
_DEFAULT_VIEJO = "to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"

RAIZ = Path(__file__).resolve().parents[2]


def _alembic_upgrade_head(url: str):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=RAIZ,
        env={**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(RAIZ)},
        capture_output=True,
        text=True,
    )


def test_la_revision_arregla_una_instancia_que_ya_existia(conn_postgres):
    """🔴 Sin esto el arreglo no le llega a ninguna base de producción.

    `init_core_schema()` crea con `CREATE TABLE IF NOT EXISTS`: sobre una base
    que ya existe **no cambia ningún DEFAULT**, y ahí es donde están las filas
    que importan. Lo que las alcanza es la revisión `0003`, y lo que se ejercita
    acá es exactamente eso — una base con el DEFAULT viejo, `alembic upgrade
    head` de verdad por subproceso, y la fila que sale después.
    """
    conn_postgres.execute(
        f"ALTER TABLE clients ALTER COLUMN created_at SET DEFAULT {_DEFAULT_VIEJO}"
    )
    conn_postgres.commit()

    # Control positivo: la base quedó como una de producción — 3 h adelantada.
    # Sin esto, un `upgrade` que no hiciera nada pasaría igual.
    conn_postgres.execute("INSERT INTO clients (name) VALUES (?)", ("Antes",))
    conn_postgres.commit()
    antes = conn_postgres.execute(
        "SELECT created_at FROM clients ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert _desfasaje(antes) >= TRES_HORAS - TOLERANCIA, (
        f"el control no reprodujo el defecto: guardó {antes!r}"
    )

    # 🔴 Cerrar la transaccion ANTES de migrar. El wrapper abre una al leer, y
    # una conexion "idle in transaction" se queda con el lock de `clients`: el
    # `ALTER TABLE` de la revision `0002` se cuelga esperandolo y el test no
    # termina nunca, en vez de fallar.
    conn_postgres.commit()

    resultado = _alembic_upgrade_head(_url())
    assert resultado.returncode == 0, resultado.stderr

    conn_postgres.execute("INSERT INTO clients (name) VALUES (?)", ("Despues",))
    conn_postgres.commit()
    despues = conn_postgres.execute(
        "SELECT created_at FROM clients ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert _desfasaje(despues) <= TOLERANCIA, (
        f"despues de migrar guardó {despues!r}; en Argentina son {_ar_now()!r}"
    )


def test_la_revision_no_toca_las_columnas_que_no_son_de_texto(conn_postgres):
    """🔴 El DEFAULT nuevo es texto: ponérselo a una columna `timestamp` corta
    con *"default expression is of type text"* y **aborta el `upgrade` entero**.

    No es hipotético. LibraDesk llegó al motor desde sus propios modelos de
    SQLAlchemy y en sus seis bases `depositos`, `proveedores` y `usuarios`
    tienen `created_at` como `timestamp` — medido el 2026-08-29. Sin la guarda
    por tipo, esta revisión rompía el deploy de LibraDesk y no el de los demás,
    que es la clase de rojo que aparece recién en producción.

    Esas columnas además **no necesitan el arreglo**: `CURRENT_TIMESTAMP` sale
    de la zona de la sesión, y los 21 servidores del VPS están en hora de
    Argentina desde el 2026-08-24.
    """
    conn_postgres.execute(
        "ALTER TABLE depositos ALTER COLUMN created_at DROP DEFAULT"
    )
    conn_postgres.execute(
        "ALTER TABLE depositos ALTER COLUMN created_at TYPE timestamp "
        "USING created_at::timestamp"
    )
    conn_postgres.execute(
        "ALTER TABLE depositos ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP"
    )
    conn_postgres.commit()

    resultado = _alembic_upgrade_head(_url())
    assert resultado.returncode == 0, resultado.stderr

    # La de texto se arregló...
    assert "interval" in _default_de(conn_postgres, "clients", "created_at")
    # ...y la `timestamp` quedó como estaba, sin romper la corrida.
    assert _default_de(conn_postgres, "depositos", "created_at") == "CURRENT_TIMESTAMP"


def _default_de(conn, tabla: str, columna: str) -> str:
    fila = conn.execute(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?",
        (tabla, columna),
    ).fetchone()
    return fila[0]


def test_alters_para_hora_ar_acepta_una_conexion_de_esta_casa(conn_postgres):
    """La otra forma de conexión con la que se llama al helper.

    Las revisiones de Alembic le pasan un `bind` de SQLAlchemy; las migraciones
    propias de LibraCommerce le pasan una `Conexion` de este motor, que no tiene
    `.dialect` ni acepta `sa.text(...)`. Son dos APIs distintas para lo mismo y
    las dos aparecen en los cinco repos que corren este arreglo, así que si el
    helper sólo sirviera para una, la mitad del barrido se escribiría de nuevo
    en LibraCommerce — que es como se separan.
    """
    sentencias = alters_para_hora_ar(conn_postgres, [("clients", "created_at")])
    assert len(sentencias) == 1
    assert sentencias[0].startswith('ALTER TABLE "clients" ALTER COLUMN "created_at" SET DEFAULT')
    assert "interval '-3 hours'" in sentencias[0]

    # Una columna que no existe no genera sentencia, en vez de generar una que
    # rompa: `alembic upgrade` corre sobre bases que llegaron por caminos
    # distintos y no todas tienen las mismas tablas.
    assert alters_para_hora_ar(conn_postgres, [("no_existe", "created_at")]) == []


def test_en_sqlite_no_hay_alters_que_correr(conn_sqlite):
    """SQLite no tiene `ALTER COLUMN ... SET DEFAULT`.

    Devolver `[]` y no una sentencia que reviente es lo que deja que la misma
    revisión corra en los dos motores. Allá el DEFAULT nuevo llega al crear la
    tabla, no al migrarla.
    """
    assert alters_para_hora_ar(conn_sqlite, [("clients", "created_at")]) == []


class _BindOffline:
    """Lo que Alembic le pasa a una revisión en modo `--sql`.

    Tiene `dialect` —así que el helper lo trata como un bind de SQLAlchemy— pero
    `execute()` **no ejecuta nada y devuelve `None`**, porque no hay base del
    otro lado: el modo offline sólo escribe el SQL.
    """

    class dialect:  # noqa: N801  (imita el atributo de SQLAlchemy)
        name = "postgresql"

    def execute(self, *_args, **_kwargs):
        return None


def test_el_render_offline_no_explota():
    """🔴 En `alembic upgrade --sql` el bind devuelve `None`.

    Sin la guarda, el helper reventaba con *"'NoneType' object is not
    iterable"* y se llevaba puesto el render de la cadena entera. Lo encontró la
    suite de LibraDesk el 2026-08-29, que renderiza su cadena como parte de los
    tests — no una base de producción, pero el mismo camino.

    Sin columnas que mirar no se emite ningún ALTER: el script renderizado queda
    **sin** estos `SET DEFAULT`. Es la limitación normal de una migración que
    depende del estado de la base, y está anotada en el docstring de cada
    revisión; el deploy de esta familia corre `upgrade` en línea, no `--sql`.
    """
    assert alters_para_hora_ar(_BindOffline(), [("clients", "created_at")]) == []


def test_una_conexion_sqlite_no_se_confunde_con_el_proceso_configurado_en_postgres(
    tmp_path, monkeypatch
):
    """🔴 El proceso puede estar configurado en PostgreSQL y la conexión ser SQLite.

    Es exactamente lo que pasa en la suite de un producto: configura la URL de
    PostgreSQL y después corre las migraciones de LibraCommerce sobre una
    `sqlite3.Connection` de memoria. Preguntando por el `is_postgres()` **global**
    —que es lo que hacía la primera versión— la consulta a `information_schema`
    salía contra SQLite y moría con *"no such table:
    information_schema.columns"*. Lo encontró el CI de VentaLibra el 2026-08-29.

    Se monta el desacople a propósito: global en PostgreSQL, conexión SQLite.
    """
    import sqlite3

    monkeypatch.setattr(core, "_database_url", "postgresql://x/y")
    monkeypatch.setattr(core, "_db_path", "postgresql://x/y")
    assert core.is_postgres()

    conn = sqlite3.connect(":memory:")
    try:
        assert alters_para_hora_ar(conn, [("clients", "created_at")]) == []
    finally:
        conn.close()
