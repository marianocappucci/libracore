"""El estado de acreditación de una línea de pago.

Paso 2 de `wiki/analyses/pago-pendiente-de-acreditacion-familia-libra.md`.

Hasta acá una línea de `ventas_pagos` **no tenía estado**: existía, y por lo
tanto contaba. El POS de Contalibra crea la venta con la línea de MercadoPago
cargada por el total, así que la venta nace `cobrada` antes de que nadie escanee
el QR — y `crear_venta_directa` escribe además el movimiento de caja, con lo
cual una venta que el cliente nunca paga **mete en la caja plata que no entró**
y el arqueo cierra mal.

## Por qué el default es `'aprobado'` y por qué eso no mueve nada

**Todo lo que está guardado hoy ya cobró.** Estas filas se escriben en el
momento en que el mostrador registra el pago, así que backfillearlas a
`aprobado` deja la aritmética exactamente como estaba: el gate de este paso es
que las sumas y los conteos **no se muevan en un solo peso**.

Medido contra una copia de la base real de `compulibra` antes de escribir esto:
13 pagos, suma 6548, 12 ventas todas `cobrada`, 112 movimientos de caja por
25.778.560. Los mismos números después.

⚠️ **La contracara, que hay que saber:** un `INSERT` que se olvide la columna
también queda en `aprobado`. Ese hueco NO se cierra acá —sin default no hay
manera de backfillear las filas viejas en un `ALTER`— sino en el camino de
escritura de `db/ventas.py`, donde el estado pasa a ser obligatorio. Mientras
tanto el comportamiento es el de hoy, que es lo que corresponde: esta migración
no cambia nada por sí sola.

El `CHECK` sí vive acá: un estado inventado no entra ni por un error de tipeo, y
el vocabulario de `libracore.pagos.EstadoAcreditacion` queda declarado en la
base.

Llama a `init_core_schema()` en vez de re-expresar el `ALTER`, como el resto de
la cadena: la fuente de verdad es el DDL del motor, y re-escribirlo acá crearía
una segunda que se desincroniza en el primer cambio.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0005_estado_acreditacion_pagos"
down_revision = "0004_punto_venta_por_caja"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar sería borrar el estado de cada pago, y con él la única marca de qué
    # plata entró y qué plata se declaró. Las filas que hoy están en `pendiente`
    # pasarían a contar como cobradas al desaparecer la columna: exactamente el
    # defecto que esta cadena vino a cerrar, y encima sobre la caja.
    raise NotImplementedError(
        "No se baja: borrar el estado haría contar como cobrados los pagos que "
        "todavía no entraron, que es el defecto que esta migración cierra. Para "
        "volver atrás, restaurar el backup."
    )
