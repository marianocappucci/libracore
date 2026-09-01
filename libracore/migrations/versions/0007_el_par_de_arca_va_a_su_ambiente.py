"""Las credenciales de ARCA pasan a vivir en las columnas de su ambiente.

Hasta hoy `arca_config` guardaba **un** par de credenciales y `ambiente` hacía
dos trabajos: decir de quién era ese par y decir cuál se usaba. Con un solo par,
probar con el cliente obligaba a **pisar** el archivo — una operación
destructiva y de ida y vuelta, justo sobre la credencial que después tiene que
quedar bien.

Ahora conviven los dos pares y `ambiente` queda como **selector** puro.

## 🔴 Sin este backfill, las instancias en homologación pierden sus credenciales

El par existente vive en las columnas **sin sufijo**, que son las de producción.
Una instancia que hoy está en `homologacion` —las demos— pasaría a buscar su par
en `*_homologacion`, que están vacías: `paths_de()` devolvería `("", "")` y la
facturación dejaría de andar **sin que nadie haya tocado nada**.

Así que el par se mueve a las columnas del ambiente que la instancia tiene
seleccionado, que es de quién es en realidad. Las de producción quedan vacías:
esa instancia todavía no tiene certificado de producción, y decir que sí lo
tiene sería peor que decir que no.

Lo llama `init_core_schema()` para el DDL, como el resto de la cadena; el
movimiento de datos va acá porque es una decisión, no un `ALTER`.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0007_par_arca_por_ambiente"
down_revision = "0006_ambiente_arca_factura"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    init_core_schema(conexion_libracore(conn))

    # Sólo las filas cuyo selector es `homologacion`: en las de producción el
    # par ya está donde corresponde y no hay nada que mover.
    #
    # 🔑 Y sólo si el destino está vacío. Correr dos veces no tiene que pisar
    # nada: si alguien ya cargó el par de homologación por la pantalla, el suyo
    # manda sobre este movimiento.
    try:
        conn.exec_driver_sql(
            """
            UPDATE arca_config
               SET clave_path_homologacion = clave_path,
                   certificado_path_homologacion = certificado_path,
                   clave_path = '',
                   certificado_path = ''
             WHERE ambiente = 'homologacion'
               AND COALESCE(clave_path_homologacion, '') = ''
               AND COALESCE(certificado_path_homologacion, '') = ''
            """
        )
    except Exception:
        # `arca_config` puede no existir en una instancia que nunca configuró
        # ARCA. Sin tabla no hay credenciales que mover.
        pass


def downgrade():
    # Bajar dejaría a las instancias de homologación con las columnas viejas
    # vacías y las nuevas por desaparecer: perderían la credencial. Volver
    # atrás es restaurar el backup previo al deploy.
    raise NotImplementedError(
        "No se baja: las instancias en homologación perderían su credencial, "
        "que es justo lo que este backfill viene a evitar."
    )
