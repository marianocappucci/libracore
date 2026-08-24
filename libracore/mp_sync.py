"""Traer de MercadoPago lo que no llegó por webhook, y facturar lo que
corresponda.

## Por qué esto es una sola función y no dos

Hasta acá el sync estaba escrito **dos veces en cada producto**: una en el botón
*Sincronizar* de la bandeja y otra en `scripts/sync_mp_auto.py`, el cron
nocturno. Las dos hacían exactamente el mismo trabajo de ingesta —pedirle los
movimientos a MercadoPago, descartar los de otra cuenta, no duplicar, guardar el
movimiento— y no compartían una línea.

> 🔴 **Esa divergencia ya se cobró dos comprobantes.** Cuando se agregaron los
> alias de facturación el 2026-07-13, se tocaron los tres caminos que se veían
> desde `web/` y el cron quedó afuera: siguió resolviendo el cliente a mano
> durante tres semanas y facturó RIPEHO y VISCO al CUIT equivocado. La
> documentación de la familia avisaba de la divergencia —*"el cron es una
> implementación separada que no comparte código con el sync manual"*— y aun
> así pasó, porque avisar de una divergencia no es lo mismo que no tenerla.

`ingerir()` es la ingesta, y la usan los dos. `sincronizar_y_facturar()` le suma
la auto-facturación, y es lo que corre el cron.
"""

import asyncio
import datetime
import logging
from dataclasses import dataclass

from libracore import config_manager, mp_api, mp_facturacion
from libracore.db import mp as db_mp
from libracore.registro_de_clientes import RegistroDeClientes, el_registro

logger = logging.getLogger(__name__)

#: Tope de días hacia atrás. Es de MercadoPago, no nuestro.
DIAS_MAX = 90


class MercadoPagoNoContesta(RuntimeError):
    """No se pudieron pedir los movimientos. El llamador decide qué hacer: la
    pantalla devuelve 502, el cron lo registra y sale."""


@dataclass(frozen=True)
class MovimientoNuevo:
    """Un cobro que acaba de entrar a la bandeja."""

    mov_id: int
    payment_id: str
    monto: float
    payer_email: str
    payer_name: str
    payer_id_number: str
    payment_type: str
    descripcion: str


async def _quien_soy(access_token: str) -> tuple[str, str]:
    """Mi `user_id` y mi email en MercadoPago.

    Sirve para descartar cobros de otra cuenta y para no guardar mi propio email
    como el del pagador. **No es fatal que falle**: sin esto entran cobros de
    más, que se descartan a mano; fallar acá dejaría la bandeja sin sincronizar
    por un dato accesorio.
    """
    try:
        info = await mp_api.obtener_usuario_info(access_token)
        return str(info.get("id", "") or ""), (info.get("email") or "").strip().lower()
    except Exception as e:
        logger.warning("No se pudo leer la cuenta propia de MercadoPago: %s", e)
        return "", ""


async def ingerir(
    cfg: dict,
    *,
    dias: int = 7,
    referencias_a_omitir: tuple[str, ...] = (),
) -> list[MovimientoNuevo]:
    """Guarda en la bandeja los movimientos que todavía no estaban.

    Devuelve sólo **los nuevos**, para que el llamador pueda decidir qué hacer
    con cada uno sin volver a consultar la base.

    > ⚠️ **No filtra por `operation_type` ni por `payer_email`**, y no es un
    > olvido: MercadoPago marca `account_fund` con el email propio a *cualquier*
    > movimiento que no sea un pago clásico de un tercero, transferencias reales
    > incluidas. Contalibra tuvo ese filtro nueve días y una transferencia real
    > de un cliente quedó invisible en la bandeja.
    """
    access_token = cfg.get("mp_access_token", "")
    if not access_token:
        raise ValueError("Configurá el Access Token de MercadoPago en Configuración.")

    hoy = datetime.date.today()
    dias = max(1, min(dias, DIAS_MAX))
    desde = (hoy - datetime.timedelta(days=dias)).isoformat()
    hasta = hoy.isoformat()
    logger.info("Sincronizando MercadoPago desde %s hasta %s", desde, hasta)

    mi_user_id, mi_email = await _quien_soy(access_token)

    try:
        movimientos = await mp_api.obtener_movimientos(access_token, desde, hasta)
    except Exception as e:
        logger.error("Error consultando movimientos de MercadoPago: %s", e)
        raise MercadoPagoNoContesta(str(e)) from None

    nuevos: list[MovimientoNuevo] = []
    for pago in movimientos:
        payment_id = str(pago.get("id", "") or "").strip()
        if not payment_id:
            continue
        # Un `collector_id` que no es el mío es literalmente el cobro de otro.
        if mi_user_id and str(pago.get("collector_id", "")) != mi_user_id:
            continue
        referencia = (pago.get("external_reference") or "").strip()
        if any(referencia.startswith(p) for p in referencias_a_omitir):
            continue
        # Ya lo trajo el webhook, o ya lo trajo un sync anterior.
        if db_mp.get_mp_pago(payment_id) or db_mp.get_mp_movimiento_by_mp_id(payment_id):
            continue

        monto = float(pago.get("transaction_amount") or 0)
        if monto <= 0:
            continue

        payer = pago.get("payer") or {}
        ident = payer.get("identification") or {}
        crudo = (payer.get("email") or "").strip()
        email = "" if (mi_email and crudo.lower() == mi_email) else crudo
        nombre = " ".join(
            x for x in ((payer.get("first_name") or "").strip(),
                        (payer.get("last_name") or "").strip()) if x
        ) or email
        descripcion = (pago.get("description") or "").strip()
        tipo_pago = (pago.get("payment_type_id") or "").strip()
        id_number = (ident.get("number") or "").strip()
        fecha = (
            pago.get("date_approved") or pago.get("date_created") or hasta
        )[:10]

        mov_id = db_mp.create_mp_movimiento(
            mp_movement_id=payment_id, tipo=tipo_pago, monto=monto, fecha=fecha,
            descripcion=descripcion, origen_nombre=nombre,
            origen_banco=(pago.get("payment_method_id") or "").strip(),
            origen_cbu="", payer_email=email, payer_name=nombre,
            payer_id_type=(ident.get("type") or "").strip(),
            payer_id_number=id_number, estado_factura="pendiente",
        )
        logger.info("Nuevo cobro: %s | $%.2f | %s", payment_id, monto, nombre or "sin nombre")
        nuevos.append(MovimientoNuevo(
            mov_id=mov_id, payment_id=payment_id, monto=monto,
            payer_email=email, payer_name=nombre, payer_id_number=id_number,
            payment_type=tipo_pago, descripcion=descripcion,
        ))

    return nuevos


def _auto_facturar_por_bandera(client: dict, contexto: dict) -> bool:
    return bool(client.get("auto_facturar"))


async def sincronizar_y_facturar(
    *,
    dias: int = 2,
    referencias_a_omitir: tuple[str, ...] = (),
    debe_auto_facturar=_auto_facturar_por_bandera,
    registro: RegistroDeClientes | None = None,
) -> dict:
    """Lo que corre el cron nocturno. **Es el camino que emite la mayoría de las
    facturas de MercadoPago**, y el que corre sin nadie mirando.

    Devuelve el resumen para el log del cron: cuántos entraron, cuántos se
    facturaron solos y cuántos quedaron esperando a una persona.
    """
    cfg = config_manager.load()
    try:
        nuevos = await ingerir(cfg, dias=dias, referencias_a_omitir=referencias_a_omitir)
    except ValueError:
        logger.error("No hay Access Token de MercadoPago configurado.")
        return {"error": "sin_token"}
    except MercadoPagoNoContesta as e:
        return {"error": str(e)}

    registro_de_clientes = el_registro(registro)
    facturados = pendientes = 0
    for mov in nuevos:
        # 🔑 Por `resolver_cliente_pago` y no por un match propio. Este es el
        # camino que se quedó afuera cuando se agregaron los alias.
        client = registro_de_clientes.resolver(mov.payer_email, mov.payer_id_number)
        contexto = {"descripcion": mov.descripcion, "monto": mov.monto}

        if not (client and debe_auto_facturar(client, contexto)):
            pendientes += 1
            razon = "sin cliente registrado" if not client else "sin criterio de auto-facturación"
            logger.info(
                "Pendiente (%s): %s $%.2f",
                razon, mov.payer_name or mov.payment_id, mov.monto,
            )
            continue

        try:
            factura_id, numero, tipo_lb, mail = await mp_facturacion.generar_factura_mp(
                monto=mov.monto,
                payer_email=client.get("email") or mov.payer_email,
                payer_name=client["name"],
                referencia=f"Transferencia MP#{mov.payment_id}",
                cfg=cfg,
                concepto_override=mov.descripcion,
                cliente_override=client,
                payment_type=mov.payment_type,
                registro=registro,
            )
            db_mp.update_mp_movimiento_estado(mov.mov_id, "facturado", factura_id=factura_id)
            facturados += 1
            logger.info("Factura %s %s → %s | email=%s", tipo_lb, numero, client["name"], mail)
            if not mail:
                logger.warning("Sin email para %s — revisá el cliente.", client["name"])
        except Exception as e:
            # Queda en la bandeja: alguien la emite a mano desde la pantalla.
            logger.error("Error al facturar el movimiento %s: %s", mov.payment_id, e)
            pendientes += 1

    resumen = {"nuevos": len(nuevos), "facturados": facturados, "pendientes": pendientes}
    logger.info(
        "Sync terminado — %(nuevos)d nuevo(s), %(facturados)d facturado(s), "
        "%(pendientes)d pendiente(s) en bandeja", resumen,
    )
    return resumen


def main(argv=None) -> dict:
    """Punto de entrada del cron.

    El producto lo invoca con un script de una línea; acá vive el
    comportamiento para que no haya seis copias del mismo `argparse`.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Sync automático de MercadoPago")
    parser.add_argument("--dias", type=int, default=2,
                        help="Días hacia atrás a sincronizar (default: 2)")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return asyncio.run(sincronizar_y_facturar(dias=args.dias))
