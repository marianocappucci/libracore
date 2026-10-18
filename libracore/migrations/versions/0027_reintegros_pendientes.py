"""El destino de la plata de una nota de crédito sin CAE (ADR-067): la tabla `reintegros_pendientes`.

Hasta acá, si la nota de una factura al contado cobrada se emitía eligiendo «devolver» o «dejar a favor» y ARCA no
le daba CAE, el destino se perdía: la nota se autorizaba después con `/autorizar` y la plata había que moverla a
mano. Ahora el destino queda anotado, una fila por nota, hasta que se autoriza (ahí se mueve la plata y la fila
se borra) o hasta que la nota se borra (la fila se va con ella, `ON DELETE CASCADE`).

Aditiva: no toca filas ni columnas existentes; las notas que ya estaban sin CAE no tienen destino anotado y se
siguen resolviendo a mano. Idempotente (`IF NOT EXISTS`), como `0021` y `0026`.

🔑 **Y deja `created_at` en hora de Argentina** con `alters_para_hora_ar`, como la `0026`
(`test_la_cadena_deja_toda_columna_con_reloj_en_hora_de_argentina_postgres`).
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0027_reintegros_pendientes"
down_revision = "0026_envios_email"
branch_labels = None
depends_on = None

_COLUMNAS_CON_RELOJ = (("reintegros_pendientes", "created_at"),)


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))
    from libracore.db.schema import AHORA_AR, alters_para_hora_ar
    for sentencia in alters_para_hora_ar(op.get_bind(), _COLUMNAS_CON_RELOJ, AHORA_AR):
        op.execute(sentencia)


def downgrade():
    # Sólo pierde los destinos de notas que siguen sin CAE: esas notas vuelven a resolverse a mano (la marca
    # `nc:factura:<id>:<nota_id>`), que es como estaban antes. No toca ninguna plata ya movida.
    op.execute("DROP TABLE IF EXISTS reintegros_pendientes")
