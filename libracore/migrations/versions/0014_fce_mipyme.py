"""Factura de Crédito Electrónica MiPyME (FCE): lo que ARCA exige y no tenía dónde vivir.

- `arca_config.fce_cbu` y `fce_transmision` (SCA o ADC): el CBU del emisor y la
  modalidad de transmisión, que van en cada FCE.
- `facturas.fce_cbu`, `fce_transmision`, `fce_anulacion` y `cbte_asoc_fecha`: lo
  que se emitió, para que un comprobante diga con qué CBU salió aunque la config
  cambie, y la fecha del comprobante asociado que exige una nota de FCE.

Todas son `TEXT NOT NULL DEFAULT ''`: las filas que ya estaban quedan vacías, y
ningún comprobante que no sea FCE las mira.

Llama a `init_core_schema()` para mantener la fuente de verdad en un solo lugar
(el DDL del motor), igual que el resto de la cadena de migraciones.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0014_fce_mipyme"
down_revision = "0013_cc_pago_en_caja_movimientos"
branch_labels = None
depends_on = None


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar perdería el CBU con el que salió cada FCE y la fecha de su asociado,
    # que ARCA ya tiene y la factura tiene que poder mostrar. No se baja:
    # restaurar el backup.
    raise NotImplementedError(
        "No se baja: se perdería el CBU y la fecha asociada de las FCE emitidas. "
        "Para volver atrás, restaurar el backup."
    )
