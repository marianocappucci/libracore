"""
Configuración ARCA (certificados, punto de venta, ambiente) por empresa.
Extraído de database.py de Contalibra/Restolibra (idéntico en ambos) como
parte de la migración real a libracore.db (Fase 3 de LibraCore, ver
wiki/entities/libracore.md).
"""
import contextlib
import json
import re

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


#: Cómo es un alias bancario en la Argentina: de 6 a 20 caracteres, en minúsculas,
#: sólo letras sin ñ ni tildes, números, punto y guion.
ALIAS_VALIDO = re.compile(r"^[a-z0-9.\-]{6,20}$")


def normalizar_cbu(valor) -> str:
    """El CBU sin espacios ni guiones, que es como se guarda y se compara."""
    return "".join(str(valor or "").split()).replace("-", "")


def normalizar_alias(valor) -> str:
    """El alias sin espacios de los costados y en minúsculas. No valida: ver `ALIAS_VALIDO`."""
    return str(valor or "").strip().lower()


def cbus_fce(config: dict | None) -> list[dict]:
    """Las cuentas donde se puede cobrar una FCE: `[{"cbu", "alias", "etiqueta"}]` (ADR-040).

    Sale de `arca_config.fce_cbus`. Devuelve **siempre las tres claves**, con `""`
    donde la fila no las tenía (lo legado y lo que rellenó la migración no traen
    alias). Un JSON inválido o que no es una lista es `[]` y no un error: una
    pantalla a medio llenar no puede dar un 500 al emitir.

    🔑 **Si la lista está vacía pero hay `fce_cbu`, ese CBU es la única cuenta.**
    Es la instancia que cargó su CBU antes de que existiera la lista, o con una
    pantalla vieja: para quien lee, tener un solo CBU es tener una lista de uno.
    """
    config = config or {}
    try:
        crudo = json.loads(config.get("fce_cbus") or "[]")
    except (TypeError, ValueError):
        crudo = []
    filas = []
    for fila in crudo if isinstance(crudo, list) else []:
        if not isinstance(fila, dict):
            continue
        cbu = normalizar_cbu(fila.get("cbu"))
        if cbu:
            filas.append({
                "cbu": cbu,
                "alias": normalizar_alias(fila.get("alias")),
                "etiqueta": str(fila.get("etiqueta") or "").strip(),
            })
    if not filas:
        predeterminado = normalizar_cbu(config.get("fce_cbu"))
        if predeterminado:
            filas = [{"cbu": predeterminado, "alias": "", "etiqueta": ""}]
    return filas


def cbu_para_fce(config: dict | None, pedido: str | None) -> str:
    """El CBU con el que se emite una FCE. Siempre devuelve **el CBU**, nunca el alias.

    Sin `pedido` (`None` o vacío), el predeterminado: `fce_cbu`, o el primero de la
    lista si ese está vacío, o `""` si no hay ninguno. Con `pedido`, tiene que ser el
    CBU **o el alias** de una fila de la lista; si no, `ValueError`.

    ⚠️ **Un CBU que no está en la lista no se acepta aunque tenga 22 dígitos.** Es lo
    que impide que un formulario mal armado cobre una FCE en una cuenta que el dueño
    no cargó.
    """
    config = config or {}
    if not str(pedido or "").strip():
        predeterminado = normalizar_cbu(config.get("fce_cbu"))
        if predeterminado:
            return predeterminado
        filas = cbus_fce(config)
        return filas[0]["cbu"] if filas else ""
    cbu_pedido, alias_pedido = normalizar_cbu(pedido), normalizar_alias(pedido)
    for fila in cbus_fce(config):
        if fila["cbu"] == cbu_pedido or (fila["alias"] and fila["alias"] == alias_pedido):
            return fila["cbu"]
    raise ValueError("El CBU elegido no está entre los cargados en la configuración de ARCA.")


def config_del_emisor_en(emisor_id, *, conn=None) -> dict | None:
    """La config de ARCA de ese `emisor_id` (o la primera activa si es `None`), leída desde `conn`.

    A diferencia de `config_del_emisor`, **no levanta** ante un id que no existe (devuelve
    `None`) y busca **también entre las inactivas**: es para mostrar y validar un documento
    ya armado (la pre factura), que tiene que seguir imprimiéndose aunque se dé de baja
    la razón social. Con `conn` trabaja en la transacción de quien llama.
    """
    with (contextlib.nullcontext(conn) if conn is not None else get_connection()) as c:
        if emisor_id is None:
            fila = c.execute(
                "SELECT * FROM arca_config WHERE activo=1 ORDER BY empresa LIMIT 1").fetchone()
        else:
            fila = c.execute("SELECT * FROM arca_config WHERE id=?", (emisor_id,)).fetchone()
        return dict(fila) if fila else None


def cuenta_fce(config: dict | None, cbu: str | None = None) -> dict | None:
    """`{"cbu", "alias", "etiqueta"}` de la cuenta de cobro, o `None` si no hay ninguna.

    Sin `cbu`, la predeterminada de la config. Si el `cbu` ya no está en la lista (se
    sacó de la configuración después de elegirlo) se devuelve igual, sin alias ni
    etiqueta: lo que se eligió no se pierde.
    """
    elegido = normalizar_cbu(cbu) or cbu_para_fce(config, None)
    if not elegido:
        return None
    for fila in cbus_fce(config):
        if fila["cbu"] == elegido:
            return dict(fila)
    return {"cbu": elegido, "alias": "", "etiqueta": ""}


def cuenta_de_cobro(documento: dict, *, conn=None) -> dict | None:
    """La cuenta donde se cobraría la FCE de un documento sin emitir (la pre factura).

    `None` si el documento no es una FCE o su emisor no tiene ningún CBU cargado. Usa
    `documento["fce_cbu"]` si lo trae y, si no, el predeterminado del `emisor_id`.
    """
    from libracore import tipos_comprobante as tipos

    if documento.get("tipo_comprobante") not in tipos.FCE_FACTURA:
        return None
    config = config_del_emisor_en(documento.get("emisor_id"), conn=conn)
    return cuenta_fce(config, documento.get("fce_cbu"))


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
                          fce_cbu=None, fce_transmision=None, fce_cbus=None):
    """Actualiza configuración ARCA.

    `fce_cbus` es la lista `[{"cbu", "alias", "etiqueta"}]`: `None` = no la toqués, una
    lista (incluso vacía) la reemplaza. Quien llama la valida; acá sólo se serializa.
    """
    with get_connection() as conn:
        config = obtener_arca_config(empresa)
        if not config:
            raise ValueError(f"Configuración ARCA no encontrada para: {empresa}")

        conn.execute(
            """UPDATE arca_config
               SET cuit=?, punto_venta=?, clave_path=?, certificado_path=?,
                   ambiente=?, alias=?,
                   clave_path_homologacion=?, certificado_path_homologacion=?,
                   fce_cbu=?, fce_transmision=?, fce_cbus=?,
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
                # Vacía se guarda como `''` y no como `'[]'`: un solo valor para «sin lista».
                ((json.dumps(fce_cbus, ensure_ascii=False) if fce_cbus else "")
                 if fce_cbus is not None else config.get("fce_cbus") or ""),
                empresa,
            ),
        )


def eliminar_arca_config(empresa):
    """Marca como inactivo la configuración ARCA."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE arca_config SET activo=0 WHERE empresa=?", (empresa,)
        )
