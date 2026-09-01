"""
Configuración ARCA (certificados, punto de venta, ambiente) por empresa.
Extraído de database.py de Contalibra/Restolibra (idéntico en ambos) como
parte de la migración real a libracore.db (Fase 3 de LibraCore, ver
wiki/entities/libracore.md).
"""
from libracore.db.core import get_connection


#: Los dos ambientes, y de qué columnas sale el par de cada uno.
#:
#: 🔴 **El par de producción vive en las columnas SIN sufijo.** Es una asimetría
#: histórica: esos nombres ya existían cuando había un solo par, y renombrarlos
#: obligaría a tocar cada lector y cada instancia viva a la vez.
#:
#: Que la asimetría no se note es el riesgo, así que vive **acá y en ningún otro
#: lado**. Nadie lee `cfg["clave_path"]` directo: todos pasan por `paths_de()`.
COLUMNAS_POR_AMBIENTE = {
    "produccion":   ("certificado_path", "clave_path"),
    "homologacion": ("certificado_path_homologacion", "clave_path_homologacion"),
}


def paths_de(config: dict | None, ambiente: str | None = None) -> tuple[str, str]:
    """El par `(certificado, clave)` del ambiente pedido.

    Sin `ambiente`, el del **selector** de la config —`cfg["ambiente"]`—, que es
    el que se usa para emitir.

    🔑 **Devuelve `("", "")` y no levanta cuando falta el par.** Que una
    instancia todavía no haya cargado el de homologación es el estado normal
    mientras se acompaña al cliente, no un error: quien llama ya sabe distinguir
    "no hay credencial" con `os.path.exists`, y hacerlo fallar acá convertiría
    una pantalla a medio llenar en un 500.
    """
    config = config or {}
    ambiente = (ambiente or config.get("ambiente") or "").strip().lower()
    columnas = COLUMNAS_POR_AMBIENTE.get(ambiente)
    if not columnas:
        # Un ambiente que no conocemos no tiene credenciales, y **no cae al par
        # de producción**: entregar las reales ante un valor raro es cómo se
        # factura de verdad creyendo que se está probando.
        return "", ""
    cert, clave = columnas
    return (config.get(cert) or ""), (config.get(clave) or "")


def crear_arca_config(empresa, cuit, punto_venta, clave_path, certificado_path,
                      ambiente="homologacion", alias=""):
    """Crea configuración ARCA para una empresa."""
    with get_connection() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO arca_config
                   (empresa, cuit, punto_venta, clave_path, certificado_path, ambiente, alias)
                   VALUES (?,?,?,?,?,?,?)""",
                (empresa, cuit, punto_venta, clave_path, certificado_path, ambiente, alias),
            )
            return cur.lastrowid
        except Exception as e:
            raise ValueError(f"Error creando configuración ARCA: {str(e)}")


def obtener_arca_config(empresa):
    """Obtiene configuración ARCA por nombre de empresa."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM arca_config WHERE empresa=? AND activo=1", (empresa,)
        ).fetchone()
        return dict(row) if row else None


def obtener_todas_arca_configs():
    """Obtiene todas las configuraciones ARCA activas."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM arca_config WHERE activo=1 ORDER BY empresa"
        ).fetchall()
        return [dict(r) for r in rows]


def actualizar_arca_config(empresa, cuit=None, punto_venta=None, clave_path=None,
                          certificado_path=None, ambiente=None, alias=None,
                          clave_path_homologacion=None,
                          certificado_path_homologacion=None):
    """Actualiza configuración ARCA."""
    with get_connection() as conn:
        config = obtener_arca_config(empresa)
        if not config:
            raise ValueError(f"Configuración ARCA no encontrada para: {empresa}")

        conn.execute(
            """UPDATE arca_config
               SET cuit=?, punto_venta=?, clave_path=?, certificado_path=?,
                   ambiente=?, alias=?,
                   clave_path_homologacion=?, certificado_path_homologacion=?,
                   updated_at=datetime('now','-3 hours')
               WHERE empresa=?""",
            (
                cuit if cuit is not None else config["cuit"],
                punto_venta if punto_venta is not None else config["punto_venta"],
                clave_path if clave_path is not None else config["clave_path"],
                certificado_path if certificado_path is not None else config["certificado_path"],
                ambiente if ambiente is not None else config["ambiente"],
                alias if alias is not None else config["alias"],
                # `None` = no lo toqués. Es lo que hace que subir el
                # certificado de un ambiente no borre el del otro.
                (clave_path_homologacion if clave_path_homologacion is not None
                 else config.get("clave_path_homologacion") or ""),
                (certificado_path_homologacion if certificado_path_homologacion is not None
                 else config.get("certificado_path_homologacion") or ""),
                empresa,
            ),
        )


def eliminar_arca_config(empresa):
    """Marca como inactivo la configuración ARCA."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE arca_config SET activo=0 WHERE empresa=?", (empresa,)
        )
