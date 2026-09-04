"""
Remitos y presupuestos: numeración, alta/baja/modificación, búsqueda y
vencimiento automático de presupuestos enviados. Extraído de database.py
de Contalibra/Restolibra (idéntico en ambos) como parte de la migración
real a libracore.db (Fase 3 de LibraCore, ver wiki/entities/libracore.md).
"""
import json

from libracore.db.core import get_connection


def get_next_remito_number():
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(id) FROM remitos").fetchone()
        next_id = (row[0] or 0) + 1
        return f"0001-{next_id:08d}"


def create_remito(number, date, client_id, client_name, client_address, client_cuit,
                  client_email, client_phone, items, subtotal, tax_rate, tax_amount,
                  total, observations="", pdf_path="", usuario_id=None):
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO remitos
               (number, date, client_id, client_name, client_address, client_cuit,
                client_email, client_phone, items, subtotal, tax_rate, tax_amount,
                total, observations, pdf_path, usuario_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                number, date, client_id, client_name, client_address, client_cuit,
                client_email, client_phone, json.dumps(items, ensure_ascii=False),
                subtotal, tax_rate, tax_amount, total, observations, pdf_path, usuario_id,
            ),
        )
        return cur.lastrowid


def update_remito_pdf_path(remito_id, pdf_path):
    with get_connection() as conn:
        conn.execute("UPDATE remitos SET pdf_path=? WHERE id=?", (pdf_path, remito_id))


def get_all_remitos(limit=100):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM remitos ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


def get_remito(remito_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM remitos WHERE id=?", (remito_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["items"] = json.loads(d["items"])
        return d


def get_remitos_by_client(client_id):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM remitos WHERE client_id=? ORDER BY id DESC", (client_id,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


def search_remitos(query):
    q = f"%{query}%"
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM remitos
               WHERE number LIKE ? OR client_name LIKE ? OR observations LIKE ?
               ORDER BY id DESC""",
            (q, q, q),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


def get_next_presupuesto_number():
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(id) FROM presupuestos").fetchone()
        next_id = (row[0] or 0) + 1
        return f"PRES-{next_id:08d}"


def auto_vencimiento_presupuestos():
    """Marca como 'vencido' los presupuestos enviados cuya validez expiró."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE presupuestos SET status='vencido'
               WHERE status IN ('enviado', 'pendiente')
               AND valid_until < date('now','-3 hours')"""
        )


def create_presupuesto(number, date, valid_until, client_id, client_name, client_address,
                       client_cuit, client_email, client_phone, items, subtotal, tax_rate,
                       tax_amount, total, observations="", pdf_path="", status="borrador",
                       usuario_id=None):
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO presupuestos
               (number, date, valid_until, status, client_id, client_name, client_address,
                client_cuit, client_email, client_phone, items, subtotal, tax_rate,
                tax_amount, total, observations, pdf_path, usuario_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                number, date, valid_until, status, client_id, client_name, client_address,
                client_cuit, client_email, client_phone, json.dumps(items, ensure_ascii=False),
                subtotal, tax_rate, tax_amount, total, observations, pdf_path, usuario_id,
            ),
        )
        return cur.lastrowid


def update_presupuesto_pdf_path(presupuesto_id, pdf_path):
    with get_connection() as conn:
        conn.execute("UPDATE presupuestos SET pdf_path=? WHERE id=?", (pdf_path, presupuesto_id))


def update_presupuesto_status(presupuesto_id, status):
    with get_connection() as conn:
        conn.execute("UPDATE presupuestos SET status=? WHERE id=?", (status, presupuesto_id))


def update_presupuesto_remito_id(presupuesto_id, remito_id):
    with get_connection() as conn:
        conn.execute("UPDATE presupuestos SET remito_id=? WHERE id=?", (remito_id, presupuesto_id))


def convertir_presupuesto_a_remito(presupuesto, *, generar_pdf=None, idempotente=False,
                                   crear_remito=None, al_convertir=None):
    """Crea un remito a partir de un presupuesto, lo deja linkeado y lo devuelve.

    El esqueleto común de la conversión presupuesto→remito (idempotencia, crear,
    PDF, link) vive acá; lo que varía entre productos entra por callbacks, no por
    ramas ``if producto``:

    - ``crear_remito``: callback ``(presupuesto) -> remito_id``. Cómo se crea el
      remito. Default ``None`` = **copiar los importes verbatim** con un
      ``number`` nuevo (Contalibra/Restolibra). LibraDesk pasa el suyo, que
      **recomputa** los totales (IVA por línea) y conserva ``usuario_id``.
    - ``al_convertir``: callback ``(presupuesto_id, remito) -> None`` que corre
      **sólo en una conversión nueva** (no en el retorno idempotente), después de
      linkear. LibraDesk marca ahí el presupuesto ``aceptado``.
    - ``idempotente``: si el presupuesto ya tiene ``remito_id`` y ese remito
      existe, lo devuelve sin crear otro. Contalibra/Restolibra pasan ``False``
      (crear siempre); LibraDesk ``True``.
    - ``generar_pdf``: callback opcional ``(remito) -> pdf_path``. El template del
      PDF es arista del producto.

    Devuelve el remito (dict): el recién creado, o el existente si fue idempotente.
    """
    if idempotente and presupuesto.get("remito_id"):
        existente = get_remito(presupuesto["remito_id"])
        if existente is not None:
            return existente

    if crear_remito is not None:
        remito_id = crear_remito(presupuesto)
    else:
        remito_id = create_remito(
            number=get_next_remito_number(),
            date=presupuesto["date"],
            client_id=presupuesto["client_id"],
            client_name=presupuesto["client_name"],
            client_address=presupuesto.get("client_address", ""),
            client_cuit=presupuesto.get("client_cuit", ""),
            client_email=presupuesto.get("client_email", ""),
            client_phone=presupuesto.get("client_phone", ""),
            items=presupuesto["items"],
            subtotal=presupuesto["subtotal"],
            tax_rate=presupuesto["tax_rate"],
            tax_amount=presupuesto["tax_amount"],
            total=presupuesto["total"],
            observations=presupuesto.get("observations", ""),
        )

    if generar_pdf is not None:
        pdf_path = generar_pdf(get_remito(remito_id))
        update_remito_pdf_path(remito_id, pdf_path)

    update_presupuesto_remito_id(presupuesto["id"], remito_id)
    remito = get_remito(remito_id)
    if al_convertir is not None:
        al_convertir(presupuesto["id"], remito)
    return remito


def get_all_presupuestos(limit=100, estado=None):
    auto_vencimiento_presupuestos()
    with get_connection() as conn:
        if estado:
            rows = conn.execute(
                "SELECT * FROM presupuestos WHERE status=? ORDER BY id DESC LIMIT ?",
                (estado, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM presupuestos ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


def get_presupuestos_count_by_estado():
    auto_vencimiento_presupuestos()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM presupuestos GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}


def get_presupuesto(presupuesto_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM presupuestos WHERE id=?", (presupuesto_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["items"] = json.loads(d["items"])
        return d


def get_presupuestos_by_client(client_id):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM presupuestos WHERE client_id=? ORDER BY id DESC", (client_id,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


def search_presupuestos(query, estado=None):
    auto_vencimiento_presupuestos()
    q = f"%{query}%"
    with get_connection() as conn:
        if estado:
            rows = conn.execute(
                """SELECT * FROM presupuestos
                   WHERE status=? AND (number LIKE ? OR client_name LIKE ? OR observations LIKE ?)
                   ORDER BY id DESC""",
                (estado, q, q, q),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM presupuestos
                   WHERE number LIKE ? OR client_name LIKE ? OR observations LIKE ?
                   ORDER BY id DESC""",
                (q, q, q),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result


def delete_remito(remito_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM remitos WHERE id=?", (remito_id,))


def delete_presupuesto(presupuesto_id):
    """Borra un presupuesto solo si está en estado 'borrador'."""
    with get_connection() as conn:
        presupuesto = conn.execute(
            "SELECT status FROM presupuestos WHERE id=?", (presupuesto_id,)
        ).fetchone()
        if not presupuesto:
            raise ValueError("Presupuesto no encontrado")
        status = dict(presupuesto)["status"] if presupuesto else None
        if status != "borrador":
            raise ValueError(f"No se puede borrar un presupuesto {status}")
        conn.execute("DELETE FROM presupuestos WHERE id=?", (presupuesto_id,))


def update_remito(remito_id, date, client_id, client_name, client_address, client_cuit,
                  client_email, client_phone, items, subtotal, tax_rate, tax_amount,
                  total, observations=""):
    with get_connection() as conn:
        conn.execute(
            """UPDATE remitos
               SET date=?, client_id=?, client_name=?, client_address=?, client_cuit=?,
                   client_email=?, client_phone=?, items=?, subtotal=?, tax_rate=?,
                   tax_amount=?, total=?, observations=?
               WHERE id=?""",
            (
                date, client_id, client_name, client_address, client_cuit,
                client_email, client_phone, json.dumps(items, ensure_ascii=False),
                subtotal, tax_rate, tax_amount, total, observations, remito_id,
            ),
        )


def update_presupuesto(presupuesto_id, date, valid_until, status, client_id, client_name,
                       client_address, client_cuit, client_email, client_phone, items,
                       subtotal, tax_rate, tax_amount, total, observations=""):
    with get_connection() as conn:
        conn.execute(
            """UPDATE presupuestos
               SET date=?, valid_until=?, status=?, client_id=?, client_name=?,
                   client_address=?, client_cuit=?, client_email=?, client_phone=?,
                   items=?, subtotal=?, tax_rate=?, tax_amount=?, total=?, observations=?
               WHERE id=?""",
            (
                date, valid_until, status, client_id, client_name, client_address,
                client_cuit, client_email, client_phone, json.dumps(items, ensure_ascii=False),
                subtotal, tax_rate, tax_amount, total, observations, presupuesto_id,
            ),
        )
