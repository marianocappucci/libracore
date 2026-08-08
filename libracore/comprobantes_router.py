"""Los dos routers de la bandeja de comprobantes pendientes.

Mismo criterio que `config_router` y que `libraauth.build_logs_router`: el
paquete arma el router, el **producto lo monta con su propio gate**. Acá eso
importa más que en otros módulos, porque los dos routers no se protegen igual:

    # lo que deposita otro producto de la familia, sin humano detrás
    app.include_router(
        build_comprobantes_ingesta_router(),
        dependencies=[Depends(solo_token_de_servicio)],
    )
    # la bandeja que mira una persona
    app.include_router(
        build_comprobantes_bandeja_router(),
        dependencies=[Depends(require_admin)],
    )

Van en **dos routers con el mismo prefijo** y no en uno con guards por endpoint
porque FastAPI evalúa las dependencias del router antes que las de la ruta — el
mismo motivo por el que los datos de empresa están partidos en `config_router`.

El gate de ingesta que corresponde es el token de servicio de libraauth
(`libraauth.session_auth.json_api_require_admin_o_servicio`), que falla cerrado
si `LIBRA_SERVICE_TOKEN` no está definido. Este paquete no lo importa: libracore
no depende de libraauth, y la elección del gate es del producto.
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from libracore import comprobantes_pendientes as dominio
from libracore.db import comprobantes_pendientes as db

_PREFIJO = "/api/comprobantes-pendientes"
_TAG = "comprobantes_pendientes"

# Cuántos resueltos muestra la bandeja. La lista de pendientes va entera —es la
# que hay que trabajar—; el historial es contexto y no tiene por qué crecer sin
# techo en la pantalla.
_TOPE_HISTORIAL = 50


class ItemPayload(BaseModel):
    description: str
    qty: float
    unit_price: float
    # Por ítem porque el productor puede tenerlo así (LibraDesk lo tiene desde
    # su revisión 0013). La factura lleva una sola tasa, y el aplastado —con
    # aviso— lo hace `comprobantes_pendientes.armar_prefill`.
    iva_rate: float = 0.21


class ComprobantePayload(BaseModel):
    origen_producto: str
    origen_tipo: str
    origen_id: str
    cliente_razon: str
    items: list[ItemPayload]
    origen_instancia: str = ""
    cliente_id: int | None = None
    cliente_cuit: str = ""
    cliente_domicilio: str = ""
    fecha_sugerida: str = ""
    periodo_desde: str = ""
    periodo_hasta: str = ""
    concepto: str = ""
    condicion_venta: str = ""
    observaciones: str = ""


class IdsPayload(BaseModel):
    ids: list[int]


class MarcarFacturadoPayload(BaseModel):
    ids: list[int]
    factura_id: int


class DescartarPayload(BaseModel):
    motivo: str = ""


def build_comprobantes_ingesta_router() -> APIRouter:
    """Lo que un producto de la familia deposita. **Montar detrás del token de
    servicio**, nunca abierto."""
    router = APIRouter(prefix=_PREFIJO, tags=[_TAG])

    @router.post("", status_code=201)
    def depositar(payload: ComprobantePayload):
        try:
            comprobante_id, creado = db.upsert_comprobante(
                origen_producto=payload.origen_producto,
                origen_tipo=payload.origen_tipo,
                origen_id=payload.origen_id,
                cliente_razon=payload.cliente_razon,
                items=[i.model_dump() for i in payload.items],
                origen_instancia=payload.origen_instancia,
                cliente_id=payload.cliente_id,
                cliente_cuit=payload.cliente_cuit,
                cliente_domicilio=payload.cliente_domicilio,
                fecha_sugerida=payload.fecha_sugerida,
                periodo_desde=payload.periodo_desde,
                periodo_hasta=payload.periodo_hasta,
                concepto=payload.concepto,
                condicion_venta=payload.condicion_venta,
                observaciones=payload.observaciones,
            )
        except db.ComprobanteYaResuelto as e:
            # 409 y no 400: el productor no hizo nada mal —reintentar es lo
            # correcto—, pero acá ya hay una resolución humana que su reenvío
            # no puede revertir. Con este código el emisor sabe que puede
            # marcar el origen y dejar de insistir.
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"id": comprobante_id, "creado": creado}

    return router


def build_comprobantes_bandeja_router(usuario_actual=None) -> APIRouter:
    """La bandeja que mira una persona. **Montar detrás del gate de admin.**

    `usuario_actual` es un callable `Request -> str` con el que el producto dice
    quién resolvió cada comprobante; por defecto queda vacío. No se toma del
    payload a propósito: sería un campo que el cliente elige, y esto es la
    trazabilidad de quién aprobó facturarle algo a alguien.
    """
    router = APIRouter(prefix=_PREFIJO, tags=[_TAG])

    def _usuario(request: Request) -> str:
        if usuario_actual is None:
            return ""
        try:
            return usuario_actual(request) or ""
        except Exception:
            return ""

    # ⚠️ Las rutas estáticas van **antes** de `/{comprobante_id}`: FastAPI
    # resuelve por orden de declaración, y si el parámetro va primero,
    # `/facturar-prefill` entra ahí y muere en un 422 de tipo en vez de llegar
    # a su endpoint.
    @router.get("")
    def bandeja():
        return {
            "pendientes": db.list_por_estado(db.ESTADO_PENDIENTE),
            "facturados": db.list_por_estado(db.ESTADO_FACTURADO,
                                             limit=_TOPE_HISTORIAL),
            "descartados": db.list_por_estado(db.ESTADO_DESCARTADO,
                                              limit=_TOPE_HISTORIAL),
            "total_pendientes": db.contar_pendientes(),
        }

    @router.post("/facturar-prefill")
    def facturar_prefill(payload: IdsPayload):
        """Arma el formulario de la factura. **No escribe nada** y no cambia
        ningún estado: recién cuando ARCA devuelve el CAE se marcan los
        pendientes, con `/marcar-facturado`."""
        comprobantes = db.get_comprobantes(payload.ids)
        if len(comprobantes) != len(set(payload.ids)):
            raise HTTPException(404, "Alguno de los comprobantes no existe")
        no_pendientes = [c["id"] for c in comprobantes
                         if c["estado"] != db.ESTADO_PENDIENTE]
        if no_pendientes:
            raise HTTPException(
                409,
                "Estos comprobantes ya estaban resueltos: "
                + ", ".join(str(i) for i in no_pendientes),
            )
        try:
            return dominio.armar_prefill(comprobantes)
        except dominio.ClientesMezclados as e:
            raise HTTPException(422, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.post("/marcar-facturado")
    def marcar_facturado(payload: MarcarFacturadoPayload, request: Request):
        """Cierra los pendientes que quedaron cubiertos por una factura ya
        emitida.

        Devuelve por separado los que movió y los que no, en vez de fallar
        entero: para cuando esto se llama la factura **ya existe y tiene CAE**,
        así que un 500 acá no desharía nada y dejaría la bandeja peor que un
        informe de qué quedó sin marcar.
        """
        usuario = _usuario(request)
        marcados, ya_resueltos = [], []
        for comprobante_id in payload.ids:
            if db.marcar_facturado(comprobante_id, payload.factura_id, usuario):
                marcados.append(comprobante_id)
            else:
                ya_resueltos.append(comprobante_id)
        return {"marcados": marcados, "ya_resueltos": ya_resueltos}

    @router.get("/{comprobante_id}")
    def detalle(comprobante_id: int):
        comprobante = db.get_comprobante(comprobante_id)
        if not comprobante:
            raise HTTPException(404, "Comprobante no encontrado")
        return comprobante

    @router.post("/{comprobante_id}/descartar")
    def descartar(comprobante_id: int, payload: DescartarPayload,
                  request: Request):
        if not db.get_comprobante(comprobante_id):
            raise HTTPException(404, "Comprobante no encontrado")
        if not db.descartar(comprobante_id, payload.motivo, _usuario(request)):
            raise HTTPException(409, "El comprobante ya estaba resuelto")
        return db.get_comprobante(comprobante_id)

    return router
