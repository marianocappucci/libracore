"""La cola de comprobantes de un nodo offline (ADR-079): la tabla `cola_arca`.

Un nodo de LibraEdge sin internet no puede emitir una factura que no nace de una venta del punto de venta (la de
LibraCargo desde una orden, la conjunta de LibraClub) ni una carta de porte electrónica: el producto deja el pedido
en esta cola y el nodo lo emite solo al volver la conexión. Una fila por pedido, idempotente por origen, con su
estado (`esperando`, `emitiendo`, `emitido`, `a_revisar`, `incierto`, `descartado`) y la `revision` con la que el
central sabe qué copia es más nueva.

Aditiva: tabla e índices nuevos, nacen vacíos y no se toca ninguna fila. Idempotente (`IF NOT EXISTS`), como `0027`.
En una instancia que no es un nodo la tabla existe y queda vacía para siempre.

🔑 **Y deja los relojes en hora de Argentina** con `alters_para_hora_ar`, como la `0027`
(`test_la_cadena_deja_toda_columna_con_reloj_en_hora_de_argentina_postgres`).
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0029_cola_arca"
down_revision = "0028_caja_del_nodo"
branch_labels = None
depends_on = None

_COLUMNAS_CON_RELOJ = (("cola_arca", "created_at"), ("cola_arca", "updated_at"))


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))
    from libracore.db.schema import AHORA_AR, alters_para_hora_ar
    for sentencia in alters_para_hora_ar(op.get_bind(), _COLUMNAS_CON_RELOJ, AHORA_AR):
        op.execute(sentencia)


def downgrade():
    # Bajar perdería los pedidos que el nodo todavía no emitió (una carta de porte o una factura que un camión
    # espera) y, peor, los que quedaron `incierto`, que son los que no se pueden volver a emitir a ciegas.
    # Además `init_core_schema()` vuelve a crear la tabla en el próximo arranque. Restaurar el backup.
    raise NotImplementedError(
        "No se baja: la cola perdería comprobantes que el nodo todavía no emitió. "
        "Para volver atrás, restaurar el backup."
    )
