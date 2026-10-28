"""El registro de los avisos por mail y WhatsApp: alta, toma, cierre y listado (ADR-090).

Una fila es **un aviso**, no un intento: `(canal, clave)` es UNIQUE y `intentos` cuenta las veces que se trató de mandar. La
clave la pone quien avisa («reserva:41:recordatorio:24») y es lo que hace que un barrido que corre dos veces, o dos barridos a la
vez, no le manden lo mismo al cliente. La lógica de qué hacer con cada estado vive en `libracore.avisos.despachador`; acá sólo hay
filas y transiciones, igual que `libracore.db.cola_arca`.

- 🔑 **El UNIQUE manda, no el `if`.** Se *reserva* la fila (`reservar`, `INSERT OR IGNORE`) **antes** de salir a la red, y sólo
  quien la reservó manda. Dos procesos que pasan a la vez por el mismo aviso: uno gana el `INSERT` y el otro se entera por el
  `rowcount`. El motor de avisos de LibraClub anotaba *después* de mandar, y en esa ventana los dos mandaban.
- **`tomar` es una compare-and-swap.** Reintentar un aviso `fallido` (o rescatar uno que quedó `enviando` porque el proceso murió
  a mitad del envío) se hace con un `UPDATE ... WHERE estado=? AND intentos=?`: si dos lo intentan, uno solo ve `rowcount == 1`.
- Con `conn`, las funciones trabajan dentro de la transacción de quien llama y no confirman nada (ADR-025). El despachador no
  pasa `conn`: **cada paso se confirma solo**, porque la reserva tiene que ser visible para los demás antes de mandar.
"""
import json

from libracore.db.core import Conexion, _ar_now
from libracore.db.facturas import _con

ESTADO_ENVIANDO = "enviando"
ESTADO_ENVIADO = "enviado"
ESTADO_REGISTRADO = "registrado"
ESTADO_FALLIDO = "fallido"
ESTADO_RECHAZADO = "rechazado"
ESTADO_OMITIDO = "omitido"

ESTADOS = (ESTADO_ENVIANDO, ESTADO_ENVIADO, ESTADO_REGISTRADO, ESTADO_FALLIDO, ESTADO_RECHAZADO, ESTADO_OMITIDO)

#: Cuánto se guarda del motivo de un fallo. Es para que una persona lo lea en la pantalla, no un log.
LARGO_DEL_DETALLE = 500

_CAMPOS_DE_LISTADO = ("id, canal, clave, tipo, destino, asunto, cuerpo, plantilla, variables, proveedor, estado, intentos, "
                      "detalle, id_externo, enviado_at, created_at, updated_at")


def _fila(row) -> dict:
    d = dict(row)
    try:
        d["variables"] = json.loads(d.get("variables") or "{}")
    except ValueError:
        d["variables"] = {}
    return d


def obtener(canal: str, clave: str, *, conn: Conexion | None = None) -> dict | None:
    """El aviso de esa `(canal, clave)` en cualquier estado, o `None`."""
    with _con(conn) as c:
        row = c.execute(f"SELECT {_CAMPOS_DE_LISTADO} FROM avisos_envios WHERE canal=? AND clave=?", (canal, clave)).fetchone()
    return _fila(row) if row else None


def obtener_varios(canal: str, claves, *, conn: Conexion | None = None) -> dict[str, dict]:
    """`{clave: aviso}` de las claves que existen en ese canal. De a 500 por consulta: un barrido pregunta por cientos."""
    claves = list(dict.fromkeys(claves))
    encontrados: dict[str, dict] = {}
    with _con(conn) as c:
        for i in range(0, len(claves), 500):
            tanda = claves[i:i + 500]
            rows = c.execute(
                f"SELECT {_CAMPOS_DE_LISTADO} FROM avisos_envios WHERE canal=? AND clave IN ({','.join('?' * len(tanda))})",
                (canal, *tanda)).fetchall()
            encontrados.update({r["clave"]: _fila(r) for r in rows})
    return encontrados


def reservar(canal: str, clave: str, *, tipo: str, destino: str, asunto: str, cuerpo: str, plantilla: str,
             variables: dict, proveedor: str, conn: Conexion | None = None) -> int | None:
    """Deja el aviso en `enviando` con su primer intento. Devuelve el `id`, o `None` si ya había uno (lo ganó otro).

    `INSERT OR IGNORE` y no el `except IntegrityError`: con una conexión de quien llama, un error aborta su transacción en
    PostgreSQL. El `rowcount` dice quién ganó la carrera."""
    with _con(conn) as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO avisos_envios (canal, clave, tipo, destino, asunto, cuerpo, plantilla, variables, "
            "proveedor, estado, intentos, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
            (canal, clave, tipo, destino, asunto, cuerpo, plantilla, json.dumps(variables, ensure_ascii=False),
             proveedor, ESTADO_ENVIANDO, _ar_now()))
        if cur.rowcount != 1:
            return None
        return dict(c.execute("SELECT id FROM avisos_envios WHERE canal=? AND clave=?", (canal, clave)).fetchone())["id"]


def tomar(fila: dict, *, tipo: str, destino: str, asunto: str, cuerpo: str, plantilla: str, variables: dict,
          proveedor: str, vencido_antes_de: str | None = None, reiniciar: bool = False,
          conn: Conexion | None = None) -> bool:
    """Pasa un aviso ya existente a `enviando`, contando el intento y con el mensaje de ahora. `False` si lo ganó otro.

    `fila` es lo que se leyó con `obtener`: la toma vale **sólo si nadie lo tocó desde entonces** (mismo `estado` y mismos
    `intentos`). `vencido_antes_de` es para rescatar un `enviando` viejo: además exige que su `updated_at` sea anterior.
    `reiniciar` vuelve el contador a 1: es para un `registrado` que pasa a un proveedor real, cuyo «intento» en modo prueba no
    gastó ninguno de los de verdad."""
    cuenta = "1" if reiniciar else "intentos+1"
    sql = (f"UPDATE avisos_envios SET estado=?, intentos={cuenta}, tipo=?, destino=?, asunto=?, cuerpo=?, plantilla=?, "
           "variables=?, proveedor=?, detalle='', id_externo='', enviado_at='', updated_at=? "
           "WHERE id=? AND estado=? AND intentos=?")
    args = [ESTADO_ENVIANDO, tipo, destino, asunto, cuerpo, plantilla, json.dumps(variables, ensure_ascii=False), proveedor,
            _ar_now(), fila["id"], fila["estado"], fila["intentos"]]
    if vencido_antes_de is not None:
        sql += " AND updated_at < ?"
        args.append(vencido_antes_de)
    with _con(conn) as c:
        return (c.execute(sql, tuple(args)).rowcount or 0) == 1


def cerrar(aviso_id: int, estado: str, *, detalle: str = "", id_externo: str = "", conn: Conexion | None = None) -> bool:
    """`enviando` → `estado`, con el motivo y el id que le dio el proveedor. Sólo vale desde `enviando`."""
    if estado not in ESTADOS or estado == ESTADO_ENVIANDO:
        raise ValueError(f"estado de cierre inválido: {estado!r}")
    enviado_at = _ar_now() if estado == ESTADO_ENVIADO else ""
    with _con(conn) as c:
        cur = c.execute(
            "UPDATE avisos_envios SET estado=?, detalle=?, id_externo=?, enviado_at=?, updated_at=? "
            "WHERE id=? AND estado=?",
            (estado, (detalle or "")[:LARGO_DEL_DETALLE], id_externo or "", enviado_at, _ar_now(), aviso_id, ESTADO_ENVIANDO))
        return (cur.rowcount or 0) == 1


def anotar_omitido(canal: str, clave: str, *, tipo: str, destino: str, motivo: str, conn: Conexion | None = None) -> bool:
    """Deja un aviso `omitido` (no correspondía mandarlo: el cliente no tiene teléfono, pidió no recibir). Idempotente.

    `True` si lo anotó ahora; `False` si ya había un aviso de esa clave (no se pisa)."""
    with _con(conn) as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO avisos_envios (canal, clave, tipo, destino, proveedor, estado, intentos, detalle, "
            "updated_at) VALUES (?,?,?,?,'',?,0,?,?)",
            (canal, clave, tipo, destino, ESTADO_OMITIDO, (motivo or "")[:LARGO_DEL_DETALLE], _ar_now()))
        return (cur.rowcount or 0) == 1


def listar(*, canal: str | None = None, estado: str | None = None, tipo: str | None = None, limite: int = 100,
           conn: Conexion | None = None) -> list[dict]:
    """Los avisos, el más reciente primero."""
    donde, args = [], []
    for columna, valor in (("canal", canal), ("estado", estado), ("tipo", tipo)):
        if valor:
            donde.append(f"{columna}=?")
            args.append(valor)
    sql = f"SELECT {_CAMPOS_DE_LISTADO} FROM avisos_envios"
    if donde:
        sql += " WHERE " + " AND ".join(donde)
    with _con(conn) as c:
        rows = c.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, int(limite))).fetchall()
    return [_fila(r) for r in rows]


def a_reintentar(canal: str, *, max_intentos: int, actualizado_desde: str, vencido_antes_de: str,
                 fallido_antes_de: str | None = None, limite: int = 100, conn: Conexion | None = None) -> list[dict]:
    """Los `fallido` con intentos de sobra y los `enviando` abandonados de ese canal, el más viejo primero.

    `actualizado_desde` acota por antigüedad (un recordatorio de hace tres días ya no sirve) y `vencido_antes_de` es el piso de
    un `enviando` para darlo por abandonado. `fallido_antes_de` es la espera entre intentos: un `fallido` cuyo último intento
    (`updated_at`) es posterior no se devuelve todavía. Sin él, no hay espera."""
    sql = (f"SELECT {_CAMPOS_DE_LISTADO} FROM avisos_envios WHERE canal=? AND updated_at >= ? AND "
           "((estado=? AND intentos < ?{espera}) OR (estado=? AND updated_at < ? AND intentos < ?)) ORDER BY id LIMIT ?")
    args = [canal, actualizado_desde, ESTADO_FALLIDO, int(max_intentos)]
    if fallido_antes_de is not None:
        args.append(fallido_antes_de)
    args += [ESTADO_ENVIANDO, vencido_antes_de, int(max_intentos), int(limite)]
    with _con(conn) as c:
        rows = c.execute(sql.format(espera=" AND updated_at <= ?" if fallido_antes_de is not None else ""),
                         tuple(args)).fetchall()
    return [_fila(r) for r in rows]


#: El motivo con el que se cierra un `enviando` abandonado al que ya no le quedan intentos.
DETALLE_DEL_ABANDONO = "El envío quedó a medias (el proceso se interrumpió) y ya no quedan intentos."


def cerrar_abandonados_sin_intentos(canal: str, *, max_intentos: int, vencido_antes_de: str, clave: str | None = None,
                                    conn: Conexion | None = None) -> int:
    """Pasa a `fallido` (terminal: ya no le quedan intentos) los `enviando` abandonados con `intentos >= max_intentos`.

    Sin esto quedan `enviando` para siempre: `despachar` no los rescata (no hay intentos) y `a_reintentar` tampoco, así que ni
    `cerrado` los da por cerrados ni el listado cuenta lo que pasó. `vencido_antes_de` es el mismo piso que en `a_reintentar`; `clave`
    acota a un solo aviso. Devuelve cuántos cerró."""
    sql = "UPDATE avisos_envios SET estado=?, detalle=?, updated_at=? WHERE canal=? AND estado=? AND updated_at < ? AND intentos >= ?"
    args = [ESTADO_FALLIDO, DETALLE_DEL_ABANDONO, _ar_now(), canal, ESTADO_ENVIANDO, vencido_antes_de, int(max_intentos)]
    if clave is not None:
        sql += " AND clave=?"
        args.append(clave)
    with _con(conn) as c:
        return c.execute(sql, tuple(args)).rowcount or 0
