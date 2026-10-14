"""El rastro de los envíos por mail de facturas, notas y presupuestos (ADR-050).

Una fila por intento. `registrar` **no puede tumbar el envío**: si la base falla al escribir el
rastro, el mail ya salió y el usuario no tiene que ver un error por eso.
"""
import contextlib

from libracore.db.core import get_connection

TIPO_FACTURA = "factura"
TIPO_PRESUPUESTO = "presupuesto"
ESTADO_ENVIADO = "enviado"
ESTADO_ERROR = "error"

_CAMPOS = "id, documento_tipo, documento_id, documento_label, destinatario, estado, error, usuario, created_at"


def registrar(
    tipo: str, documento_id: int, *, label: str, destinatario: str, error: str | None = None,
    usuario: str = "",
) -> None:
    """Anota un intento: `error=None` es un envío logrado."""
    with contextlib.suppress(Exception), get_connection() as conn:
        conn.execute(
            "INSERT INTO envios_email (documento_tipo, documento_id, documento_label, destinatario,"
            " estado, error, usuario) VALUES (?,?,?,?,?,?,?)",
            (tipo, documento_id, label or "", destinatario, ESTADO_ERROR if error else ESTADO_ENVIADO,
             (error or "")[:500], usuario or ""),
        )
        conn.commit()


def de_documento(tipo: str, documento_id: int) -> list[dict]:
    """Los intentos de un documento, el más reciente primero."""
    with get_connection() as conn:
        filas = conn.execute(
            f"SELECT {_CAMPOS} FROM envios_email WHERE documento_tipo = ? AND documento_id = ?"
            " ORDER BY id DESC",
            (tipo, documento_id),
        ).fetchall()
    return [dict(f) for f in filas]


def resumen_de(tipo: str, ids: list[int]) -> dict[int, dict]:
    """`{documento_id: {cantidad, enviados, ultimo_estado, ultimo_at, ultimo_destino}}` en una sola
    consulta, para el listado. Los documentos sin intentos no aparecen."""
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    marcas = ",".join("?" * len(ids))
    with get_connection() as conn:
        filas = conn.execute(
            f"SELECT documento_id, destinatario, estado, created_at FROM envios_email"
            f" WHERE documento_tipo = ? AND documento_id IN ({marcas}) ORDER BY id",
            (tipo, *ids),
        ).fetchall()
    res: dict[int, dict] = {}
    for f in filas:  # ascendente: el último en pasar es el más reciente
        r = res.setdefault(f["documento_id"], {"cantidad": 0, "enviados": 0})
        r["cantidad"] += 1
        r["enviados"] += f["estado"] == ESTADO_ENVIADO
        r.update(ultimo_estado=f["estado"], ultimo_at=f["created_at"], ultimo_destino=f["destinatario"])
    return res
