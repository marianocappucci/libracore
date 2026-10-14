"""El rastro de los envíos por mail de comprobantes y presupuestos (ADR-050).

Agrega la tabla `envios_email` con su índice. No toca filas: los envíos anteriores no dejaron
rastro y no se pueden reconstruir. Idempotente (`IF NOT EXISTS`), como `0021`.

🔑 **Y deja `created_at` en hora de Argentina** con `alters_para_hora_ar`, como la `0019` y la `0022`:
el DDL ya nace así, pero una base cuyo DEFAULT volvió a UTC necesita que alguna revisión nombre
la tabla (`test_la_cadena_deja_toda_columna_con_reloj_en_hora_de_argentina_postgres`).
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0026_envios_email"
down_revision = "0025_concepto_sin_factura_doble"
branch_labels = None
depends_on = None

_COLUMNAS_CON_RELOJ = (("envios_email", "created_at"),)


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))
    from libracore.db.schema import AHORA_AR, alters_para_hora_ar
    for sentencia in alters_para_hora_ar(op.get_bind(), _COLUMNAS_CON_RELOJ, AHORA_AR):
        op.execute(sentencia)


def downgrade():
    op.execute("DROP TABLE IF EXISTS envios_email")
