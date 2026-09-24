"""El POS de MercadoPago, por caja.

Hasta acá `mp_pos_id` vivía en la configuración de instancia (`config.json`).
Eso funciona con una sola caja, pero con varias cajas compartiendo el mismo POS
la última venta pisa el monto de las anteriores: el modelo de QR de MercadoPago
escribe el monto en el POS.

La columna `cajas.mp_pos_id` es nullable a propósito: una caja sin QR no lo
necesita. El backfill de la configuración vieja a la caja es responsabilidad de
cada producto, porque `config.json` vive en el contenedor/producto y no en la
base de LibraCore.

Llama a `init_core_schema()` para mantener la fuente de verdad en un solo lugar
(el DDL del motor), igual que el resto de la cadena de migraciones.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0012_mp_pos_id_por_caja"
down_revision = "0011_reabrir_cierre_diario"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar sería perder la asignación de POS por caja. Los comprobantes ya
    # emitidos no se ven afectados, pero volver a un POS compartido haría que
    # varias cajas pisen sus montos. No se baja: restaurar el backup.
    raise NotImplementedError(
        "No se baja: volvería a compartir el POS entre cajas, con el riesgo de "
        "que una venta pise el monto de otra. Para volver atrás, restaurar el backup."
    )
