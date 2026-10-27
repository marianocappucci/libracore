"""La moneda del libro de terceros y del recibo (ADR-088): `cc_asientos.moneda` y `recibos.moneda`.

LibraInmo tiene contratos de alquiler en dólares, y la cuenta del inquilino y la del propietario no pueden sumar pesos
con dólares. Cada asiento y cada recibo dicen en qué moneda están (`libracore.monedas`).

Aditiva: dos columnas `TEXT NOT NULL DEFAULT 'ARS'`. Todo lo que ya había queda en pesos, que es lo que era, sin
reescribir una fila. Idempotente (`init_core_schema` sólo agrega lo que falta), como la `0020`.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0031_moneda_del_libro"
down_revision = "0030_indices_ajuste"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar haría que los asientos y recibos en dólares se lean como pesos: los saldos se mezclarían sin aviso.
    raise NotImplementedError(
        "No se baja: los asientos y recibos en dólares se leerían como pesos. "
        "Para volver atrás, restaurar el backup."
    )
