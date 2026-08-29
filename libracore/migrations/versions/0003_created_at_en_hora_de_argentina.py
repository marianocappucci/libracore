"""Los 29 `created_at`/`updated_at` del core dejan de estampar UTC.

El DEFAULT de esas columnas era `datetime('now')`, que en SQLite es UTC y que
el adaptador de PostgreSQL traduce a UTC **a propósito**, para que las dos bases
guarden el mismo texto (ver el comentario largo en `db/_postgres.py`). O sea que
las dos guardaban la hora equivocada, y de la misma manera. Pasa a ser
`datetime('now','-3 hours')` — el mismo offset fijo de `_ar_now()`, que es la
implementación de referencia de la familia para "ahora en Argentina".

Medido en la instancia `compulibra` de Contalibra el 2026-08-29: las 81 filas de
`facturas` y las 112 de `caja_movimientos` con la hora 3 h adelantada. **No sólo
las del cron nocturno de MercadoPago**, que fue por donde se lo vio: también las
escritas a mano, que pasaban por buenas porque una operación de las 12:56
guardada como `15:56` sigue pareciendo un horario de trabajo. El borde sí se ve:
lo creado entre las 21:00 y la medianoche quedaba fechado el día siguiente.

Es la mitad que faltaba del barrido de huso del 2026-08-23. Aquel arregló los
relojes de **los procesos** (`TZ` del contenedor, `date.today()`, la zona de la
sesión de PostgreSQL); éste arregla el reloj que estampa **la base**, que no lo
toca ninguna variable de entorno.

**Por qué esto es una revisión y no sólo una línea en `init_core_schema()`:** esa
función usa `CREATE TABLE IF NOT EXISTS`, así que sobre una base que ya existe no
cambia ningún DEFAULT — y ahí es donde están las filas que importan. La función
igual se corrigió, porque es la que define cómo nace una tabla; sin esta revisión
el arreglo sólo alcanzaría a las instancias nuevas.

🔴 **Sólo PostgreSQL, y es deliberado.** SQLite no tiene `ALTER COLUMN ... SET
DEFAULT`: cambiar un default obliga a reconstruir la tabla entera (el rodeo de
12 pasos que `batch_alter_table` automatiza), y serían 29 reconstrucciones sobre
bases vivas para un motor que la familia ya no usa — PostgreSQL es el único
motor desde el 2026-08-12, con LibraEdge como excepción, que no lleva este
schema. En SQLite el DEFAULT nuevo llega igual, pero por `init_core_schema()`:
al crear la tabla, no al migrarla.

⚠️ **Lo que esta revisión NO hace: tocar las filas ya escritas.** Quedan como
están, 3 h adelantadas, y a partir de acá hay una discontinuidad en cada
instancia — la misma que dejó el barrido del 2026-08-23 y por el mismo motivo.
Corregirlas es un trabajo aparte y con decisión humana: son datos de
comprobantes.
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_created_at_hora_ar"
down_revision = "0002_clients_libradesk"
branch_labels = None
depends_on = None


#: Las 29 columnas con reloj de `init_core_schema()`, como estaban al momento de
#: escribir esta revisión. Una tabla que nazca después ya viene con el DEFAULT
#: nuevo desde el DDL, así que no hace falta agregarla acá.
_COLUMNAS = (
    ("clients", "created_at"),
    ("remitos", "created_at"),
    ("presupuestos", "created_at"),
    ("facturas", "created_at"),
    ("cajas", "created_at"),
    ("caja_movimientos", "created_at"),
    ("mp_pagos", "created_at"),
    ("mp_movimientos", "created_at"),
    ("facturacion_alias", "created_at"),
    ("arca_config", "created_at"),
    ("arca_config", "updated_at"),
    ("usuarios", "created_at"),
    ("productos", "created_at"),
    ("depositos", "created_at"),
    ("proveedores", "created_at"),
    ("egresos", "created_at"),
    ("egresos_pagos", "created_at"),
    ("turnos_caja", "created_at"),
    ("movimientos_stock", "created_at"),
    ("ventas", "created_at"),
    ("ventas_pagos", "created_at"),
    ("cuentas_tesoreria", "created_at"),
    ("movimientos_tesoreria", "created_at"),
    ("listas_precio", "created_at"),
    ("cc_pagos", "created_at"),
    ("cc_debitos", "created_at"),
    ("cc_resumenes_enviados", "created_at"),
    ("recibos", "created_at"),
    ("comprobantes_pendientes", "created_at"),
)

#: El DEFAULT viejo, para el `downgrade()`.
_UTC = "datetime('now')"


def _expresion(sqlite: str) -> str:
    """La forma PostgreSQL de un `datetime(...)` de SQLite.

    Sale de la MISMA traducción que usa el adaptador en cada consulta, y no de
    una copia escrita a mano: el texto exacto importa —estas columnas son TEXT y
    hay código que las parsea con `strptime`, y los rangos de fecha se comparan
    lexicográficamente— así que si el formato cambiara, tiene que cambiar en los
    dos lados o en ninguno.
    """
    from libracore.db._postgres import _paramstyle

    return _paramstyle(f"SELECT {sqlite}").removeprefix("SELECT ")


def _aplicar(sqlite_expr: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Ver el docstring del módulo: en SQLite el DEFAULT nuevo llega por
        # `init_core_schema()`, no por acá.
        return

    inspector = sa.inspect(bind)
    tablas = set(inspector.get_table_names())
    expresion = _expresion(sqlite_expr)

    for tabla, columna in _COLUMNAS:
        # Idempotente y tolerante, igual que `0002`: `alembic upgrade` corre
        # sobre bases vivas que llegaron acá por caminos distintos, y no todos
        # los productos tienen las 29 tablas creadas.
        if tabla not in tablas:
            continue
        if columna not in {c["name"] for c in inspector.get_columns(tabla)}:
            continue
        op.execute(f'ALTER TABLE "{tabla}" ALTER COLUMN "{columna}" SET DEFAULT {expresion}')


def upgrade() -> None:
    from libracore.db.schema import AHORA_AR

    _aplicar(AHORA_AR)


def downgrade() -> None:
    _aplicar(_UTC)
