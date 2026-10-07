"""Las credenciales de ARCA por servicio: `arca_credenciales_servicio` (ADR-032).

Hasta acá el motor sabía de **un** servicio de ARCA, la facturación (`wsfe`): su par
certificado/clave vive en las columnas de `arca_config` (el de producción en las sin
sufijo, ver `COLUMNAS_POR_AMBIENTE`). LibraCargo necesita además el del CTG y la Carta
de Porte (`wscpe`), con un certificado propio, y la pantalla de Configuración / ARCA
tiene que mostrar los dos.

Crea una tabla vacía: una fila por (empresa, servicio, ambiente) con la ruta del
certificado y de la clave. **No toca `arca_config` ni ninguna fila**: la facturación
sigue exactamente como estaba. Llama a `init_core_schema()`, como el resto de la
cadena: la fuente de verdad es el DDL del motor.

🔑 **Y deja las dos columnas con reloj de la tabla en hora de Argentina**, con el mismo
helper que la `0003` y la `0019` (`alters_para_hora_ar`): el DDL ya nace así, pero una
base cuyo DEFAULT volvió a UTC necesita que alguna revisión nombre la tabla. Lo mide
`test_la_cadena_deja_toda_columna_con_reloj_en_hora_de_argentina_postgres`.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0022_credenciales_por_servicio"
down_revision = "0021_pre_factura"
branch_labels = None
depends_on = None


_COLUMNAS_CON_RELOJ = (
    ("arca_credenciales_servicio", "created_at"),
    ("arca_credenciales_servicio", "updated_at"),
)


def upgrade():
    init_core_schema(conexion_libracore(op.get_bind()))
    from libracore.db.schema import AHORA_AR, alters_para_hora_ar

    for sentencia in alters_para_hora_ar(op.get_bind(), _COLUMNAS_CON_RELOJ, AHORA_AR):
        op.execute(sentencia)


def downgrade():
    # Se puede bajar porque la tabla sólo guarda *dónde* están los archivos, no los
    # archivos: los `.crt` y `.key` quedan en `CERTS_DIR` y se vuelven a asociar
    # subiéndolos de nuevo. Lo único que se pierde es esa asociación. La facturación
    # no se toca. `IF EXISTS` para que bajar sobre una base que nunca la tuvo no falle.
    op.execute("DROP TABLE IF EXISTS arca_credenciales_servicio")
