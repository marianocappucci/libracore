"""Por qué ARCA no autorizó un comprobante: `facturas.cae_error`.

`arca_facturacion.solicitar_cae` tragaba el rechazo de ARCA —sólo `logger.error`—
y devolvía la factura sin CAE: numerada, cobrada y sin autorizar, y nadie lo
veía. No puede levantar la excepción, porque en los tres caminos que lo llaman
el comprobante ya existe y se sigue con el cobro o el vínculo a la venta. Lo que
sí puede es **guardar el motivo** en la factura, donde la pantalla y el reintento
(`POST /api/facturas/{id}/autorizar`) lo ven.

`TEXT NOT NULL DEFAULT ''`: las filas que ya estaban quedan sin error, y un CAE
obtenido lo borra.

Llama a `init_core_schema()` para mantener la fuente de verdad en un solo lugar
(el DDL del motor), igual que el resto de la cadena de migraciones.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0015_cae_error_en_facturas"
down_revision = "0014_fce_mipyme"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar perdería el motivo por el que cada comprobante quedó sin CAE. No se
    # baja: restaurar el backup.
    raise NotImplementedError(
        "No se baja: se perdería el motivo por el que cada comprobante quedó sin CAE. "
        "Para volver atrás, restaurar el backup."
    )
