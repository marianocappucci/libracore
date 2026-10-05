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
import datetime
import logging
import os
import random

from . import arca_credenciales, arca_wsaa, arca_wsfe, config_manager
from .db import arca_config as db_arca_config
from .db import facturas as db_facturas

logger = logging.getLogger(__name__)


def _es_dev() -> bool:
    return os.environ.get("ENV", "") == "development"


def _mock_cae() -> dict:
    """Genera CAE y vencimiento falsos para entorno de desarrollo."""
    cae = str(random.randint(10_000_000_000_000, 99_999_999_999_999))
    vto = (datetime.date.today() + datetime.timedelta(days=10)).strftime("%Y%m%d")
    return {"cae": cae, "cae_vto": vto}


#: Los receptores a los que ARCA acepta **Factura A** y sólo A (o C): los
#: inscriptos y los monotributistas. A todos los demás les corresponde B (o C).
#:
#: 🔴 **Un monotributista NO recibe B.** Medido contra homologación el
#: 2026-10-02: una B con receptor Monotributo (6) o Responsable Inscripto (1) se
#: rechaza con 10243, y una A al mismo receptor sale con CAE. Antes de la
#: RG 5616 ARCA no miraba esta combinación y el código daba B a todo lo que no
#: fuera inscripto.
RECEPTORES_DE_FACTURA_A = frozenset({
    "Responsable Inscripto", "IVA Responsable Inscripto",
    "Monotributista", "Responsable Monotributo",
})


def tipo_de_comprobante(emisor_cond: str, receptor_cond: str) -> int:
    """El tipo de factura que un emisor le emite a un receptor: 1 (A), 6 (B) u 11 (C).

    Un monotributista emite C a cualquiera; uno inscripto emite A al receptor que
    ARCA acepta con A (`RECEPTORES_DE_FACTURA_A`) y B a todos los demás.
    """
    if emisor_cond == "Monotributista":
        return 11
    return 1 if receptor_cond in RECEPTORES_DE_FACTURA_A else 6


def _en(conn) -> dict:
    """`{"conn": conn}` sólo si hay una transacción de quien llama (ADR-025).

    Sin ella no se pasa nada, y las funciones de la base se llaman exactamente
    como antes: un reemplazo de `get_factura` o `update_factura_cae` con la firma
    vieja —los tests de la familia los tienen— sigue andando.
    """
    return {"conn": conn} if conn is not None else {}


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


#: Cuando no hay ticket de ARCA pese a estar configurada; el motivo concreto está en el log.
MOTIVO_SIN_TICKET = (
    "No se pudo autenticar ni pedir el número a ARCA (certificado, conexión o "
    "servicio caído); el comprobante quedó con numeración local y sin CAE."
)


async def get_next_numero_with_arca(punto_venta: int, tipo: int, emisor_id=None, *, conn=None):
    """
    Devuelve (numero, ta, arca).
    En dev: usa contador local y marca ta/arca como mock.
    En prod: intenta ARCA, cae a local si falla.

    `emisor_id` elige la configuración de ARCA (`arca_config.config_del_emisor`);
    sin él, la del emisor único de la instancia. La numeración local también es
    la de ese emisor.

    Con `conn`, la numeración local se lee en la transacción de quien llama: ve
    los comprobantes que esa transacción ya escribió y todavía no confirmó.
    """
    if _es_dev():
        # Sin ARCA la secuencia es la de la propia instancia, que es la real:
        # `ambiente_de("_dev_mock_")` da `produccion` por lo mismo.
        numero = db_facturas.get_next_factura_numero(
            punto_venta, tipo, ambiente_de("_dev_mock_"), emisor_id, **_en(conn))
        return numero, "_dev_mock_", "_dev_mock_"

    arca     = db_arca_config.config_del_emisor(emisor_id)
    ta       = None

    # 🔑 Una llamada y no el baile de dos pasos: `paths_de` elige el par del
    # ambiente y el rescate necesita saber cuál es. Separados, el segundo
    # deshace al primero y esto termina firmando con el certificado real
    # creyendo que prueba — que es lo que pasaba acá hasta el 2026-09-01.
    cert_path, clave_path = arca_credenciales.paths_en_disco(arca)
    if arca and cert_path and clave_path:
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
                punto_venta, tipo, ambiente_de(arca), emisor_id, **_en(conn))
    else:
        numero = db_facturas.get_next_factura_numero(
            punto_venta, tipo, ambiente_de(arca), emisor_id, **_en(conn))

    return numero, ta, arca


async def solicitar_cae(factura_id: int, factura: dict, ta, arca, *, conn=None) -> dict:
    """
    Solicita el CAE real (prod) o genera uno simulado (dev).
    Devuelve la factura actualizada.

    Con `conn`, el CAE (o el motivo del rechazo) se escribe en la transacción de
    quien llama: si después el producto revierte, se revierte también. Es lo que
    pide un producto que autoriza dentro de la misma transacción que cierra su
    operación (LibraCargo, su ADR-024; ADR-025 de este motor): si algo falla, no queda nada.
    """
    if ta == "_dev_mock_":
        mock = _mock_cae()
        db_facturas.update_factura_cae(factura_id, mock["cae"], mock["cae_vto"], **_en(conn))
        return db_facturas.get_factura(factura_id, **_en(conn))

    if arca and not ta:
        # ARCA está configurada y no hubo ticket: falló la autenticación o el
        # pedido del número (`get_next_numero_with_arca` lo registró en el log y
        # cayó a numeración local). Sin esto el comprobante queda sin CAE y mudo.
        db_facturas.update_factura_cae_error(factura_id, MOTIVO_SIN_TICKET, **_en(conn))
        return db_facturas.get_factura(factura_id, **_en(conn))
    if not (ta and arca):
        return factura   # una instancia sin ARCA: no hay CAE que pedir

    try:
        cae_data = await arca_wsfe.solicitar_cae(
            factura, arca["cuit"], ta["token"], ta["sign"], arca["ambiente"]
        )
        db_facturas.update_factura_cae(factura_id, cae_data["cae"], cae_data["cae_vto"], **_en(conn))
        return db_facturas.get_factura(factura_id, **_en(conn))
    except Exception as e:
        logger.error("Error al solicitar CAE para factura %s: %s", factura_id, e)
        # 🔴 **No se relanza**: el comprobante ya existe y quien llama sigue con el
        # cobro o con el vínculo a la venta; levantar acá dejaría un cobro sin
        # factura o una factura huérfana. Se guarda el motivo en la factura, que
        # es lo que ven la pantalla y el reintento (`/autorizar`).
        db_facturas.update_factura_cae_error(factura_id, str(e), **_en(conn))
        return db_facturas.get_factura(factura_id, **_en(conn))
