"""
MercadoPago: pagos (QR/checkout), alias de facturación (excepciones
payer CUIT/email → cliente) y movimientos bancarios entrantes
(transferencias). Migrado a libracore.db (Fase 3 de LibraCore, migración
real, Tier 2 — el sistema de alias de facturación era solo de Contalibra
y pasa a core, confirmado con el usuario — ver wiki/entities/libracore.md).
"""
from libracore.db.core import get_connection
from libracore.db.clients import get_client, get_client_by_email, get_client_by_cuit


def get_mp_pago(mp_payment_id: str):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM mp_pagos WHERE mp_payment_id=?", (str(mp_payment_id),)
        ).fetchone()
        return dict(row) if row else None


def create_mp_pago(mp_payment_id: str, status: str, monto: float,
                   payer_email: str, payer_name: str, factura_id=None,
                   estado_factura: str = None, payment_type: str = None,
                   payment_method: str = None, descripcion_mp: str = None,
                   payer_id_type: str = None, payer_id_number: str = None):
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO mp_pagos
               (mp_payment_id, status, monto, payer_email, payer_name, factura_id,
                estado_factura, payment_type, payment_method, descripcion_mp,
                payer_id_type, payer_id_number)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(mp_payment_id), status, float(monto), payer_email, payer_name, factura_id,
             estado_factura, payment_type, payment_method, descripcion_mp,
             payer_id_type, payer_id_number),
        )
        return cur.lastrowid


def get_mp_pago_by_id(id: int):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM mp_pagos WHERE id=?", (id,)).fetchone()
        return dict(row) if row else None


def get_mp_pagos_by_estado(estado: str):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM mp_pagos WHERE estado_factura=? ORDER BY created_at DESC",
            (estado,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_mp_pagos_historial(limit: int = 50):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM mp_pagos
               WHERE estado_factura IN ('facturado', 'ignorado')
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_mp_pago_estado(id: int, estado: str, factura_id=None):
    with get_connection() as conn:
        if factura_id is not None:
            conn.execute(
                "UPDATE mp_pagos SET estado_factura=?, factura_id=? WHERE id=?",
                (estado, factura_id, id),
            )
        else:
            conn.execute(
                "UPDATE mp_pagos SET estado_factura=? WHERE id=?",
                (estado, id),
            )


def _normalizar_alias(tipo: str, valor: str) -> str:
    valor = (valor or "").strip()
    if tipo == "cuit":
        return valor.replace("-", "")
    return valor.lower()


def crear_alias_facturacion(tipo: str, valor: str, cliente_id: int) -> int:
    """Registra que los pagos de MP identificados por este CUIT o email deben
    facturarse al cliente indicado, en vez de al cliente que coincide directo
    con esos datos (o de crear uno nuevo)."""
    if tipo not in ("cuit", "email"):
        raise ValueError("Tipo de alias inválido.")
    valor_norm = _normalizar_alias(tipo, valor)
    if not valor_norm:
        raise ValueError("El valor del alias no puede estar vacío.")
    if not get_client(cliente_id):
        raise ValueError("El cliente destino no existe.")
    with get_connection() as conn:
        existente = conn.execute(
            "SELECT fa.id, c.name FROM facturacion_alias fa JOIN clients c ON c.id = fa.cliente_id "
            "WHERE fa.tipo=? AND fa.valor=? AND fa.activo=1",
            (tipo, valor_norm),
        ).fetchone()
        if existente:
            raise ValueError(
                f'Ese {tipo.upper()} ya está asignado a "{existente["name"]}". '
                "Eliminá ese alias primero si querés reasignarlo."
            )
        cur = conn.execute(
            "INSERT INTO facturacion_alias (tipo, valor, cliente_id) VALUES (?,?,?)",
            (tipo, valor_norm, cliente_id),
        )
        return cur.lastrowid


def get_alias_facturacion_by_cliente(cliente_id: int) -> list[dict]:
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM facturacion_alias WHERE cliente_id=? AND activo=1 ORDER BY created_at",
            (cliente_id,),
        ).fetchall()]


def eliminar_alias_facturacion(alias_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM facturacion_alias WHERE id=?", (alias_id,))


def get_cliente_por_alias_pago(payer_email: str = "", payer_cuit: str = ""):
    """Si el CUIT o el email del pagador tienen un alias de facturación
    configurado, devuelve el cliente destino de ese alias."""
    cuit_norm  = _normalizar_alias("cuit", payer_cuit)
    email_norm = _normalizar_alias("email", payer_email)
    with get_connection() as conn:
        row = None
        if cuit_norm:
            row = conn.execute(
                "SELECT cliente_id FROM facturacion_alias WHERE tipo='cuit' AND valor=? AND activo=1",
                (cuit_norm,),
            ).fetchone()
        if not row and email_norm:
            row = conn.execute(
                "SELECT cliente_id FROM facturacion_alias WHERE tipo='email' AND valor=? AND activo=1",
                (email_norm,),
            ).fetchone()
    return get_client(row["cliente_id"]) if row else None


def resolver_cliente_pago(payer_email: str = "", payer_cuit: str = ""):
    """Resuelve a qué cliente corresponde facturar un pago de MP: primero
    respeta un alias explícito (excepción configurada), y si no hay,
    matchea al cliente cuyo email o CUIT coincide con el del pagador."""
    alias = get_cliente_por_alias_pago(payer_email, payer_cuit)
    if alias:
        return alias
    client = get_client_by_email(payer_email) if payer_email else None
    if not client and payer_cuit:
        client = get_client_by_cuit(payer_cuit)
    return client


def get_mp_movimiento_by_mp_id(mp_movement_id: str):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM mp_movimientos WHERE mp_movement_id=?", (str(mp_movement_id),)
        ).fetchone()
        return dict(row) if row else None


def create_mp_movimiento(mp_movement_id: str, tipo: str, monto: float, fecha: str,
                         descripcion: str = "", origen_nombre: str = "",
                         origen_banco: str = "", origen_cbu: str = "",
                         payer_email: str = "", payer_name: str = "",
                         payer_id_type: str = "", payer_id_number: str = "",
                         estado_factura: str = "pendiente"):
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO mp_movimientos
               (mp_movement_id, tipo, monto, fecha, descripcion, origen_nombre, origen_banco,
                origen_cbu, payer_email, payer_name, payer_id_type, payer_id_number, estado_factura)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(mp_movement_id), tipo, float(monto), fecha, descripcion,
             origen_nombre, origen_banco, origen_cbu,
             payer_email, payer_name, payer_id_type, payer_id_number, estado_factura),
        )
        return cur.lastrowid


def get_mp_movimiento_by_id(id: int):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM mp_movimientos WHERE id=?", (id,)).fetchone()
        return dict(row) if row else None


def get_mp_movimientos_by_estado(estado: str):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM mp_movimientos WHERE estado_factura=? ORDER BY fecha DESC, created_at DESC",
            (estado,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_mp_movimientos_historial(limit: int = 50):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM mp_movimientos
               WHERE estado_factura IN ('facturado', 'ignorado')
               ORDER BY fecha DESC, created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_mp_movimiento_datos(id: int, payer_email: str = None, payer_name: str = None,
                               payer_id_type: str = None, payer_id_number: str = None):
    fields = {}
    if payer_email is not None:
        fields["payer_email"] = payer_email
    if payer_name is not None:
        fields["payer_name"] = payer_name
    if payer_id_type is not None:
        fields["payer_id_type"] = payer_id_type
    if payer_id_number is not None:
        fields["payer_id_number"] = payer_id_number
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE mp_movimientos SET {set_clause} WHERE id=?",
            (*fields.values(), id),
        )


def update_mp_movimiento_estado(id: int, estado: str, factura_id=None):
    with get_connection() as conn:
        if factura_id is not None:
            conn.execute(
                "UPDATE mp_movimientos SET estado_factura=?, factura_id=? WHERE id=?",
                (estado, factura_id, id),
            )
        else:
            conn.execute(
                "UPDATE mp_movimientos SET estado_factura=? WHERE id=?",
                (estado, id),
            )


def get_mp_pending_count() -> int:
    with get_connection() as conn:
        return conn.execute(
            """SELECT
               (SELECT COUNT(*) FROM mp_pagos WHERE estado_factura='pendiente') +
               (SELECT COUNT(*) FROM mp_movimientos WHERE estado_factura='pendiente')"""
        ).fetchone()[0]


def vincular_mp_pago_cliente(mp_pago_id: int, payer_email: str, payer_name: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE mp_pagos SET payer_email=?, payer_name=? WHERE id=?",
            (payer_email, payer_name, mp_pago_id),
        )
