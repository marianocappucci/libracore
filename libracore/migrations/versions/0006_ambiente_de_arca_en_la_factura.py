"""Contra qué ambiente de ARCA se emitió cada comprobante.

Un comprobante emitido contra homologación trae CAE y numeración del WSFE de
homologación. Sin esta columna es **indistinguible** de uno real: cae en la
misma tabla, entra al libro IVA y rompe la correlatividad de los libros del
cliente.

Es la pieza que permite que una instancia de producción pruebe con el cliente
antes del corte a facturación real — el pendiente que el humano planteó el
2026-08-30. El segundo par de credenciales viene después; sin esto primero,
probar contra homologación desde una instancia viva ensucia los libros.

## 🔑 El backfill NO es un default: sale de `arca_config`

`ALTER TABLE ... ADD COLUMN NOT NULL` exige un default, y `produccion` es el
menos malo —marcar de prueba un comprobante real lo saca del libro IVA en
silencio, y un libro al que le faltan comprobantes es un problema fiscal—. Pero
ese default sería **falso** en toda instancia que hoy factura contra
homologación: las demos.

Así que después del `ALTER` se corrige mirando el ambiente que la instancia
tiene configurado. Es un proxy, no un dato: si alguien cambió el selector
alguna vez, los comprobantes anteriores al cambio quedan etiquetados con el
ambiente de hoy. **Es la mejor información disponible** —la fila no guarda con
qué ambiente se emitió, que es justamente lo que esta migración viene a
arreglar— y quedar sin corregir sería peor: dejaría 100% de las demos marcadas
como producción.

Llama a `init_core_schema()` en vez de re-expresar el `ALTER`, como el resto de
la cadena: la fuente de verdad es el DDL del motor.
"""
from alembic import op

from libracore.db.migraciones import conexion_libracore
from libracore.db.schema import init_core_schema

revision = "0006_ambiente_arca_factura"
down_revision = "0005_estado_acreditacion_pagos"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    init_core_schema(conexion_libracore(conn))

    # ── El backfill, desde el ambiente configurado de la instancia ──────────
    #
    # 🔴 Se toca **sólo** lo que quedó en el default. Si una corrida anterior ya
    # etiquetó filas, no se pisan: la migración tiene que poder correr dos veces
    # sin cambiar nada la segunda.
    #
    # `arca_config` puede no existir todavía (una instancia que nunca configuró
    # ARCA) o estar vacía. En los dos casos no hay nada que corregir y el
    # default queda: sin ARCA configurado tampoco hay comprobantes emitidos.
    try:
        fila = conn.exec_driver_sql(
            "SELECT ambiente FROM arca_config WHERE activo = 1 "
            "ORDER BY id LIMIT 1"
        ).fetchone()
    except Exception:
        fila = None

    ambiente = (fila[0] if fila else "") or ""
    if ambiente.strip().lower() == "homologacion":
        conn.exec_driver_sql(
            "UPDATE facturas SET ambiente = 'homologacion' "
            "WHERE ambiente = 'produccion'"
        )


def downgrade():
    # Bajar sería borrar la única marca de qué comprobante es real. Los de
    # prueba volverían a ser indistinguibles de los reales y entrarían al libro
    # IVA del cliente: exactamente el defecto que esta migración cierra, y
    # encima sobre los libros.
    raise NotImplementedError(
        "No se baja: borrar el ambiente haría entrar al libro IVA los "
        "comprobantes de prueba, que es el defecto que esta migración cierra. "
        "Para volver atrás, restaurar el backup previo al deploy."
    )
