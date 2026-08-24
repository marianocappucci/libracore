"""La bandeja de MercadoPago: los cobros que entraron y todavía no son factura.

Es la pantalla donde una persona mira lo que MercadoPago cobró y decide. Tiene
dos listas porque MercadoPago devuelve dos cosas distintas por el mismo
endpoint: **pagos** (lo que llegó por el webhook) y **movimientos** (lo que
aparece al sincronizar hacia atrás, típicamente transferencias entrantes).

Migrada desde `app/web/api/mp_bandeja.py` de Contalibra. Restolibra tenía su
copia, con la misma divergencia que el resto de su MercadoPago: llamaba a
`generar_factura_mp` **sin** `payer_cuit`, así que ni siquiera podía llegar a un
alias por CUIT. Acá los dos botones de *Facturar* pasan el CUIT del pagador.

## Los dos filtros que NO están, y no es un olvido

> 🔴 **No reintroducir un filtro por `operation_type`/`payer_email` propio.**
> Contalibra los tuvo nueve días (2026-07-05 → 2026-07-14) para descartar
> auto-fondeos con tarjeta propia, y una transferencia **real** de un cliente
> quedó invisible en la bandeja: MercadoPago marca `account_fund` con el email
> propio a *cualquier* movimiento que no sea un pago clásico de un tercero,
> transferencias reales incluidas. Decisión explícita del humano: entra todo, y
> lo que resulte ser plata propia se descarta a mano.

Lo que sí se filtra es distinto y no tiene ese riesgo: los cobros de **otro
collector** (o sea, de otra cuenta) y los que ya están registrados.
"""

import datetime
import logging
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from libracore import (
    config_manager,
    email_sender,
    mp_facturacion,
    mp_sync,
    pdf_generator as pdf_gen,
)
from libracore.registro_de_clientes import RegistroDeClientes, el_registro
from libracore.db import facturas as db_facturas
from libracore.db import mp as db_mp

logger = logging.getLogger(__name__)

class SincronizarPayload(BaseModel):
    dias: int = 7


class CrearClientePayload(BaseModel):
    nombre: str
    email: str = ""
    cuit_dni: str = ""
    iva_condition: str = "Consumidor Final"
    address: str = ""


class GuardarDatosPayload(BaseModel):
    payer_email: str = ""
    payer_name: str = ""
    payer_id_type: str = ""
    payer_id_number: str = ""


class FacturarPayload(BaseModel):
    concepto: str = ""


class PagoDeDemo(BaseModel):
    """Un cobro como los que devolvería MercadoPago, escrito a mano."""

    mp_payment_id: str
    monto: float
    payer_name: str = ""
    payer_email: str = ""
    payer_id_number: str = ""
    payment_type: str = "account_money"
    payment_method: str = "account_money"
    descripcion: str = ""
    #: `pago` va a la lista de cobros; `transferencia` a la de movimientos
    #: bancarios entrantes. La pantalla tiene las dos solapas y con una sola
    #: llena queda a medias.
    clase: str = "pago"


def _con_cliente(items: list, registro: RegistroDeClientes) -> list:
    """Le cuelga a cada fila el cliente que le correspondería, si lo hay.

    Se resuelve **en dos consultas para toda la lista** y no una por fila: la
    bandeja trae hasta 60 filas entre las cuatro listas y el N+1 se nota.
    """
    emails = {p["payer_email"] for p in items if p.get("payer_email")}
    cuits = {
        (p.get("payer_id_number") or "").replace("-", "").strip()
        for p in items if p.get("payer_id_number")
    }
    cuits.discard("")

    por_email, por_cuit = registro.buscar_muchos(emails, cuits)

    for p in items:
        cuit = (p.get("payer_id_number") or "").replace("-", "").strip()
        p["cliente"] = por_email.get(p.get("payer_email") or "") or (
            por_cuit.get(cuit) if cuit else None
        )
    return items


def _crear_cliente_si_no_esta(
    payload: CrearClientePayload, registro: RegistroDeClientes
) -> None:
    if not payload.nombre.strip():
        raise HTTPException(422, "El nombre es obligatorio.")
    # Se resuelve por el mismo camino que usa la facturacion: si el registro ya
    # lo encuentra --por alias o por match-- no se crea uno nuevo al lado.
    if registro.resolver(payload.email, payload.cuit_dni):
        return
    registro.crear(
        nombre=payload.nombre, email=payload.email, cuit_dni=payload.cuit_dni,
        iva_condition=payload.iva_condition, address=payload.address,
    )


def _cfg_con_concepto(concepto: str) -> dict:
    cfg = config_manager.load()
    if concepto.strip():
        return {**cfg, "mp_concepto_descripcion": concepto.strip()}
    return cfg


def build_mp_bandeja_router(
    *,
    prefix: str = "/api/mp-bandeja",
    referencias_a_omitir: tuple[str, ...] = (),
    permitir_siembra_de_demo: bool | None = None,
    registro: RegistroDeClientes | None = None,
) -> APIRouter:
    """La bandeja. Va detrás del gate de rol del producto.

    `referencias_a_omitir` son prefijos de `external_reference` que la
    sincronización **no** debe traer a la bandeja porque el producto ya los
    maneja por otro lado — en Contalibra, `"venta-"`: esos cobros pertenecen a
    una venta presencial y su factura sale del circuito de ventas.

    `permitir_siembra_de_demo` decide si existe la ruta de siembra. Por omisión
    lo dice `DEMO_MODE`, **leído al armar el router**: en la instancia de un
    cliente la ruta no existe, no es un `if` adentro del endpoint.
    """
    router = APIRouter(prefix=prefix, tags=["mp_bandeja"])
    registro_de_clientes = el_registro(registro)

    @router.get("")
    def bandeja():
        cfg = config_manager.load()
        return {
            "pendientes": _con_cliente(db_mp.get_mp_pagos_by_estado("pendiente"), registro_de_clientes),
            "historial": _con_cliente(db_mp.get_mp_pagos_historial(limit=30), registro_de_clientes),
            "transferencias": _con_cliente(db_mp.get_mp_movimientos_by_estado("pendiente"), registro_de_clientes),
            "transferencias_hist": _con_cliente(db_mp.get_mp_movimientos_historial(limit=30), registro_de_clientes),
            "mp_concepto_default": cfg.get("mp_concepto_descripcion", "") or "",
        }

    @router.post("/sincronizar")
    async def sincronizar(payload: SincronizarPayload):
        """Trae de MercadoPago lo que no llegó por webhook.

        🔑 **Es la misma función que corre el cron nocturno** (`mp_sync.ingerir`),
        no una segunda copia. Tenerlas separadas es lo que dejó al cron afuera
        del cambio que introdujo los alias de facturación, y costó dos
        comprobantes emitidos al CUIT equivocado.
        """
        try:
            nuevos = await mp_sync.ingerir(
                config_manager.load(),
                dias=payload.dias,
                referencias_a_omitir=referencias_a_omitir,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except mp_sync.MercadoPagoNoContesta:
            raise HTTPException(502, "No se pudo sincronizar con MercadoPago.") from None
        return {"nuevos": len(nuevos)}

    # ── Pagos ───────────────────────────────────────────────────────────────

    @router.post("/pagos/{mp_pago_id}/ignorar")
    def ignorar_pago(mp_pago_id: int):
        db_mp.update_mp_pago_estado(mp_pago_id, "ignorado")
        return {"ok": True}

    @router.post("/pagos/{mp_pago_id}/crear-cliente")
    def crear_cliente_pago(mp_pago_id: int, payload: CrearClientePayload):
        if not db_mp.get_mp_pago_by_id(mp_pago_id):
            raise HTTPException(404, "Pago no encontrado")
        _crear_cliente_si_no_esta(payload, registro_de_clientes)
        return {"ok": True}

    @router.post("/pagos/{mp_pago_id}/facturar")
    async def facturar_pago(mp_pago_id: int, payload: FacturarPayload):
        pago = db_mp.get_mp_pago_by_id(mp_pago_id)
        if not pago or pago.get("estado_factura") != "pendiente":
            # 404 y no 409: para la pantalla, un pago ya facturado no está en
            # la lista de pendientes. Ver uno acá significa que la vista quedó
            # vieja, y el mensaje tiene que mandar a refrescar.
            raise HTTPException(404, "Pago no encontrado o ya procesado")

        factura_id, numero, tipo_label, mail = await mp_facturacion.generar_factura_mp(
            monto=float(pago["monto"]),
            payer_email=pago["payer_email"] or "",
            payer_name=pago["payer_name"] or "",
            referencia=f"MP#{pago['mp_payment_id']}",
            cfg=_cfg_con_concepto(payload.concepto),
            payment_type=pago.get("payment_type") or "",
            # 🔑 El CUIT del pagador va SIEMPRE: sin él, un alias por CUIT no
            # puede resolver. Es lo que le faltaba a la copia de Restolibra.
            payer_cuit=pago.get("payer_id_number") or "",
            registro=registro,
        )
        db_mp.update_mp_pago_estado(mp_pago_id, "facturado", factura_id=factura_id)
        return {"factura_id": factura_id, "numero": numero,
                "tipo_label": tipo_label, "email_sent": mail}

    # ── Movimientos (transferencias entrantes) ──────────────────────────────

    @router.post("/movimientos/{mov_id}/guardar-datos")
    def guardar_datos_movimiento(mov_id: int, payload: GuardarDatosPayload):
        """Completar a mano los datos del pagador.

        Una transferencia entrante suele llegar sin CUIT ni email, y sin eso no
        hay a quién facturarle.
        """
        db_mp.update_mp_movimiento_datos(
            mov_id,
            payer_email=payload.payer_email.strip() or None,
            payer_name=payload.payer_name.strip() or None,
            payer_id_type=payload.payer_id_type.strip() or None,
            payer_id_number=payload.payer_id_number.strip() or None,
        )
        return {"ok": True}

    @router.post("/movimientos/{mov_id}/crear-cliente")
    def crear_cliente_movimiento(mov_id: int, payload: CrearClientePayload):
        if not db_mp.get_mp_movimiento_by_id(mov_id):
            raise HTTPException(404, "Movimiento no encontrado")
        _crear_cliente_si_no_esta(payload, registro_de_clientes)
        db_mp.update_mp_movimiento_datos(
            mov_id, payer_email=payload.email or None,
            payer_name=payload.nombre or None,
            payer_id_number=payload.cuit_dni or None,
        )
        return {"ok": True}

    @router.post("/movimientos/{mov_id}/ignorar")
    def ignorar_movimiento(mov_id: int):
        db_mp.update_mp_movimiento_estado(mov_id, "ignorado")
        return {"ok": True}

    @router.post("/movimientos/{mov_id}/facturar")
    async def facturar_movimiento(mov_id: int, payload: FacturarPayload):
        mov = db_mp.get_mp_movimiento_by_id(mov_id)
        if not mov or mov.get("estado_factura") != "pendiente":
            raise HTTPException(404, "Movimiento no encontrado o ya procesado")

        factura_id, numero, tipo_label, mail = await mp_facturacion.generar_factura_mp(
            monto=float(mov["monto"]),
            payer_email=mov["payer_email"] or "",
            payer_name=mov["payer_name"] or mov["origen_nombre"] or "",
            referencia=f"Transferencia MP#{mov['mp_movement_id']}",
            cfg=_cfg_con_concepto(payload.concepto),
            payment_type=mov.get("tipo") or "",
            payer_cuit=mov.get("payer_id_number") or "",
            registro=registro,
        )
        db_mp.update_mp_movimiento_estado(mov_id, "facturado", factura_id=factura_id)
        return {"factura_id": factura_id, "numero": numero,
                "tipo_label": tipo_label, "email_sent": mail}

    # ── Reenviar el comprobante ─────────────────────────────────────────────

    @router.post("/facturas/{factura_id}/reenviar")
    def reenviar_email(factura_id: int):
        factura = db_facturas.get_factura(factura_id)
        if not factura:
            raise HTTPException(404, "Factura no encontrada")

        cfg = config_manager.load()
        smtp_host = cfg.get("email_smtp_host", "")
        smtp_user = cfg.get("email_smtp_user", "")
        smtp_pass = cfg.get("email_smtp_password", "")
        from_email = cfg.get("email_from", "")
        if not (smtp_host and smtp_user and smtp_pass and from_email):
            raise HTTPException(400, "Configurá el servidor SMTP en Configuración → Email.")

        destino = factura.get("cliente_email", "") or ""
        if not destino:
            # Por el registro, no por la tabla: en un producto cuyos
            # clientes viven en otro motor, `db_clients` esta vacia y el
            # comprobante se quedaria sin destinatario sin decir por que.
            cliente = registro_de_clientes.resolver("", factura.get("cliente_cuit", ""))
            destino = (cliente or {}).get("email", "")
        if not destino:
            raise HTTPException(422, "El cliente no tiene email registrado.")

        pdf_path = factura.get("pdf_path", "")
        if not pdf_path or not os.path.exists(pdf_path):
            # El PDF se regenera y no se falla: el comprobante ya existe en
            # ARCA, y que su archivo se haya perdido no es motivo para no
            # poder mandarlo.
            pdf_path = pdf_gen.generate_pdf_factura(factura)

        tipo_lb = pdf_gen._TIPO_LABELS.get(factura.get("tipo"), "Factura")
        etiqueta = (
            f"{tipo_lb} {str(factura.get('punto_venta', 1)).zfill(4)}"
            f"-{str(factura.get('numero', 0)).zfill(8)}"
        )
        try:
            email_sender.enviar_comprobante(
                to_email=destino, to_name=factura.get("cliente_razon", ""),
                pdf_path=pdf_path, empresa_nombre=cfg.get("empresa_nombre", ""),
                factura_label=etiqueta, total=float(factura.get("total", 0)),
                smtp_host=smtp_host,
                smtp_port=int(cfg.get("email_smtp_port", "587") or "587"),
                smtp_user=smtp_user, smtp_password=smtp_pass,
                from_email=from_email, from_name=cfg.get("email_from_name", ""),
            )
        except Exception as e:
            logger.error("Error reenviando el comprobante %s: %s", factura_id, e)
            raise HTTPException(502, f"Error al enviar: {e}") from None
        return {"ok": True}

    # ── Siembra de la bandeja, SÓLO en demos ────────────────────────────────
    #
    # 🔴 La bandeja se llena sincronizando contra MercadoPago de verdad, y una
    # demo pública no tiene cuenta de MP ni puede tenerla: la pantalla que mejor
    # muestra el producto —el cobro entra y se factura solo— se abría vacía.
    #
    # **La ruta no existe fuera de una demo**, y eso no se resuelve con un `if`
    # adentro del endpoint: se decide al armar el router. En la instancia de un
    # cliente da 404 y ni siquiera figura en el openapi.

    es_demo = (
        permitir_siembra_de_demo
        if permitir_siembra_de_demo is not None
        else os.environ.get("DEMO_MODE", "").strip() in ("1", "true", "True")
    )

    if es_demo:

        @router.post("/demo/sembrar")
        def sembrar_bandeja_de_demo(items: list[PagoDeDemo]):
            """Idempotente por `mp_payment_id`: correrla de nuevo no duplica
            nada, que es lo que necesita el reset diario de la demo."""
            creados = 0
            for it in items:
                pid = it.mp_payment_id.strip()
                if not pid:
                    continue
                if db_mp.get_mp_pago(pid) or db_mp.get_mp_movimiento_by_mp_id(pid):
                    continue
                tipo_doc = "CUIT" if it.payer_id_number else ""
                if it.clase == "transferencia":
                    db_mp.create_mp_movimiento(
                        mp_movement_id=pid, tipo=it.payment_type, monto=it.monto,
                        fecha=datetime.date.today().isoformat(),
                        descripcion=it.descripcion, origen_nombre=it.payer_name,
                        origen_banco=it.payment_method, origen_cbu="",
                        payer_email=it.payer_email, payer_name=it.payer_name,
                        payer_id_type=tipo_doc, payer_id_number=it.payer_id_number,
                        estado_factura="pendiente",
                    )
                else:
                    db_mp.create_mp_pago(
                        mp_payment_id=pid, status="approved", monto=it.monto,
                        payer_email=it.payer_email, payer_name=it.payer_name,
                        estado_factura="pendiente", payment_type=it.payment_type,
                        payment_method=it.payment_method, descripcion_mp=it.descripcion,
                        payer_id_type=tipo_doc, payer_id_number=it.payer_id_number,
                    )
                creados += 1
            return {"creados": creados}

    return router
