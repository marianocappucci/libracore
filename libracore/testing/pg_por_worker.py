"""Una base de PostgreSQL por worker de pytest-xdist, restaurada desde plantillas.

Las suites de la familia arrancan **cada test** de una base nueva, y rearmarla
(`DROP SCHEMA` + crear el schema + `create_app()` + las cadenas de Alembic) es lo
que más cuesta: medido en `ventalibra`, ~1,6 s por test; en `libracommerce`, ~1,5 s.
Con `CREATE DATABASE ... TEMPLATE` la base sale de una copia ya armada, ~0,1 s
(PostgreSQL 16, que no fuerza un checkpoint). Y como la restauración borra la base
con `FORCE`, **dos procesos no pueden compartirla**: cada worker usa la suya.

Uso, desde el `conftest.py` del producto, antes de que nadie lea la URL::

    from libracore.testing.pg_por_worker import base_por_worker

    _pg = base_por_worker("miproducto", os.environ.get("MIPRODUCTO_TEST_DATABASE_URL", ""))
    if _pg:
        os.environ["MIPRODUCTO_TEST_DATABASE_URL"] = _pg.url   # o la constante del producto

    # en la fixture, en vez del DROP SCHEMA + armado:
    _pg.restaurar("armada", construir)    # construir(url) deja la base como la quiere el test

`construir(url)` recibe la URL de la **plantilla** y la deja como un test la
necesita; sólo corre la primera vez que se pide cada plantilla en cada worker. Una
plantilla por *estado de partida* distinto (p. ej. "vacía" y "armada"): cada una
guarda una base y sale ~0,1 s.

Qué NO resuelve: lo que el producto comparta fuera de la base (archivos, puertos,
variables de entorno, nombres fijos). Eso aparece al correr la suite entera con
`-n 4`, y hay que correrla una vez por repo.

Requisitos: PostgreSQL >= 13 (`DROP DATABASE ... WITH (FORCE)`) y un rol con
CREATEDB (el del servicio de CI es el superusuario del contenedor). Sin xdist el
worker se llama `main`; dos corridas **a la vez** contra el mismo servidor y sin
xdist comparten esa base.
"""
from __future__ import annotations

import atexit
import os
import re
from collections.abc import Callable

from libracore.respaldo_postgres import con_base

_NOMBRE_VALIDO = re.compile(r"^[a-z0-9_]+$")
_MAX_IDENTIFICADOR = 63  # PostgreSQL recorta en silencio por encima de esto


def _normal(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _base_de(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path.lstrip("/")


class BasePorWorker:
    """La base de ESTE worker y sus plantillas. Se obtiene con `base_por_worker()`."""

    def __init__(self, url_original: str, nombre: str):
        self.url_original = url_original
        self.nombre = nombre

    @property
    def url(self) -> str:
        """La URL de la base de este worker."""
        return con_base(self.url_original, self.nombre)

    def _sql(self, *sentencias):
        import psycopg

        # Se administra conectado a la base ORIGINAL: un `CREATE DATABASE` va desde
        # cualquier base, así que no se supone que exista la `postgres`.
        with psycopg.connect(_normal(self.url_original), autocommit=True) as conexion:
            for sentencia in sentencias:
                conexion.execute(sentencia)

    def _existe(self, nombre: str) -> bool:
        import psycopg

        with psycopg.connect(_normal(self.url_original), autocommit=True) as conexion:
            return conexion.execute("SELECT 1 FROM pg_database WHERE datname = %s", (nombre,)).fetchone() is not None

    def _crear(self, nombre: str, plantilla: str | None = None):
        from psycopg import sql

        consulta = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(nombre))
        if plantilla:
            consulta += sql.SQL(" TEMPLATE {}").format(sql.Identifier(plantilla))
        self._sql(consulta)

    def _soltar(self, *nombres: str):
        from psycopg import sql

        # `FORCE`: el test anterior puede haber dejado conexiones vivas (apps que no
        # cierran la suya), y sin eso el DROP falla o se cuelga esperándolas.
        self._sql(*(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(n)) for n in nombres))

    def _plantillas_existentes(self) -> list[str]:
        import psycopg

        patron = re.sub(r"([\\%_])", r"\\\1", self.nombre) + r"\_t\_%"
        with psycopg.connect(_normal(self.url_original), autocommit=True) as conexion:
            filas = conexion.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (patron,)).fetchall()
        return [f[0] for f in filas]

    def soltar_todo(self) -> None:
        """Borra la base del worker y todas sus plantillas. Corre solo al salir del proceso."""
        self._soltar(self.nombre, *self._plantillas_existentes())

    def restaurar(self, plantilla: str, construir: Callable[[str], None]) -> None:
        """Deja la base del worker como una nueva copiada de la plantilla `plantilla`.

        La plantilla se arma la primera vez con `construir(url_de_la_plantilla)`.
        `CREATE DATABASE ... TEMPLATE` falla si queda **alguien** conectado a ella,
        y `construir` suele levantar una app que deja su conexión viva: por eso se
        las termina al final. Si armarla falla a medias se borra, para que la
        próxima vez no se tome una incompleta por buena.
        """
        if not _NOMBRE_VALIDO.match(plantilla):
            raise ValueError(f"nombre de plantilla inválido: {plantilla!r} (sólo a-z, 0-9 y _)")
        nombre = f"{self.nombre}_t_{plantilla}"
        if len(nombre) > _MAX_IDENTIFICADOR:
            raise ValueError(f"{nombre!r} supera los {_MAX_IDENTIFICADOR} caracteres de un identificador de PostgreSQL")
        if not self._existe(nombre):
            self._crear(nombre)
            try:
                construir(con_base(self.url_original, nombre))
                self._sql(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{nombre}' AND pid <> pg_backend_pid()"
                )
            except BaseException:
                self._soltar(nombre)
                raise
        self._soltar(self.nombre)
        self._crear(self.nombre, plantilla=nombre)


def base_por_worker(clave: str, url: str) -> BasePorWorker | None:
    """La base de este worker, creada (y borrada al salir) si todavía no existía. `None` si `url` está vacía.

    `clave` identifica al producto (`"ventalibra"`), no al worker. `url` es la
    del servidor **original**, la que daría la variable de entorno del producto.

    🔴 **Idempotente por proceso, y a propósito.** Un `conftest.py` puede importarse
    dos veces (un test que hace `from tests.motor import ...` y otro
    `from motor import ...` cargan DOS módulos), y el proceso que lanza a los
    workers de xdist también lo importa y ellos heredan su entorno. Lo que decide
    "ya está hecho" vive en `os.environ`, con la URL original **y** el worker en
    la clave: ni el segundo import recrea la base (se la borraría a un test en
    curso) ni un worker deriva su nombre de la base del controlador.
    """
    if not url:
        return None
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    prefijo = f"_LIBRACORE_PG_{re.sub(r'[^A-Z0-9]', '_', clave.upper())}"
    original = os.environ.setdefault(f"{prefijo}_ORIGINAL", url)
    nombre = f"{_base_de(original)}_{worker}"
    if len(nombre) > _MAX_IDENTIFICADOR - 12:  # deja sitio a `_t_<plantilla>`
        raise ValueError(f"{nombre!r} es demasiado largo para derivarle plantillas")
    base = BasePorWorker(original, nombre)
    marca = f"{prefijo}_{worker}"
    if marca not in os.environ:
        base.soltar_todo()  # restos de una corrida interrumpida
        base._crear(nombre)
        atexit.register(base.soltar_todo)
        os.environ[marca] = "1"
    return base
