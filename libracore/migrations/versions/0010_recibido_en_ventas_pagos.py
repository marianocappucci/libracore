"""El vuelto: cuánto entregó el cliente por un pago.

Fase F2 de `wiki/analyses/plan-ventalibra-a-libracommerce.md`, decisión D4.

## Qué resuelve

`ventas_pagos` guardaba `monto` — lo que el pago LIQUIDÓ a la venta — pero no
lo que el cliente entregó en mano. Sin eso el mostrador no puede calcular el
vuelto ($500 entregados − $380 de la venta = $120 de vuelto): hoy lo hace de
cabeza, o no lo hace.

## `recibido` es NULLABLE, SIN default — eso es la decisión (D4)

`NULL` significa "no se registró": es el estado de TODAS las filas de hoy, y
el que van a seguir teniendo Contalibra y Restolibra, que no llenan esta
columna. La escribe sólo LibraCommerce/VentaLibra, y sólo cuando el mostrador
carga un valor — LibraCore no escribe acá (`db/ventas.py:add_venta_pago` no
cambia; sigue insertando `venta_id, medio, monto, referencia` nomás).

Tipo `Float` (⇒ `REAL` en SQLite, `FLOAT` en PostgreSQL) para seguir el mismo
estilo que `monto`, que es `REAL` en el DDL nativo de `schema.py`.

## Por qué esto es Alembic puro, y NO una línea en `init_core_schema()`

El resto de la cadena que agrega una columna a una tabla ya existente (`0004`
sobre `cajas`, `0005` sobre esta misma `ventas_pagos`, `0006` sobre
`facturas`) lo hace con un `ALTER TABLE ... ADD COLUMN` defensivo DENTRO de
`init_core_schema()` — ver `db/schema.py` líneas ≈800-870 — con el mismo
motivo siempre: esa función corre en CADA arranque de cada producto (ver
`db/schema.py`, línea 4), así que ponerlo ahí es lo único que una instancia
vieja recibe sin correr Alembic.

Pero esa misma idempotencia le arruina el `downgrade`: si `recibido` naciera
en `init_core_schema()`, un `alembic downgrade` que la saque queda deshecho en
el próximo arranque del producto, que la vuelve a crear. Por eso `0004` a
`0008` bajan con `NotImplementedError` — en varias el motivo que dan además es
de integridad de datos, pero de fondo el downgrade tampoco se sostendría
aunque quisieran.

Acá se sigue el patrón de la `0002` (las cuatro columnas de `clients`, la
única revisión de la cadena que hoy baja de verdad): la columna entra por
Alembic puro (`op.add_column`/`op.drop_column`), nunca por
`init_core_schema()`, así que Alembic queda como único dueño y el `downgrade`
se sostiene.

**El costo, y por qué no importa hoy:** una instancia que arme su base sólo
con `init_core_schema()` sin correr Alembic después no tiene `recibido` (ver
`libracore/provisioning/panel_admin.py` sobre Contalibra/Restolibra vs. el
camino de VentaLibra). Esas dos no van a escribir esta columna nunca, así que
no tener el `alembic upgrade head` corrido no les cuesta nada.

## El downgrade es seguro: nada la agrega ni la usa en un cálculo

A diferencia de `0005`/`0006`/`0007`, bajar acá no reclasifica ni desordena
nada: se relevó cada uso de `ventas_pagos` en el paquete
(`db/reportes.py`, `db/cuenta_corriente.py`, `db/turnos.py`, `db/ventas.py`) y
ninguno hace `SELECT *` ni agrega sobre `recibido` — todos nombran columnas
explícitas (`vp.monto`, `vp.medio`) o, el único `SELECT *`
(`ventas.get_venta()`), la vuelve dict y no la necesita para nada. Perder la
columna al bajar sólo le saca al comprobante ya emitido el dato del vuelto,
recuperable restaurando el backup previo si hiciera falta.
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_recibido_en_ventas_pagos"
down_revision = "0009_cierre_diario"
branch_labels = None
depends_on = None


def _columnas_existentes(bind) -> set[str]:
    """Qué columnas tiene ya `ventas_pagos`.

    Mismo criterio que `0002`: `alembic upgrade head` corre sobre bases vivas
    que pueden llegar acá por caminos distintos, así que la revisión tiene que
    poder correr dos veces sin romper.
    """
    return {c["name"] for c in sa.inspect(bind).get_columns("ventas_pagos")}


def upgrade():
    existentes = _columnas_existentes(op.get_bind())
    if "recibido" not in existentes:
        op.add_column(
            "ventas_pagos", sa.Column("recibido", sa.Float(), nullable=True)
        )


def downgrade():
    existentes = _columnas_existentes(op.get_bind())
    if "recibido" in existentes:
        op.drop_column("ventas_pagos", "recibido")
