"""La caja del nodo sube al central (ADR-076): `origen` y `origen_id` en `turnos_caja`, `caja_movimientos` y
`cierres_diarios`.

El nodo de LibraEdge abre turnos, anota movimientos y cierra el día sin ver al central, y todo eso sube. El central
tiene que saber **que una fila vino de un nodo y cuál era su id allá**, para aplicar dos veces la misma operación sin
duplicar nada (el outbox entrega al menos una vez). Dos columnas por tabla, `origen` (el id del nodo) y `origen_id`
(el id de la fila en la base del nodo), y un índice único parcial sobre el par.

Aditiva: nacen vacías (`NULL` es «fila propia», que es todo lo que existe hoy) y no se toca ninguna fila.
Idempotente, como `0017` y `0020`.

- `turnos_caja` y `caja_movimientos`: las columnas y sus índices los da `init_core_schema()`, como el resto de la
  cadena desde la `0012`.
- `cierres_diarios` **no** está en `init_core_schema()` (la crea `cierre_diario.crear_tablas()`, la `0009`), y su
  forma final ya trae las columnas para una tabla nueva. Esta revisión las agrega a la que ya existe con `ALTER`
  puro, como la `0011`, y después `crear_tablas()` pone el índice (que sólo crea si las columnas están: se llama en
  cada arranque de LibraClub y no puede morir en una base sin migrar).
"""
import sqlalchemy as sa
from alembic import op

from libracore.db.cierre_diario import crear_tablas
from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0028_caja_del_nodo"
down_revision = "0027_reintegros_pendientes"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    init_core_schema(conexion_libracore(bind))

    columnas = {c["name"] for c in sa.inspect(bind).get_columns("cierres_diarios")}
    if "origen" not in columnas:
        op.execute("ALTER TABLE cierres_diarios ADD COLUMN origen TEXT")
    if "origen_id" not in columnas:
        op.execute("ALTER TABLE cierres_diarios ADD COLUMN origen_id INTEGER")
    crear_tablas(conexion_libracore(bind))


def downgrade():
    # Bajar perdería de qué nodo vino cada fila, y con eso la idempotencia: el próximo reintento del outbox
    # duplicaría turnos, movimientos y cierres. Además `init_core_schema()` vuelve a poner las columnas en el
    # próximo arranque. Restaurar el backup.
    raise NotImplementedError(
        "No se baja: la caja que subió un nodo perdería su origen y un reintento la duplicaría. "
        "Para volver atrás, restaurar el backup."
    )
