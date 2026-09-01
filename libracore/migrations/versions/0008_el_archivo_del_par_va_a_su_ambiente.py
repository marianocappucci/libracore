"""El ARCHIVO del par pasa a llamarse como su ambiente, no sólo el path.

## 🔴 Lo que la `0007` dejó a medias

Esa revisión movió el par de credenciales a las **columnas** de su ambiente,
que era lo urgente: sin eso las instancias en `homologacion` se quedaban sin
facturar. Pero movió los **paths**, no los **archivos**: una instancia que
estaba en homologación quedó con su par apuntando a `certificado.crt`, que
desde `v1.72.0` es el nombre de **producción**.

El día que el operador suba el certificado real por la pantalla nueva, el upload
escribe **ese mismo archivo** y pisa el de homologación — y las columnas de
homologación pasan a apuntar al certificado real del cliente. Es exactamente la
operación destructiva que separar los pares vino a evitar, reaparecida una capa
más abajo.

Se relevaron las 21 instancias de la flota antes de escribir esto: la única
afectada era `contalibra-dev`. **Ninguna instancia de cliente lo estaba** —la de
Contalibra está en `produccion`, así que la `0007` no le movió nada—. O sea que
esta revisión es, en el parque actual, casi toda no-op; existe porque el estado
lo produce el flujo normal (probar en homologación y después cortar a
producción), no un accidente.

## Qué hace, y qué NO hace

Sólo toca una columna cuando su valor apunta a un archivo cuyo **nombre es el de
otro ambiente**. Un nombre propio, uno raro o una columna vacía no se tocan.

- Si el destino ya existe, **no lo pisa**: sólo apunta la columna ahí.
- Si el archivo no está en ningún lado, **vacía la columna**. Dejarla apuntando
  a un nombre ajeno es la trampa: el día que el otro ambiente suba su par, ese
  archivo aparece y esta columna empieza a leer el par del vecino.
- Si el otro ambiente apunta al mismo archivo, **no adivina**: lo deja como está.
  Con dos dueños no hay forma de saber de quién es.

No hay DDL: es sólo un movimiento de datos y de archivos.
"""
import os

import sqlalchemy as sa
from alembic import op

from libracore.config_manager import ARCHIVOS_POR_AMBIENTE, CERTS_DIR
from libracore.db.arca_config import COLUMNAS_POR_AMBIENTE

revision = "0008_archivo_par_por_ambiente"
down_revision = "0007_par_arca_por_ambiente"
branch_labels = None
depends_on = None

#: Con qué nombre canónico se corresponde cada columna.
#:
#: 🔑 Sale de los dos mapas del código, no de literales: los nombres de archivo
#: los define `config_manager` y las columnas `db.arca_config`. Repetirlos acá
#: sería una tercera copia que puede quedar atrás de las otras dos.
def _columnas_y_nombres():
    for ambiente, (cert_col, clave_col) in COLUMNAS_POR_AMBIENTE.items():
        cert_nom, clave_nom = ARCHIVOS_POR_AMBIENTE[ambiente]
        yield ambiente, cert_col, cert_nom
        yield ambiente, clave_col, clave_nom


def _nombres_ajenos(ambiente: str) -> set[str]:
    """Los nombres de archivo que pertenecen a los OTROS ambientes."""
    return {n for a, nombres in ARCHIVOS_POR_AMBIENTE.items() if a != ambiente
            for n in nombres}


def upgrade():
    conn = op.get_bind()
    try:
        filas = conn.exec_driver_sql(
            "SELECT * FROM arca_config"
        ).mappings().all()
    except Exception:
        # Una instancia que nunca configuró ARCA no tiene la tabla.
        return

    for fila in filas:
        empresa = fila.get("empresa")
        for ambiente, columna, canonico in _columnas_y_nombres():
            valor = (fila.get(columna) or "").strip()
            if not valor:
                continue
            base = os.path.basename(valor)
            if base == canonico or base not in _nombres_ajenos(ambiente):
                continue  # ya está bien, o es un nombre que no reconocemos

            # 🔑 Con dos dueños no se adivina: si otra columna apunta al mismo
            # archivo, no hay forma de saber de quién es el par.
            otros = [c for _, c, _ in _columnas_y_nombres() if c != columna]
            if any(os.path.basename((fila.get(c) or "").strip()) == base
                   for c in otros if fila.get(c)):
                continue

            destino = os.path.join(CERTS_DIR, canonico)
            # El path guardado puede ser de un DATA_DIR viejo —pasó— así que el
            # archivo se busca también por su nombre dentro del CERTS_DIR real.
            origen = valor if os.path.exists(valor) else os.path.join(CERTS_DIR, base)

            if os.path.exists(destino):
                nuevo = destino
            elif os.path.exists(origen):
                os.replace(origen, destino)
                nuevo = destino
            else:
                # No está en ningún lado: se vacía. Dejar la columna apuntando a
                # un nombre ajeno es la trampa que esta revisión cierra.
                nuevo = ""

            # 🔑 `text()` con parámetros nombrados y no `exec_driver_sql`: los
            # dos motores usan marcadores distintos (`?` y `%(x)s`) y ramificar
            # por `paramstyle` es una copia más de algo que SQLAlchemy ya sabe.
            # El nombre de columna sí va interpolado: no es un valor, y sale de
            # `COLUMNAS_POR_AMBIENTE`, no de la fila.
            conn.execute(
                sa.text(f"UPDATE arca_config SET {columna} = :valor "
                        "WHERE empresa = :empresa"),
                {"valor": nuevo, "empresa": empresa},
            )


def downgrade():
    # Bajar volvería a poner el par de un ambiente bajo el nombre del otro, que
    # es justo el estado que esto viene a deshacer. Volver atrás es restaurar el
    # backup previo al deploy.
    raise NotImplementedError(
        "No se baja: dejaría el par de un ambiente bajo el nombre de archivo "
        "del otro, que es la colisión que esta revisión cierra."
    )
