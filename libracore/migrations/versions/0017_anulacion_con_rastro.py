"""La anulación con rastro de un comprobante sin CAE: `facturas.anulada_en`, `anulada_por` y `anulacion_motivo`.

Hasta acá un comprobante sin CAE sólo se podía **borrar**: desaparecía su número
y nadie sabía que existió. Un producto que registra comprobantes a mano
(LibraCargo, ADR-024 de ese producto) los anula y conserva el rastro, y para
usar la tabla del motor necesita lo mismo
(`wiki/analyses/libracargo-modelo-normalizado-diseno.md`, M3). Al resto de la
familia le sirve para auditar; el `DELETE` sigue para quien no lo use.

Las tres columnas son opcionales y no se tocan filas: todo lo ya emitido queda
vigente (`anulada_en` en `NULL`).

Llama a `init_core_schema()`, como el resto de la cadena.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0017_anulacion_con_rastro"
down_revision = "0016_emisor_del_comprobante"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar haría que los comprobantes anulados vuelvan a contar como vigentes:
    # entrarían al libro IVA y a la cuenta corriente. No se baja: restaurar el backup.
    raise NotImplementedError(
        "No se baja: los comprobantes anulados volverían a contar como vigentes. "
        "Para volver atrás, restaurar el backup."
    )
