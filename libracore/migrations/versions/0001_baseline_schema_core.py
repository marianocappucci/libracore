"""Baseline: el schema del core tal como ya existe, sin reescribirlo.

Esta revisión **llama a `init_core_schema()`** en vez de re-expresar sus 33
tablas en `op.create_table(...)`. Es deliberado y es lo contrario de lo que hace
la baseline de LibraGenda, porque el punto de partida es distinto: allá la
fuente de verdad son los modelos SQLAlchemy y la migración los espeja; acá la
fuente es un `executescript()` de ~500 líneas de DDL crudo, y re-escribirlo
crearía **una segunda fuente de verdad que se desincroniza en el primer
cambio**.

Lo que esto congela, y cómo se sostiene:

- Desde esta revisión, `init_core_schema()` es de **sólo lectura**. Todo cambio
  posterior de schema va como revisión nueva, no como línea agregada ahí.
- El congelamiento no depende de que alguien se acuerde: lo sostiene
  `tests/db/test_schema_congelado.py`, que compara el resultado de la función
  contra una fixture por motor.

**Se puede correr sobre una instancia que ya existe.** La función entera es
idempotente —`CREATE TABLE IF NOT EXISTS`, `ALTER` guardados por introspección,
seeds condicionales—, así que `alembic upgrade head` sobre una base viva hace lo
mismo que ya hace cada arranque de la app, más registrar la versión. Por eso las
instancias existentes **se migran, no se estampan a ciegas**: el resultado es el
mismo y además queda verificado.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar de la baseline es borrar el schema entero del core, con los datos
    # de todos los productos adentro. No hay caso de uso que lo justifique y sí
    # una forma muy barata de perder una instancia: el rollback de esta
    # revisión es restaurar el backup.
    raise NotImplementedError(
        "La baseline no se baja: para volver atrás, restaurar el backup de la "
        "instancia."
    )
