"""Reabrir día: anular un cierre diario en vez de vivir cerrado para siempre.

## Qué resuelve

Hasta acá `cerrar_dia()` no tenía vuelta atrás: una sucursal que cerraba el
día por error (o que necesitaba corregir algo y volver a operar) se quedaba
sin poder abrir turnos hasta el día siguiente — `verificar_dia_abierto()`
frena `turnos.create_turno()` para SIEMPRE una vez que el día está cerrado.

Ahora un admin puede anular el cierre (`libracore.db.cierre_diario.
reabrir_dia()`, endpoint `POST /cierre-diario/{id}/reabrir`) con un motivo
obligatorio. Anular no borra nada: es un `UPDATE` que marca `anulado_en`/
`anulado_por`/`motivo_anulacion` sobre la MISMA fila — el cierre sigue
existiendo, con su número, como corresponde a un acto registrado.

## Las tres columnas

- `anulado_en` (timestamptz/TEXT, NULL): cuándo se anuló. `NULL` es el
  estado normal — "activo".
- `anulado_por` (FK a `usuarios(id)`, `ON DELETE SET NULL`): quién. La misma
  regla que el resto de las FK de auditoría del motor (ver `db/schema.py`,
  `usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL` repetido
  en `facturas`, `remitos`, `presupuestos`, `caja_movimientos`, etc.) —
  borrar al usuario no debe arrastrarse el cierre ni bloquear el borrado.
- `motivo_anulacion` (TEXT, NULL): por qué.

## El índice de fecha pasa a ser PARCIAL, sin ventana sin UNIQUE

El UNIQUE de `(COALESCE(sucursal_id,0), fecha)` impedía cerrar el mismo día
dos veces — y con `reabrir_dia()` eso deja de ser cierto: un día reabierto
tiene que poder volver a cerrarse. La regla real ahora es "una sucursal no
tiene dos cierres ACTIVOS el mismo día", así que el índice queda `WHERE
anulado_en IS NULL`.

El reemplazo se hace **sin dejar un instante sin ningún UNIQUE activo**:
primero se crea el índice nuevo (con otro nombre,
`ux_cierres_diarios_sucursal_fecha_activo` — ya empieza a proteger la
unicidad de las filas ACTIVAS apenas existe) y recién después se dropea el
viejo (`ux_cierres_diarios_sucursal_fecha`). Nada de `CONCURRENTLY`: correr
las dos sentencias dentro de la misma transacción de la migración es lo que
garantiza que un fallo a mitad de camino no deje la base sin ninguno de los
dos — `CONCURRENTLY` en PostgreSQL no puede correr dentro de una
transacción.

No se renombra el índice nuevo de vuelta al nombre viejo: SQLite no tiene
`ALTER INDEX ... RENAME` (sólo tablas/columnas), así que hacerlo requeriría
una rama por motor para un cambio cosmético — el nombre ya es autodescriptivo
(`..._activo`).

El UNIQUE de **número** no cambia: un cierre anulado conserva el suyo, y
`_cerrar_dia_en_transaccion` calcula el próximo contra `MAX(numero)` de la
sucursal sin filtrar anulados (ver `db/cierre_diario.py`).

## Por qué esto es Alembic puro, y NO un ALTER dentro de `crear_tablas()`

Mismo motivo que `0010_recibido_en_ventas_pagos`, con el mismo costo si se
ignora: `cierre_diario.crear_tablas()` no es un DDL que corra sólo una vez.
**LibraClub la llama en CADA arranque** (`app/servicios/facturacion.py::
configurar()`, `crear_tablas()` es `CREATE TABLE/INDEX IF NOT EXISTS`,
documentada para eso) — así que un `ALTER` idempotente puesto ahí adentro se
reaplicaría en el próximo boot del producto aunque alguien hubiera bajado
esta revisión con `alembic downgrade`, dejando un downgrade que no se
sostiene un reinicio.

Por eso el `_DDL_TABLAS` de `cierre_diario.py` sólo define la forma FINAL
(para una tabla realmente nueva — `CREATE TABLE IF NOT EXISTS` no toca una
que ya existe) y esta revisión es quien migra, con `op.add_column`/
`op.execute` puros, una tabla que YA EXISTE desde la `0009` con la forma
vieja. Una base nueva llega a la forma final directo desde la `0009` (que
llama a la versión VIVA de `crear_tablas()`); acá el `upgrade` encuentra las
columnas ya puestas y no hace nada — ver `_columnas_existentes`, mismo
patrón que `0010`.

## Downgrade: falla cerrado si hay algo que el índice viejo no toleraría

Bajar reconstruye el UNIQUE liso (sin `WHERE`) y borra las tres columnas —
pero un UNIQUE liso rechaza dos filas con la misma `(sucursal, fecha)`
aunque una esté anulada. Si existe ese par (activo+anulado, o dos anulados,
de la misma sucursal y fecha), recrear el índice viejo fallaría a mitad de
camino con la base ya sin el índice nuevo: un estado peor que no bajar. Por
eso el chequeo va ANTES de tocar nada, y si encuentra un choque el downgrade
levanta con un mensaje claro y no cambia una sola fila. No hay forma
automática de resolverlo: hay que decidir a mano qué cierre de cada par
conservar (y, si corresponde, restaurar el backup previo al deploy).
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_reabrir_cierre_diario"
down_revision = "0010_recibido_en_ventas_pagos"
branch_labels = None
depends_on = None

_INDICE_VIEJO = "ux_cierres_diarios_sucursal_fecha"
_INDICE_NUEVO = "ux_cierres_diarios_sucursal_fecha_activo"


def _columnas_existentes(bind) -> set[str]:
    """Qué columnas tiene ya `cierres_diarios`. Mismo criterio que `0010`:
    `alembic upgrade head` corre sobre bases vivas que pueden llegar acá por
    caminos distintos (una base nueva ya las trae desde la `0009`, que llama
    a la versión viva de `crear_tablas()`), así que la revisión tiene que
    poder correr dos veces sin romper."""
    return {c["name"] for c in sa.inspect(bind).get_columns("cierres_diarios")}


def _indices_existentes(bind) -> set[str]:
    """Qué índices tiene ya `cierres_diarios`, por SQL directo y NO por
    `sa.inspect(...).get_indexes()`.

    🔴 **El inspector de SQLAlchemy no ve un índice por EXPRESIÓN en SQLite**
    (`COALESCE(sucursal_id, 0), fecha` es justo eso) — lo salta en silencio
    con un `SAWarning` y devuelve la lista sin él. Con eso, este `upgrade()`
    creía que `ux_cierres_diarios_sucursal_fecha_activo` no existía en una
    base que acababa de crearlo (vía la `0009`, que llama a la versión viva
    de `crear_tablas()`) y volvía a intentar el `CREATE UNIQUE INDEX` —
    `index ... already exists`. En PostgreSQL el inspector sí los ve; el
    camino de acá es el mismo para los dos motores para no depender de esa
    diferencia."""
    if bind.dialect.name == "sqlite":
        filas = bind.execute(sa.text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='cierres_diarios'"
        )).fetchall()
    else:
        filas = bind.execute(sa.text(
            "SELECT indexname AS name FROM pg_indexes WHERE tablename='cierres_diarios'"
        )).fetchall()
    return {fila[0] for fila in filas}


def upgrade():
    bind = op.get_bind()
    columnas = _columnas_existentes(bind)

    # `op.execute()` con SQL crudo y NO `op.add_column()`/`sa.ForeignKey`:
    # `anulado_por` lleva una FK, y agregar una columna CON una constraint
    # por `ALTER` no lo soporta el dialecto SQLite de Alembic sin "batch
    # mode" (copiar la tabla entera) — `NotImplementedError: No support for
    # ALTER of constraints in SQLite dialect`. El `REFERENCES ... ON DELETE
    # SET NULL` inline en el propio `ADD COLUMN` es sintaxis válida en los
    # DOS motores tal cual (mismo patrón que usa cada ALTER idempotente de
    # `db/schema.py`, ver p.ej. `facturas.usuario_id`), así que no hace
    # falta pasar por esa abstracción para nada.
    if "anulado_en" not in columnas:
        op.execute("ALTER TABLE cierres_diarios ADD COLUMN anulado_en TEXT")
    if "anulado_por" not in columnas:
        op.execute(
            "ALTER TABLE cierres_diarios ADD COLUMN anulado_por INTEGER "
            "REFERENCES usuarios(id) ON DELETE SET NULL"
        )
    if "motivo_anulacion" not in columnas:
        op.execute("ALTER TABLE cierres_diarios ADD COLUMN motivo_anulacion TEXT")

    indices = _indices_existentes(bind)
    if _INDICE_NUEVO not in indices:
        # Primero el nuevo (empieza a proteger las filas activas apenas
        # existe), recién después se dropea el viejo — nunca hay un instante
        # sin ningún UNIQUE sobre (sucursal, fecha).
        op.execute(
            f"CREATE UNIQUE INDEX {_INDICE_NUEVO} ON cierres_diarios "
            "(COALESCE(sucursal_id, 0), fecha) WHERE anulado_en IS NULL"
        )
    if _INDICE_VIEJO in _indices_existentes(bind):
        op.execute(f"DROP INDEX {_INDICE_VIEJO}")


def downgrade():
    bind = op.get_bind()

    # Fallar CERRADO: un UNIQUE liso (sin WHERE) rechaza dos filas con la
    # misma (sucursal, fecha) aunque una esté anulada. Si ese par existe,
    # ni se crea el índice viejo ni se toca una sola columna.
    chocan = bind.execute(sa.text(
        "SELECT COALESCE(sucursal_id, 0) AS suc, fecha, COUNT(*) AS n "
        "FROM cierres_diarios GROUP BY COALESCE(sucursal_id, 0), fecha "
        "HAVING COUNT(*) > 1"
    )).fetchall()
    if chocan:
        detalle = "; ".join(f"sucursal {r.suc}, {r.fecha} ({r.n} cierres)" for r in chocan)
        raise RuntimeError(
            "No se puede bajar esta revisión: hay más de un cierre diario "
            "(activo y/o anulado) para la misma sucursal y fecha, y el "
            "índice único anterior (sin `WHERE`) los rechazaría. Resolver a "
            "mano cuál cierre de cada par conservar antes de bajar, o "
            f"restaurar el backup previo al deploy. Choques: {detalle}."
        )

    indices = _indices_existentes(bind)
    if _INDICE_VIEJO not in indices:
        # Ídem upgrade: se crea el reemplazo ANTES de soltar el nuevo.
        op.execute(
            f"CREATE UNIQUE INDEX {_INDICE_VIEJO} ON cierres_diarios "
            "(COALESCE(sucursal_id, 0), fecha)"
        )
    if _INDICE_NUEVO in _indices_existentes(bind):
        op.execute(f"DROP INDEX {_INDICE_NUEVO}")

    # Mismo motivo que en `upgrade()`: SQL crudo, no `op.drop_column()` --
    # ver el comentario de arriba sobre el "batch mode" que pide SQLite para
    # tocar columnas con una constraint por el camino alto de Alembic.
    columnas = _columnas_existentes(bind)
    if "motivo_anulacion" in columnas:
        op.execute("ALTER TABLE cierres_diarios DROP COLUMN motivo_anulacion")
    if "anulado_por" in columnas:
        op.execute("ALTER TABLE cierres_diarios DROP COLUMN anulado_por")
    if "anulado_en" in columnas:
        op.execute("ALTER TABLE cierres_diarios DROP COLUMN anulado_en")
