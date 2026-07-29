"""
Clientes: alta/baja/modificación, búsqueda por CUIT/email, facturas
asociadas. Migrado a libracore.db (Fase 3 de LibraCore, migración real,
Tier 2 — convergencia de comportamiento confirmada con el usuario:
`activar_cliente`, la validación de CUIT/DNI duplicado en `create_client`
y la normalización de email/CUIT en las búsquedas eran solo de Contalibra
y pasan a core — ver wiki/entities/libracore.md).
"""
import json

from libracore.db.core import get_connection

CC_RESUMEN_FRECUENCIAS = ("semanal", "quincenal", "mensual")


def create_client(name, address="", cuit_dni="", email="", phone="", iva_condition=""):
    if (cuit_dni or "").replace("-", "").strip():
        existing = get_client_by_cuit(cuit_dni)
        if existing:
            estado = "activo" if existing.get("activo") else "inactivo"
            sugerencia = "Reactivalo desde /clientes en vez de crear uno nuevo." if not existing.get("activo") \
                else "Editalo si necesitás cambiar sus datos."
            raise ValueError(
                f'Ya existe un cliente con el CUIT/DNI {cuit_dni}: "{existing["name"]}" ({estado}). {sugerencia}'
            )
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO clients (name, address, cuit_dni, email, phone, iva_condition) VALUES (?,?,?,?,?,?)",
            (name, address, cuit_dni, email, phone, iva_condition),
        )
        return cur.lastrowid


def resolver_cliente_externo(external_ref: str, name: str, cuit_dni: str = "",
                             email: str = "", phone: str = "") -> int:
    """Devuelve el `clients.id` del cliente identificado por `external_ref`,
    creándolo si es la primera vez que aparece.

    Es para un producto cuyos clientes viven en otra base (VentaLibra: son
    `parties` de LibraCommerce) y que igual necesita llevarles cuenta
    corriente acá, donde está la caja. Sin esto no habría forma de reencontrar
    al mismo deudor en su segunda compra fiada.

    No espeja la cartera de clientes: sólo entra el que efectivamente fía. El
    nombre se refresca en cada llamada porque el que vale en el resumen de
    cuenta es el actual, no el que tenía la primera vez.
    """
    if not external_ref:
        raise ValueError("external_ref no puede estar vacío")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE external_ref = ?", (external_ref,)
        ).fetchone()
        if row:
            conn.execute("UPDATE clients SET name = ? WHERE id = ?", (name, row["id"]))
            return row["id"]
        cur = conn.execute(
            """INSERT INTO clients (name, cuit_dni, email, phone, external_ref)
               VALUES (?,?,?,?,?)""",
            (name, cuit_dni or "", email or "", phone or "", external_ref),
        )
        return cur.lastrowid


def get_all_clients():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM clients WHERE activo = 1 ORDER BY name")]


def get_all_clients_including_inactive():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM clients ORDER BY name")]


def get_client(client_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        return dict(row) if row else None


def desactivar_cliente(client_id: int) -> bool:
    """Marca un cliente como inactivo (soft delete)."""
    with get_connection() as conn:
        conn.execute("UPDATE clients SET activo = 0 WHERE id = ?", (client_id,))
        return True


def activar_cliente(client_id: int) -> bool:
    """Reactiva un cliente previamente desactivado."""
    with get_connection() as conn:
        conn.execute("UPDATE clients SET activo = 1 WHERE id = ?", (client_id,))
        return True


def tiene_presupuestos_aprobados(client_id: int) -> bool:
    """Verifica si un cliente tiene presupuestos en estado 'aceptado'."""
    with get_connection() as conn:
        result = conn.execute(
            "SELECT COUNT(*) FROM presupuestos WHERE client_id = ? AND status = 'aceptado'",
            (client_id,)
        ).fetchone()
        return result[0] > 0 if result else False


def get_facturas_by_client(cuit_dni: str, name: str, limit: int = 100) -> list:
    """Facturas asociadas a un cliente, buscando por CUIT o razón social."""
    with get_connection() as conn:
        conds, params = [], []
        if cuit_dni:
            conds.append("cliente_cuit = ?")
            params.append(cuit_dni)
        if name:
            conds.append("cliente_razon = ?")
            params.append(name)
        if not conds:
            return []
        where = " OR ".join(conds)
        rows = conn.execute(
            f"SELECT * FROM facturas WHERE {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


def update_client(client_id, name=None, address=None, cuit_dni=None, email=None,
                  phone=None, iva_condition=None, auto_facturar=None,
                  cc_resumen_auto=None, cc_resumen_frecuencia=None):
    client = get_client(client_id)
    if not client:
        return
    if cc_resumen_frecuencia is not None and cc_resumen_frecuencia not in CC_RESUMEN_FRECUENCIAS:
        raise ValueError(
            f"Frecuencia de resumen inválida: {cc_resumen_frecuencia!r}. "
            f"Válidas: {', '.join(CC_RESUMEN_FRECUENCIAS)}."
        )
    with get_connection() as conn:
        conn.execute(
            """UPDATE clients SET name=?, address=?, cuit_dni=?, email=?, phone=?,
               iva_condition=?, auto_facturar=?, cc_resumen_auto=?,
               cc_resumen_frecuencia=? WHERE id=?""",
            (
                name          if name          is not None else client["name"],
                address       if address       is not None else client["address"],
                cuit_dni      if cuit_dni      is not None else client["cuit_dni"],
                email         if email         is not None else client["email"],
                phone         if phone         is not None else client["phone"],
                iva_condition if iva_condition is not None else client.get("iva_condition", ""),
                int(auto_facturar) if auto_facturar is not None else int(client.get("auto_facturar", 0)),
                int(cc_resumen_auto) if cc_resumen_auto is not None else int(client.get("cc_resumen_auto", 0)),
                cc_resumen_frecuencia if cc_resumen_frecuencia is not None
                else (client.get("cc_resumen_frecuencia") or "mensual"),
                client_id,
            ),
        )


def toggle_auto_facturar(client_id: int) -> bool:
    """Invierte el flag auto_facturar. Devuelve el nuevo valor."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE clients SET auto_facturar = 1 - auto_facturar WHERE id=?",
            (client_id,),
        )
        row = conn.execute("SELECT auto_facturar FROM clients WHERE id=?", (client_id,)).fetchone()
        return bool(row["auto_facturar"]) if row else False


def toggle_cc_resumen_auto(client_id: int) -> bool:
    """Invierte el flag de envío automático del resumen de cuenta corriente.
    Devuelve el nuevo valor. Mismo patrón que `toggle_auto_facturar`."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE clients SET cc_resumen_auto = 1 - cc_resumen_auto WHERE id=?",
            (client_id,),
        )
        row = conn.execute("SELECT cc_resumen_auto FROM clients WHERE id=?", (client_id,)).fetchone()
        return bool(row["cc_resumen_auto"]) if row else False


def get_clients_cc_resumen_auto() -> list[dict]:
    """Clientes activos con el envío automático de resumen habilitado."""
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM clients WHERE activo = 1 AND cc_resumen_auto = 1 ORDER BY name"
        )]


def delete_client(client_id):
    with get_connection() as conn:
        remito_count = conn.execute(
            "SELECT COUNT(*) FROM remitos WHERE client_id=?", (client_id,)
        ).fetchone()[0]
        presupuesto_count = conn.execute(
            "SELECT COUNT(*) FROM presupuestos WHERE client_id=?", (client_id,)
        ).fetchone()[0]
        total_count = remito_count + presupuesto_count
        if total_count > 0:
            msg_parts = []
            if remito_count > 0:
                msg_parts.append(f"{remito_count} remito(s)")
            if presupuesto_count > 0:
                msg_parts.append(f"{presupuesto_count} presupuesto(s)")
            raise ValueError(f"El cliente tiene {' y '.join(msg_parts)} asociado(s) y no puede eliminarse.")
        conn.execute("DELETE FROM clients WHERE id=?", (client_id,))


def get_client_by_email(email: str):
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE LOWER(email)=? ORDER BY activo DESC, id DESC LIMIT 1",
            (normalized,),
        ).fetchone()
        return dict(row) if row else None


def get_client_by_cuit(cuit: str):
    """Busca cliente por CUIT normalizando guiones (ej: 20317819162 == 20-31781916-2).
    Si hay más de un cliente con el mismo CUIT (duplicado), prioriza el activo
    y, entre iguales, el más reciente."""
    normalized = (cuit or "").replace("-", "").strip()
    if not normalized:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE REPLACE(cuit_dni, '-', '') = ? ORDER BY activo DESC, id DESC LIMIT 1",
            (normalized,),
        ).fetchone()
    return dict(row) if row else None
