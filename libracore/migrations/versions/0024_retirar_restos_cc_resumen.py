"""Se retiran los restos de datos del resumen de cuenta corriente por mail (ADR-042).

El envío periódico del resumen (`libracore/cc_resumen.py`) se borró en la v1.154.0 por ser
código muerto: ningún producto, script ni router de la familia lo importaba. Quedaban sus
datos, que nadie lee ni escribe:

- la tabla `cc_resumenes_enviados` (el rastro de cada intento de envío, con su índice
  `idx_cc_resumenes_cliente` y su FK a `clients`);
- tres columnas de `clients`: `cc_resumen_auto`, `cc_resumen_frecuencia` (con su CHECK
  `clients_cc_resumen_frecuencia_check`) y `cc_resumen_ultimo_envio`.

`init_core_schema()` ya no las crea; acá se sacan de las bases que las traen. **Se pierde el
historial de envíos** (decisión del dueño, 2026-10-12: «los borramos con una migración»): como
el módulo estuvo apagado (`cc_resumen_habilitado` en `0` por defecto) lo normal es que la tabla
esté vacía, pero no se verifica acá. Sin el backup, no hay vuelta.

Las claves `cc_resumen_*` del `config.json` **no son de esta migración**: viven en un archivo
de cada instancia, no en la base. Dejaron de estar en `DEFAULTS`, y si una instancia las tiene
guardadas quedan ahí, sin leerlas nadie; se borran la próxima vez que se edite esa config a
mano, o nunca, y da lo mismo.

Idempotente y tolerante, como `0002` y `0003`: `alembic upgrade` corre sobre bases que llegaron
acá por caminos distintos. Una base nueva ya no trae nada de esto (la cadena pasó por
`init_core_schema()` en su forma actual) y la revisión no hace nada.

- **PostgreSQL** lo dice con `IF EXISTS` en cada sentencia.
- **SQLite** no tiene `DROP COLUMN IF EXISTS`: pregunta por las columnas y borra las que hay.
  `DROP COLUMN` anda porque nada más las referencia (el CHECK es de la propia columna).

🔴 **Orden de despliegue: primero el pin, después la migración.** El `update_client` de las
versiones anteriores a la v1.155.0 escribe `cc_resumen_auto` y `cc_resumen_frecuencia` en cada
edición de un cliente: con las columnas ya borradas, esa edición revienta. Esta revisión se
corre al subir el pin, con el código nuevo ya en el contenedor, no antes.
"""
import sqlalchemy as sa
from alembic import op

revision = "0024_retirar_cc_resumen"
down_revision = "0023_fce_varios_cbu"
branch_labels = None
depends_on = None

_COLUMNAS = ("cc_resumen_auto", "cc_resumen_frecuencia", "cc_resumen_ultimo_envio")


def upgrade():
    bind = op.get_bind()

    # La tabla se va primero: lleva la FK a `clients`, y así ni el índice ni la FK estorban.
    # `DROP TABLE` se lleva el índice `idx_cc_resumenes_cliente`.
    op.execute("DROP TABLE IF EXISTS cc_resumenes_enviados")

    if bind.dialect.name == "sqlite":
        presentes = {c["name"] for c in sa.inspect(bind).get_columns("clients")}
        for columna in _COLUMNAS:
            if columna in presentes:
                op.execute(f"ALTER TABLE clients DROP COLUMN {columna}")
        return

    # El CHECK se nombra aparte aunque `DROP COLUMN` se lo llevaría puesto: queda dicho qué
    # se retira, y no depende de cómo PostgreSQL resuelva una constraint de una sola columna.
    op.execute("ALTER TABLE clients DROP CONSTRAINT IF EXISTS clients_cc_resumen_frecuencia_check")
    for columna in _COLUMNAS:
        op.execute(f"ALTER TABLE clients DROP COLUMN IF EXISTS {columna}")


def downgrade():
    # Bajar no devolvería lo que importa: el historial de envíos y el flag de cada cliente se
    # borraron con sus datos, y recrear la tabla y las columnas vacías sería fingir que
    # volvieron. No se baja: restaurar el backup.
    raise NotImplementedError(
        "No se baja: el historial de resúmenes enviados y las preferencias de envío de cada "
        "cliente se borraron. Para volver atrás, restaurar el backup."
    )
