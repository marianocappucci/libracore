"""Los valores de los índices de ajuste (ICL, IPC, CER, UVA, dólar): alta, lectura y listado. ADR-085.

Una fila es **un valor publicado de un índice en una fecha**: el ICL del 26-10-2026, el IPC de septiembre. Son datos
del país, no de un producto: los usa quien ajusta un precio por índice (el alquiler de LibraInmo, el contrato con
`metodo_actualizacion='indice'` de LibraDesk). Qué índices hay, de dónde salen y cómo se calcula un ajuste vive en
`libracore.indices_ajuste`; bajarlos de la fuente oficial, en `libracore.indices_descarga`. Este módulo sólo escribe y
lee filas, igual que `libracore.db.comprobantes_pendientes`.

- 🔑 **El valor se guarda como texto decimal, tal cual lo publicó la fuente.** No es plata (no va a
  `COLUMNAS_DE_DINERO`) y tampoco puede ser `REAL`: el CER se publica con once decimales y un ajuste que se recalcula
  después tiene que dar exactamente lo mismo. Se lee como `Decimal`.
- **Lo cargado a mano no lo pisa una descarga.** Una persona cargó ese valor por algo (la fuente estaba caída, el
  contrato pacta otro): `guardar(..., pisar_manual=False)`, que es lo que usa la descarga, lo deja y avisa si difiere.
- La fecha de un índice mensual es el **primer día del mes** (`2026-09-01` es el IPC de septiembre), sin importar con
  qué día lo publique la fuente. La normaliza `libracore.indices_ajuste.fecha_del_indice`.

Con `conn`, las funciones trabajan dentro de la transacción de quien llama y no confirman nada (ADR-025).
"""
from decimal import Decimal, InvalidOperation

from libracore.db.core import Conexion
from libracore.db.facturas import _con

FUENTE_MANUAL = "manual"


class ValorInvalido(ValueError):
    """El valor no se puede guardar así. El mensaje dice por qué."""


def _decimal(valor) -> Decimal:
    if isinstance(valor, float):
        # Un float ya perdió los decimales que importan (0.1 no es 0.1): quien llama pasa texto o Decimal.
        raise ValorInvalido("El valor de un índice no se pasa como float: usar texto o Decimal.")
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except (InvalidOperation, AttributeError) as exc:
        raise ValorInvalido(f"«{valor}» no es un número.") from exc
    if not d.is_finite() or d <= 0:
        raise ValorInvalido("El valor de un índice es un número mayor que cero.")
    return d


def _texto(d: Decimal) -> str:
    """El decimal sin exponente ni ceros de cola: `Decimal('37.09000000000')` → `'37.09'`."""
    t = format(d, "f")
    return t.rstrip("0").rstrip(".") if "." in t else t


def _fila(row) -> dict:
    d = dict(row)
    d["valor"] = Decimal(d["valor"])
    return d


def guardar(indice: str, fecha: str, valor, fuente: str, *, usuario_id: int | None = None,
            pisar_manual: bool = True, conn: Conexion | None = None) -> str:
    """Guarda el valor de `indice` en `fecha`. Devuelve qué pasó.

    `'insertado'`, `'actualizado'`, `'igual'` (ya estaba con ese valor) o `'manual_distinto'` (había uno cargado a
    mano, distinto, y `pisar_manual=False` lo dejó)."""
    if not indice or not fecha or not fuente:
        raise ValorInvalido("Faltan el índice, la fecha o la fuente.")
    nuevo = _decimal(valor)
    with _con(conn) as c:
        previo = c.execute(
            "SELECT id, valor, fuente FROM indices_ajuste WHERE indice=? AND fecha=?", (indice, fecha)
        ).fetchone()
        if previo is None:
            c.execute(
                "INSERT INTO indices_ajuste (indice, fecha, valor, fuente, usuario_id) VALUES (?, ?, ?, ?, ?)",
                (indice, fecha, _texto(nuevo), fuente, usuario_id),
            )
            return "insertado"
        previo = dict(previo)
        if Decimal(previo["valor"]) == nuevo:
            return "igual"
        if previo["fuente"] == FUENTE_MANUAL and not pisar_manual:
            return "manual_distinto"
        c.execute(
            "UPDATE indices_ajuste SET valor=?, fuente=?, usuario_id=?, updated_at=datetime('now','-3 hours') "
            "WHERE id=?",
            (_texto(nuevo), fuente, usuario_id, previo["id"]),
        )
        return "actualizado"


def obtener(indice: str, fecha: str, *, conn: Conexion | None = None) -> dict | None:
    """El valor de `indice` en esa fecha exacta, o `None`. No busca el anterior: eso es `ultimo_hasta`."""
    with _con(conn) as c:
        row = c.execute("SELECT * FROM indices_ajuste WHERE indice=? AND fecha=?", (indice, fecha)).fetchone()
    return _fila(row) if row else None


def ultimo_hasta(indice: str, fecha: str | None = None, *, conn: Conexion | None = None) -> dict | None:
    """El último valor publicado de `indice` en `fecha` o antes (sin fecha, el último que haya)."""
    sql, args = "SELECT * FROM indices_ajuste WHERE indice=?", [indice]
    if fecha:
        sql += " AND fecha<=?"
        args.append(fecha)
    with _con(conn) as c:
        row = c.execute(sql + " ORDER BY fecha DESC LIMIT 1", tuple(args)).fetchone()
    return _fila(row) if row else None


def listar(indice: str, *, desde: str | None = None, hasta: str | None = None,
           conn: Conexion | None = None) -> list[dict]:
    """Los valores de `indice` entre `desde` y `hasta` (extremos incluidos), del más viejo al más nuevo."""
    sql, args = "SELECT * FROM indices_ajuste WHERE indice=?", [indice]
    if desde:
        sql += " AND fecha>=?"
        args.append(desde)
    if hasta:
        sql += " AND fecha<=?"
        args.append(hasta)
    with _con(conn) as c:
        rows = c.execute(sql + " ORDER BY fecha", tuple(args)).fetchall()
    return [_fila(r) for r in rows]


def borrar(indice: str, fecha: str, *, conn: Conexion | None = None) -> bool:
    """Borra un valor (uno cargado a mano por error). Lo publicado vuelve con la próxima descarga."""
    with _con(conn) as c:
        cur = c.execute("DELETE FROM indices_ajuste WHERE indice=? AND fecha=?", (indice, fecha))
        return (cur.rowcount or 0) > 0
