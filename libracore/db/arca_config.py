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


class EmisorDesconocido(ValueError):
    """Se pidió emitir con un emisor (`arca_config.id`) que no existe o está inactivo."""


class ArcaAmbiguo(ValueError):
    """Hay más de una configuración activa para el mismo CUIT: no se sabe con cuál emitir."""


def obtener_arca_config_por_id(emisor_id):
    """La configuración activa con ese id, o `None`."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM arca_config WHERE id=? AND activo=1", (emisor_id,)
        ).fetchone()
        return dict(row) if row else None


def config_del_emisor(emisor_id=None):
    """La configuración con la que emite un comprobante. **El único lugar que elige emisor.**

    Sin `emisor_id`, la primera activa: es lo que la familia hizo siempre
    (`configs[0]`) y lo que siguen haciendo los productos de un solo emisor, que
    nunca pasan uno. Con `emisor_id`, esa fila y ninguna otra: un producto con
    varias razones sociales (LibraCargo) emite con el par de la que factura, y
    caer a otra sería emitir con el CUIT equivocado. Por eso un id que no existe
    levanta `EmisorDesconocido` en vez de devolver `None`, que quien llama lee
    como «no hay ARCA, se numera local».
    """
    if emisor_id is None:
        activas = obtener_todas_arca_configs()
        return activas[0] if activas else None
    cfg = obtener_arca_config_por_id(emisor_id)
    if cfg is None:
        raise EmisorDesconocido(f"No hay una configuración de ARCA activa con id {emisor_id}.")
    return cfg


def _digitos(valor) -> str:
    return "".join(c for c in str(valor or "") if c.isdigit())


def config_por_cuit(cuit):
    """La configuración activa de ese CUIT, o `None` si no hay. `ArcaAmbiguo` si hay dos.

    Es la guarda de un producto con varias razones sociales: el certificado es de
    un CUIT, así que una razón social sólo emite con la fila de su CUIT. Si no
    hay, registra a mano; si hay dos, no adivina.
    """
    digitos = _digitos(cuit)
    if not digitos:
        return None
    candidatas = [c for c in obtener_todas_arca_configs() if _digitos(c.get("cuit")) == digitos]
    if len(candidatas) > 1:
        raise ArcaAmbiguo(
            f"Hay {len(candidatas)} configuraciones de ARCA activas para el CUIT {digitos}."
        )
    return candidatas[0] if candidatas else None


def actualizar_arca_config(empresa, cuit=None, punto_venta=None, clave_path=None,
                          certificado_path=None, ambiente=None, alias=None,
                          clave_path_homologacion=None,
                          certificado_path_homologacion=None,
                          fce_cbu=None, fce_transmision=None):
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
                   fce_cbu=?, fce_transmision=?,
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
                # Igual: `None` es «no lo toqués», así que un PUT de la pantalla
                # que no conoce la FCE no borra el CBU que ya estaba.
                fce_cbu if fce_cbu is not None else config.get("fce_cbu") or "",
                (fce_transmision if fce_transmision is not None
                 else config.get("fce_transmision") or ""),
                empresa,
            ),
        )


def eliminar_arca_config(empresa):
    """Marca como inactivo la configuración ARCA."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE arca_config SET activo=0 WHERE empresa=?", (empresa,)
        )
