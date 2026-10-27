"""Qué pasó con el respaldo de anoche: lo escribe el host, lo lee la app.

El mismo patrón que `resguardo_estado` (la copia externa), por el mismo motivo: `provisioning.panel_admin.cmd_backup_all`
lo **escribe** desde el host, después de cada instancia, y `estado_del_sistema` lo **lee** desde adentro del
contenedor para la barra del pie (ADR-075).

Sin este archivo la barra sólo veía la fecha del último ZIP, y un ZIP de hace dos días se ve igual si el cron no
corrió que si corrió y falló el dump. Son dos avisos distintos: «no corrió» es del VPS (el cron, el host), «falló»
es de la instancia (la base, el disco), y quien lo lee necesita saber a quién avisar.

🔴 **El texto del error queda en el archivo y no sale de acá hacia la pantalla**: puede traer la URL de la base o lo
que contestó `pg_dump`. `estado_del_sistema` publica sólo si falló y cuándo.
"""
import json
from datetime import UTC, datetime
from pathlib import Path

#: Nombre del archivo, en el mismo directorio que los ZIP de backup (al lado del `.externo.json`).
ESTADO = ".nocturno.json"


def escribir_intento(backups_dir, *, ok: bool, error: str | None = None, cuando: datetime | None = None) -> Path:
    """Deja el `.nocturno.json` con el último intento, haya salido bien o no. Ver el docstring del módulo."""
    destino = Path(backups_dir) / ESTADO
    destino.parent.mkdir(parents=True, exist_ok=True)
    datos = {"cuando": (cuando or datetime.now(UTC)).isoformat(timespec="seconds"), "ok": bool(ok)}
    if not ok and error:
        datos["error"] = str(error)[:500]
    destino.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
    return destino


def leer_intento(backups_dir) -> dict | None:
    """`{"cuando": datetime con zona, "ok": bool}` del último intento, o `None` si nunca hubo uno o el archivo no se
    entiende. Una fecha sin zona se toma como UTC, que es como la escribe `escribir_intento`."""
    ruta = Path(backups_dir) / ESTADO
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        cuando = datetime.fromisoformat(datos["cuando"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=UTC)
    return {"cuando": cuando, "ok": datos.get("ok") is True}


__all__ = ["ESTADO", "escribir_intento", "leer_intento"]
