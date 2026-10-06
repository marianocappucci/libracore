"""La pre factura: `comprobantes_pendientes` con número interno, emisor, tipo y envío (ADR-030).

La bandeja de comprobantes por facturar pasa a poder ser una **pre factura**: un documento
no fiscal con número propio (`PF-0001`), que se manda al cliente para que confirme los datos
y se factura después. Agrega ocho columnas que nacen vacías y un índice único parcial:
`numero_interno`, `emisor_id`, `tipo_comprobante`, `fecha_vencimiento_pago`, `enviado_at`,
`enviado_a`, `aceptado_at` y `aceptado_por`. No toca filas: lo que ya está en la bandeja
(Contalibra, LibraDesk) sigue igual.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0021_pre_factura"
down_revision = "0020_origen_del_asiento"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar sólo esto dejaría las pre facturas ya numeradas sin número, emisor ni tipo, y las
    # `enviado` y `aceptado` en estados que la bandeja de antes no conoce. Restaurar el backup.
    raise NotImplementedError(
        "No se baja: las pre facturas perderían su número, su emisor y su estado. "
        "Para volver atrás, restaurar el backup."
    )
