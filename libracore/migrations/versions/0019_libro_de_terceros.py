"""El libro de cuenta corriente de terceros: `cc_asientos` (ADR-026).

Asientos de debe y haber por (tercero, rol), opcionales y aparte de la cuenta
corriente de clientes, que sigue calculada como siempre. Ningún producto lo usa
todavía: el primero va a ser LibraCargo, que hoy lleva su libro propio
(`wiki/analyses/cuenta-corriente-de-terceros-diseno.md`, etapa 5 del diseño
«LibraCargo sobre el modelo de comprobantes del motor»).

Crea una tabla vacía y sus índices: no toca filas. Llama a `init_core_schema()`,
como el resto de la cadena.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0019_libro_de_terceros"
down_revision = "0018_dinero_exacto"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar borraría los asientos de los productos que ya lo usen, y con ellos sus
    # cuentas corrientes. No se baja: restaurar el backup.
    raise NotImplementedError(
        "No se baja: se perderían los asientos del libro de terceros. "
        "Para volver atrás, restaurar el backup."
    )
