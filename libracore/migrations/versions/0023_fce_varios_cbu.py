"""La FCE con varios CBU: `arca_config.fce_cbus` y la cuenta de la pre factura (ADR-040).

Hasta acá una empresa tenía **un** CBU para cobrar la Factura de Crédito Electrónica
(`arca_config.fce_cbu`). Ahora puede cargar varios —cada uno con su alias bancario y una
etiqueta— y elegir en cuál cobrar cada FCE.

- `arca_config.fce_cbus TEXT NOT NULL DEFAULT ''`: JSON con
  `[{"cbu": "<22 dígitos>", "alias": "<alias o vacío>", "etiqueta": "<texto o vacío>"}]`.
- `comprobantes_pendientes.fce_cbu TEXT` (nullable, como `fecha_vencimiento_pago`): la cuenta
  elegida para cobrar la FCE de una pre factura. `NULL` es «la predeterminada»; las pre
  facturas que ya estaban quedan así.
- `fce_cbu` **se conserva** y pasa a ser el CBU predeterminado: quien ya lo lee
  (el PDF, WSFE, los productos) sigue andando sin enterarse.

El DDL lo pone `init_core_schema()`, como el resto de la cadena. Acá va además el
**relleno**, que es una decisión y no un `ALTER`: la fila que ya tenía `fce_cbu` y no
tenía lista pasa a tener una lista de uno, así la pantalla nueva muestra la cuenta que
ya estaba cargada. Es idempotente: sólo toca filas con `fce_cbus = ''`, así que correrla
dos veces no pisa una lista que alguien ya armó por la pantalla.
"""
import json

import sqlalchemy as sa
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0023_fce_varios_cbu"
down_revision = "0022_credenciales_por_servicio"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    init_core_schema(conexion_libracore(bind))

    filas = bind.execute(
        sa.text("SELECT id, fce_cbu FROM arca_config WHERE fce_cbu <> '' AND fce_cbus = ''")
    ).fetchall()
    for id_, cbu in filas:
        bind.execute(
            sa.text("UPDATE arca_config SET fce_cbus = :lista WHERE id = :id"),
            {"lista": json.dumps([{"cbu": cbu, "alias": "", "etiqueta": ""}]), "id": id_},
        )


def downgrade():
    # Bajar perdería las cuentas que el dueño cargó además del predeterminado, con sus
    # alias y etiquetas, y no hay de dónde recuperarlas: `fce_cbu` sólo guarda una. No se
    # baja: restaurar el backup.
    raise NotImplementedError(
        "No se baja: se perderían los CBU, alias y etiquetas de la FCE cargados además del "
        "predeterminado. Para volver atrás, restaurar el backup."
    )
