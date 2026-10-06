"""
La pre factura: un comprobante por facturar que el cliente puede ver antes (ADR-030).

Es **la evolución de la bandeja** `comprobantes_pendientes` y no una tabla nueva: la
bandeja ya es «un comprobante por facturar» con el cliente como foto (sin depender de
`clients`), los ítems, un total que sale de los ítems y el vínculo a la factura. A una
fila de esas se le suma un **número interno** (`PF-0001`), el emisor, el tipo de
comprobante que va a salir y el ciclo de enviarla y aceptarla. Los productores de
siempre (Contalibra, LibraDesk: `db.comprobantes_pendientes.upsert_comprobante`) no
la usan ni la ven cambiada.

Una pre factura **no es una factura**: no tiene punto de venta, ni número fiscal, ni
CAE, ni consume numeración de ARCA. Su PDF lo dice en grande
(`pdf_generator.LEYENDA_PRE_FACTURA`). El número y el punto de venta los pone ARCA
cuando se factura de verdad; eso lo hace el producto con el camino de emisión de
siempre y después llama a `marcar_facturada`.

## El ciclo

    pendiente ──enviada──▶ enviado ──aceptada──▶ aceptado ──facturada──▶ facturado
        │  ▲                   │  ▲                  │
        │  └────── editar ─────┴──┴──────────────────┘   (vuelve a `pendiente`)
        └─────────── anular, desde cualquiera de los tres ──▶ descartado

- **`facturado` y `descartado` son finales.** Una resolución la tomó una persona y no
  se revierte (la misma regla de la bandeja). No se edita, no se envía, no se acepta.
- **Aceptada se puede marcar desde `pendiente`**, no sólo desde `enviado`: el PDF se
  puede mandar por otro medio (WhatsApp) y el operador marca que el cliente está de
  acuerdo sin haberlo enviado desde la app.
- **Facturar se puede desde cualquiera de los tres estados abiertos.** El motor no
  exige que esté aceptada (Contalibra factura pendientes que nunca pasaron por
  acá); un producto que sí lo exige pasa `exigir_aceptada=True` o mira el estado
  antes de emitir.
- **Editar una pre factura enviada o aceptada la devuelve a `pendiente`** si cambió
  algo, y borra el rastro de envío y aceptación: el cliente aceptó **otros** datos, y
  conservar la marca de aceptada sobre un contenido distinto es afirmar una
  conformidad que no existe. Hay que volver a enviarla y a aceptarla. Un `editar` que
  no cambia nada no mueve el estado.
- **Enviar una aceptada la deja aceptada** (reenviar la copia no es un evento del
  ciclo, como el reenvío de un presupuesto aceptado); sólo actualiza a quién y cuándo.

## La numeración

`PF-0001`, correlativa **por `(origen_producto, origen_instancia)`** y asignada por el
motor al crear. Un anulado conserva su número, así que no se reusa. La unicidad la
cierra el índice `idx_comprobantes_pendientes_numero_interno`; dos altas simultáneas
que calculen el mismo número reintentan en un `SAVEPOINT` (como `create_factura`).

## `conn=` (ADR-025)

Toda función acepta `conn=`: con él trabaja en la transacción de quien llama y **no
confirma**; sin él, confirma la suya. Es lo que deja a LibraCargo reservar las
órdenes de la pre factura, o marcarla facturada, en la misma transacción.
"""
import json
import sqlite3

from libracore import config_manager, email_sender, pdf_generator
from libracore import tipos_comprobante as tipos
from libracore.db import comprobantes_pendientes as bandeja
from libracore.db.arca_config import EmisorDesconocido
from libracore.db.core import Conexion, _ar_now, sql_busqueda
from libracore.db.facturas import _con, _savepoint
from libracore.facturas_router import smtp_efectivo
from libracore.validacion import rechazar_booleanos

PREFIJO_NUMERO = "PF-"

ESTADOS = bandeja.ESTADOS
ESTADOS_ABIERTOS = bandeja.ESTADOS_ABIERTOS
ESTADOS_FINALES = bandeja.ESTADOS_FINALES

#: Lo que `editar` puede cambiar. El origen (producto, instancia, tipo e id) y el número
#: no están: son lo que identifica a la pre factura.
EDITABLES = frozenset({
    "cliente_id", "cliente_cuit", "cliente_razon", "cliente_domicilio", "emisor_id",
    "tipo_comprobante", "fecha_sugerida", "fecha_vencimiento_pago", "periodo_desde",
    "periodo_hasta", "concepto", "condicion_venta", "observaciones", "items",
})

_MAX_INTENTOS = 5


class PreFacturaError(Exception):
    """Base de lo que levanta este módulo, para quien quiera atrapar todo junto."""


class PreFacturaNoEncontrada(PreFacturaError, LookupError):
    """No existe, o es una fila de la bandeja que no es una pre factura (no tiene número)."""


class TransicionInvalida(PreFacturaError):
    """El estado en que está no admite esa operación (una facturada no se edita ni se anula)."""


class PreFacturaYaExiste(PreFacturaError):
    """Ya hay una pre factura de ese origen: `(producto, instancia, tipo, id)` es único."""


class SmtpNoConfigurado(PreFacturaError):
    """No hay servidor SMTP resuelto (ni el del producto ni el de `config.json`)."""


# ── Lectura ────────────────────────────────────────────────────────────────────


def numero_interno(n: int) -> str:
    """`PF-0001`. Pasado de 9999 sigue sin tope (`PF-10000`)."""
    return f"{PREFIJO_NUMERO}{int(n):04d}"


def get(pre_factura_id: int, *, conn: Conexion | None = None) -> dict | None:
    """La pre factura, o `None` si no existe o la fila no es una pre factura."""
    fila = bandeja.get_comprobante(pre_factura_id, conn=conn)
    return fila if fila and fila.get("numero_interno") else None


def _cargar(pre_factura_id: int, conn: Conexion | None) -> dict:
    pf = get(pre_factura_id, conn=conn)
    if pf is None:
        raise PreFacturaNoEncontrada(f"No existe la pre factura {pre_factura_id}.")
    return pf


def listar(*, estado: str | None = None, cliente: str = "", cliente_id: int | None = None,
           origen_producto: str | None = None, origen_instancia: str | None = None,
           limit: int | None = None, conn: Conexion | None = None) -> list[dict]:
    """Las pre facturas, las más nuevas primero.

    `cliente` busca en la razón social y el CUIT, sin distinguir mayúsculas.
    `origen_producto` y `origen_instancia` acotan a las de un producto o una instancia
    (el router los fija por el producto: no los elige el cliente HTTP).
    """
    if estado is not None and estado not in ESTADOS:
        raise ValueError(f"estado invalido: {estado!r} (esperado uno de {ESTADOS})")
    where, params = ["numero_interno IS NOT NULL"], []
    if estado is not None:
        where.append("estado = ?")
        params.append(estado)
    if (cliente or "").strip():
        where.append(sql_busqueda("cliente_razon", "cliente_cuit"))
        params += [f"%{cliente.strip()}%"] * 2
    if cliente_id is not None:
        where.append("cliente_id = ?")
        params.append(int(cliente_id))
    if origen_producto is not None:
        where.append("origen_producto = ?")
        params.append(origen_producto)
    if origen_instancia is not None:
        where.append("origen_instancia = ?")
        params.append(origen_instancia)
    sql = f"SELECT * FROM comprobantes_pendientes WHERE {' AND '.join(where)} ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    with _con(conn) as c:
        return [bandeja._row_a_dict(r) for r in c.execute(sql, tuple(params)).fetchall()]


def contar_por_estado(*, origen_producto: str | None = None, origen_instancia: str | None = None,
                      conn: Conexion | None = None) -> dict:
    """`{estado: cantidad}` con todos los estados, también los que están en cero."""
    where, params = ["numero_interno IS NOT NULL"], []
    if origen_producto is not None:
        where.append("origen_producto = ?")
        params.append(origen_producto)
    if origen_instancia is not None:
        where.append("origen_instancia = ?")
        params.append(origen_instancia)
    with _con(conn) as c:
        filas = c.execute(
            f"SELECT estado, COUNT(*) FROM comprobantes_pendientes WHERE {' AND '.join(where)} "
            "GROUP BY estado", tuple(params)).fetchall()
    cuentas = {e: 0 for e in ESTADOS}
    cuentas.update({f[0]: f[1] for f in filas})
    return cuentas


# ── Validación ─────────────────────────────────────────────────────────────────


def _validar_items(items) -> list:
    """Los ítems tal como vienen (con las claves de más que el producto quiera dejar:
    `detalle`, el id de su orden), después de mirar que sirvan para sumar.

    `iva_rate` es una fracción (`0.21`); si falta cuenta como 0, igual que en
    `calcular_total`.
    """
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("Una pre factura necesita al menos un ítem.")
    items = list(items)
    rechazar_booleanos(items, ("qty", "unit_price", "iva_rate"), "items[].")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{i}] tiene que ser un objeto con descripción, cantidad y precio.")
        if not str(item.get("description") or "").strip():
            raise ValueError(f"items[{i}] necesita una descripción.")
        for campo in ("qty", "unit_price", "iva_rate"):
            try:
                float(item.get(campo) or 0)
            except (TypeError, ValueError):
                raise ValueError(f"items[{i}].{campo} tiene que ser un número.") from None
    return items


def _validar_tipo(tipo, items) -> None:
    if tipo is None:
        return
    if isinstance(tipo, bool) or tipo not in tipos.FACTURAS:
        raise ValueError(
            f"tipo_comprobante inválido: {tipo!r} (una factura: {tipos.FACTURAS}).")
    if tipo in tipos.C and any(float(i.get("iva_rate") or 0) for i in items):
        # Un comprobante clase C no discrimina IVA: ARCA lo rechazaría al emitir. Mejor
        # decirlo ahora, con la pre factura todavía en la mano.
        raise ValueError(
            "Un comprobante clase C no lleva IVA: los ítems van con iva_rate 0 "
            "(o se cambia el tipo).")


def _validar_emisor(emisor_id, c) -> None:
    if emisor_id is None:
        return
    if isinstance(emisor_id, bool) or not isinstance(emisor_id, int):
        raise ValueError("emisor_id tiene que ser el id de una configuración de ARCA.")
    if c.execute("SELECT 1 FROM arca_config WHERE id=? AND activo=1", (emisor_id,)).fetchone() is None:
        raise EmisorDesconocido(f"No hay una configuración de ARCA activa con id {emisor_id}.")


def _validar_cliente(razon) -> str:
    razon = str(razon or "").strip()
    if not razon:
        raise ValueError("El cliente necesita una razón social.")
    return razon


# ── Escritura ──────────────────────────────────────────────────────────────────


def _siguiente_numero(c, origen_producto: str, origen_instancia: str) -> str:
    # `numero_interno IS NOT NULL` y no un `LIKE 'PF-%'`: el `%` choca con los
    # placeholders de psycopg, y todo número que hay lo puso `crear`.
    fila = c.execute(
        "SELECT MAX(CAST(SUBSTR(numero_interno, ?) AS INTEGER)) FROM comprobantes_pendientes "
        "WHERE origen_producto=? AND origen_instancia=? AND numero_interno IS NOT NULL",
        (len(PREFIJO_NUMERO) + 1, origen_producto, origen_instancia),
    ).fetchone()
    return numero_interno((fila[0] or 0) + 1)


def crear(*, origen_producto: str, cliente_razon: str, items: list, origen_instancia: str = "",
          origen_tipo: str = bandeja.ORIGEN_PRE_FACTURA, origen_id: str | None = None,
          cliente_id: int | None = None, cliente_cuit: str = "", cliente_domicilio: str = "",
          emisor_id: int | None = None, tipo_comprobante: int | None = None,
          fecha_sugerida: str = "", fecha_vencimiento_pago: str = "", periodo_desde: str = "",
          periodo_hasta: str = "", concepto: str = "", condicion_venta: str = "",
          observaciones: str = "", conn: Conexion | None = None) -> dict:
    """Crea una pre factura `pendiente`, le asigna su número interno y la devuelve.

    El cliente va **como foto** (razón social, CUIT, domicilio): no depende de que
    exista en `clients`; `cliente_id` es opcional.

    - `origen_producto` / `origen_instancia`: de quién es. Define la numeración.
    - `origen_tipo` y `origen_id` completan la clave única de la bandeja. Una pre
      factura que junta varias cosas del producto (órdenes) no tiene un solo origen,
      así que sin `origen_id` se usa su propio número interno: es único por
      construcción. Si el producto pasa uno y ya existe, `PreFacturaYaExiste`.
    - `emisor_id`: `arca_config.id` de la razón social que facturaría (ADR-021); `None`
      es «el emisor único de la instancia».
    - `tipo_comprobante`: 1/6/11 (A/B/C) o 201/206/211 (FCE). `None` es «todavía no se
      eligió».
    - `fecha_sugerida`: la fecha del documento (sale en el PDF); vacía es hoy.
    - `items`: `{description, qty, unit_price, iva_rate}` (y lo que el producto quiera
      guardar). **El total sale de los ítems**, nunca de afuera
      (`db.comprobantes_pendientes.calcular_total`).

    Levanta `ValueError` ante datos inválidos (también `EmisorDesconocido`).
    """
    if not str(origen_producto or "").strip():
        raise ValueError("origen_producto es obligatorio: define la numeración de la pre factura.")
    if origen_tipo not in bandeja.ORIGENES:
        raise ValueError(f"origen_tipo invalido: {origen_tipo!r} (esperado uno de {bandeja.ORIGENES})")
    cliente_razon = _validar_cliente(cliente_razon)
    items = _validar_items(items)
    _validar_tipo(tipo_comprobante, items)
    if origen_id is not None and not str(origen_id).strip():
        raise ValueError("origen_id, si se pasa, no puede ser vacío.")
    fecha = fecha_sugerida or _ar_now()[:10]
    total = bandeja.calcular_total(items)

    for intento in range(_MAX_INTENTOS):
        try:
            with _savepoint(conn, "libracore_pre_factura"), _con(conn) as c:
                _validar_emisor(emisor_id, c)
                numero = _siguiente_numero(c, origen_producto, origen_instancia)
                o_id = str(origen_id) if origen_id is not None else numero
                ya = c.execute(
                    "SELECT 1 FROM comprobantes_pendientes WHERE origen_producto=? AND "
                    "origen_instancia=? AND origen_tipo=? AND origen_id=?",
                    (origen_producto, origen_instancia, origen_tipo, o_id)).fetchone()
                if ya is not None:
                    raise PreFacturaYaExiste(
                        f"Ya hay una pre factura de {origen_producto} para {origen_tipo}:{o_id}.")
                cur = c.execute(
                    "INSERT INTO comprobantes_pendientes "
                    "(origen_producto, origen_instancia, origen_tipo, origen_id, cliente_id, "
                    " cliente_cuit, cliente_razon, cliente_domicilio, fecha_sugerida, "
                    " periodo_desde, periodo_hasta, concepto, condicion_venta, observaciones, "
                    " items, total, estado, numero_interno, emisor_id, tipo_comprobante, "
                    " fecha_vencimiento_pago) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (origen_producto, origen_instancia, origen_tipo, o_id, cliente_id,
                     cliente_cuit, cliente_razon, cliente_domicilio, fecha, periodo_desde,
                     periodo_hasta, concepto, condicion_venta, observaciones,
                     json.dumps(items, ensure_ascii=False), total, bandeja.ESTADO_PENDIENTE,
                     numero, emisor_id, tipo_comprobante, fecha_vencimiento_pago or None))
                return bandeja.get_comprobante(cur.lastrowid, conn=c)
        except sqlite3.IntegrityError:
            # Dos altas calcularon el mismo número y la otra ganó (el índice único
            # parcial lo cierra): se reintenta con el que sigue. Cualquier otra
            # violación (una FK) sale tal cual después de los intentos.
            if intento == _MAX_INTENTOS - 1:
                raise


def _igual(a, b) -> bool:
    return (a if a is not None else "") == (b if b is not None else "")


def editar(pre_factura_id: int, *, conn: Conexion | None = None, **campos) -> dict:
    """Cambia los `campos` (los de `EDITABLES`) de una pre factura que no esté facturada
    ni anulada, recalcula el total y la devuelve.

    **Si estaba `enviado` o `aceptado` y algo cambió, vuelve a `pendiente`** y se borran
    `enviado_*` y `aceptado_*`: el cliente aceptó otros datos. Un `editar` que deja todo
    igual no mueve nada.
    """
    desconocidos = set(campos) - EDITABLES
    if desconocidos:
        raise TypeError(f"editar: campos que no se editan {sorted(desconocidos)}")
    if "fecha_sugerida" in campos and not campos["fecha_sugerida"]:
        campos["fecha_sugerida"] = _ar_now()[:10]  # la fecha del documento no queda vacía
    with _con(conn) as c:
        actual = _cargar(pre_factura_id, c)
        if actual["estado"] not in ESTADOS_ABIERTOS:
            raise TransicionInvalida(
                f"La pre factura {actual['numero_interno']} está {actual['estado']}: no se edita.")
        nuevo = actual | campos
        _validar_cliente(nuevo["cliente_razon"])
        if "items" in campos:
            nuevo["items"] = _validar_items(campos["items"])
        _validar_tipo(nuevo["tipo_comprobante"], nuevo["items"])
        if "emisor_id" in campos:
            _validar_emisor(campos["emisor_id"], c)
        cambios = {k: nuevo[k] for k in campos if not _igual(actual[k], nuevo[k])}
        if not cambios:
            return actual

        if "items" in cambios:
            cambios["total"] = bandeja.calcular_total(nuevo["items"])
            cambios["items"] = json.dumps(nuevo["items"], ensure_ascii=False)
        if "fecha_vencimiento_pago" in cambios:
            cambios["fecha_vencimiento_pago"] = cambios["fecha_vencimiento_pago"] or None
        if actual["estado"] in (bandeja.ESTADO_ENVIADO, bandeja.ESTADO_ACEPTADO):
            cambios.update(estado=bandeja.ESTADO_PENDIENTE, enviado_at=None, enviado_a=None,
                           aceptado_at=None, aceptado_por=None)
        asignaciones = ", ".join(f"{k} = ?" for k in cambios)
        c.execute(f"UPDATE comprobantes_pendientes SET {asignaciones} WHERE id = ?",
                  (*cambios.values(), pre_factura_id))
        return bandeja.get_comprobante(pre_factura_id, conn=c)


def marcar_enviada(pre_factura_id: int, email: str, *, conn: Conexion | None = None) -> dict:
    """Deja asentado que se mandó a `email`, ahora. `pendiente` pasa a `enviado`; una
    `enviado` o `aceptado` conserva su estado y sólo actualiza a quién y cuándo."""
    email = str(email or "").strip()
    if not email:
        raise ValueError("Falta la dirección de correo a la que se envió.")
    with _con(conn) as c:
        pf = _cargar(pre_factura_id, c)
        if pf["estado"] not in ESTADOS_ABIERTOS:
            raise TransicionInvalida(
                f"La pre factura {pf['numero_interno']} está {pf['estado']}: no se envía.")
        estado = bandeja.ESTADO_ENVIADO if pf["estado"] == bandeja.ESTADO_PENDIENTE else pf["estado"]
        c.execute("UPDATE comprobantes_pendientes SET estado=?, enviado_at=?, enviado_a=? WHERE id=?",
                  (estado, _ar_now(), email, pre_factura_id))
        return bandeja.get_comprobante(pre_factura_id, conn=c)


def marcar_aceptada(pre_factura_id: int, usuario: str = "", *, conn: Conexion | None = None) -> dict:
    """El cliente está de acuerdo (lo marca el operador). Desde `pendiente` o `enviado`.
    Una que ya está `aceptado` se devuelve como está: no se pisa quién la aceptó."""
    with _con(conn) as c:
        pf = _cargar(pre_factura_id, c)
        if pf["estado"] == bandeja.ESTADO_ACEPTADO:
            return pf
        if pf["estado"] not in ESTADOS_ABIERTOS:
            raise TransicionInvalida(
                f"La pre factura {pf['numero_interno']} está {pf['estado']}: no se acepta.")
        c.execute("UPDATE comprobantes_pendientes SET estado=?, aceptado_at=?, aceptado_por=? WHERE id=?",
                  (bandeja.ESTADO_ACEPTADO, _ar_now(), usuario or "", pre_factura_id))
        return bandeja.get_comprobante(pre_factura_id, conn=c)


def anular(pre_factura_id: int, usuario: str = "", motivo: str = "", *,
           conn: Conexion | None = None) -> dict:
    """La descarta (`descartado`, final) con quién y por qué (`resuelto_por`,
    `motivo_descarte`). Desde cualquier estado abierto. Su número no se reusa."""
    with _con(conn) as c:
        pf = _cargar(pre_factura_id, c)
        if not bandeja.descartar(pre_factura_id, motivo or "", usuario or "", conn=c):
            raise TransicionInvalida(
                f"La pre factura {pf['numero_interno']} está {pf['estado']}: no se anula.")
        return bandeja.get_comprobante(pre_factura_id, conn=c)


def marcar_facturada(pre_factura_id: int, factura_id: int, usuario: str = "", *,
                     exigir_aceptada: bool = False, conn: Conexion | None = None) -> dict:
    """Cierra la pre factura con la factura que la cubrió (`factura_id`, `facturado`,
    final). Reusa `db.comprobantes_pendientes.marcar_facturado`.

    Se llama **después** de emitir (con `conn=` de la misma transacción en que se emite).
    `exigir_aceptada=True` rechaza una que el operador todavía no marcó aceptada.
    """
    with _con(conn) as c:
        pf = _cargar(pre_factura_id, c)
        if exigir_aceptada and pf["estado"] != bandeja.ESTADO_ACEPTADO and pf["estado"] in ESTADOS_ABIERTOS:
            raise TransicionInvalida(
                f"La pre factura {pf['numero_interno']} está {pf['estado']}: falta que se acepte.")
        if not bandeja.marcar_facturado(pre_factura_id, factura_id, usuario or "", conn=c):
            raise TransicionInvalida(
                f"La pre factura {pf['numero_interno']} está {pf['estado']}: no se factura.")
        return bandeja.get_comprobante(pre_factura_id, conn=c)


# ── PDF y correo ───────────────────────────────────────────────────────────────


def _emisor_para_el_pdf(c, pf: dict, emisor: dict | None) -> dict | None:
    """Los datos del emisor para el PDF: lo que pasó quien llama, o —si la pre factura
    tiene `emisor_id`— el nombre y el CUIT de esa configuración de ARCA. El domicilio y la
    condición de IVA no están en `arca_config`: salen de la configuración de la instancia
    (`config_manager`), y un producto con varias razones sociales los pasa en `emisor`."""
    if emisor is not None:
        return emisor
    if pf.get("emisor_id") is None:
        return None
    fila = c.execute("SELECT empresa, cuit FROM arca_config WHERE id=?", (pf["emisor_id"],)).fetchone()
    return {"nombre": fila[0], "cuit": fila[1]} if fila else None


def pdf(pre_factura_id: int, *, emisor: dict | None = None, conn: Conexion | None = None) -> bytes:
    """El PDF de la pre factura: el aspecto de la factura, con el sello **PRE FACTURA —
    NO VÁLIDA COMO COMPROBANTE FISCAL**, el número interno y sin punto de venta, CAE ni
    QR. `emisor` pisa los datos del emisor (`nombre`, `cuit`, `direccion`,
    `iva_condition`, `iibb`, `inicio_actividades`, `logo_path`); ver `_emisor_para_el_pdf`.
    Se puede pedir de cualquier estado."""
    with _con(conn) as c:
        pf = _cargar(pre_factura_id, c)
        return pdf_generator.generate_pdf_pre_factura(pf, _emisor_para_el_pdf(c, pf, emisor))


def enviar_por_correo(pre_factura_id: int, destinatario: str, *, smtp_resolver=None,
                      asunto: str = "", cuerpo: str = "", emisor: dict | None = None,
                      conn: Conexion | None = None) -> dict:
    """Manda el PDF a `destinatario` y la marca enviada. Devuelve la pre factura.

    El SMTP se resuelve como en el resto del motor, con `facturas_router.smtp_efectivo`:
    el `smtp_resolver` del producto (el de libraauth) si lo hay, y `config.json` si no.
    Sin servidor, `SmtpNoConfigurado`. Si el envío falla (cualquier excepción de
    `smtplib`) **sube y la pre factura queda como estaba**: el estado sigue al hecho.
    """
    destinatario = str(destinatario or "").strip()
    if not destinatario:
        raise ValueError("Ingresá una dirección de correo.")
    with _con(conn) as c:
        pf = _cargar(pre_factura_id, c)
        if pf["estado"] not in ESTADOS_ABIERTOS:
            raise TransicionInvalida(
                f"La pre factura {pf['numero_interno']} está {pf['estado']}: no se envía.")
        smtp = smtp_efectivo(smtp_resolver)
        if not (smtp["host"] and smtp["user"]):
            raise SmtpNoConfigurado("No hay un servidor SMTP configurado para mandar correos.")
        datos_emisor = _emisor_para_el_pdf(c, pf, emisor)
        documento = pdf_generator.generate_pdf_pre_factura(pf, datos_emisor)
        empresa = (datos_emisor or {}).get("nombre") or config_manager.load().get("empresa_nombre", "")
        numero = pf["numero_interno"]
        email_sender.enviar_documento(
            to_email=destinatario, to_name=pf["cliente_razon"], pdf_path="",
            asunto=asunto or f"Pre factura {numero} - {empresa}".rstrip(" -"),
            cuerpo=cuerpo or (
                f"Estimado/a {pf['cliente_razon']},\n\n"
                f"Le enviamos la pre factura {numero} para que confirme los datos.\n\n"
                f"Total: $ {pdf_generator._ar(pf['total'])}\n\n"
                "Este documento no es una factura ni tiene valor fiscal: la factura se "
                "emite cuando nos confirme que está de acuerdo.\n\n"
                f"Muchas gracias.\n{empresa}"),
            smtp_host=smtp["host"], smtp_port=smtp["port"], smtp_user=smtp["user"],
            smtp_password=smtp["password"], from_email=smtp["from_email"],
            from_name=smtp["from_name"], filename=f"{numero}.pdf", pdf_bytes=documento)
        return marcar_enviada(pre_factura_id, destinatario, conn=c)
