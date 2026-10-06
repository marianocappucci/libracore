"""La cuenta de clientes también como libro: `cc_asientos.origen` (opción B, etapa B1).

El motor empieza a llevar la cuenta corriente de clientes de los siete productos
también en el libro de terceros (`libracore.db.libro_de_clientes`), al lado del
saldo calculado, que es el que se sigue leyendo. Cada asiento dice de qué hecho
viene (`cc_pago:12`, `caja_mov:34`...), y eso es lo que permite reconstruir el
libro sin duplicar, revertir lo que se borra o se anula, y comparar contra el
cálculo (`wiki/analyses/libro-de-terceros-para-la-familia-diseno.md`).

Agrega una columna que nace vacía y su índice: no toca filas. El libro se llena
con `libro_de_clientes.reconstruir()`, que se corre aparte al desplegar.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0020_origen_del_asiento"
down_revision = "0019_libro_de_terceros"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # La `0019` no se baja, y esta columna es de su tabla: bajar sólo esta dejaría
    # los asientos de la cuenta de clientes sin origen. Restaurar el backup.
    raise NotImplementedError(
        "No se baja: los asientos de la cuenta de clientes perderían su origen. "
        "Para volver atrás, restaurar el backup."
    )
