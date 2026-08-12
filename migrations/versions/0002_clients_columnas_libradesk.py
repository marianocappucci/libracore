"""Cuatro columnas a `clients`, para que LibraDesk pueda adoptar el módulo.

LibraDesk es el único producto de la familia que mantiene una tabla de
clientes propia (`clientes`, SQLAlchemy) en vez de usar `libracore.db.clients`
— Contalibra, Restolibra y VentaLibra ya la usan, y Gestiolibra y MedLibra
heredan la suya de LibraGenda. La decisión del 2026-08-12 fue unificar todo
acá, y para que LibraDesk pueda migrar sin perder datos el motor tiene que
absorber las cuatro columnas que su tabla tiene y ésta no:

- `empresa`, `ciudad`, `observaciones` — texto libre, opcional.
- `tipo_facturacion` — si el cliente se cobra por servicio prestado o por
  abono fijo.

**Por qué esto es una revisión y no una línea en `init_core_schema()`**: esa
función quedó de sólo lectura en la revisión `0001` (ver su docstring y
`tests/db/test_schema_congelado.py`). El primer intento de este cambio fue
justamente editarla, y el gate del schema congelado es lo que lo frenó.

**Efecto sobre los productos que ya usan el módulo**: ninguno. Las tres
columnas de texto entran vacías y `tipo_facturacion` entra con el default de
LibraDesk. Contalibra, Restolibra y VentaLibra no leen ninguna de las cuatro,
así que ningún comportamiento existente cambia — es el mismo criterio con el
que `productos.estacion` y `external_ref` viven en core sin que todos los
consumidores las usen.
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_clients_libradesk"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


#: Las cuatro, con su default. `server_default` es lo que rellena las filas que
#: ya existen: sin él, agregar `tipo_facturacion` como NOT NULL sobre una tabla
#: con clientes reales falla en las dos bases.
#:
#: Es una tupla de descripciones, no de `sa.Column`: un `Column` se consume al
#: agregarlo (queda ligado a la tabla) y no se puede reusar entre `upgrade()` y
#: `downgrade()` — `Column.copy()`, que sería la salida obvia, no existe en
#: SQLAlchemy 2.0.
_COLUMNAS = (
    ("empresa", dict(nullable=True, server_default="")),
    ("ciudad", dict(nullable=True, server_default="")),
    ("observaciones", dict(nullable=True, server_default="")),
    ("tipo_facturacion", dict(nullable=False, server_default="por_servicio")),
)


def _columnas_existentes(bind) -> set[str]:
    """Qué columnas tiene ya `clients`.

    La revisión tiene que ser idempotente igual que el resto del schema: hay
    instancias que van a llegar acá desde `init_core_schema()` y otras desde
    una base ya migrada a mano, y `alembic upgrade head` corre sobre bases
    vivas (ver el docstring de `0001`).
    """
    return {c["name"] for c in sa.inspect(bind).get_columns("clients")}


def upgrade():
    existentes = _columnas_existentes(op.get_bind())
    for nombre, kwargs in _COLUMNAS:
        if nombre not in existentes:
            op.add_column("clients", sa.Column(nombre, sa.Text(), **kwargs))


def downgrade():
    existentes = _columnas_existentes(op.get_bind())
    for nombre, _ in reversed(_COLUMNAS):
        if nombre in existentes:
            op.drop_column("clients", nombre)
