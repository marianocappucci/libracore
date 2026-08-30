"""El punto de venta de ARCA, por caja.

Hasta acá había **uno solo por instancia**, el de `arca_config`. Un cliente con
varios POS en el mismo salón necesita numeración fiscal separada por mostrador,
porque ARCA numera por **(tipo, punto de venta)**: dos cajas con el mismo punto
de venta comparten la serie y compiten por el próximo número, y el choque lo
detecta ARCA rechazando el segundo comprobante.

**La columna es nullable a propósito y no se llena.** `NULL` significa "esta caja
usa el punto de venta de la empresa", que es exactamente como funcionan todas las
instancias que existen hoy: la migración no les cambia el comportamiento en nada.
Ponerle un default habría hecho lo contrario — inventar un punto de venta por
caja donde nadie lo pidió.

Llama a `init_core_schema()` en vez de re-expresar el `ALTER`, por el mismo
motivo que el resto de la cadena: la fuente de verdad es el DDL del motor, y
re-escribirlo acá crearía una segunda fuente que se desincroniza en el primer
cambio.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0004_punto_venta_por_caja"
down_revision = "0003_created_at_hora_ar"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar sería borrar el punto de venta de cada caja, y con él la separación
    # fiscal entre mostradores. Los comprobantes ya emitidos guardan el suyo en
    # `facturas.punto_venta`, así que no se pierden — pero la próxima emisión
    # volvería a numerar sobre la serie de la empresa, encima de números que ya
    # existen.
    raise NotImplementedError(
        "No se baja: dejaría a las cajas numerando sobre la serie de la empresa, "
        "encima de comprobantes que ya existen. Para volver atrás, restaurar el "
        "backup."
    )
