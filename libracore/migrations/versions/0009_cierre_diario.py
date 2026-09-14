"""Cierre diario: el acto registrado de cerrar el día operativo de una
sucursal, con la foto de sus turnos, sus cajas y sus medios de pago.

## Por qué esto NO pasa por `init_core_schema()`

Esa función está congelada desde la `0001` (ver `test_schema_congelado.py`):
agregarle tablas nuevas movería la fixture que la vigila. Las tres tablas de
`libracore.db.cierre_diario.crear_tablas()` son DDL de esta revisión, escrito
una sola vez ahí — como todo lo que viene después de la baseline según el
propio contrato de esa función. El DDL en sí vive en `cierre_diario.py` y no
acá: es lo que le permite a un test armar la tabla sin depender de Alembic
(ver el docstring de `_DDL_TABLAS`).

## Las tres tablas

- `cierres_diarios`: la cabecera. Un UNIQUE por EXPRESIÓN
  (`COALESCE(sucursal_id,0), fecha`) es lo que impide cerrar el mismo día dos
  veces en la misma sucursal — un UNIQUE liso no alcanza porque ni SQLite ni
  PostgreSQL consideran que dos `NULL` colisionen, y una sucursal-NULL (turnos
  sin caja, o cajas sin sucursal — el caso de todo dato viejo, y de cualquier
  producto que llegue a esta primera fase antes de terminar de asignar cajas a
  sucursales) es un caso real, no un error de carga. Mismo criterio para la
  numeración correlativa: `(COALESCE(sucursal_id,0), numero)`.
- `cierres_diarios_turnos`: la foto por turno (cajero, caja, apertura, cierre,
  montos). Una fila por turno incluido en el cierre.
- `cierres_diarios_medios`: ingresos/egresos/neto por medio de pago, en TRES
  niveles que se distinguen por qué FK llevan puesta: por turno
  (`cierre_turno_id` puesto), por caja (`cierre_turno_id` NULL, `caja_id`
  puesto, sumando los turnos de esa caja en este cierre) y de la sucursal
  (las dos NULL). Sin esto el ticket de cierre diario no podría mostrar "esta
  caja hizo tanto en efectivo" sin volver a sumar `caja_movimientos` a mano
  cada vez que se reimprime — y una foto que se recalcula no es una foto.

Via `conexion_libracore(op.get_bind())`, con el MISMO dialecto
SQLite-de-siempre que usa `schema.py`: el adaptador de PostgreSQL traduce
`AUTOINCREMENT`→`BIGSERIAL`, `REAL`→`DOUBLE PRECISION`, etc. Escribir un DDL
paralelo a mano por motor es la segunda fuente que este mismo adaptador existe
para evitar.
"""
from alembic import op

from libracore.db.cierre_diario import crear_tablas
from libracore.db.migraciones import conexion_libracore

revision = "0009_cierre_diario"
down_revision = "0008_archivo_par_por_ambiente"
branch_labels = None
depends_on = None


def upgrade():
    crear_tablas(conexion_libracore(op.get_bind()))


def downgrade():
    # Bajar borraría cierres ya numerados y entregados como comprobante al
    # cliente — un acto registrado, por diseño, no se deshace. Para volver
    # atrás, restaurar el backup previo al deploy.
    raise NotImplementedError(
        "No se baja: un cierre diario es un acto registrado y entregado "
        "(el ticket ya salió por la impresora). Para volver atrás, restaurar "
        "el backup previo al deploy."
    )
