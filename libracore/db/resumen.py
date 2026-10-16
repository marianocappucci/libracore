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
from libracore import tipos_comprobante as tipos
from libracore.db.caja import sql_no_anulado
from libracore.db.core import get_connection
from libracore.db.facturas import sql_con_pendiente, sql_pendiente_positivo, sql_vigente
from libracore.fechas import rango_por_dia

#: Los tipos que son factura. Las notas de credito y debito quedan afuera de
#  "facturado": restan o suman por otro lado y mezclarlas infla el numero.
TIPOS_FACTURA = tipos.FACTURAS


def get_resumen_core(desde: str, hasta: str) -> dict:
    """Facturacion y caja del periodo. Todo con COUNT/SUM, en una conexion."""
    ph = ",".join("?" * len(TIPOS_FACTURA))
    tipos = list(TIPOS_FACTURA)
    # Rango por día (ADR-037): una fila con hora del último día entra igual.
    c_fecha, p_fecha = rango_por_dia("fecha", desde, hasta)
    w_fecha = " AND ".join(c_fecha) or "1=1"

    with get_connection() as conn:
        facturado, comprobantes = conn.execute(
            f"SELECT COALESCE(SUM(total), 0), COUNT(*) FROM facturas "
            f"WHERE tipo IN ({ph}) AND {w_fecha} AND {sql_vigente()}",
            tipos + p_fecha,
        ).fetchone()

        cobrado, egresos = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN tipo='ingreso' THEN monto ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN tipo='egreso'  THEN monto ELSE 0 END), 0)
               FROM caja_movimientos
               WHERE """ + w_fecha + " AND " + sql_no_anulado(),
            p_fecha,
        ).fetchone()

        # El saldo es historico a proposito: es cuanta plata hay, no cuanta
        # entro en el periodo.
        saldo_caja = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN tipo='ingreso' THEN monto ELSE -monto END), 0) "
            f"FROM caja_movimientos WHERE {sql_no_anulado()}"
        ).fetchone()[0]

        # Sin cobrar: TODAS las que todavía deben algo, no las del periodo. Una
        # factura de marzo sin cobrar sigue siendo plata que falta en agosto.
        #
        # 🔴 Lo que falta sale de `sql_pendiente` (ADR-059): `total − cobrado − acreditado por
        # notas de crédito`, con la misma definición de «cobrado» que `get_cobros_factura`
        # (sin anulados y sin la marca de cuenta corriente: fiar no es cobrar). Antes era «no tiene
        # ningún cobro»: una factura cobrada a medias desaparecía de acá, una con un cobro ya anulado
        # no figuraba, y la plata se sumaba por su total y no por lo que falta. Dos definiciones de
        # «cobrada» en el mismo sistema es exactamente lo que hay que evitar.
        sin_cobrar_cant, sin_cobrar_monto = conn.execute(
            f"""SELECT COUNT(*), COALESCE(SUM({sql_pendiente_positivo('f')}), 0)
                FROM facturas f
                WHERE f.tipo IN ({ph}) AND {sql_vigente('f')} AND {sql_con_pendiente('f')}""",
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
