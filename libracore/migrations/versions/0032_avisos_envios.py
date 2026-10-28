"""El registro de los avisos por mail y WhatsApp (ADR-090): la tabla `avisos_envios`.

Una fila por aviso, con su estado, sus intentos y una clave única `(canal, clave)` que impide mandar dos veces lo mismo.
La escriben `libracore.avisos` (el despachador) y la leen las pantallas de «Avisos» de los productos.

Aditiva: tabla e índices nuevos, nacen vacíos y no se toca ninguna fila. Idempotente (`IF NOT EXISTS`), como `0030`.

🔑 **Y deja los relojes en hora de Argentina** con `alters_para_hora_ar`, como la `0030`
(`test_la_cadena_deja_toda_columna_con_reloj_en_hora_de_argentina_postgres`).
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0032_avisos_envios"
down_revision = "0031_moneda_del_libro"
branch_labels = None
depends_on = None

_COLUMNAS_CON_RELOJ = (("avisos_envios", "created_at"), ("avisos_envios", "updated_at"))


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))
    from libracore.db.schema import AHORA_AR, alters_para_hora_ar
    for sentencia in alters_para_hora_ar(op.get_bind(), _COLUMNAS_CON_RELOJ, AHORA_AR):
        op.execute(sentencia)


def downgrade():
    # Bajar perdería el rastro de lo que se le avisó a cada cliente, que es lo que contesta «no me llegó el recordatorio»,
    # y dejaría de valer la clave única: el próximo barrido volvería a mandar todo. `init_core_schema()` además vuelve a
    # crear la tabla en el próximo arranque. Restaurar el backup.
    raise NotImplementedError(
        "No se baja: se perdería el registro de los avisos enviados y el próximo barrido volvería a mandarlos. "
        "Para volver atrás, restaurar el backup."
    )
