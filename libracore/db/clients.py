"""
Clientes: alta/baja/modificación, búsqueda por CUIT/email, facturas
asociadas. Migrado a libracore.db (Fase 3 de LibraCore, migración real,
Tier 2 — convergencia de comportamiento confirmada con el usuario:
`activar_cliente`, la validación de CUIT/DNI duplicado en `create_client`
y la normalización de email/CUIT en las búsquedas eran solo de Contalibra
y pasan a core — ver wiki/entities/libracore.md).

**Auditoría de escrituras (ADR-043).** Toda función de este módulo que escribe
`clients` le avisa a `libracore.auditoria` qué cambió (`Cambio` con la fila de antes y
la de después), **dentro de la misma transacción**. Sin registrador configurado no
hace nada ni lee nada de más. Un test (`tests/test_auditoria_de_escrituras.py`) recorre
el AST del motor y falla si una función con `INSERT`/`UPDATE`/`DELETE` sobre `clients`
no emite el cambio: una escritura nueva que se olvide de auditar no pasa el CI.

**Altas en un nodo offline (ADR-077).** Los tres caminos que insertan en `clients` (`create_client`, `crear_cliente` y
`resolver_cliente_externo`) guardan al cliente con un **id provisorio negativo** si la instancia es un nodo de LibraEdge, y
después de escribirlo —con su `parties`, el libro y la auditoría— avisan a `libracore.altas_en_nodo` en la misma
transacción. Fuera de un nodo, o sin `altas_en_nodo.configurar()`, todo queda como estaba.
"""
import contextlib
import json

from libracore import altas_en_nodo, auditoria
from libracore.db.core import get_connection

_TABLA = "clients"


#: El rol de los clientes en el libro de terceros (`libro_de_clientes.ROL`; no se importa para no crear un ciclo).
_ROL_CLIENTE = "cliente"


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


def _fila_cliente(conn, client_id) -> dict | None:
    """La fila entera de `clients`, con la conexión de la escritura (la misma transacción)."""
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    return dict(row) if row else None


def _antes(conn, client_id) -> dict | None:
    """La fila antes de escribirla, **sólo si hay registrador**: sin él no se hace el SELECT."""
    return _fila_cliente(conn, client_id) if auditoria.auditando() else None


def _con_id_provisorio(conn, columnas: list[str], valores: list) -> int | None:
    """Si esta instancia es un nodo, pone el `id` provisorio (negativo) al principio de lo que se va a insertar y lo
    devuelve; si no, `None` y la base asigna el suyo. **El id explícito es también el `client_id` del alta**: el
    `lastrowid` de un `INSERT` con id propio no es confiable en PostgreSQL (`RETURNING id` sí, pero acá ya se sabe)."""
    provisorio = altas_en_nodo.id_provisorio_de_cliente(conn)
    if provisorio is not None:
        columnas.insert(0, "id")
        valores.insert(0, provisorio)
    return provisorio


def _avisar_alta(conn, client_id) -> None:
    """Le avisa a `altas_en_nodo` del alta, **adentro de la transacción** y al final: después del espejo en `parties`, del
    libro y de la auditoría, así que si el gancho levanta se cae todo junto. Sólo lee la fila si la instancia es un nodo."""
    if altas_en_nodo.en_un_nodo():
        altas_en_nodo.avisar_alta_de_cliente(conn, _fila_cliente(conn, client_id))


def _registrar_cambio(conn, accion: str, client_id, antes: dict | None = None) -> None:
    """Le pasa el cambio al registrador, con la fila de después leída acá, en la misma
    conexión. Va al final de la escritura, después del espejo y del libro: si el registro
    falla, se cae todo (ver `libracore.auditoria`).

    Una edición o una baja sobre un id que no existía (`antes is None`) no cambió nada y
    no se registra.
    """
    if not auditoria.auditando():
        return
    if accion != auditoria.CREAR and antes is None:
        return
    despues = None if accion == auditoria.BORRAR else _fila_cliente(conn, client_id)
    auditoria.registrar(conn, _TABLA, accion, client_id, antes, despues)


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
        provisorio = _con_id_provisorio(conn, columnas, valores)
        cur = conn.execute(
            f"INSERT INTO clients ({', '.join(columnas)})"
            f" VALUES ({', '.join(['?'] * len(columnas))})",
            tuple(valores),
        )
        client_id = provisorio if provisorio is not None else cur.lastrowid
        # En la MISMA transacción que el alta: un cliente sin su party es
        # un cliente al que no se le puede vender.
        _espejar_party(conn, client_id, name, cuit_dni, email, phone)
        _al_libro(conn, client_id)
        _registrar_cambio(conn, auditoria.CREAR, client_id)
        _avisar_alta(conn, client_id)
        return client_id


def _columnas_a_escribir(conn, datos: dict, que: str) -> list[str]:
    """Las claves de `datos`, validadas contra las columnas que tiene HOY la tabla.

    Una clave que no es columna de esta instancia levanta `ValueError` **antes de
    escribir nada**: es un error de quien llama (o una columna de una revisión que esta
    instancia todavía no corrió), no algo que se pueda ignorar sin mentir. `id` no se
    escribe nunca: lo asigna la base, y en PostgreSQL un `id` explícito deja la secuencia
    atrás. Las claves salen de ese conjunto de nombres de columna, así que van al SQL sin
    riesgo de inyección.
    """
    if not datos:
        raise ValueError(f"{que}: no hay ninguna columna para escribir")
    desconocidas = sorted(set(datos) - _columnas_de_clients(conn))
    if desconocidas or "id" in datos:
        raise ValueError(
            f"{que}: columnas que clients no tiene en esta instancia o que no se escriben: "
            + ", ".join(desconocidas + (["id"] if "id" in datos else []))
        )
    return list(datos)


def crear_cliente(datos: dict, conn=None) -> int:
    """Alta de un cliente con **exactamente** las columnas de `datos` (`None` = NULL).

    Es la costura para lo que `create_client` no conoce: una columna propia de un
    producto (`price_list_id` de LibraDesk) o `activo` en el alta. Acepta cualquier
    columna que exista en la instancia (`_columnas_de_clients`); una desconocida levanta
    `ValueError`. No pone defaults ni normaliza `None` a `""`: lo que no está en `datos`
    queda con el default de la base, y eso es lo que distingue a esta función de
    `create_client`, que conserva su comportamiento para los productos que ya la usan.

    Hace lo mismo que `create_client` a su alrededor, en la misma transacción: el espejo
    en `parties` (si la instancia lo tiene), el asiento al libro de clientes y el aviso a
    `libracore.auditoria`. **No** valida CUIT duplicado: eso lo decide quien llama con
    `validar_cuit_no_duplicado`, que existe aparte justamente para esto.

    **`conn=` (ADR-077).** Con una conexión, el alta se escribe **en la transacción de quien llama** y el commit es
    suyo (el patrón de `create_caja_movimiento(conn=)`): es lo que necesita el central para aplicar el alta de un
    cliente que subió un nodo junto con su mapeo, o las dos cosas quedan o ninguna. Sin ella, abre la suya.
    """
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        columnas = _columnas_a_escribir(c, datos, "crear_cliente")
        valores = [datos[col] for col in columnas]
        provisorio = _con_id_provisorio(c, columnas, valores)
        cur = c.execute(
            f"INSERT INTO clients ({', '.join(columnas)})"
            f" VALUES ({', '.join(['?'] * len(columnas))})",
            tuple(valores),
        )
        client_id = provisorio if provisorio is not None else cur.lastrowid
        _espejar_party(
            c, client_id, datos.get("name"), datos.get("cuit_dni"), datos.get("email"),
            datos.get("phone"), bool(datos.get("activo", 1)),
        )
        _al_libro(c, client_id)
        _registrar_cambio(c, auditoria.CREAR, client_id)
        _avisar_alta(c, client_id)
        return client_id


#: Columna de `clients` -> su columna en el `parties` espejo.
_ESPEJO = {"name": "display_name", "cuit_dni": "tax_id", "email": "email", "phone": "phone"}


def actualizar_cliente(client_id: int, cambios: dict) -> bool:
    """Edita un cliente con **exactamente** las columnas de `cambios` (`None` = NULL).

    La contraparte de `crear_cliente`; `update_client` conserva su semántica de «`None`
    es no tocar». Lo que no está en `cambios` no se toca. Una columna desconocida levanta
    `ValueError` antes de escribir. Devuelve `False` si no hay un cliente con ese id (no
    escribe ni avisa nada), `True` si lo editó.

    En la misma transacción mantiene el espejo en `parties` (sólo las columnas que
    cambiaron), reasienta el libro si cambió el CUIT y avisa a `libracore.auditoria`.
    """
    with get_connection() as conn:
        columnas = _columnas_a_escribir(conn, cambios, "actualizar_cliente")
        antes = _fila_cliente(conn, client_id) if (auditoria.auditando() or "cuit_dni" in cambios) else None
        cur = conn.execute(
            f"UPDATE clients SET {', '.join(c + '=?' for c in columnas)} WHERE id=?",
            (*(cambios[c] for c in columnas), client_id),
        )
        if not cur.rowcount:
            return False
        if _hay_parties(conn):
            sets = [f"{_ESPEJO[c]}=?" for c in columnas if c in _ESPEJO]
            valores = [cambios[c] if c == "name" else (cambios[c] or None) for c in columnas if c in _ESPEJO]
            if "activo" in cambios:
                sets.append("active=?")
                valores.append(int(bool(cambios["activo"])))
            if sets:
                conn.execute(f"UPDATE parties SET {', '.join(sets)} WHERE id=?", (*valores, client_id))
        if "cuit_dni" in cambios and (cambios["cuit_dni"] or "") != ((antes or {}).get("cuit_dni") or ""):
            _al_libro(conn, client_id)
        _registrar_cambio(conn, auditoria.EDITAR, client_id, antes)
        return True


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
            antes = _antes(conn, row["id"])
            conn.execute("UPDATE clients SET name = ? WHERE id = ?", (name, row["id"]))
            _registrar_cambio(conn, auditoria.EDITAR, row["id"], antes)
            return row["id"]
        columnas = ["name", "cuit_dni", "email", "phone", "external_ref"]
        valores = [name, cuit_dni or "", email or "", phone or "", external_ref]
        provisorio = _con_id_provisorio(conn, columnas, valores)
        cur = conn.execute(
            f"INSERT INTO clients ({', '.join(columnas)}) VALUES ({', '.join(['?'] * len(columnas))})",
            tuple(valores),
        )
        client_id = provisorio if provisorio is not None else cur.lastrowid
        _al_libro(conn, client_id)
        _registrar_cambio(conn, auditoria.CREAR, client_id)
        _avisar_alta(conn, client_id)
        return client_id


def _al_libro(conn, client_id: int) -> None:
    """Un cliente nuevo, o con CUIT nuevo, puede hacer contar ventas y facturas que
    antes no eran de nadie: se asientan (`libro_de_clientes.sincronizar_cliente`). Lo
    ya asentado a otro cliente no se mueve. Import tardío: ese módulo importa caja,
    facturas y cuenta corriente."""
    from libracore.db import libro_de_clientes

    libro_de_clientes.sincronizar_cliente(client_id, conn=conn)


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
        antes = _antes(conn, client_id)
        conn.execute("UPDATE clients SET activo = 0 WHERE id = ?", (client_id,))
        if _hay_parties(conn):
            conn.execute("UPDATE parties SET active = 0 WHERE id = ?", (client_id,))
        _registrar_cambio(conn, auditoria.EDITAR, client_id, antes)
        return True


def activar_cliente(client_id: int) -> bool:
    """Reactiva un cliente previamente desactivado."""
    with get_connection() as conn:
        antes = _antes(conn, client_id)
        conn.execute("UPDATE clients SET activo = 1 WHERE id = ?", (client_id,))
        if _hay_parties(conn):
            conn.execute("UPDATE parties SET active = 1 WHERE id = ?", (client_id,))
        _registrar_cambio(conn, auditoria.EDITAR, client_id, antes)
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
                  empresa=None, ciudad=None, observaciones=None,
                  tipo_facturacion=None):
    client = get_client(client_id)
    if not client:
        return
    nuevo_name     = name          if name          is not None else client["name"]
    nuevo_cuit     = cuit_dni      if cuit_dni      is not None else client["cuit_dni"]
    nuevo_email    = email         if email         is not None else client["email"]
    nuevo_phone    = phone         if phone         is not None else client["phone"]
    with get_connection() as conn:
        # Mismo criterio que en el alta: las cuatro de la revisión `0002` se
        # tocan sólo si la instancia las tiene. Ver `_columnas_de_clients`.
        asignaciones = [
            "name=?", "address=?", "cuit_dni=?", "email=?", "phone=?",
            "iva_condition=?", "auto_facturar=?",
        ]
        valores = [
            nuevo_name,
            address       if address       is not None else client["address"],
            nuevo_cuit,
            nuevo_email,
            nuevo_phone,
            iva_condition if iva_condition is not None else client.get("iva_condition", ""),
            int(auto_facturar) if auto_facturar is not None else int(client.get("auto_facturar", 0)),
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
        antes = _antes(conn, client_id)
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
        if (nuevo_cuit or "") != (client["cuit_dni"] or ""):
            _al_libro(conn, client_id)
        _registrar_cambio(conn, auditoria.EDITAR, client_id, antes)


def toggle_auto_facturar(client_id: int) -> bool:
    """Invierte el flag auto_facturar. Devuelve el nuevo valor."""
    with get_connection() as conn:
        antes = _antes(conn, client_id)
        conn.execute(
            "UPDATE clients SET auto_facturar = 1 - auto_facturar WHERE id=?",
            (client_id,),
        )
        row = conn.execute("SELECT auto_facturar FROM clients WHERE id=?", (client_id,)).fetchone()
        _registrar_cambio(conn, auditoria.EDITAR, client_id, antes)
        return bool(row["auto_facturar"]) if row else False


def delete_client(client_id):
    """Borra un cliente sólo si no tiene nada asociado: remitos, presupuestos ni cuenta corriente.

    La cuenta corriente bloquea la baja (ADR-057): en la baseline `cc_pagos` y `cc_debitos` tienen
    `ON DELETE CASCADE` hacia `clients`, los recibos quedan sin cliente (`SET NULL`) y el libro
    (`cc_asientos`) guarda el id sin FK: borrar el cliente se llevaba sus movimientos o los dejaba huérfanos.
    Un cliente con historia se desactiva, no se borra."""
    with get_connection() as conn:
        def contar(sql, *antes):
            return conn.execute(sql, (*antes, client_id)).fetchone()[0]

        remito_count = contar("SELECT COUNT(*) FROM remitos WHERE client_id=?")
        presupuesto_count = contar("SELECT COUNT(*) FROM presupuestos WHERE client_id=?")
        cuenta_count = (
            contar("SELECT COUNT(*) FROM cc_pagos WHERE cliente_id=?")
            + contar("SELECT COUNT(*) FROM cc_debitos WHERE cliente_id=?")
            + contar("SELECT COUNT(*) FROM recibos WHERE cliente_id=?")
            + contar("SELECT COUNT(*) FROM cc_asientos WHERE rol=? AND tercero_id=?", _ROL_CLIENTE)
        )
        if remito_count or presupuesto_count or cuenta_count:
            msg_parts = []
            if remito_count > 0:
                msg_parts.append(f"{remito_count} remito(s)")
            if presupuesto_count > 0:
                msg_parts.append(f"{presupuesto_count} presupuesto(s)")
            if cuenta_count > 0:
                msg_parts.append(f"{cuenta_count} movimiento(s) de cuenta corriente")
            raise ValueError(
                f"El cliente tiene {' y '.join(msg_parts)} asociado(s) y no puede eliminarse. "
                "Se puede desactivar.")
        # La fila se lee ANTES del DELETE: después ya no está, y la baja tiene que decir qué se borró.
        antes = _antes(conn, client_id)
        conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
        _registrar_cambio(conn, auditoria.BORRAR, client_id, antes)


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


def get_client_by_cuit(cuit: str, conn=None):
    """Busca cliente por CUIT normalizando guiones (ej: 20317819162 == 20-31781916-2).
    Si hay más de un cliente con el mismo CUIT (duplicado), prioriza el activo
    y, entre iguales, el más reciente.

    `conn=` (ADR-077): lee con la conexión de quien llama —en el central que aplica el alta de un nodo, la de su
    transacción, que es donde está lo que escribió un alta anterior de la misma tanda—."""
    normalized = (cuit or "").replace("-", "").strip()
    if not normalized:
        return None
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        row = c.execute(
            "SELECT * FROM clients WHERE REPLACE(cuit_dni, '-', '') = ? ORDER BY activo DESC, id DESC LIMIT 1",
            (normalized,),
        ).fetchone()
    return dict(row) if row else None
