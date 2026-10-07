"""Credenciales de ARCA de los servicios que no son la facturación (ADR-032).

La facturación (`wsfe`) guarda su par en las columnas de `arca_config`
(`db/arca_config.py`) y **no pasa por acá**: sus ocho lectores no cambian. Esta
tabla es para los demás servicios (`wscpe`), con una fila por
`(empresa, servicio, ambiente)`.

A diferencia de `arca_config`, no hay asimetría de columnas: cada ambiente es una
fila, así que no existe un «par sin sufijo» que confundir con producción.

🔑 **Un ambiente que no conocemos no tiene credenciales**, y no cae a
producción. Misma regla que `paths_de()`: entregar las reales ante un valor raro
es cómo se opera de verdad creyendo que se está probando. Por eso `ambiente` se
valida acá y no sólo en el `CHECK` de la tabla.
"""
from __future__ import annotations

from libracore.db.core import get_connection

AMBIENTES = ("homologacion", "produccion")


def _ambiente(valor: str | None) -> str:
    amb = (valor or "").strip().lower()
    if amb not in AMBIENTES:
        raise ValueError(
            f"Ambiente desconocido: {valor!r}. Los válidos son {' y '.join(AMBIENTES)}.")
    return amb


def paths_de_servicio(empresa: str, servicio: str, ambiente: str) -> tuple[str, str]:
    """El `(certificado, clave)` guardado para ese servicio y ambiente.

    Devuelve `("", "")` cuando no hay fila o un ambiente que no conocemos: que
    falte es el estado normal de un servicio que todavía no se cargó, no un error.
    Son las rutas **tal como se guardaron**: si el archivo se movió de volumen,
    `arca_credenciales.paths_en_disco_de_servicio` es quien las rescata.
    """
    amb = (ambiente or "").strip().lower()
    if amb not in AMBIENTES:
        return "", ""
    with get_connection() as conn:
        fila = conn.execute(
            "SELECT certificado_path, clave_path FROM arca_credenciales_servicio "
            "WHERE empresa=? AND servicio=? AND ambiente=?",
            (empresa, servicio, amb),
        ).fetchone()
    if not fila:
        return "", ""
    return (fila["certificado_path"] or ""), (fila["clave_path"] or "")


def guardar_paths_de_servicio(empresa: str, servicio: str, ambiente: str, *,
                              certificado_path: str | None = None,
                              clave_path: str | None = None) -> None:
    """Escribe el path de una o de las dos mitades, creando la fila si no está.

    🔑 `None` es «no lo toqués», como en `actualizar_arca_config`: subir el
    certificado no borra la clave que ya estaba.
    """
    amb = _ambiente(ambiente)
    with get_connection() as conn:
        existe = conn.execute(
            "SELECT 1 FROM arca_credenciales_servicio "
            "WHERE empresa=? AND servicio=? AND ambiente=?",
            (empresa, servicio, amb),
        ).fetchone()
        if not existe:
            conn.execute(
                "INSERT INTO arca_credenciales_servicio "
                "(empresa, servicio, ambiente, certificado_path, clave_path) VALUES (?,?,?,?,?)",
                (empresa, servicio, amb, certificado_path or "", clave_path or ""),
            )
            return
        sets, valores = [], []
        if certificado_path is not None:
            sets.append("certificado_path=?")
            valores.append(certificado_path)
        if clave_path is not None:
            sets.append("clave_path=?")
            valores.append(clave_path)
        if not sets:
            return
        sets.append("updated_at=datetime('now','-3 hours')")
        conn.execute(
            f"UPDATE arca_credenciales_servicio SET {', '.join(sets)} "
            "WHERE empresa=? AND servicio=? AND ambiente=?",
            (*valores, empresa, servicio, amb),
        )


def borrar_paths_de_servicio(empresa: str, servicio: str, ambiente: str) -> None:
    """Quita la fila de ese servicio y ambiente. No toca los otros ambientes."""
    amb = _ambiente(ambiente)
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM arca_credenciales_servicio WHERE empresa=? AND servicio=? AND ambiente=?",
            (empresa, servicio, amb),
        )


def listar_de_empresa(empresa: str) -> list[dict]:
    """Todas las filas de una empresa, ordenadas. Para diagnóstico y tests."""
    with get_connection() as conn:
        filas = conn.execute(
            "SELECT empresa, servicio, ambiente, certificado_path, clave_path "
            "FROM arca_credenciales_servicio WHERE empresa=? ORDER BY servicio, ambiente",
            (empresa,),
        ).fetchall()
    return [dict(f) for f in filas]
