"""Factory del router JSON de las pre facturas (ADR-030), compartido por los productos.

El dominio (el ciclo, la numeración, el PDF, el correo) es de `libracore.pre_facturas`;
acá sólo está la capa HTTP. Mismo criterio que `presupuestos_router` y
`comprobantes_router`: el paquete arma el router y **el producto lo monta con su propio
gate** y le dice quién es el usuario.

    app.include_router(
        build_pre_facturas_router(
            origen_producto="libracargo", origen_instancia=INSTANCIA,
            usuario_actual=lambda request: request.state.usuario,
            dependencies=[Depends(require_admin)],
            al_crear=reservar_ordenes, al_anular=liberar_ordenes,
        ),
    )

## De quién es cada pre factura

`origen_producto` y `origen_instancia` los fija **el producto al armar el router**, no el
cuerpo del request: definen la numeración (`PF-0001` es por producto e instancia) y qué
pre facturas se ven. El router sólo lista y toca las suyas; las de otro origen dan 404.

## Los ganchos del producto

`al_crear`, `al_editar` y `al_anular` reciben `(conn, pre_factura, datos)` y corren **en la
misma transacción** que el cambio: lo que el producto escriba ahí (reservar o liberar las
órdenes de la pre factura) se confirma o se deshace junto con ella. Si el gancho levanta
una excepción —típicamente `HTTPException(409, "la orden 7 ya está reservada")`—, no queda
nada escrito.

- `pre_factura` es la pre factura **ya escrita** (con su `id` y su `numero_interno`).
- `datos` es el cuerpo del request tal cual llegó, con las claves propias del producto:
  `CrearPayload` y `EditarPayload` aceptan claves de más (`extra="allow"`, como
  `FacturaPayload`) justamente para que el producto mande sus `orden_ids` y los lea acá.
  Para `al_anular` es `{"motivo": ...}`.
- `al_editar` corre también cuando no cambió nada del motor (el producto puede haber
  cambiado sus órdenes), y recibe la pre factura como quedó.

El «facturar» **no está acá**: lo hace el producto con su camino de emisión y después llama
a `pre_facturas.marcar_facturada(id, factura_id, conn=...)` en la misma transacción.
"""
import contextlib
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from libracore import pre_facturas as dominio
from libracore.db import comprobantes_pendientes as bandeja
from libracore.db.core import get_connection
from libracore.validacion import sin_booleanos

_PREFIJO = "/api/pre-facturas"
_TAG = "pre_facturas"


class ItemPayload(BaseModel):
    description: str
    qty: float
    unit_price: float
    # Fracción (`0.21`), por ítem, como en la bandeja. Un comprobante clase C va con 0.
    iva_rate: float = 0.21
    detalle: str = ""

    _no_son_booleanos = sin_booleanos("qty", "unit_price", "iva_rate")


class CrearPayload(BaseModel):
    """`extra="allow"` **a propósito**: el producto le suma sus propias claves (las
    órdenes de la pre factura) y las lee desde su gancho `al_crear`. El motor no las
    conoce ni las guarda."""

    model_config = ConfigDict(extra="allow")

    cliente_razon: str
    items: list[ItemPayload]
    cliente_id: int | None = None
    cliente_cuit: str = ""
    cliente_domicilio: str = ""
    emisor_id: int | None = None
    tipo_comprobante: int | None = None
    fecha_sugerida: str = ""
    fecha_vencimiento_pago: str = ""
    periodo_desde: str = ""
    periodo_hasta: str = ""
    concepto: str = ""
    condicion_venta: str = ""
    observaciones: str = ""
    origen_tipo: str = bandeja.ORIGEN_PRE_FACTURA
    origen_id: str | None = None

    _no_son_booleanos = sin_booleanos("cliente_id", "emisor_id", "tipo_comprobante")


class EditarPayload(BaseModel):
    """Lo que se manda se cambia; lo que no, se queda. `extra="allow"` por lo mismo que
    `CrearPayload`."""

    model_config = ConfigDict(extra="allow")

    cliente_razon: str | None = None
    items: list[ItemPayload] | None = None
    cliente_id: int | None = None
    cliente_cuit: str | None = None
    cliente_domicilio: str | None = None
    emisor_id: int | None = None
    tipo_comprobante: int | None = None
    fecha_sugerida: str | None = None
    fecha_vencimiento_pago: str | None = None
    periodo_desde: str | None = None
    periodo_hasta: str | None = None
    concepto: str | None = None
    condicion_venta: str | None = None
    observaciones: str | None = None

    _no_son_booleanos = sin_booleanos("cliente_id", "emisor_id", "tipo_comprobante")


class EmailPayload(BaseModel):
    email: str
    asunto: str = ""
    cuerpo: str = ""


class AnularPayload(BaseModel):
    motivo: str = ""


@contextlib.contextmanager
def _traducir():
    """Los errores del dominio como los códigos HTTP que corresponden."""
    try:
        yield
    except dominio.PreFacturaNoEncontrada as e:
        raise HTTPException(404, str(e))
    except (dominio.TransicionInvalida, dominio.PreFacturaYaExiste) as e:
        raise HTTPException(409, str(e))
    except dominio.SmtpNoConfigurado as e:
        raise HTTPException(400, str(e))
    except (ValueError, TypeError) as e:  # incluye EmisorDesconocido
        raise HTTPException(422, str(e))


@contextlib.contextmanager
def _transaccion():
    """Una conexión para el cambio y el gancho del producto: se confirma entera o no se
    confirma nada."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def build_pre_facturas_router(
    *,
    origen_producto: str,
    origen_instancia: str = "",
    usuario_actual: Callable[[Request], str] | None = None,
    smtp_resolver: Callable[[], Any] | None = None,
    emisor_del_pdf: Callable[[dict], dict | None] | None = None,
    al_crear: Callable[[Any, dict, dict], None] | None = None,
    al_editar: Callable[[Any, dict, dict], None] | None = None,
    al_anular: Callable[[Any, dict, dict], None] | None = None,
    donde_configurar_smtp: str = "Configuración → Email",
    dependencies: list | None = None,
    prefix: str = _PREFIJO,
) -> APIRouter:
    """Las rutas de la pre factura, con lo del producto inyectado.

    - `origen_producto` / `origen_instancia`: de quién son las pre facturas (ver arriba).
    - `usuario_actual`: `Request -> str`, el nombre con que se asienta quién aceptó o
      anuló. No se toma del cuerpo a propósito: es la trazabilidad de quién dio la
      conformidad. Por defecto queda vacío.
    - `smtp_resolver`: el resolver de SMTP de libraauth, como en `smtp_efectivo`.
    - `emisor_del_pdf`: `pre_factura -> dict | None`, los datos del emisor para el PDF
      (`nombre`, `cuit`, `direccion`, `iva_condition`...). Un producto con varias
      razones sociales devuelve los de la que factura esa pre factura.
    - `al_crear`, `al_editar`, `al_anular`: `(conn, pre_factura, datos)`, en la misma
      transacción (ver el docstring del módulo).
    - `dependencies`: el gate de auth del producto, aplicado a todas las rutas.
    """
    router = APIRouter(prefix=prefix, tags=[_TAG], dependencies=dependencies)

    # Es una dependencia y no un `request: Request` en cada endpoint: así el endpoint no
    # declara el `Request` él mismo y la guardia de cuerpos sin tipar (ADR-015) no lo toma por
    # uno que lee el cuerpo a mano.
    def _usuario(request: Request) -> str:
        if usuario_actual is None:
            return ""
        try:
            return usuario_actual(request) or ""
        except Exception:
            return ""

    def _propia(pre_factura_id: int, conn=None) -> dict:
        """La pre factura, si es de este producto e instancia; si no, 404 (no se revela
        que existe una de otro origen)."""
        pf = dominio.get(pre_factura_id, conn=conn)
        if (pf is None or pf["origen_producto"] != origen_producto
                or pf["origen_instancia"] != origen_instancia):
            raise HTTPException(404, "Pre factura no encontrada")
        return pf

    def _emisor(pf: dict) -> dict | None:
        return emisor_del_pdf(pf) if emisor_del_pdf else None

    @router.get("")
    def listar(estado: str = "", cliente: str = "", cliente_id: int | None = None,
               limit: int | None = None):
        if estado and estado not in dominio.ESTADOS:
            raise HTTPException(422, f"Estado inválido: {estado!r}.")
        propio = dict(origen_producto=origen_producto, origen_instancia=origen_instancia)
        return {
            "items": dominio.listar(estado=estado or None, cliente=cliente,
                                    cliente_id=cliente_id, limit=limit, **propio),
            "counts": dominio.contar_por_estado(**propio),
        }

    @router.post("", status_code=201)
    def crear(payload: CrearPayload):
        datos = payload.model_dump()
        with _traducir(), _transaccion() as conn:
            pf = dominio.crear(
                origen_producto=origen_producto, origen_instancia=origen_instancia,
                origen_tipo=payload.origen_tipo, origen_id=payload.origen_id,
                cliente_razon=payload.cliente_razon, cliente_id=payload.cliente_id,
                cliente_cuit=payload.cliente_cuit, cliente_domicilio=payload.cliente_domicilio,
                items=[i.model_dump() for i in payload.items], emisor_id=payload.emisor_id,
                tipo_comprobante=payload.tipo_comprobante, fecha_sugerida=payload.fecha_sugerida,
                fecha_vencimiento_pago=payload.fecha_vencimiento_pago,
                periodo_desde=payload.periodo_desde, periodo_hasta=payload.periodo_hasta,
                concepto=payload.concepto, condicion_venta=payload.condicion_venta,
                observaciones=payload.observaciones, conn=conn)
            if al_crear:
                al_crear(conn, pf, datos)
        return pf

    @router.get("/{pre_factura_id}")
    def detalle(pre_factura_id: int):
        return _propia(pre_factura_id)

    @router.put("/{pre_factura_id}")
    def editar(pre_factura_id: int, payload: EditarPayload):
        datos = payload.model_dump()
        campos = {k: v for k, v in payload.model_dump(exclude_unset=True).items()
                  if k in dominio.EDITABLES}
        # `None` explícito en un texto no es «vaciar», es un descuido del cliente: los
        # únicos campos que admiten `None` son los opcionales numéricos.
        for k in [k for k, v in campos.items()
                  if v is None and k not in ("cliente_id", "emisor_id", "tipo_comprobante")]:
            del campos[k]
        with _traducir(), _transaccion() as conn:
            _propia(pre_factura_id, conn)
            pf = dominio.editar(pre_factura_id, conn=conn, **campos)
            if al_editar:
                al_editar(conn, pf, datos)
        return pf

    @router.get("/{pre_factura_id}/pdf")
    def descargar_pdf(pre_factura_id: int):
        pf = _propia(pre_factura_id)
        with _traducir():
            contenido = dominio.pdf(pre_factura_id, emisor=_emisor(pf))
        return Response(
            contenido, media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{pf["numero_interno"]}.pdf"'})

    @router.post("/{pre_factura_id}/enviar-email")
    def enviar_email(pre_factura_id: int, payload: EmailPayload):
        pf = _propia(pre_factura_id)
        with _traducir():
            try:
                return dominio.enviar_por_correo(
                    pre_factura_id, payload.email, smtp_resolver=smtp_resolver,
                    asunto=payload.asunto, cuerpo=payload.cuerpo, emisor=_emisor(pf))
            except dominio.SmtpNoConfigurado:
                raise HTTPException(400, f"Configurá el servidor SMTP en {donde_configurar_smtp}.")
            except OSError as e:  # `smtplib.SMTPException` y los errores de red son OSError
                raise HTTPException(502, f"Error al enviar: {e}")

    @router.post("/{pre_factura_id}/aceptar")
    def aceptar(pre_factura_id: int, usuario: str = Depends(_usuario)):
        _propia(pre_factura_id)
        with _traducir():
            return dominio.marcar_aceptada(pre_factura_id, usuario)

    @router.post("/{pre_factura_id}/anular")
    def anular(pre_factura_id: int, payload: AnularPayload, usuario: str = Depends(_usuario)):
        with _traducir(), _transaccion() as conn:
            _propia(pre_factura_id, conn)
            pf = dominio.anular(pre_factura_id, usuario, payload.motivo, conn=conn)
            if al_anular:
                al_anular(conn, pf, payload.model_dump())
        return pf

    return router

