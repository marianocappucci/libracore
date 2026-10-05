"""El dinero del motor se guarda exacto en PostgreSQL: 33 columnas de `DOUBLE PRECISION` a `NUMERIC` (ADR-024).

Decisión del humano (2026-10-05, «NUMERIC para todos», alcance «guardar
exacto»): las columnas de dinero del motor pasan a `NUMERIC` sin escala fija, sin
redondear nada de lo que ya está. **La lectura no cambia**: el adaptador sigue
devolviendo `float`, así que ningún producto toca su aritmética. Es parte de la
etapa 2 del diseño `libracargo-modelo-normalizado-diseno` (M4).

La lista y la conversión viven en `init_core_schema()` (`COLUMNAS_DE_DINERO`,
`_dinero_exacto_en_postgres`), como el resto de la cadena. Sólo convierte las que
hoy son `double precision` o `real`; en SQLite no hace nada.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0018_dinero_exacto"
down_revision = "0017_anulacion_con_rastro"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Volver a `DOUBLE PRECISION` es posible, pero devolvería el error de punto
    # flotante a las sumas que esta revisión saca. No se baja: restaurar el backup.
    raise NotImplementedError(
        "No se baja: el dinero volvería a guardarse con error de punto flotante. "
        "Para volver atrás, restaurar el backup."
    )
