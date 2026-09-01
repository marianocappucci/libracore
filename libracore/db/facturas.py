"""
Facturas electrónicas (facturas, notas de crédito, notas de débito):
numeración con retry ante colisión, alta/baja, búsqueda, filtros y
resolución de comprobantes asociados. Migrado a libracore.db (Fase 3 de
LibraCore, migración real, Tier 2 — código ya idéntico entre productos —
ver wiki/entities/libracore.md).
"""
import json
import sqlite3

from libracore.db.caja import sql_no_anulado, sql_no_es_cuenta_corriente
from libracore.db.core import get_connection


#: Los comprobantes que cuentan para los libros y los totales.
#:
#: 🔴 **Un comprobante emitido contra homologación NO es del cliente.** Trae CAE
#: y numeración del WSFE de homologación: si entra al libro IVA rompe la
#: correlatividad, y si entra a los totales infla la facturación del período con
#: plata que no existe.
#:
#: Es un fragmento y no ocho literales sueltos a propósito: repetir
#: `ambiente = 'produccion'` en cada consulta es de donde sale la que se olvida.
SOLO_FISCALES = "ambiente = 'produccion'"


def sql_solo_fiscales(alias: str = "") -> str:
    """El filtro, con el alias de la tabla si la consulta usa uno."""
    return f"{alias}.{SOLO_FISCALES}" if alias else SOLO_FISCALES


def get_next_factura_numero(punto_venta, tipo, ambiente: str = "produccion"):
    """El próximo número correlativo para tipo+punto_venta **en ese ambiente**.

    🔴 **El ambiente parte la secuencia, y es lo más peligroso de todo esto.**
    ARCA lleva numeraciones **independientes** en homologación y en producción.
    Sin separarlas acá, un comprobante de prueba numerado 500 —el que le tocaba
    en homologación— haría que el próximo real salga 501, cuando producción va
    por 84. La numeración local quedaría desalineada de la de ARCA y cada
    emisión posterior chocaría contra el "último autorizado" real.

    Es el defecto que **más caro sale** de los que abre poder probar desde una
    instancia viva: los totales mal se ven, un salto de numeración se descubre
    en la próxima presentación.

    El default `produccion` es el caso normal —quien no sabe de ambientes está
    facturando de verdad— y mantiene la firma vieja andando.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(numero) FROM facturas "
            "WHERE punto_venta=? AND tipo=? AND ambiente=?",
            (punto_venta, tipo, ambiente),
        ).fetchone()
        return (row[0] or 0) + 1


def create_factura(tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon,
                   cliente_iva_cond, items, subtotal, iva_amount, total,
                   concepto=1, cae="", cae_vto="", observaciones="", pdf_path="",
                   cliente_domicilio="", fch_serv_desde="", fch_serv_hasta="",
                   fch_vto_pago="", cbte_asoc_tipo=0, cbte_asoc_pv=0, cbte_asoc_nro=0,
                   condicion_venta="", usuario_id=None, *, ambiente: str):
    """Crea una nueva factura electrónica. `numero` es el número calculado por el
    caller (local o vía ARCA) pero puede haber quedado obsoleto si otra factura
    concurrente para el mismo tipo+punto_venta se creó en el medio (no había
    ningún UNIQUE ni retry — hallazgo cruzado desde la auditoría de Restolibra,
    "race condition en numeración"). Si el INSERT choca contra
    idx_facturas_numero_unico, se recalcula el número y se reintenta — el
    caller debe releer la factura por id (`get_factura`) para conocer el
    número real, nunca asumir que es el que pasó.

    🔴 **`ambiente` es obligatorio y va por nombre.** La columna tiene default
    `'produccion'` en la base —lo necesita el backfill de las filas viejas, ver
    la revisión `0006`— así que un `INSERT` que la omitiera declararía real un
    comprobante que puede no serlo, y entraría al libro IVA del cliente.

    Los dos defaults posibles mienten en direcciones opuestas y las dos duelen:
    marcar de producción un comprobante de prueba ensucia los libros; marcar de
    prueba uno real lo **saca** del libro IVA en silencio, que es peor. Por eso
    no hay default: acá el ambiente **se declara**, o no se escribe la fila.
    """
    ambiente = (ambiente or "").strip().lower()
    if ambiente not in ("homologacion", "produccion"):
        raise ValueError(
            f"ambiente inválido para un comprobante: {ambiente!r}. "
            "Tiene que ser 'homologacion' o 'produccion'."
        )
    MAX_INTENTOS = 5
    for intento in range(MAX_INTENTOS):
        try:
            with get_connection() as conn:
                cur = conn.execute(
                    """INSERT INTO facturas
                       (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon,
                        cliente_iva_cond, items, subtotal, iva_amount, total, concepto,
                        cae, cae_vto, observaciones, pdf_path, cliente_domicilio,
                        fch_serv_desde, fch_serv_hasta, fch_vto_pago,
                        cbte_asoc_tipo, cbte_asoc_pv, cbte_asoc_nro, condicion_venta, usuario_id,
                        ambiente)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon,
                     cliente_iva_cond, json.dumps(items, ensure_ascii=False), subtotal,
                     iva_amount, total, concepto, cae, cae_vto, observaciones, pdf_path,
                     cliente_domicilio, fch_serv_desde, fch_serv_hasta, fch_vto_pago,
                     cbte_asoc_tipo, cbte_asoc_pv, cbte_asoc_nro, condicion_venta, usuario_id,
                     ambiente),
                )
                return cur.lastrowid
        except sqlite3.IntegrityError:
            if intento == MAX_INTENTOS - 1:
                raise
            numero = get_next_factura_numero(punto_venta, tipo, ambiente)


_TIPOS_FACTURA = (1, 6, 11)
_TIPOS_NC      = (3, 8, 13)
_TIPOS_ND      = (2, 7, 12)

_VISTA_TIPOS = {
    "facturas": _TIPOS_FACTURA,
    "nc":       _TIPOS_NC,
    "nd":       _TIPOS_ND,
}


def get_all_facturas(limit=100, vista="facturas"):
    """Obtiene facturas, notas de crédito o notas de débito (últimas primero)."""
    tipos = _VISTA_TIPOS.get(vista, _TIPOS_FACTURA)
    placeholders = ",".join("?" * len(tipos))
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM facturas WHERE tipo IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            (*tipos, limit),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


def get_facturas_filtradas(desde="", hasta="", q="", vista="facturas", limit=50, offset=0):
    """Listado de facturas con filtros de fecha, búsqueda y paginación."""
    solo_sin_cobrar = (vista == "sin_cobrar")
    tipos = _VISTA_TIPOS.get("facturas" if solo_sin_cobrar else vista, _TIPOS_FACTURA)
    ph = ",".join("?" * len(tipos))
    conds = [f"f.tipo IN ({ph})"]
    params = list(tipos)
    if desde:
        conds.append("f.fecha >= ?"); params.append(desde)
    if hasta:
        conds.append("f.fecha <= ?"); params.append(hasta)
    if q:
        conds.append("(CAST(f.numero AS TEXT) LIKE ? OR f.cliente_razon LIKE ? OR f.observaciones LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    # 🔴 Los dos criterios juntos y en UNA variable: se usan en dos lugares de
    # esta función —la columna `total_cobrado` y el filtro `solo_sin_cobrar`— y
    # si divergieran, una factura podría listarse como impaga y a la vez mostrar
    # el total cobrado completo.
    _cc_excl = (f"AND {sql_no_es_cuenta_corriente('cm.medio_pago')}"
                f" AND {sql_no_anulado('cm')}")
    if solo_sin_cobrar:
        conds.append("f.cae != '' AND f.cae IS NOT NULL AND f.cae != 'PENDIENTE'")
        conds.append(f"""
            COALESCE((SELECT SUM(cm.monto) FROM caja_movimientos cm
                      WHERE cm.factura_id=f.id AND cm.tipo='ingreso' {_cc_excl}), 0) < f.total
        """)
    where = " AND ".join(conds)
    cobrada_col = f"""
        COALESCE((SELECT SUM(cm.monto) FROM caja_movimientos cm
                  WHERE cm.factura_id=f.id AND cm.tipo='ingreso' {_cc_excl}), 0) AS total_cobrado
    """
    with get_connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM facturas f WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT f.*, {cobrada_col} FROM facturas f WHERE {where} ORDER BY f.id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["items"] = json.loads(d["items"])
        result.append(d)
    return {"items": result, "total": total}


def get_factura(factura_id):
    """Obtiene una factura por ID."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM facturas WHERE id=?", (factura_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["items"] = json.loads(d["items"])
        return d


def update_factura_cae(factura_id, cae, cae_vto):
    """Actualiza CAE de una factura después de obtenerlo de ARCA."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE facturas SET cae=?, cae_vto=? WHERE id=?",
            (cae, cae_vto, factura_id)
        )


def update_factura_pdf_path(factura_id, pdf_path):
    """Actualiza el path del PDF de la factura."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE facturas SET pdf_path=? WHERE id=?",
            (pdf_path, factura_id)
        )


def search_facturas(query, vista="facturas"):
    """Busca facturas por número, cliente u observaciones."""
    tipos = _VISTA_TIPOS.get(vista, _TIPOS_FACTURA)
    placeholders = ",".join("?" * len(tipos))
    q = f"%{query}%"
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT * FROM facturas
               WHERE tipo IN ({placeholders})
                 AND (numero LIKE ? OR cliente_razon LIKE ? OR observaciones LIKE ?)
               ORDER BY id DESC""",
            (*tipos, q, q, q),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


def get_notas_de_factura(tipo, punto_venta, numero, tipos_nota):
    """Devuelve notas (NC o ND) que referencian un comprobante."""
    placeholders = ",".join("?" * len(tipos_nota))
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT * FROM facturas
               WHERE tipo IN ({placeholders})
                 AND cbte_asoc_tipo=? AND cbte_asoc_pv=? AND cbte_asoc_nro=?
               ORDER BY id DESC""",
            (*tipos_nota, tipo, punto_venta, numero),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


def get_nc_de_factura(tipo, punto_venta, numero):
    """Devuelve las notas de crédito que anulan un comprobante."""
    return get_notas_de_factura(tipo, punto_venta, numero, _TIPOS_NC)


def get_nd_de_factura(tipo, punto_venta, numero):
    """Devuelve las notas de débito asociadas a un comprobante."""
    return get_notas_de_factura(tipo, punto_venta, numero, _TIPOS_ND)


def get_factura_por_tipo_pv_nro(tipo, punto_venta, numero):
    """Busca un comprobante por tipo + punto de venta + número."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM facturas WHERE tipo=? AND punto_venta=? AND numero=?",
            (tipo, punto_venta, numero),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["items"] = json.loads(d["items"])
        return d


def delete_factura(factura_id):
    """Elimina una factura."""
    with get_connection() as conn:
        conn.execute("DELETE FROM facturas WHERE id=?", (factura_id,))
