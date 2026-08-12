"""Que paso con la copia externa: lo escribe el host, lo lee la app.

Vive aca y no en `provisioning/` porque tiene **dos lectores de distinta capa**:

- `provisioning.resguardo_externo` lo **escribe** desde el host, despues de
  subir (o de fallar subiendo).
- `config_router` lo **lee** desde adentro del contenedor, para que la pantalla
  del cliente pueda mostrar el estado.

Si viviera del lado de `provisioning`, la app tendria que importar el modulo que
maneja contenedores del host para leer un JSON. Son cuarenta lineas: mejor
compartirlas que cruzar las capas.

🔴 **El archivo se escribe tambien cuando la subida falla.** Una pantalla que no
distingue "nunca se configuro" de "hace cuatro dias que no sube" no sirve de
nada, y el silencio es justo el modo de falla que el resguardo externo tiene que
cerrar: el dia que el cliente revoque el acceso OAuth, la subida deja de
funcionar y sin esto todo se ve igual.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

#: Nombre del archivo, en el mismo directorio que los ZIP de backup.
ESTADO = ".externo.json"

#: Cuantas horas puede tener la ultima copia antes de considerarse vencida.
#: 36 y no 24 para que una noche que se corre un poco no genere un falso aviso.
HORAS_FRESCURA = 36


def escribir_estado(backups_dir, datos: dict) -> Path:
    """Deja el `.externo.json`. Ver el docstring del modulo."""
    destino = Path(backups_dir) / ESTADO
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
    return destino


def leer_estado(backups_dir) -> dict | None:
    """El ultimo estado conocido, o `None` si nunca se subio nada."""
    ruta = Path(backups_dir) / ESTADO
    if not ruta.exists():
        return None
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except ValueError:
        return None


def esta_al_dia(backups_dir, *, horas: int = HORAS_FRESCURA, ahora=None) -> tuple[bool, str]:
    """`(al_dia, motivo)`.

    Los cuatro "no" que tiene que distinguir, porque se ven igual desde afuera y
    significan cosas muy distintas: nunca se configuro, el archivo esta roto, la
    ultima subida fallo, y la ultima subida anduvo pero es vieja.
    """
    ahora = ahora or datetime.now()
    ruta = Path(backups_dir) / ESTADO
    if not ruta.exists():
        return False, "nunca subio: no hay .externo.json"
    datos = leer_estado(backups_dir)
    if datos is None:
        return False, ".externo.json ilegible"
    if not datos.get("ok"):
        return False, f"la ultima subida fallo: {datos.get('error') or 'sin detalle'}"
    try:
        cuando = datetime.fromisoformat(datos["cuando"])
    except (KeyError, TypeError, ValueError):
        return False, ".externo.json sin fecha valida"
    if ahora - cuando > timedelta(hours=horas):
        return False, (
            f"la ultima copia externa es de {datos['cuando']}, "
            f"hace mas de {horas} horas"
        )
    return True, f"al dia ({datos['cuando']})"


def resumen(backups_dir, *, horas: int = HORAS_FRESCURA, ahora=None) -> dict:
    """Lo que la pantalla necesita para contar el estado en una linea.

    `contratado` es `False` cuando no hay archivo **y** nadie subio nunca: para
    la pantalla eso es "no tenes el add-on", no "esta fallando". La diferencia
    importa — mostrarle una alarma a quien no contrato el servicio es ruido.
    """
    datos = leer_estado(backups_dir)
    if datos is None:
        return {"contratado": False, "al_dia": None, "motivo": None, "detalle": None}
    al_dia, motivo = esta_al_dia(backups_dir, horas=horas, ahora=ahora)
    return {
        "contratado": True,
        "al_dia": al_dia,
        "motivo": motivo,
        "detalle": {
            "cuando": datos.get("cuando"),
            "archivo": datos.get("archivo"),
            "destino": datos.get("destino"),
            "bytes": datos.get("bytes"),
            "en_destino": datos.get("en_destino"),
            "error": datos.get("error"),
        },
    }
