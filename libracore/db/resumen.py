"""Los numeros de una instancia, agregados, para el panel del cliente.

Es el **nucleo**: lo unico que el panel puede pedirle a cualquier producto de la
familia sin preguntar antes. Sale de las tablas de este motor —`facturas` y
`caja_movimientos`— y por lo tanto lo tienen los seis.

Lo demas es **por motor, no por producto**: ventas y stock salen de
LibraCommerce (4 productos), turnos de LibraGenda (3). Cada producto arma su
respuesta con el nucleo mas los bloques que puede contestar.

🔴 **Conteos, no muestras.** `dashboard.get_dashboard_data` devuelve
`facturas_sin_cobrar` con `LIMIT 8`: alcanza para pintar una tarjeta, pero
consolidando cinco sucursales sumar esas muestras daria "40 sin cobrar" cuando
el numero real puede ser cualquiera — seria el tope, no el dato.

Ver wiki/analyses/panel-del-dueno-multisucursal.md.
"""
from libracore.db.caja import sql_no_es_cuenta_corriente
from libracore.db.core import get_connection

#: Los tipos que son factura. Las notas de credito y debito quedan afuera de
#  "facturado": restan o suman por otro lado y mezclarlas infla el numero.
TIPOS_FACTURA = (1, 6, 11)


def get_resumen_core(desde: str, hasta: str) -> dict:
    """Facturacion y caja del periodo. Todo con COUNT/SUM, en una conexion."""
    ph = ",".join("?" * len(TIPOS_FACTURA))
    tipos = list(TIPOS_FACTURA)

    with get_connection() as conn:
        facturado, comprobantes = conn.execute(
            f"SELECT COALESCE(SUM(total), 0), COUNT(*) FROM facturas "
            f"WHERE tipo IN ({ph}) AND fecha BETWEEN ? AND ?",
            tipos + [desde, hasta],
        ).fetchone()

        cobrado, egresos = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN tipo='ingreso' THEN monto ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN tipo='egreso'  THEN monto ELSE 0 END), 0)
               FROM caja_movimientos WHERE fecha BETWEEN ? AND ?""",
            (desde, hasta),
        ).fetchone()

        # El saldo es historico a proposito: es cuanta plata hay, no cuanta
        # entro en el periodo.
        saldo_caja = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN tipo='ingreso' THEN monto ELSE -monto END), 0) "
            "FROM caja_movimientos"
        ).fetchone()[0]

        # Sin cobrar: TODAS las que quedan impagas, no las del periodo. Una
        # factura de marzo sin cobrar sigue siendo plata que falta en agosto.
        #
        # 🔴 La condicion de "cobrada" sale de `sql_no_es_cuenta_corriente()` y
        # no se reescribe: mirar solo `factura_id IS NULL` cuenta como cobrada
        # una factura pagada a cuenta corriente —plata que NO entro— y ademas
        # discrepa con `get_cobros_factura`, que es lo que muestra la pantalla
        # de comprobantes. Dos definiciones de "cobrada" en el mismo sistema es
        # exactamente lo que hay que evitar.
        sin_cobrar_cant, sin_cobrar_monto = conn.execute(
            f"""SELECT COUNT(*), COALESCE(SUM(f.total), 0)
                FROM facturas f
                LEFT JOIN caja_movimientos c
                       ON c.factura_id = f.id
                      AND c.tipo = 'ingreso'
                      AND {sql_no_es_cuenta_corriente('c.medio_pago')}
                WHERE f.tipo IN ({ph}) AND c.id IS NULL""",
            tipos,
        ).fetchone()

    return {
        "facturado": float(facturado),
        "cobrado": float(cobrado),
        "egresos": float(egresos),
        "saldo_caja": float(saldo_caja),
        "comprobantes": int(comprobantes),
        "sin_cobrar": {
            "cantidad": int(sin_cobrar_cant),
            "monto": float(sin_cobrar_monto),
        },
    }
