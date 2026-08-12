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


def _hay_parties(conn) -> bool:
    """`parties` es una tabla de LibraCommerce, no de LibraCore.

    La tienen Contalibra, Restolibra y VentaLibra; Gestiolibra, MedLibra y
    LibraDesk no. Por eso todo lo que la toca es condicional: LibraCore no
    puede asumir que el producto que lo usa tiene LibraCommerce al lado.
    """
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='parties'"
    ).fetchone() is not None


def _espejar_party(conn, client_id: int, name, cuit_dni, email, phone, activo=1):
    """Crea el `parties` espejo de un cliente, con el MISMO id.

    La identidad de ids no es casual: la migración P7
    (`libracommerce/scripts/migrate_from_contalibra.py`) estableció que un
    cliente es el party de igual id, y los proveedores van con offset
    100.000. `sales.customer_party_id` tiene FK a `parties`, mientras que
    los clientes de estos productos siguen viviendo en `clients` — sin este
    espejo, vender a un cliente creado después de P7 falla con FOREIGN KEY
    constraint, que el router traduce a un 409 "conflicto con otra venta
    simultánea" e invita a reintentar algo que nunca va a andar.

    Encontrado el 2026-07-30 por la suite nueva de Contalibra: en la base
    del cliente real los 31 clientes tenían su party porque los creó P7, y
    el alta nunca lo replicó — el bug esperaba al cliente número 32.
    """
    if not _hay_parties(conn):
        return
    conn.execute(
        """INSERT OR IGNORE INTO parties
               (id, party_type, display_name, legal_name, tax_id, tax_id_type,
                email, phone, active)
           VALUES (?, 'person', ?, NULL, ?, NULL, ?, ?, ?)""",
        (client_id, name, cuit_dni or None, email or None, phone or None, int(activo)),
    )


def sincronizar_parties_de_clientes() -> int:
    """Backfill: crea los `parties` que falten para clientes ya existentes.

    Idempotente y barato (un INSERT ... SELECT sobre los que no tienen
    espejo). Lo llama el `init_db()` de cada producto con LibraCommerce,
    DESPUÉS de `init_schema` de LibraCommerce — antes de eso la tabla
    `parties` todavía no existe. Devuelve cuántos creó.
    """
    with get_connection() as conn:
        if not _hay_parties(conn):
            return 0
        cur = conn.execute("""
            INSERT INTO parties
                (id, party_type, display_name, legal_name, tax_id, tax_id_type,
                 email, phone, active)
            SELECT c.id, 'person', c.name, NULL, NULLIF(c.cuit_dni, ''), NULL,
                   NULLIF(c.email, ''), NULLIF(c.phone, ''), COALESCE(c.activo, 1)
            FROM clients c
            LEFT JOIN parties p ON p.id = c.id
            WHERE p.id IS NULL
        """)
        return cur.rowcount


def validar_cuit_no_duplicado(cuit_dni, excluir_id: int | None = None) -> None:
    """Levanta `ValueError` si ese CUIT/DNI ya lo tiene otro cliente.

    Está separada de `create_client` para que la pueda usar **un producto que
    escribe la tabla por su propia capa** y no por este módulo. El caso vivo es
    [[libradesk]]: comparte la tabla `clients` desde su revisión `0017`, pero
    sus escrituras van por SQLAlchemy a propósito, porque el log de actividad
    de `libraauth` cuelga de los eventos de `flush` de la sesión — escribir por
    la conexión cruda de acá lo dejaría **sin auditar y sin que nadie se
    entere**.

    `excluir_id` es para la edición: un cliente no choca consigo mismo.

    > ⚠️ La normalización es la de `get_client_by_cuit`: saca guiones y nada
    > más. Un CUIT tipeado con puntos o espacios **no** matchea. El adaptador
    > de SOS de LibraDesk se quedaba con los dígitos, que es más fuerte;
    > unificar hacia allá cambiaría a qué fila matchean Contalibra, Restolibra
    > y VentaLibra, así que es una decisión aparte y no se hace de pasada.
    """
    if not (cuit_dni or "").replace("-", "").strip():
        return
    existing = get_client_by_cuit(cuit_dni)
    if not existing or existing["id"] == excluir_id:
        return
    estado = "activo" if existing.get("activo") else "inactivo"
    sugerencia = "Reactivalo desde /clientes en vez de crear uno nuevo." if not existing.get("activo") \
        else "Editalo si necesitás cambiar sus datos."
    raise ValueError(
        f'Ya existe un cliente con el CUIT/DNI {cuit_dni}: "{existing["name"]}" ({estado}). {sugerencia}'
    )


#: Las cuatro columnas que agregó la revisión `0002` de Alembic para que
#: LibraDesk pudiera adoptar el módulo.
_COLUMNAS_0002 = ("empresa", "ciudad", "observaciones", "tipo_facturacion")


def _columnas_de_clients(conn) -> set[str]:
    """Qué columnas tiene HOY la tabla, en esta instancia.

    🔴 **Hace falta porque el schema del motor y el de una instancia viva no son
    lo mismo.** Las cuatro de la revisión `0002` no están en
    `init_core_schema()` —esa función quedó congelada en la `0001`— así que sólo
    las tiene una base que haya corrido la cadena de Alembic del motor. Y hoy
    **nadie la corre**: no hay ningún punto de entrada que la ejecute.

    Medido el 2026-08-12 sobre las ocho bases PostgreSQL del VPS: **siete no
    tienen ninguna de las cuatro**. Escribirlas sin preguntar rompía el alta de
    clientes con `table clients has no column named empresa` — en Contalibra y
    Restolibra lo agarró su CI; en los otros no, porque ahí la tabla de los
    tests nace del `CREATE TABLE` y no de la base real.

    Cuando la revisión se aplique, las columnas van a estar y se escriben solas.
    Esto no compite con Alembic: lo hace tolerable mientras tanto.
    """
    return {r[1] for r in conn.execute("PRAGMA table_info(clients)").fetchall()}


def create_client(name, address="", cuit_dni="", email="", phone="", iva_condition="",
                  empresa="", ciudad="", observaciones="", tipo_facturacion="por_servicio"):
    validar_cuit_no_duplicado(cuit_dni)
    with get_connection() as conn:
        columnas = ["name", "address", "cuit_dni", "email", "phone", "iva_condition"]
        valores = [name, address, cuit_dni, email, phone, iva_condition]
        nuevas = {
            "empresa": empresa or "",
            "ciudad": ciudad or "",
            "observaciones": observaciones or "",
            "tipo_facturacion": tipo_facturacion or "por_servicio",
        }
        presentes = _columnas_de_clients(conn)
        for col in _COLUMNAS_0002:
            if col in presentes:
                columnas.append(col)
                valores.append(nuevas[col])
        cur = conn.execute(
            f"INSERT INTO clients ({', '.join(columnas)})"
            f" VALUES ({', '.join(['?'] * len(columnas))})",
            tuple(valores),
        )
        client_id = cur.lastrowid
        # En la MISMA transacción que el alta: un cliente sin su party es
        # un cliente al que no se le puede vender.
        _espejar_party(conn, client_id, name, cuit_dni, email, phone)
        return client_id


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

    **A propósito NO crea el party espejo** que sí crea `create_client`: acá
    la dirección es la inversa (el cliente YA es un party, y esta fila de
    `clients` es su reflejo para la cuenta corriente). Crear un party con
    este `clients.id` inventaría una entidad nueva y podría pisar el id de
    un party ajeno.
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
        if _hay_parties(conn):
            conn.execute("UPDATE parties SET active = 0 WHERE id = ?", (client_id,))
        return True


def activar_cliente(client_id: int) -> bool:
    """Reactiva un cliente previamente desactivado."""
    with get_connection() as conn:
        conn.execute("UPDATE clients SET activo = 1 WHERE id = ?", (client_id,))
        if _hay_parties(conn):
            conn.execute("UPDATE parties SET active = 1 WHERE id = ?", (client_id,))
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
                  cc_resumen_auto=None, cc_resumen_frecuencia=None,
                  empresa=None, ciudad=None, observaciones=None,
                  tipo_facturacion=None):
    client = get_client(client_id)
    if not client:
        return
    if cc_resumen_frecuencia is not None and cc_resumen_frecuencia not in CC_RESUMEN_FRECUENCIAS:
        raise ValueError(
            f"Frecuencia de resumen inválida: {cc_resumen_frecuencia!r}. "
            f"Válidas: {', '.join(CC_RESUMEN_FRECUENCIAS)}."
        )
    nuevo_name     = name          if name          is not None else client["name"]
    nuevo_cuit     = cuit_dni      if cuit_dni      is not None else client["cuit_dni"]
    nuevo_email    = email         if email         is not None else client["email"]
    nuevo_phone    = phone         if phone         is not None else client["phone"]
    with get_connection() as conn:
        # Mismo criterio que en el alta: las cuatro de la revisión `0002` se
        # tocan sólo si la instancia las tiene. Ver `_columnas_de_clients`.
        asignaciones = [
            "name=?", "address=?", "cuit_dni=?", "email=?", "phone=?",
            "iva_condition=?", "auto_facturar=?", "cc_resumen_auto=?",
            "cc_resumen_frecuencia=?",
        ]
        valores = [
            nuevo_name,
            address       if address       is not None else client["address"],
            nuevo_cuit,
            nuevo_email,
            nuevo_phone,
            iva_condition if iva_condition is not None else client.get("iva_condition", ""),
            int(auto_facturar) if auto_facturar is not None else int(client.get("auto_facturar", 0)),
            int(cc_resumen_auto) if cc_resumen_auto is not None else int(client.get("cc_resumen_auto", 0)),
            cc_resumen_frecuencia if cc_resumen_frecuencia is not None
            else (client.get("cc_resumen_frecuencia") or "mensual"),
        ]
        nuevas = {
            "empresa": empresa if empresa is not None else (client.get("empresa") or ""),
            "ciudad": ciudad if ciudad is not None else (client.get("ciudad") or ""),
            "observaciones": observaciones if observaciones is not None
            else (client.get("observaciones") or ""),
            "tipo_facturacion": tipo_facturacion if tipo_facturacion is not None
            else (client.get("tipo_facturacion") or "por_servicio"),
        }
        presentes = _columnas_de_clients(conn)
        for col in _COLUMNAS_0002:
            if col in presentes:
                asignaciones.append(f"{col}=?")
                valores.append(nuevas[col])
        valores.append(client_id)
        conn.execute(
            f"UPDATE clients SET {', '.join(asignaciones)} WHERE id=?",
            tuple(valores),
        )
        # El espejo se mantiene al día (no solo se crea): el party es el
        # que ve LibraCommerce, y un nombre viejo ahí contradice al de
        # `clients` sin que nada lo delate.
        if _hay_parties(conn):
            conn.execute(
                """UPDATE parties SET display_name=?, tax_id=?, email=?, phone=?
                   WHERE id=?""",
                (nuevo_name, nuevo_cuit or None, nuevo_email or None,
                 nuevo_phone or None, client_id),
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
