"""
Orquestación de numeración y CAE para facturación electrónica ARCA/AFIP:
combina `db.facturas`, `db.arca_config`, `config_manager` y los clientes
de protocolo `arca_wsaa`/`arca_wsfe` (todos ya en libracore) en el flujo
de dos pasos que un emisor de comprobantes necesita — pedir el próximo
número (local o autorizado por ARCA) y, ya con la factura creada, pedir
el CAE real. Migrado desde `web/helpers/arca_helper.py` de Contalibra
(idéntico en Restolibra salvo el logging de errores, que Restolibra
todavía no había recibido — ver `wiki/entities/medlibra.md`, sesión de
retomar facturación con LibraCore).
"""
import os
import random
import datetime
import logging

from .db import arca_config as db_arca_config
from .db import facturas as db_facturas
from . import config_manager
from . import arca_wsaa
from . import arca_wsfe

logger = logging.getLogger(__name__)


def _es_dev() -> bool:
    return os.environ.get("ENV", "") == "development"


def _mock_cae() -> dict:
    """Genera CAE y vencimiento falsos para entorno de desarrollo."""
    cae = str(random.randint(10_000_000_000_000, 99_999_999_999_999))
    vto = (datetime.date.today() + datetime.timedelta(days=10)).strftime("%Y%m%d")
    return {"cae": cae, "cae_vto": vto}


#: Los dos ambientes de ARCA. Cualquier otra cosa no es un ambiente.
AMBIENTES = ("homologacion", "produccion")


def ambiente_de(arca) -> str:
    """Contra qué ambiente se emitió, a partir de lo que devuelve
    `get_next_numero_with_arca`.

    🔴 **Ese tercer valor NO es siempre un dict.** En dev devuelve el string
    `"_dev_mock_"`, y sin ARCA configurado devuelve `None`. Un `.get()` derecho
    revienta con `AttributeError: 'str' object has no attribute 'get'` —pasó al
    escribir esto, y lo delataron 64 tests—. El nombre de la variable no dice de
    qué tipo es.

    🔑 **Sin ARCA, `produccion`.** No hay CAE y el número es el de la propia
    instancia: ese comprobante **es** el real del cliente. No es un default
    silencioso, es la respuesta a *"¿contra qué se emitió?"* cuando no se emitió
    contra nada — y es lo que hace que entre al libro IVA, que es donde tiene
    que estar.

    Existe para que los tres call sites de `create_factura` no repitan el mismo
    guard: tres copias de esta decisión es de donde salen las divergencias.
    """
    if isinstance(arca, dict):
        ambiente = str(arca.get("ambiente") or "").strip().lower()
        if ambiente in AMBIENTES:
            return ambiente
    return "produccion"


async def get_next_numero_with_arca(punto_venta: int, tipo: int):
    """
    Devuelve (numero, ta, arca).
    En dev: usa contador local y marca ta/arca como mock.
    En prod: intenta ARCA, cae a local si falla.
    """
    if _es_dev():
        # Sin ARCA la secuencia es la de la propia instancia, que es la real:
        # `ambiente_de("_dev_mock_")` da `produccion` por lo mismo.
        numero = db_facturas.get_next_factura_numero(
            punto_venta, tipo, ambiente_de("_dev_mock_"))
        return numero, "_dev_mock_", "_dev_mock_"

    arca_cfg = db_arca_config.obtener_todas_arca_configs()
    arca     = arca_cfg[0] if arca_cfg else None
    ta       = None

    if arca and arca.get("certificado_path") and arca.get("clave_path"):
        cert_path, clave_path = config_manager.resolve_cert_paths(
            arca["certificado_path"], arca["clave_path"]
        )
        try:
            ta = await arca_wsaa.autenticar(
                cert_path, clave_path, arca["ambiente"]
            )
            ultimo = await arca_wsfe.ultimo_numero_autorizado(
                punto_venta, tipo, arca["cuit"],
                ta["token"], ta["sign"], arca["ambiente"],
            )
            numero = ultimo + 1
        except Exception as e:
            logger.error(
                "ARCA no disponible al pedir numero para PV=%s tipo=%s, "
                "cae a numeracion local: %s", punto_venta, tipo, e,
            )
            ta     = None
            # 🔴 **En LA MISMA secuencia que se estaba pidiendo.** ARCA lleva
            # numeraciones independientes por ambiente: caer a la local sin
            # decir cuál desalinea la secuencia contra la de ARCA, y el próximo
            # comprobante choca con el "último autorizado" real.
            numero = db_facturas.get_next_factura_numero(
                punto_venta, tipo, ambiente_de(arca))
    else:
        numero = db_facturas.get_next_factura_numero(
            punto_venta, tipo, ambiente_de(arca))

    return numero, ta, arca


async def solicitar_cae(factura_id: int, factura: dict, ta, arca) -> dict:
    """
    Solicita el CAE real (prod) o genera uno simulado (dev).
    Devuelve la factura actualizada.
    """
    if ta == "_dev_mock_":
        mock = _mock_cae()
        db_facturas.update_factura_cae(factura_id, mock["cae"], mock["cae_vto"])
        return db_facturas.get_factura(factura_id)

    if not (ta and arca):
        return factura

    try:
        cae_data = await arca_wsfe.solicitar_cae(
            factura, arca["cuit"], ta["token"], ta["sign"], arca["ambiente"]
        )
        db_facturas.update_factura_cae(factura_id, cae_data["cae"], cae_data["cae_vto"])
        return db_facturas.get_factura(factura_id)
    except Exception as e:
        logger.error("Error al solicitar CAE para factura %s: %s", factura_id, e)
        return factura
