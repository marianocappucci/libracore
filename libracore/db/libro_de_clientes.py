"""La cuenta corriente de clientes, llevada también como libro (opción B, etapa B1).

El saldo de un cliente se sigue **leyendo calculado** (`cuenta_corriente.get_cc_saldo`,
desde ventas fiadas, facturas cobradas a cuenta, débitos y pagos). Este módulo lo
lleva además en el libro de terceros (`cc_asientos`, ADR-026), con rol `cliente` y
`tercero_id = clients.id`, para poder comparar los dos y, cuando den igual en todas
las instancias, pasar las lecturas al libro
(`wiki/analyses/libro-de-terceros-para-la-familia-diseno.md`).

**Un hecho, un origen.** Cada asiento dice de qué hecho viene, como `tabla:id`:

| Origen | Cuenta cuando | Columna |
|---|---|---|
| `cc_pago:<id>` | el pago existe | haber |
| `cc_debito:<id>` | el débito existe | debe |
| `caja_mov:<id>` | es un ingreso a cuenta corriente, con factura, no anulado, y el CUIT de la factura es de un cliente | debe |
| `venta_pago:<id>` | es un pago con medio `cuenta_corriente` de una venta de un cliente (según el `OrigenVentas` del producto) | debe |

Es el mismo criterio que `get_cc_saldo`, hecho por hecho.

**`sincronizar(origen)` es lo único que escribe.** Mira si el hecho cuenta hoy y
qué asiento vigente tiene, y deja el libro de acuerdo:
- si cuenta y no tiene asiento, lo asienta;
- si dejó de contar (se borró, se anuló), lo revierte con `contraasentar`, con la
  fecha del original: el cálculo lo saca entero, y el libro tiene que dar lo mismo
  en cualquier período;
- si cambió el importe, revierte y vuelve a asentar.

Es idempotente: los escritores del motor la llaman después de cada cambio, en su
misma transacción (`conn=`), y `reconstruir()` la corre sobre todos los hechos.

🔑 **El cliente de un hecho se fija la primera vez que se asienta** (decisión del
humano, 2026-10-06). Si después le cambian el CUIT a un cliente, el cálculo le pasa
hacia atrás la deuda de las facturas de ese CUIT; el libro no la mueve. Para
corregir un CUIT mal cargado se hace un asiento de traspaso explícito.
`comparar()` muestra esas diferencias.

Un CUIT de factura que es de dos clientes a la vez no se asienta a ninguno: el
cálculo le suma la deuda a los dos, y elegir uno sería inventar.
"""
from libracore.db import libro_de_terceros as libro
from libracore.db.caja import MEDIOS_CUENTA_CORRIENTE, sql_es_cuenta_corriente, sql_no_anulado
from libracore.db.core import Conexion, get_connection
from libracore.db.cuenta_corriente import VENTAS_LIBRACORE, OrigenVentas, get_cc_saldo
from libracore.db.facturas import _con

ROL = "cliente"

_origen_de_ventas: OrigenVentas = VENTAS_LIBRACORE


def registrar_origen_de_ventas(origen: OrigenVentas) -> None:
    """Dónde están las ventas de este producto, para asentar las fiadas.

    `build_cuenta_corriente_router` lo registra con el `origen` que recibe. Un
    producto que no monta ese router y fía ventas desde `ventas_pagos` lo llama al
    arrancar. Sin registro, `VENTAS_LIBRACORE`, el mismo default que el cálculo.
    """
    global _origen_de_ventas
    _origen_de_ventas = origen


def origen_de_ventas() -> OrigenVentas:
    return _origen_de_ventas


def es_cuenta_corriente(medio: str | None) -> bool:
    return (medio or "").strip().lower() in MEDIOS_CUENTA_CORRIENTE


def _tabla_de_ventas(origen: OrigenVentas) -> str:
    """La tabla real detrás del origen (el de `external_ref` es una subconsulta de `sales`)."""
    return "sales" if origen.tabla.lstrip().startswith("(") else origen.tabla


def _existe_tabla(c, tabla: str) -> bool:
    try:
        return bool(c.execute(f"PRAGMA table_info({tabla})").fetchall())
    except Exception:  # noqa: BLE001 — una tabla que no existe no es un error acá
        return False


def _cuit(valor) -> str:
    return (valor or "").replace("-", "").strip()


def _clientes_del_cuit(c, cuit: str) -> list[int]:
    if not cuit:
        return []
    filas = c.execute(
        "SELECT id FROM clients WHERE TRIM(REPLACE(COALESCE(cuit_dni, ''), '-', '')) = ? ORDER BY id",
        (cuit,),
    ).fetchall()
    return [f[0] for f in filas]


def _fecha(valor) -> str:
    return str(valor or "")[:10]


def _hecho(tercero_id, importe, fecha, concepto) -> dict | None:
    """Lo que el hecho le hace a la cuenta: `importe` > 0 al debe, < 0 al haber."""
    importe = round(float(importe or 0), 2)
    if importe == 0:
        return None
    return {"tercero_id": tercero_id, "importe": importe, "fecha": _fecha(fecha),
            "concepto": (concepto or "").strip()}


def _esperado(c, origen: str, ventas: OrigenVentas) -> dict | None:
    """Qué asiento le corresponde hoy al hecho, o `None` si no mueve ninguna cuenta.

    `tercero_id` puede venir en `None`: el hecho cuenta, pero hoy no se sabe de qué
    cliente es (un CUIT sin cliente, o de dos).
    """
    tipo, _, id_texto = origen.partition(":")
    id_ = int(id_texto)
    if tipo == "cc_pago":
        f = c.execute("SELECT cliente_id, monto, fecha, concepto FROM cc_pagos WHERE id = ?",
                      (id_,)).fetchone()
        return f and _hecho(f[0], -float(f[1] or 0), f[2], f[3] or "Pago a cuenta")
    if tipo == "cc_debito":
        f = c.execute("SELECT cliente_id, monto, fecha, concepto FROM cc_debitos WHERE id = ?",
                      (id_,)).fetchone()
        return f and _hecho(f[0], f[1], f[2], f[3] or "Débito")
    if tipo == "caja_mov":
        f = c.execute(
            "SELECT cm.monto, cm.fecha, cm.concepto, f.cliente_cuit "
            "FROM caja_movimientos cm JOIN facturas f ON f.id = cm.factura_id "
            f"WHERE cm.id = ? AND cm.tipo = 'ingreso' AND {sql_es_cuenta_corriente('cm.medio_pago')} "
            f"AND {sql_no_anulado('cm')}",
            (id_,),
        ).fetchone()
        if not f:
            return None
        clientes = _clientes_del_cuit(c, (f[3] or "").replace("-", ""))
        return _hecho(clientes[0] if len(clientes) == 1 else None, f[0], f[1],
                      f[2] or "Factura a cuenta corriente")
    if tipo == "venta_pago":
        if not _existe_tabla(c, _tabla_de_ventas(ventas)):
            return None
        f = c.execute(
            f"SELECT vp.monto, v.{ventas.columna_fecha}, v.{ventas.columna_numero}, cl.id "
            f"FROM ventas_pagos vp JOIN {ventas.tabla} v ON v.id = vp.venta_id "
            f"JOIN clients cl ON cl.id = v.{ventas.columna_cliente} "
            "WHERE vp.id = ? AND vp.medio = 'cuenta_corriente'",
            (id_,),
        ).fetchone()
        return f and _hecho(f[3], f[0], f[1], f"Venta {f[2]}" if f[2] else "Venta a cuenta corriente")
    raise ValueError(f"Origen desconocido: {origen!r}")


def _vigente(c, origen: str) -> dict | None:
    """El asiento del hecho que hoy cuenta: el original sin reversión."""
    fila = c.execute(
        "SELECT * FROM cc_asientos a WHERE a.origen = ? AND a.contrapartida_de IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM cc_asientos r WHERE r.contrapartida_de = a.id) "
        "ORDER BY a.id DESC",
        (origen,),
    ).fetchone()
    return dict(fila) if fila else None


def _importe(asiento: dict) -> float:
    return round(float(asiento["debe"] or 0) - float(asiento["haber"] or 0), 2)


def sincronizar(origen: str, *, ventas: OrigenVentas | None = None,
                conn: Conexion | None = None) -> None:
    """Deja el libro de acuerdo con el hecho `origen` (`cc_pago:12`). Idempotente."""
    ventas = ventas or _origen_de_ventas
    with _con(conn) as c:
        esperado = _esperado(c, origen, ventas)
        vigente = _vigente(c, origen)
        if vigente is not None:
            if esperado is not None and _importe(vigente) == esperado["importe"]:
                return
            libro.contraasentar(vigente["id"], conn=c)
            if esperado is None:
                return
            # Cambió el importe: se vuelve a asentar, al mismo cliente.
            esperado["tercero_id"] = vigente["tercero_id"]
        if esperado is None or esperado["tercero_id"] is None:
            return
        importe = esperado["importe"]
        libro.asentar(esperado["tercero_id"], ROL, esperado["fecha"], esperado["concepto"],
                      debe=max(importe, 0), haber=max(-importe, 0), origen=origen, conn=c)


def al_libro_venta_pago(conn, venta_pago_id: int, medio: str | None) -> None:
    """Lleva al libro un pago de venta recién insertado, si es fiado. Lo llaman el
    motor (`ventas.add_venta_pago`), LibraCommerce y quien inserte en `ventas_pagos`
    con SQL propio, con su conexión."""
    if (medio or "") == "cuenta_corriente":
        sincronizar(f"venta_pago:{venta_pago_id}", conn=conn)


def sincronizar_cliente(cliente_id: int, *, ventas: OrigenVentas | None = None,
                        conn: Conexion | None = None) -> None:
    """Asienta lo que hoy resuelve a este cliente y no tenía cliente.

    Lo llaman el alta y la edición de un cliente: una factura emitida antes de que
    su CUIT tuviera cliente, o una venta de un party que recién ahora se enlaza
    (VentaLibra), empiezan a contar en el cálculo. Lo ya asentado a otro cliente no
    se mueve (ver el docstring del módulo).
    """
    ventas = ventas or _origen_de_ventas
    with _con(conn) as c:
        if not _existe_tabla(c, "cc_asientos"):
            return  # una base sin el schema del motor (sólo `clients`): no hay libro
        origenes = []
        fila = c.execute("SELECT cuit_dni FROM clients WHERE id = ?", (cliente_id,)).fetchone()
        cuit = _cuit(fila[0]) if fila else ""
        if cuit:
            origenes += [f"caja_mov:{f[0]}" for f in c.execute(
                "SELECT cm.id FROM caja_movimientos cm JOIN facturas f ON f.id = cm.factura_id "
                f"WHERE REPLACE(f.cliente_cuit, '-', '') = ? AND cm.tipo = 'ingreso' "
                f"AND {sql_es_cuenta_corriente('cm.medio_pago')} AND {sql_no_anulado('cm')}",
                (cuit,),
            ).fetchall()]
        if _existe_tabla(c, _tabla_de_ventas(ventas)):
            origenes += [f"venta_pago:{f[0]}" for f in c.execute(
                f"SELECT vp.id FROM ventas_pagos vp JOIN {ventas.tabla} v ON v.id = vp.venta_id "
                f"WHERE v.{ventas.columna_cliente} = ? AND vp.medio = 'cuenta_corriente'",
                (cliente_id,),
            ).fetchall()]
        for origen in origenes:
            if _vigente(c, origen) is None:
                sincronizar(origen, ventas=ventas, conn=c)


def _todos_los_origenes(c, ventas: OrigenVentas) -> list[str]:
    origenes = [f"cc_pago:{f[0]}" for f in c.execute("SELECT id FROM cc_pagos ORDER BY id").fetchall()]
    origenes += [f"cc_debito:{f[0]}" for f in c.execute("SELECT id FROM cc_debitos ORDER BY id").fetchall()]
    origenes += [f"caja_mov:{f[0]}" for f in c.execute(
        "SELECT id FROM caja_movimientos WHERE factura_id IS NOT NULL AND tipo = 'ingreso' "
        f"AND {sql_es_cuenta_corriente()} AND {sql_no_anulado()} ORDER BY id").fetchall()]
    origenes += [f"venta_pago:{f[0]}" for f in c.execute(
        "SELECT id FROM ventas_pagos WHERE medio = 'cuenta_corriente' ORDER BY id").fetchall()]
    # Lo que ya está en el libro y dejó de contar sin pasar por el motor (un pago
    # borrado, un movimiento anulado con SQL propio): sincronizarlo lo revierte.
    vistos = set(origenes)
    origenes += [f[0] for f in c.execute(
        "SELECT DISTINCT origen FROM cc_asientos WHERE origen IS NOT NULL AND rol = ? ORDER BY origen",
        (ROL,)).fetchall() if f[0] not in vistos]
    return origenes


def reconstruir(ventas: OrigenVentas | None = None, *, conn: Conexion | None = None) -> int:
    """Sincroniza todos los hechos y devuelve cuántos asientos escribió. Se puede repetir:
    la segunda vez no escribe nada."""
    ventas = ventas or _origen_de_ventas
    with _con(conn) as c:
        antes = c.execute("SELECT COUNT(*) FROM cc_asientos").fetchone()[0]
        for origen in _todos_los_origenes(c, ventas):
            sincronizar(origen, ventas=ventas, conn=c)
        return c.execute("SELECT COUNT(*) FROM cc_asientos").fetchone()[0] - antes


def saldos_del_libro() -> dict[int, float]:
    """El saldo de cada cliente en el libro. Sólo los asientos de este módulo (con
    origen): LibraCargo lleva en la misma tabla su propia cuenta de clientes."""
    with get_connection() as c:
        filas = c.execute(
            "SELECT tercero_id, COALESCE(SUM(debe), 0) - COALESCE(SUM(haber), 0) FROM cc_asientos "
            "WHERE rol = ? AND origen IS NOT NULL GROUP BY tercero_id",
            (ROL,)).fetchall()
    return {f[0]: round(float(f[1] or 0), 2) for f in filas}


def comparar(ventas: OrigenVentas | None = None) -> list[dict]:
    """Los clientes cuyo saldo en el libro no es el calculado:
    `[{cliente_id, libro, calculado, diferencia}]`. Vacío es lo que se busca."""
    ventas = ventas or _origen_de_ventas
    del_libro = saldos_del_libro()
    with get_connection() as c:
        ids = {f[0] for f in c.execute("SELECT id FROM clients").fetchall()}
    diferencias = []
    for cliente_id in sorted(ids | set(del_libro)):
        calculado = round(get_cc_saldo(cliente_id, ventas), 2) if cliente_id in ids else 0.0
        en_libro = del_libro.get(cliente_id, 0.0)
        if abs(en_libro - calculado) >= 0.005:
            diferencias.append({"cliente_id": cliente_id, "libro": en_libro, "calculado": calculado,
                                "diferencia": round(en_libro - calculado, 2)})
    return diferencias
