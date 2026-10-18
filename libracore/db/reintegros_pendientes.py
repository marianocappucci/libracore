"""A dónde va la plata de una nota de crédito que todavía no tiene CAE (ADR-067, sobre el ADR-065).

Una fila por nota, sólo mientras está sin autorizar: la escribe el router de la nota cuando ARCA no le dio CAE
y había plata que mover, y la borra quien la mueve (`reintegro_de_nota.aplicar_pendiente`) en la misma
transacción que escribe el movimiento. Borrar la nota borra la fila (`ON DELETE CASCADE`).

Con `conn`, las funciones trabajan dentro de la transacción de quien llama y no confirman nada (idioma de
`libracore.db`).
"""
from libracore.db.facturas import _con


def anotar(nota_id: int, factura_id: int, destino: str, medio_pago: str = "", *, conn=None) -> None:
    """Deja anotado el destino de la nota. Anotar dos veces la misma nota deja la última."""
    with _con(conn) as c:
        c.execute("DELETE FROM reintegros_pendientes WHERE nota_id = ?", (nota_id,))
        c.execute(
            "INSERT INTO reintegros_pendientes (nota_id, factura_id, destino, medio_pago) VALUES (?,?,?,?)",
            (nota_id, factura_id, destino, medio_pago or ""))


def de_nota(nota_id: int, *, conn=None) -> dict | None:
    """El destino anotado de la nota (`nota_id`, `factura_id`, `destino`, `medio_pago`), o `None`."""
    with _con(conn) as c:
        fila = c.execute(
            "SELECT nota_id, factura_id, destino, medio_pago FROM reintegros_pendientes WHERE nota_id = ?",
            (nota_id,)).fetchone()
    return None if fila is None else dict(fila)


def borrar(nota_id: int, *, conn=None) -> None:
    with _con(conn) as c:
        c.execute("DELETE FROM reintegros_pendientes WHERE nota_id = ?", (nota_id,))
