"""El pago a cuenta que originó cada movimiento de caja.

Un pago a cuenta que se aplica a facturas escribe un movimiento de caja por
factura, más el resto suelto. Para poder dar de baja el pago —y devolver las
facturas a "Sin cobrar"— sin adivinar sus movimientos por fecha o referencia,
cada uno lleva `caja_movimientos.cc_pago_id`.

La columna es nullable a propósito: los movimientos que ya estaban (y todo lo
que no nace de un pago a cuenta) quedan en NULL. Un pago anterior a esta
revisión no tiene movimientos ligados, y darlo de baja se comporta como antes.

Llama a `init_core_schema()` para mantener la fuente de verdad en un solo lugar
(el DDL del motor), igual que el resto de la cadena de migraciones.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0013_cc_pago_en_caja_movimientos"
down_revision = "0012_mp_pos_id_por_caja"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar perdería el vínculo entre cada cobro y el pago que lo originó: un
    # pago aplicado a facturas dejaría de poder darse de baja limpiamente. No se
    # baja: restaurar el backup.
    raise NotImplementedError(
        "No se baja: se perdería qué pago originó cada movimiento de caja. "
        "Para volver atrás, restaurar el backup."
    )
