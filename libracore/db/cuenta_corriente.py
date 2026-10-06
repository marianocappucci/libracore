"""
Cuenta corriente por cliente: saldo y movimientos, leídos del libro `cc_asientos`
(`libro_de_clientes`, ADR-026/028/029), que lleva ventas a cuenta corriente, facturas
cobradas a cuenta corriente, débitos directos (`cc_debitos`) y pagos manuales
(`cc_pagos`). Extraído de database.py de Contalibra/Restolibra (idéntico en ambos)
como parte de la migración real a libracore.db (Fase 3 de LibraCore, ver
wiki/entities/libracore.md).

## El libro es la única lectura

`get_cc_saldo`, `get_cc_movimientos`, `get_cc_movimientos_periodo` y
`get_clientes_con_saldo_cc` leen SIEMPRE del libro (ADR-029). Hasta v1.138.0 se
calculaban desde los documentos y la instancia podía encender
`LIBRACORE_CC_DESDE_EL_LIBRO`; el interruptor, el cálculo y `libro_de_clientes.comparar`
se retiraron cuando el libro dio cero diferencias en todas las instancias.

El criterio de qué cuenta sigue siendo el de siempre — **débitos por venta + débitos
por factura + débitos directos − abonos** — pero ahora lo aplica `sincronizar`, hecho
por hecho, cuando los escritores del motor (`create_cc_pago`, `create_cc_debito`,
`add_venta_pago`, los movimientos de caja con factura) cambian algo. Lo que se carga
por fuera de los escritores (SQL propio) no se ve hasta que se corre
`libro_de_clientes.reconstruir()`, que es lo que hace un deploy.

Una base sin la tabla `cc_asientos` no tiene de dónde leer: las cuatro lecturas tiran
`RuntimeError` y dicen qué hacer (migración `0020` / `init_core_schema`).
`get_facturas_pendientes_cc` no cambia: sigue por factura (ADR-018).

## De dónde salen las ventas

Los productos migrados a LibraCommerce tienen las ventas en `sales` (con
`customer_party_id`/`occurred_on`/`number`) y los que todavía no, en `ventas` (con
`cliente_id`/`fecha`/`numero`). Eso se declara con un `OrigenVentas` en vez de duplicar
las funciones. Ya no decide el saldo —el saldo es el del libro—: el libro lo usa para
asentar las ventas fiadas y para completar el número de venta de cada movimiento. Las
lecturas siguen aceptando `origen` para no cambiarle la firma a ningún producto.

Hasta el 2026-07-28 esa duplicación era literal: Contalibra y Restolibra
tenían cada uno una copia byte-a-byte de este módulo (`db_cuenta_corriente.py`)
que sólo cambiaba el `JOIN`. Tres copias del mismo algoritmo de dinero, que
había que corregir tres veces.

## Cuando las ventas ni siquiera están en esta base

`OrigenVentas` alcanza mientras las ventas vivan en la misma base que la
caja. VentaLibra las tiene en un archivo SQLite separado (el de
LibraCommerce; acá sólo viven caja y facturas), así que ningún `JOIN` las
alcanza. Para ese caso está `cc_debitos`: el producto registra el débito
explícitamente al confirmar la venta fiada, con la misma forma que un
`cc_pago` pero del otro signo. La tabla queda vacía en los productos que no
la usan, así que suma cero y su saldo no cambia.

## Cuando el id del party no es el id del cliente

`VENTAS_LIBRACOMMERCE` asume `clients.id == parties.id` — invariante real en
Contalibra y Restolibra porque `clients._espejar_party` crea el party con el
MISMO id al dar de alta. En VentaLibra ese invariante no existe: sus
clientes se dan de alta como party primero (autoincremento propio, y los
proveedores también son parties) y el cliente de LibraCore se enlaza
después por `clients.external_ref = 'party-<party_id>'` (ver
`clients.resolver_cliente_externo`). Cruzar por `customer_party_id ==
clients.id` ahí le pondría la deuda a otra persona, en silencio — medido en
`ventalibra-dev`: 0 de 3 coinciden en nombre.

Para ese caso está `VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF`: en vez de una
tabla real, `tabla` es una subconsulta que resuelve el cliente por
`external_ref` antes de que el resto del módulo la trate como si fuera
`sales`. Decisión del humano, 2026-09-14.
"""
import contextlib
from dataclasses import dataclass

from libracore import tipos_comprobante as tipos
from libracore.db.caja import sql_no_anulado, sql_no_es_cuenta_corriente
from libracore.db.core import get_connection
from libracore.db.facturas import sql_vigente

from .core import Conexion

_TIPO_LABEL = {
    1: "FACTURA A", 6: "FACTURA B", 11: "FACTURA C",
    2: "ND A", 3: "NC A", 7: "ND B", 8: "NC B", 12: "ND C", 13: "NC C",
    201: "FCE A", 206: "FCE B", 211: "FCE C",
    202: "ND FCE A", 203: "NC FCE A", 207: "ND FCE B", 208: "NC FCE B",
    212: "ND FCE C", 213: "NC FCE C",
}


@dataclass(frozen=True)
class OrigenVentas:
    """En qué tabla de esta base están las ventas y cómo se llaman sus
    columnas. Los nombres se interpolan en el SQL, así que sólo pueden salir
    de las constantes de abajo — nunca de entrada de usuario."""

    tabla: str
    columna_cliente: str
    columna_fecha: str
    columna_numero: str


#: Productos que todavía no migraron sus ventas a LibraCommerce.
VENTAS_LIBRACORE = OrigenVentas("ventas", "cliente_id", "fecha", "numero")
#: Productos con las ventas ya en `sales`, en esta misma base (Contalibra
#: desde P7, Restolibra desde P8).
VENTAS_LIBRACOMMERCE = OrigenVentas("sales", "customer_party_id", "occurred_on", "number")
#: Igual que `VENTAS_LIBRACOMMERCE`, pero para cuando `sales.customer_party_id`
#: NO coincide con `clients.id` (VentaLibra — ver la sección "Cuando el id del
#: party no es el id del cliente" arriba). `tabla` es una subconsulta: resuelve
#: el `clients.id` de cada venta por `external_ref` y expone las columnas con
#: los mismos nombres que usa el libro (`libro_de_clientes`) para asentar las
#: ventas fiadas y completar sus movimientos, así que no necesita saber que no
#: está leyendo `sales` directamente. Una venta cuyo party no tiene cliente
#: enlazado queda afuera del INNER JOIN — no rompe, y no le suma a nadie.
VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF = OrigenVentas(
    "(SELECT s.id, s.occurred_on, s.number, c.id AS cliente_id "
    "FROM sales s "
    "JOIN clients c ON c.external_ref = 'party-' || CAST(s.customer_party_id AS TEXT))",
    "cliente_id", "occurred_on", "number",
)


def _cuit_de(conn, cliente_id: int) -> str:
    """El CUIT del cliente, **normalizado sin guiones**.

    🔴 Los guiones no son cosméticos acá: `clients.cuit_dni` y
    `facturas.cliente_cuit` conviven con y sin guiones en la misma base —el
    alta de cliente no fuerza un formato, y la factura arrastra el que tenía
    el cliente al momento de emitirse. Comparar los dos campos tal cual
    (`f.cliente_cuit = c.cuit_dni`) sólo matchea cuando casualmente coinciden
    en formato; para el resto, la deuda por facturas queda invisible y el
    saldo da a favor en vez de deudor. Mismo criterio que
    `clients.get_client_by_cuit` — ver el incidente de Municipalidad de
    Suipacha en contalibra, wiki/entities/contalibra.md (2026-09-03).
    """
    row = conn.execute("SELECT cuit_dni FROM clients WHERE id=?", (cliente_id,)).fetchone()
    cuit = (row["cuit_dni"] if row else "") or ""
    return cuit.replace("-", "").strip()


def get_cc_saldo(cliente_id: int, origen: OrigenVentas = VENTAS_LIBRACORE) -> float:
    """El saldo del cliente, del libro `cc_asientos` (ADR-029).

    `origen` ya no decide el saldo: se acepta para no cambiarle la firma a los productos.
    Sin la tabla del libro tira `RuntimeError`.
    """
    from libracore.db import libro_de_clientes

    return libro_de_clientes.saldo_de(cliente_id)


def get_cc_movimientos(cliente_id: int, origen: OrigenVentas = VENTAS_LIBRACORE) -> list[dict]:
    """Los movimientos del cliente, por fecha, del libro `cc_asientos` (ADR-029).

    `origen` no decide qué movimientos hay (eso lo dice el libro): sólo dónde buscar
    el número de cada venta para completar el concepto. Sin la tabla del libro tira
    `RuntimeError`.
    """
    from libracore.db import libro_de_clientes

    return libro_de_clientes.movimientos_de(cliente_id, origen)


def get_facturas_pendientes_cc(cliente_id: int) -> list[dict]:
    """Las facturas a cuenta corriente del cliente que todavía no tienen cobro.

    Es lo que separa un **pago a cuenta** de un **cobro de factura**. Los dos
    bajan el saldo (`cc_pagos`), pero sólo el segundo escribe un movimiento de
    caja ligado a la factura, y de ese movimiento sale el "Cobrada" de la lista
    de comprobantes. Un pago a cuenta suelto deja la factura en "Sin cobrar"
    para siempre, aunque el saldo del cliente ya la descuente. Incidente real:
    Municipalidad de Suipacha, FC 74 y 75, 2026-09-14.

    Excluye las notas, las facturas sin CAE y las anuladas por una nota de
    crédito (esas ya se cancelaron por `cc_pagos` sin ningún cobro, así que
    aparecerían pendientes para siempre). Se cruza por CUIT normalizado, por
    la misma razón que `_cuit_de`. Orden: la más vieja primero, que es el orden
    en que se aplica un pago.
    """
    with get_connection() as conn:
        cuit = _cuit_de(conn, cliente_id)
        if not cuit:
            return []
        rows = conn.execute(f"""
            SELECT f.id, f.tipo, f.punto_venta, f.numero, f.fecha, f.total,
                   COALESCE((
                       SELECT SUM(cm.monto) FROM caja_movimientos cm
                       WHERE cm.factura_id = f.id AND cm.tipo = 'ingreso'
                         AND {sql_no_es_cuenta_corriente('cm.medio_pago')}
                         AND {sql_no_anulado('cm')}
                   ), 0) AS cobrado
            FROM facturas f
            WHERE REPLACE(f.cliente_cuit, '-', '') = ?
              AND f.tipo IN ({tipos.en_sql(tipos.FACTURAS)})
              AND f.condicion_venta = 'Cuenta Corriente'
              AND COALESCE(f.cae, '') NOT IN ('', 'PENDIENTE')
              AND {sql_vigente('f')}
              AND NOT EXISTS (
                  SELECT 1 FROM facturas n
                  WHERE n.tipo IN ({tipos.en_sql(tipos.NC)}) AND n.cbte_asoc_tipo = f.tipo
                    AND n.cbte_asoc_pv = f.punto_venta AND n.cbte_asoc_nro = f.numero
                    -- Del mismo emisor: con dos razones sociales, las dos pueden
                    -- tener la Factura A 0001-00000005 (ADR-021).
                    AND COALESCE(n.emisor_id, 0) = COALESCE(f.emisor_id, 0)
                    -- Una nota anulada (sin CAE) no cancela nada (ADR-022).
                    AND {sql_vigente('n')}
              )
            ORDER BY f.fecha, f.id
        """, (cuit,)).fetchall()
    resultado = []
    for r in rows:
        pendiente = round(float(r["total"]) - float(r["cobrado"]), 2)
        if pendiente <= 0:
            continue
        d = dict(r)
        d["concepto"] = (
            f"{_TIPO_LABEL.get(r['tipo'], 'COMP')} "
            f"{str(r['punto_venta']).zfill(4)}-{str(r['numero']).zfill(8)}"
        )
        d["pendiente"] = pendiente
        resultado.append(d)
    return resultado


def get_cc_movimientos_periodo(
    cliente_id: int, desde: str, hasta: str, origen: OrigenVentas = VENTAS_LIBRACORE
) -> dict:
    """Movimientos de un rango de fechas más el saldo con el que se entra al rango.

    `desde`/`hasta` son fechas ISO (YYYY-MM-DD) inclusive. Se resuelve sobre
    `get_cc_movimientos()` en vez de repetir las tres consultas: la cuenta
    corriente es chica por cliente y así no hay riesgo de que el resumen que se
    manda por mail se calcule distinto que la pantalla. Por eso también sale del
    libro (ADR-029): no tiene lectura propia.
    """
    movs = get_cc_movimientos(cliente_id, origen)

    def _signo(m):
        return float(m["monto"]) if m["tipo"] == "debito" else -float(m["monto"])

    anteriores = [m for m in movs if m["fecha"] and m["fecha"] < desde]
    del_periodo = [m for m in movs if desde <= (m["fecha"] or "") <= hasta]
    # Un movimiento sin fecha no se puede ubicar en el tiempo: entra en el
    # saldo anterior para que el saldo final siga cerrando con get_cc_saldo().
    sin_fecha = [m for m in movs if not m["fecha"]]

    saldo_anterior = sum(_signo(m) for m in anteriores + sin_fecha)
    total_debitos = sum(float(m["monto"]) for m in del_periodo if m["tipo"] == "debito")
    total_creditos = sum(float(m["monto"]) for m in del_periodo if m["tipo"] == "credito")

    return {
        "desde": desde,
        "hasta": hasta,
        "saldo_anterior": saldo_anterior,
        "movimientos": del_periodo,
        "total_debitos": total_debitos,
        "total_creditos": total_creditos,
        "saldo_final": saldo_anterior + total_debitos - total_creditos,
    }


def registrar_resumen_enviado(cliente_id: int, fecha: str, desde: str, hasta: str,
                              saldo: float, email: str, estado: str = "ok",
                              detalle: str = "", automatico: bool = True) -> int:
    """Deja rastro de cada intento de envío (ok o error) y, si salió bien,
    adelanta `cc_resumen_ultimo_envio` del cliente — que es lo que evita que
    una segunda corrida del cron el mismo día reenvíe el mismo resumen."""
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO cc_resumenes_enviados
               (cliente_id, fecha, periodo_desde, periodo_hasta, saldo, email,
                estado, detalle, automatico)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (cliente_id, fecha, desde, hasta, float(saldo), email or "",
             estado, detalle or "", 1 if automatico else 0),
        )
        if estado == "ok":
            conn.execute(
                "UPDATE clients SET cc_resumen_ultimo_envio=? WHERE id=?",
                (fecha, cliente_id),
            )
        return cur.lastrowid


def get_resumenes_enviados(cliente_id: int | None = None, limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        if cliente_id is None:
            rows = conn.execute(
                """SELECT r.*, c.name AS cliente_nombre
                   FROM cc_resumenes_enviados r
                   LEFT JOIN clients c ON c.id = r.cliente_id
                   ORDER BY r.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT r.*, c.name AS cliente_nombre
                   FROM cc_resumenes_enviados r
                   LEFT JOIN clients c ON c.id = r.cliente_id
                   WHERE r.cliente_id = ? ORDER BY r.id DESC LIMIT ?""",
                (cliente_id, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def get_clientes_con_saldo_cc(origen: OrigenVentas = VENTAS_LIBRACORE) -> list[dict]:
    """Los clientes con movimientos y su saldo, del libro `cc_asientos` (ADR-029).

    `origen` ya no decide nada: se acepta para no cambiarle la firma a los productos.
    Sin la tabla del libro tira `RuntimeError`.
    """
    from libracore.db import libro_de_clientes

    return libro_de_clientes.clientes_con_saldo()


def create_cc_pago(cliente_id: int, monto: float, fecha: str, concepto: str,
                   referencia: str, medio_pago: str, caja_id, usuario_id,
                   conn: Conexion | None = None) -> int:
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        cur = c.execute(
            """INSERT INTO cc_pagos
               (cliente_id, monto, fecha, concepto, referencia, medio_pago, caja_id, usuario_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (cliente_id, float(monto), fecha, concepto, referencia, medio_pago, caja_id, usuario_id),
        )
        _al_libro(c, f"cc_pago:{cur.lastrowid}")
        return cur.lastrowid


def get_cc_pago(pago_id: int) -> dict | None:
    """Un pago a cuenta, con el nombre del cliente resuelto.

    Existe para `libracore.recibos`: el recibo de cobranza se arma desde el
    pago, y necesita el cliente además del monto."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT p.*, c.name AS cliente_nombre, c.cuit_dni AS cliente_cuit, "
            "       c.address AS cliente_domicilio "
            "FROM cc_pagos p LEFT JOIN clients c ON c.id = p.cliente_id "
            "WHERE p.id = ?",
            (pago_id,),
        ).fetchone()
        return dict(row) if row else None


def delete_cc_pago(pago_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM cc_pagos WHERE id=?", (pago_id,))
        _al_libro(conn, f"cc_pago:{pago_id}")


def create_cc_debito(cliente_id: int, monto: float, fecha: str, concepto: str = "",
                     referencia: str = "", usuario_id=None,
                     conn: Conexion | None = None) -> int:
    """Registra deuda que no nace de una venta de ESTA base.

    Idempotente por `referencia` cuando se pasa una: el producto la arma con
    el id de su venta (`sale-12`), así que un reintento del cobro no fía dos
    veces lo mismo. Devuelve el id existente si ya estaba registrada — mismo
    criterio que `create_caja_movimiento`.
    """
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        if referencia:
            row = c.execute(
                "SELECT id FROM cc_debitos WHERE referencia = ?", (referencia,)
            ).fetchone()
            if row:
                return row["id"]
        cur = c.execute(
            """INSERT INTO cc_debitos
               (cliente_id, monto, fecha, concepto, referencia, usuario_id)
               VALUES (?,?,?,?,?,?)""",
            (cliente_id, float(monto), fecha, concepto, referencia, usuario_id),
        )
        _al_libro(c, f"cc_debito:{cur.lastrowid}")
        return cur.lastrowid


def delete_cc_debito(debito_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM cc_debitos WHERE id=?", (debito_id,))
        _al_libro(conn, f"cc_debito:{debito_id}")


def _al_libro(conn, origen: str) -> None:
    """Lleva el hecho al libro de clientes, en la misma transacción (opción B, etapa B1).

    Import tardío: `libro_de_clientes` importa este módulo.
    """
    from libracore.db import libro_de_clientes

    libro_de_clientes.sincronizar(origen, conn=conn)
