"""Dónde está, en disco, el par de credenciales de ARCA que hay que usar.

Son **dos** decisiones encadenadas y hasta el 2026-09-01 cada llamador las
escribía por su cuenta:

1. **De qué ambiente es el par** — `db.arca_config.paths_de()`. El de producción
   vive en las columnas SIN sufijo, por historia.
2. **Dónde está ese archivo realmente** — `config_manager.resolve_cert_paths()`,
   que rescata un path obsoleto cayendo al nombre estándar **del ambiente**.

🔴 **Separadas, el segundo paso deshace al primero.** `paths_de()` devuelve
`("", "")` para no entregar las credenciales reales cuando falta el par de
homologación; el rescate, si no sabe el ambiente, cae al nombre de producción y
**las repone**. Lo mismo pasa cuando el path guardado existe pero el archivo se
movió —el caso para el que el rescate existe—: una instancia de homologación
termina firmando con el certificado real del cliente.

**Y no es hipotético: pasó.** Al separar los pares se arregló el llamador del
router y quedaron **dos** call sites del motor pasando el par sin el ambiente
(`arca_facturacion` y `facturas_router`), justo los del camino de emisión. El
baile estaba escrito cuatro veces y salió mal en dos.

Por eso hay **una** función, y un test que barre el motor buscando a quien la
esquive.
"""
from __future__ import annotations

from libracore import config_manager
from libracore.db import arca_config as db_arca_config


def paths_en_disco(config: dict | None, ambiente: str = "") -> tuple[str, str]:
    """El `(certificado, clave)` listo para abrir, del ambiente que corresponde.

    Sin `ambiente`, el del **selector** de la config — el que la instancia está
    usando para emitir.

    Devuelve `("", "")` cuando no hay par cargado para ese ambiente. Que falte
    es un estado normal mientras se acompaña al cliente, no un error: quien
    llama ya distingue "no hay credencial" y hacerlo fallar acá convertiría una
    pantalla a medio llenar en un 500.
    """
    # La normalización (`  Homologacion ` → `homologacion`) la hacen las dos
    # piezas que esto encadena, así que repetirla acá es código que ningún test
    # puede distinguir: una mutación que la sacaba sobrevivió a la batería, y la
    # conclusión correcta fue que sobraba, no que faltaba un test.
    cfg = config or {}
    amb = ambiente or cfg.get("ambiente") or ""
    return config_manager.resolve_cert_paths(
        *db_arca_config.paths_de(cfg, amb), ambiente=amb,
    )
