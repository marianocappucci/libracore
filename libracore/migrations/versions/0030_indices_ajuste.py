"""Los valores de los índices de ajuste (ADR-085): la tabla `indices_ajuste`.

Un valor publicado de un índice en una fecha (ICL, IPC, CER, UVA, dólar A 3500, Casa Propia), bajado de la fuente
oficial (`libracore.indices_descarga`) o cargado a mano. Lo usan quienes ajustan un precio por índice: LibraInmo y el
contrato con `metodo_actualizacion='indice'` de LibraDesk.

Aditiva: tabla e índice nuevos, nacen vacíos y no se toca ninguna fila. Idempotente (`IF NOT EXISTS`), como `0029`.

🔑 **Y deja los relojes en hora de Argentina** con `alters_para_hora_ar`, como la `0029`
(`test_la_cadena_deja_toda_columna_con_reloj_en_hora_de_argentina_postgres`).
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0030_indices_ajuste"
down_revision = "0029_cola_arca"
branch_labels = None
depends_on = None

_COLUMNAS_CON_RELOJ = (("indices_ajuste", "created_at"), ("indices_ajuste", "updated_at"))


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))
    from libracore.db.schema import AHORA_AR, alters_para_hora_ar
    for sentencia in alters_para_hora_ar(op.get_bind(), _COLUMNAS_CON_RELOJ, AHORA_AR):
        op.execute(sentencia)


def downgrade():
    # Bajar perdería los valores cargados a mano (los que no vuelven con una descarga) y dejaría sin base a los
    # ajustes ya calculados. Además `init_core_schema()` vuelve a crear la tabla en el próximo arranque. Restaurar
    # el backup.
    raise NotImplementedError(
        "No se baja: se perderían los valores de los índices cargados a mano. "
        "Para volver atrás, restaurar el backup."
    )
