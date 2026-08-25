"""Entorno de Alembic para LibraCore.

Vive **adentro del paquete** (`libracore/migrations/`) y no en la raíz del
repo: es lo que hace que las revisiones viajen en el wheel y que un
consumidor pueda aplicarlas con `libracore-migrar` sin clonar nada. Hay un
test que abre el wheel construido y lo verifica.

Dos diferencias con el entorno de LibraGenda, que es el precedente de la
familia, y las dos salen de que acá el schema es DDL crudo y no modelos
SQLAlchemy:

1. **No hay `target_metadata`.** No existe un modelo del que autogenerar: la
   fuente de verdad es `init_core_schema()`. `alembic revision --autogenerate`
   no sirve en este repo y va a producir una revisión vacía — las revisiones se
   escriben a mano.
2. **El destino se acepta en las dos formas** en las que LibraCore lo maneja en
   todos lados: una URL PostgreSQL o una **ruta de archivo** SQLite. Se traduce
   a URL de SQLAlchemy sólo para el bind, y se configura además
   `libracore.db.core` con el destino original, porque el DDL consulta
   `core.is_postgres()` para decidir dos constraints. Si esa configuración y el
   bind no coinciden, el schema sale distinto del de producción **sin fallar**.
"""
import os
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

from libracore.db import core


def _destino() -> str:
    """El destino tal como lo escribe un producto: URL PostgreSQL o ruta SQLite.

    El orden de precedencia, y por qué:

    1. **`libracore.url`**, que pone `libracore.migrar.configuracion()` cuando
       alguien pasa el destino explícito. Va primero porque es la única señal
       inequívoca de intención.
    2. `DATABASE_URL` del entorno — el caso de un script parado en el host.
    3. `sqlalchemy.url` del `alembic.ini`, que en este repo es un placeholder.

    🔴 **El paso 1 existe porque sin él el destino explícito se ignoraba en
    silencio, y acá eso es peor que en LibraGenda.** Este comando corre adentro
    del contenedor del producto, donde `DATABASE_URL` apunta a la base **del
    dominio**. En Gestiolibra, MedLibra y LibraClub el schema de LibraCore vive
    en una base **aparte**: sin esta precedencia, `upgrade(url_del_core)`
    habría creado las tablas del core al lado de las del dominio, dejado la base
    real sin tocar y devuelto éxito.
    """
    destino = (
        context.config.get_main_option("libracore.url", default="")
        or os.environ.get("DATABASE_URL")
        or context.config.get_main_option("sqlalchemy.url", default="")
    )
    if not destino or destino.startswith("postgresql://user:password@"):
        raise RuntimeError(
            "Falta DATABASE_URL. Acepta una URL PostgreSQL "
            "(postgresql://usuario:clave@host/base) o la ruta del archivo SQLite "
            "de la instancia (/root/contalibra/clientes/demo/data/contalibra.db)."
        )
    return destino


def _url_sqlalchemy(destino: str) -> str:
    if core.es_url_postgres(destino):
        # SQLAlchemy resuelve "postgresql://" a psycopg2, que este repo no
        # instala: el driver de LibraCore es psycopg 3.
        return destino.replace("postgresql://", "postgresql+psycopg://", 1)
    return f"sqlite:///{Path(destino).expanduser().resolve()}"


def run_migrations_online():
    destino = _destino()
    # El orden importa: `configure` antes de abrir nada, porque las revisiones
    # llaman a código de `libracore.db` que lee esta configuración.
    core.configure(destino)
    connectable = create_engine(_url_sqlalchemy(destino), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    # `--sql` genera el SQL sin conectarse. La baseline no puede: **ejecuta
    # Python** (`init_core_schema()`), que decide parte del DDL mirando la base
    # que tiene enfrente. Un modo offline que emitiera algo estaría mintiendo.
    raise RuntimeError(
        "El modo offline (--sql) no está soportado: la baseline ejecuta "
        "init_core_schema(), que inspecciona la base antes de decidir el DDL."
    )

run_migrations_online()
