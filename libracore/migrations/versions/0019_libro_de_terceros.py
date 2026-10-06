"""El libro de cuenta corriente de terceros: `cc_asientos` (ADR-026).

Asientos de debe y haber por (tercero, rol), opcionales y aparte de la cuenta
corriente de clientes, que sigue calculada como siempre. Ningún producto lo usa
todavía: el primero va a ser LibraCargo, que hoy lleva su libro propio
(`wiki/analyses/cuenta-corriente-de-terceros-diseno.md`, etapa 5 del diseño
«LibraCargo sobre el modelo de comprobantes del motor»).

Crea una tabla vacía y sus índices: no toca filas. Llama a `init_core_schema()`,
como el resto de la cadena.

🔑 **Y deja `cc_asientos.created_at` en hora de Argentina** con el mismo helper que
la `0003` (`alters_para_hora_ar`). El DDL ya nace así, pero una base que llegó con
el DEFAULT viejo —o la guarda de Contalibra y Restolibra, que lo vuelve a UTC en
todas las columnas con reloj y corre la cadena para ver que la cadena lo arregla—
necesita que alguna revisión lo nombre. La `0003` tiene su lista fija de 2026-08 y
esta tabla no estaba.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0019_libro_de_terceros"
down_revision = "0018_dinero_exacto"
branch_labels = None
depends_on = None


#: Las columnas con reloj que ninguna revisión anterior nombra (ver la `0003`): la de
#: esta tabla, y `cierres_diarios.created_at`, que entró después de la lista de la
#: `0003` y en los productos la cubría su propia cadena. Lo midió
#: `test_la_cadena_deja_toda_columna_con_reloj_en_hora_de_argentina_postgres`.
_COLUMNAS_CON_RELOJ = (("cc_asientos", "created_at"), ("cierres_diarios", "created_at"))


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))
    from libracore.db.schema import AHORA_AR, alters_para_hora_ar

    for sentencia in alters_para_hora_ar(op.get_bind(), _COLUMNAS_CON_RELOJ, AHORA_AR):
        op.execute(sentencia)


def downgrade():
    # Bajar borraría los asientos de los productos que ya lo usen, y con ellos sus
    # cuentas corrientes. No se baja: restaurar el backup.
    raise NotImplementedError(
        "No se baja: se perderían los asientos del libro de terceros. "
        "Para volver atrás, restaurar el backup."
    )
