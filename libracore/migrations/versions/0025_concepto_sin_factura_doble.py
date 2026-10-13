"""El concepto de la factura a cuenta corriente deja de decir «Factura» dos veces (ADR-047).

La emisión a crédito escribía en `caja_movimientos.concepto` `"Factura Factura C 0001-00000001 — Juan"`:
la etiqueta del tipo (`tipos_comprobante.NOMBRE`) ya trae la palabra y el texto se la antepone otra
vez. Venía de las copias de Contalibra y Restolibra y se preservó tal cual al extraer el módulo; la
v1.161.0 lo arregla de ahí en adelante (`facturas_router`). Esta revisión corrige **las filas que ya
quedaron escritas**, para que el extracto de un cliente no muestre los dos formatos mezclados.

Dos tablas llevan ese texto, y las dos se corrigen:

- `caja_movimientos.concepto`: donde lo escribe la emisión.
- `cc_asientos.concepto`: el libro de clientes (ADR-029) **copia** el concepto del movimiento de caja
  al asentar (`libro_de_clientes._esperado`) y `sincronizar` no lo vuelve a leer mientras el importe no
  cambie, así que el asiento no se entera de que la caja cambió.

Se tocan **sólo** las filas cuyo concepto empieza exactamente con `Factura Factura ` (distingue
mayúsculas) y se les quita el primer `Factura `: `substr(concepto, 9)`, porque `Factura ` tiene 8
caracteres y `substr` cuenta desde 1 en PostgreSQL y en SQLite. No se mira `LIKE` (en SQLite no
distingue mayúsculas): se compara `substr(concepto, 1, 16)` con el prefijo. No se toca nada más: ni
montos, ni vínculos, ni fechas, ni conceptos que contienen la frase en otro lugar («Cobro Factura C …»,
«Reversión: Factura Factura C …»).

Idempotente: una fila corregida ya no empieza con el prefijo, así que correrla dos veces no cambia
nada la segunda. Tolerante: una base sin alguna de las dos tablas (un producto que arma a mano las
tablas del motor que usa) se saltea esa tabla. Escribe a stderr cuántas filas tocó por tabla.

No se baja: restaurar el backup. Volver a anteponer `Factura ` a lo que hoy empieza con «Factura »
tocaría también las filas nuevas, que están bien, y no hay forma de distinguirlas de las corregidas.
"""
import sys

import sqlalchemy as sa
from alembic import op

# ≤ 32 caracteres: es el ancho de `alembic_version.version_num` en PostgreSQL (VARCHAR(32)).
revision = "0025_concepto_sin_factura_doble"
down_revision = "0024_retirar_cc_resumen"
branch_labels = None
depends_on = None

_PREFIJO = "Factura Factura "
_TABLAS = ("caja_movimientos", "cc_asientos")


def _avisar(texto):
    # A stderr y no al logger: `env.py` no configura el logging, y sin eso el `INFO` de alembic no
    # sale con `alembic upgrade` ni con `libracore-migrar`. Cuántas filas se tocaron es lo que quien
    # despliega quiere ver.
    print(texto, file=sys.stderr)


def upgrade():
    bind = op.get_bind()
    existentes = set(sa.inspect(bind).get_table_names())
    for tabla in _TABLAS:
        if tabla not in existentes:
            _avisar(f"0025: {tabla} no existe en esta base: se saltea.")
            continue
        # `substr(x, 9)` es «desde el carácter 9»: se queda sin el primer «Factura » (8 caracteres).
        resultado = bind.execute(
            sa.text(
                f"UPDATE {tabla} SET concepto = substr(concepto, {len('Factura ') + 1}) "
                "WHERE substr(concepto, 1, :largo) = :prefijo"
            ),
            {"largo": len(_PREFIJO), "prefijo": _PREFIJO},
        )
        _avisar(f"0025: {tabla}: {resultado.rowcount} fila(s) con el concepto corregido.")


def downgrade():
    # Bajar tocaría las filas nuevas, que ya salen bien, y no hay cómo distinguirlas de las
    # corregidas. No se baja: restaurar el backup.
    raise NotImplementedError(
        "No se baja: los conceptos corregidos no se pueden distinguir de los que ya salían bien. "
        "Para volver atrás, restaurar el backup."
    )
