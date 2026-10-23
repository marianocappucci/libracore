"""
La cola de comprobantes de un nodo offline (ADR-079): alta idempotente, listados por estado y las transiciones.

Un pedido **no es un comprobante**. Es la intención de emitir uno (una factura que no nace de una venta del punto de
venta, o una carta de porte electrónica) que el producto dejó porque no había red. No tiene número, ni CAE, ni CTG
hasta que el nodo lo emite; la lógica que lo emite (el candado, el orden, qué hacer con cada error) vive en
`libracore.cola_arca`. Este módulo sólo escribe y lee filas, igual que `libracore.db.comprobantes_pendientes`.

**Un solo escritor de `estado`**: `_mover`. Cada transición dice desde qué estados vale (`WHERE estado IN (…)`), así que
dos procesos que quieren mover el mismo pedido no lo pisan: gana uno y el otro recibe `False`. Cada cambio sube
`revision`, que es con lo que el central sabe qué copia es la más nueva (`aplicar_copia`).

Estados::

    esperando ──tomar──▶ emitiendo ──▶ emitido
        ▲                    ├──────▶ a_revisar   (ARCA rechazó o el emisor falló: lo mira una persona)
        │                    └──────▶ incierto    (no se sabe si ARCA lo emitió: NO se reintenta a ciegas)
        └── reabrir ◀── a_revisar / incierto        descartado (lo cierra una persona, desde cualquier estado abierto)

Con `conn`, las funciones trabajan dentro de la transacción de quien llama y no confirman nada (idioma de
`libracore.db`, ADR-025): es lo que deja a un producto guardar su orden y encolar la factura en una sola transacción.
"""
import json

from libracore.db.core import Conexion
from libracore.db.facturas import _con

ESTADO_ESPERANDO = "esperando"
ESTADO_EMITIENDO = "emitiendo"
ESTADO_EMITIDO = "emitido"
ESTADO_A_REVISAR = "a_revisar"
ESTADO_INCIERTO = "incierto"
ESTADO_DESCARTADO = "descartado"

ESTADOS = (ESTADO_ESPERANDO, ESTADO_EMITIENDO, ESTADO_EMITIDO, ESTADO_A_REVISAR, ESTADO_INCIERTO, ESTADO_DESCARTADO)

#: Lo que todavía no terminó. `emitido` y `descartado` son finales: no se mueven más.
ESTADOS_ABIERTOS = (ESTADO_ESPERANDO, ESTADO_EMITIENDO, ESTADO_A_REVISAR, ESTADO_INCIERTO)
ESTADOS_FINALES = (ESTADO_EMITIDO, ESTADO_DESCARTADO)
#: Lo que una persona tiene que mirar.
ESTADOS_DE_REVISION = (ESTADO_A_REVISAR, ESTADO_INCIERTO)

#: Una factura que no nace de una venta del punto de venta (la de LibraCargo desde una orden, la conjunta de LibraClub).
ORIGEN_DOCUMENTO = "documento"
#: Una carta de porte electrónica (servicio `wscpe`).
ORIGEN_CPE = "cpe"
#: Lista cerrada a propósito, como la de la bandeja: un tipo libre convierte la cola en un buzón donde nadie sabe qué
#: emisor corresponde. Un tipo nuevo se suma acá y en el `ColaDeArca` del producto.
ORIGENES = (ORIGEN_DOCUMENTO, ORIGEN_CPE)

#: El largo del mensaje que se guarda. ARCA contesta cosas largas y la pantalla muestra una línea.
LARGO_DEL_ERROR = 500


class PedidoYaResuelto(Exception):
    """El producto volvió a pedir algo que acá ya salió, se descartó o espera que una persona lo mire.

    No es un error del producto: reintentar es lo correcto cuando se corta la red. Lo que no puede pasar es que un
    reintento automático pise lo que ya se emitió o reabra solo lo que ARCA rechazó. `estado` dice dónde está."""

    def __init__(self, mensaje: str, *, estado: str, pedido_id: int):
        super().__init__(mensaje)
        self.estado = estado
        self.pedido_id = pedido_id


def _json(valor, nombre: str, *, lenient: bool = False) -> str:
    """El JSON de `datos` o `resultado`. **Estricto** con `datos`: un `Decimal` o una fecha sin convertir es un error del
    producto, y se dice al encolar. `lenient` (el `resultado`) pasa lo que no es JSON por `str`: el comprobante ya se
    emitió y perder el registro por un tipo raro es peor que guardarlo como texto."""
    if not isinstance(valor, dict):
        raise TypeError(f"{nombre} tiene que ser un dict JSON, no {type(valor).__name__}")
    return json.dumps(valor, ensure_ascii=False, default=str if lenient else None)


def _fila(row) -> dict:
    d = dict(row)
    d["datos"] = json.loads(d["datos"] or "{}")
    d["resultado"] = json.loads(d["resultado"]) if d.get("resultado") else None
    return d


def _recortar(texto) -> str:
    return str(texto or "")[:LARGO_DEL_ERROR]


def _validar_origen(origen_producto, origen_tipo, origen_id) -> None:
    if origen_tipo not in ORIGENES:
        raise ValueError(f"origen_tipo invalido: {origen_tipo!r} (esperado uno de {ORIGENES})")
    if not str(origen_producto or "").strip():
        raise ValueError("origen_producto es obligatorio: sin el, la cola no puede decir de donde vino la fila")
    if not str(origen_id or "").strip():
        raise ValueError("origen_id es obligatorio: es lo que hace idempotente el reenvio")


# ── Alta ─────────────────────────────────────────────────────────────────


def encolar(origen_producto: str, origen_instancia: str, origen_tipo: str, origen_id, *, datos: dict,
            fecha_ref: str = "", concepto: int = 1, conn: Conexion | None = None) -> tuple[int, bool]:
    """Deja un pedido en la cola. Devuelve `(id, creado)`.

    **Idempotente por origen**: `(origen_producto, origen_instancia, origen_tipo, origen_id)` tiene un UNIQUE, así que
    pedir lo mismo dos veces no duplica. Qué hace ante un reenvío:

    - Si sigue `esperando`, **actualiza los datos** y devuelve `creado=False`: lo último que mandó el producto es lo que
      vale (el importe o el chofer pueden haber cambiado entre un intento y el siguiente).
    - Si está en cualquier otro estado, **no toca nada** y levanta `PedidoYaResuelto`. Un pedido `emitiendo` lo tiene otro;
      uno `emitido` ya salió; uno `a_revisar` o `incierto` lo reabre una persona (`reabrir`), no un reintento.

    `datos` es el JSON que el emisor del producto necesita para armar el comprobante (lo ideal es poco: el id de su
    entidad y lo que no se puede releer después). `fecha_ref` es la fecha del hecho (ISO) o, en una `cpe`, la de partida;
    `concepto` el de ARCA (1 productos, 2 servicios, 3 ambos) y manda sobre el plazo de la fecha de una factura.
    """
    _validar_origen(origen_producto, origen_tipo, origen_id)
    cuerpo = _json(datos, "datos")
    clave = (origen_producto, origen_instancia or "", origen_tipo, str(origen_id))
    with _con(conn) as c:
        for _ in range(2):
            fila = c.execute(
                "SELECT id, estado FROM cola_arca WHERE origen_producto=? AND origen_instancia=? "
                "AND origen_tipo=? AND origen_id=?", clave).fetchone()
            if fila is not None:
                fila = dict(fila)
                if fila["estado"] != ESTADO_ESPERANDO:
                    raise PedidoYaResuelto(
                        f"El pedido {origen_tipo}:{origen_id} de {origen_producto} ya esta {fila['estado']}",
                        estado=fila["estado"], pedido_id=fila["id"])
                c.execute(
                    "UPDATE cola_arca SET datos=?, fecha_ref=?, concepto=?, revision=revision+1, "
                    "updated_at=datetime('now','-3 hours') WHERE id=? AND estado=?",
                    (cuerpo, fecha_ref or "", int(concepto), fila["id"], ESTADO_ESPERANDO))
                return fila["id"], False
            # `OR IGNORE` y no el `except IntegrityError` de la bandeja: con una conexión de quien llama, un error
            # aborta su transacción en PostgreSQL. Si otro ganó la carrera, el UNIQUE lo ignora y se vuelve a leer.
            cur = c.execute(
                "INSERT OR IGNORE INTO cola_arca (origen_producto, origen_instancia, origen_tipo, origen_id, "
                "estado, datos, fecha_ref, concepto) VALUES (?,?,?,?,?,?,?,?)",
                (*clave, ESTADO_ESPERANDO, cuerpo, fecha_ref or "", int(concepto)))
            if cur.rowcount == 1:
                nuevo = c.execute(
                    "SELECT id FROM cola_arca WHERE origen_producto=? AND origen_instancia=? "
                    "AND origen_tipo=? AND origen_id=?", clave).fetchone()
                return dict(nuevo)["id"], True
    raise RuntimeError("cola_arca: no se pudo encolar ni leer el pedido de la carrera")  # pragma: no cover


# ── Lectura ──────────────────────────────────────────────────────────────


def get_pedido(pedido_id: int, *, conn: Conexion | None = None) -> dict | None:
    with _con(conn) as c:
        row = c.execute("SELECT * FROM cola_arca WHERE id=?", (pedido_id,)).fetchone()
        return _fila(row) if row else None


def get_por_origen(origen_producto: str, origen_instancia: str, origen_tipo: str, origen_id, *,
                   conn: Conexion | None = None) -> dict | None:
    """La fila de un origen (la cuádrupla del UNIQUE), en cualquier estado, o `None`."""
    with _con(conn) as c:
        row = c.execute(
            "SELECT * FROM cola_arca WHERE origen_producto=? AND origen_instancia=? AND origen_tipo=? AND origen_id=?",
            (origen_producto, origen_instancia or "", origen_tipo, str(origen_id))).fetchone()
        return _fila(row) if row else None


def listar(*, estados: tuple[str, ...] | None = None, origen_producto: str | None = None,
           origen_instancia: str | None = None, origen_tipo: str | None = None,
           limite: int | None = None,
           mas_nuevos_primero: bool = False, conn: Conexion | None = None) -> list[dict]:
    """Los pedidos que cumplen los filtros, **los más viejos primero** (por id) salvo `mas_nuevos_primero`.

    Es el orden en que se emiten: una factura que nació antes sale antes."""
    sql, params = "SELECT * FROM cola_arca WHERE 1=1", []
    if estados is not None:
        malos = [e for e in estados if e not in ESTADOS]
        if malos:
            raise ValueError(f"estado invalido: {malos!r} (esperado uno de {ESTADOS})")
        sql += f" AND estado IN ({','.join('?' * len(estados))})"
        params += list(estados)
    for columna, valor in (("origen_producto", origen_producto), ("origen_instancia", origen_instancia),
                           ("origen_tipo", origen_tipo)):
        if valor is not None:
            sql += f" AND {columna}=?"
            params.append(valor)
    sql += " ORDER BY id DESC" if mas_nuevos_primero else " ORDER BY id"
    if limite is not None:
        sql += " LIMIT ?"
        params.append(int(limite))
    with _con(conn) as c:
        return [_fila(r) for r in c.execute(sql, tuple(params)).fetchall()]


def contar_por_estado(origen_producto: str | None = None, origen_instancia: str | None = None, *,
                      conn: Conexion | None = None) -> dict[str, int]:
    """`{estado: cantidad}` con los seis estados siempre presentes (en 0 los que no tienen filas)."""
    sql, params = "SELECT estado, COUNT(*) FROM cola_arca WHERE 1=1", []
    for columna, valor in (("origen_producto", origen_producto), ("origen_instancia", origen_instancia)):
        if valor is not None:
            sql += f" AND {columna}=?"
            params.append(valor)
    with _con(conn) as c:
        filas = c.execute(sql + " GROUP BY estado", tuple(params)).fetchall()
    cuentas = dict.fromkeys(ESTADOS, 0)
    for f in filas:
        cuentas[f[0]] = f[1]
    return cuentas


# ── Transiciones ─────────────────────────────────────────────────────────


def _mover(pedido_id: int, desde: tuple[str, ...], hacia: str, *, error: str | None = None,
           resultado: dict | None = None, datos: dict | None = None, intentos: str = "igual",
           final: bool = False, conn: Conexion | None = None) -> bool:
    """**El único escritor de `estado`.** Mueve el pedido sólo si está en uno de `desde`; `False` si no (ya lo movió otro
    o no existe). Sube `revision`. `intentos` es `"mas"` (+1), `"menos"` (-1, sin bajar de 0) o `"igual"`."""
    sets = ["estado=?", "revision=revision+1", "updated_at=datetime('now','-3 hours')"]
    valores: list = [hacia]
    if error is not None:
        sets.append("ultimo_error=?")
        valores.append(_recortar(error))
    if resultado is not None:
        sets.append("resultado=?")
        valores.append(_json(resultado, "resultado", lenient=True))
    if datos is not None:
        sets.append("datos=?")
        valores.append(_json(datos, "datos"))
    if intentos == "mas":
        sets.append("intentos=intentos+1")
    elif intentos == "menos":
        sets.append("intentos=CASE WHEN intentos > 0 THEN intentos - 1 ELSE 0 END")
    if final:
        sets.append("resuelto_at=datetime('now','-3 hours')")
    marcas = ",".join("?" * len(desde))
    with _con(conn) as c:
        cur = c.execute(f"UPDATE cola_arca SET {', '.join(sets)} WHERE id=? AND estado IN ({marcas})",
                        (*valores, pedido_id, *desde))
        return cur.rowcount > 0


def tomar_para_emitir(pedido_id: int, *, conn: Conexion | None = None) -> bool:
    """`esperando` → `emitiendo`, contando el intento. **Se confirma antes de llamar al emisor**: si el proceso muere a
    mitad de la emisión, la fila queda `emitiendo` y el próximo en tomar el candado sabe que quedó a medias."""
    return _mover(pedido_id, (ESTADO_ESPERANDO,), ESTADO_EMITIENDO, intentos="mas", conn=conn)


def devolver_a_esperando(pedido_id: int, error: str, *, conn: Conexion | None = None) -> bool:
    """`emitiendo` → `esperando` sin consumir el intento: un corte de red o un ticket que el central no presta no dicen
    nada del pedido. El motivo queda en `ultimo_error`."""
    return _mover(pedido_id, (ESTADO_EMITIENDO,), ESTADO_ESPERANDO, error=error, intentos="menos", conn=conn)


def marcar_emitido(pedido_id: int, resultado: dict, *, conn: Conexion | None = None) -> bool:
    """→ `emitido`, con el resultado (`factura_id`/`cae`, o el CTG). Vale desde `emitiendo` (el camino normal) y desde
    `incierto` o `a_revisar` (una persona comprobó en ARCA que sí salió, o autorizó la factura a mano)."""
    return _mover(pedido_id, (ESTADO_EMITIENDO, ESTADO_INCIERTO, ESTADO_A_REVISAR), ESTADO_EMITIDO,
                  error="", resultado=resultado, final=True, conn=conn)


def marcar_a_revisar(pedido_id: int, error: str, resultado: dict | None = None, *,
                     conn: Conexion | None = None) -> bool:
    """`emitiendo` → `a_revisar`: ARCA rechazó el pedido o el emisor falló. El mensaje es lo que ve la persona."""
    return _mover(pedido_id, (ESTADO_EMITIENDO,), ESTADO_A_REVISAR, error=error, resultado=resultado, conn=conn)


def marcar_incierto(pedido_id: int, error: str, *, conn: Conexion | None = None) -> bool:
    """`emitiendo` → `incierto`: no se sabe si ARCA lo emitió. 🔴 No se reintenta: se consulta antes (`reabrir`)."""
    return _mover(pedido_id, (ESTADO_EMITIENDO,), ESTADO_INCIERTO, error=error, conn=conn)


def reabrir(pedido_id: int, *, datos: dict | None = None, conn: Conexion | None = None) -> bool:
    """`a_revisar` o `incierto` → `esperando`, con `datos` corregidos si se pasan. Lo decide una persona: en un
    `incierto` es decir «consulté en ARCA y no salió»."""
    return _mover(pedido_id, ESTADOS_DE_REVISION, ESTADO_ESPERANDO, error="", datos=datos, conn=conn)


def descartar(pedido_id: int, motivo: str = "", *, conn: Conexion | None = None) -> bool:
    """→ `descartado` desde `esperando`, `a_revisar` o `incierto`. **No desde `emitiendo`**: lo está emitiendo alguien."""
    return _mover(pedido_id, (ESTADO_ESPERANDO, ESTADO_A_REVISAR, ESTADO_INCIERTO), ESTADO_DESCARTADO,
                  error=motivo or "", final=True, conn=conn)


# ── El central ───────────────────────────────────────────────────────────


def aplicar_copia(pedido: dict, *, conn: Conexion | None = None) -> tuple[int, bool]:
    """**En el central.** Escribe la copia de un pedido del nodo (`cola_arca.payload_de_pedido`). Devuelve `(id, aplicada)`.

    Idempotente y a prueba de desorden: el outbox entrega al menos una vez y no necesariamente en orden, así que sólo
    pisa si la `revision` que llega es **mayor** que la que ya está. `aplicada=False` es «ya tenía algo igual o más nuevo».
    La clave incluye `origen_instancia` (el id del nodo): dos nodos no se cruzan."""
    clave = (pedido["origen_producto"], pedido["origen_instancia"], pedido["origen_tipo"], str(pedido["origen_id"]))
    _validar_origen(clave[0], clave[2], clave[3])
    if pedido["estado"] not in ESTADOS:
        raise ValueError(f"estado invalido: {pedido['estado']!r} (esperado uno de {ESTADOS})")
    campos = (pedido["estado"], _json(pedido.get("datos") or {}, "datos"), pedido.get("fecha_ref") or "",
              int(pedido.get("concepto") or 1), int(pedido.get("intentos") or 0), _recortar(pedido.get("ultimo_error")),
              _json(pedido["resultado"], "resultado") if pedido.get("resultado") else "", int(pedido["revision"]),
              pedido.get("resuelto_at") or "")
    with _con(conn) as c:
        existente = c.execute(
            "SELECT id, revision FROM cola_arca WHERE origen_producto=? AND origen_instancia=? AND origen_tipo=? "
            "AND origen_id=?", clave).fetchone()
        if existente is None:
            cur = c.execute(
                "INSERT OR IGNORE INTO cola_arca (origen_producto, origen_instancia, origen_tipo, origen_id, estado, "
                "datos, fecha_ref, concepto, intentos, ultimo_error, resultado, revision, resuelto_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (*clave, *campos))
            existente = c.execute(
                "SELECT id, revision FROM cola_arca WHERE origen_producto=? AND origen_instancia=? "
                "AND origen_tipo=? AND origen_id=?", clave).fetchone()
            if cur.rowcount == 1:
                return dict(existente)["id"], True
        existente = dict(existente)
        if int(pedido["revision"]) <= existente["revision"]:
            return existente["id"], False
        c.execute(
            "UPDATE cola_arca SET estado=?, datos=?, fecha_ref=?, concepto=?, intentos=?, ultimo_error=?, resultado=?, "
            "revision=?, resuelto_at=?, updated_at=datetime('now','-3 hours') WHERE id=? AND revision < ?",
            (*campos, existente["id"], campos[7]))
        return existente["id"], True
