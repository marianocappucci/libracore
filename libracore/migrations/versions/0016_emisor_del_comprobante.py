"""El emisor de cada comprobante: `facturas.emisor_id`, y la numeración por emisor y ambiente.

Hasta acá el motor suponía **un emisor por instancia**: diez lugares tomaban
`configs[0]` y la tabla `facturas` no decía con qué configuración de ARCA se
emitió cada comprobante. LibraCargo factura con varias razones sociales y por
eso tenía su propio modelo de comprobantes. Esta revisión es el primer paso para
que use el del motor (`wiki/analyses/libracargo-modelo-normalizado-diseno.md`).

- `facturas.emisor_id`: FK a `arca_config.id`, **opcional**. `NULL` es «el único
  emisor de la instancia», que es lo que tiene todo comprobante ya emitido y lo
  que sigue escribiendo todo producto que no pasa emisor.
- `idx_facturas_numeracion` reemplaza a `idx_facturas_numero_unico`: la unicidad
  pasa de `(tipo, punto_venta, numero)` a `(emisor, ambiente, tipo, punto_venta,
  numero)`. Es más laxa, así que no puede fallar sobre datos que cumplían la
  vieja. Corrige además el choque entre un comprobante de homologación y uno real
  con el mismo número, que ARCA permite y el índice viejo no.

Llama a `init_core_schema()`, como el resto de la cadena: la fuente de verdad es
el DDL del motor.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0016_emisor_del_comprobante"
down_revision = "0015_cae_error_en_facturas"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar borraría con qué emisor se emitió cada comprobante, y con dos
    # razones sociales eso no se puede reconstruir. No se baja: restaurar el backup.
    raise NotImplementedError(
        "No se baja: se perdería con qué emisor se emitió cada comprobante. "
        "Para volver atrás, restaurar el backup."
    )
