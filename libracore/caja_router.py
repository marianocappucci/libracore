"""Caja, cajas y turnos de caja como factories de router (P9-M3).

Tres routers que Contalibra y Restolibra tenían escritos dos veces
(`web/api/caja.py`, `web/api/cajas.py`, `web/api/turnos.py`), byte a byte
salvo una cosa: Restolibra le había agregado a las cajas el **punto de venta
de ARCA por mostrador** (`punto_venta`, con el 409 cuando ya lo tiene otra
caja). Queda para los dos: es el modelo *usuario → turno abierto → caja → punto
de venta* que ya resuelve `db.caja.resolver_punto_venta`, y en una instancia
de un solo POS el campo queda vacío y no hay que tocarlo.

```python
app.include_router(build_caja_router(usuario_actual=get_current_user_json),
                   dependencies=[_auth_json, Depends(require_module("caja"))])
app.include_router(build_cajas_router(),
                   dependencies=[_auth_json, Depends(require_module("cajas"))])
app.include_router(build_turnos_router(usuario_actual=get_current_user_json,
                                       resumen_turno=db.get_resumen_turno,
                                       cerrar_turno=db.cerrar_turno),
                   dependencies=[_auth_json])
```

El de turnos recibe `resumen_turno` y `cerrar_turno` porque dependen de **dónde
viven las ventas**: los de `libracore.db.turnos` leen la tabla `ventas` de este
motor; un producto cuyas ventas están en LibraCommerce pasa los de
`libracommerce.erp.ventas`, y VentaLibra los que arquean sobre la caja.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from libracore import medios_pago, ticket_generator
from libracore.db import caja as db_caja
from libracore.db import cierre_diario as db_cierre_diario
from libracore.db import turnos as db_turnos
from libracore.db.caja import ExternalIdMercadoPagoInvalido, PuntoDeVentaRepetido
from libracore.db.cierre_diario import DiaCerradoError

# ── Caja: los movimientos ────────────────────────────────────────────────


class MovimientoPayload(BaseModel):
    fecha: str
    tipo: str  # ingreso | egreso
    concepto: str
    monto: float
    referencia: str = ""
    factura_id: int | None = None
    caja_id: int | None = None
    medio_pago: str = ""


def _periodo_actual():
    hoy = datetime.date.today()
    return hoy.replace(day=1).isoformat(), hoy.isoformat()


def build_caja_router(*, usuario_actual: Callable[..., Any], prefix: str = "/api/caja") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["caja"])

    @router.get("")
    def listar(desde: str = "", hasta: str = "", caja_id: int = 0):
        if not desde or not hasta:
            desde, hasta = _periodo_actual()
        return {
            "movimientos": db_caja.get_caja_movimientos(desde, hasta, caja_id=caja_id or None),
            "resumen": db_caja.get_caja_resumen(desde, hasta, caja_id=caja_id or None),
        }

    @router.post("")
    def crear(payload: MovimientoPayload, user: dict = Depends(usuario_actual)):
        concepto = payload.concepto.strip()
        if not concepto:
            raise HTTPException(422, "El concepto es obligatorio.")
        if payload.tipo not in ("ingreso", "egreso"):
            raise HTTPException(422, "Tipo inválido.")
        if payload.monto <= 0:
            raise HTTPException(422, "El monto debe ser mayor a cero.")
        mov_id = db_caja.create_caja_movimiento(
            payload.fecha, payload.tipo, concepto, payload.monto, payload.referencia.strip(),
            payload.factura_id, user.get("id"), caja_id=payload.caja_id,
            medio_pago=payload.medio_pago,
        )
        return {"id": mov_id}

    @router.delete("/{mov_id}")
    def eliminar(mov_id: int):
        """Anula el movimiento. **La fila queda.** La ruta sigue siendo `DELETE`
        para no romper al frontend, pero lo que hace es marcar `anulado=1`:
        sale de los totales del arqueo y la lista lo sigue mostrando."""
        db_caja.anular_caja_movimiento(mov_id)
        return {"ok": True}

    return router


# ── Cajas: los puntos de cobro ───────────────────────────────────────────


class CajaPayload(BaseModel):
    nombre: str
    descripcion: str = ""
    medios_pago: list[str] = []
    #: El punto de venta de ARCA de este mostrador. `None` deja la caja usando
    #: el de la empresa, que es como funcionan las instancias de un solo POS.
    punto_venta: int | None = None
    #: El `external_id` del POS de MercadoPago de esta caja. Cada caja con QR
    #: necesita el suyo propio; `None` o vacío deja la caja sin QR.
    mp_pos_id: str | None = None
    #: La sucursal de esta caja, para los productos con sedes. Sólo se usa al
    #: crear: una caja no se muda de sede (se da de baja y se crea otra donde
    #: corresponda). `None` deja la caja sin sucursal, que es lo de siempre.
    sucursal_id: int | None = None


class CajaUpdatePayload(CajaPayload):
    activo: bool = True


@dataclass(frozen=True)
class OpcionesCajas:
    """Lo que un producto le agrega a las cajas. Sin nada, el router hace lo
    que hacían Contalibra y Restolibra.

    Cada gancho decide con una `HTTPException` (el motor no sabe qué código le
    corresponde a la regla de cada producto: VentaLibra usa 422 y 409).

    - `validar_alta(payload)`: antes de crear (sucursal válida, medios válidos…).
    - `validar_edicion(payload, actual)`: antes de guardar; `actual` es la caja
      como está hoy, para saber si el cambio es una baja.
    - `al_desactivar(actual)`: después de guardar una caja que pasó de activa a
      inactiva (mover la predeterminada de su sucursal a otra activa).
    - `predeterminar(caja_id)`: reemplaza a `db.caja.set_default_caja`, que
      desmarca **todas**; un producto con sedes la quiere por sucursal.
    - `enriquecer(caja) -> caja`: campos de más en cada caja de la respuesta
      (el nombre de la sucursal, que el motor no conoce).
    - `autorizar_escritura`: quién puede crear, editar, predeterminar y borrar
      (una `Depends(...)`, como `autorizar_cierre` del cierre diario). El motor no
      sabe cómo se llama el rol admin de cada producto; sin esto, las escrituras
      quedan con la protección con la que el producto monte el router, que es
      la misma de la lectura.
    """

    validar_alta: Callable[[CajaPayload], None] | None = None
    validar_edicion: Callable[[CajaUpdatePayload, dict], None] | None = None
    al_desactivar: Callable[[dict], None] | None = None
    predeterminar: Callable[[int], None] | None = None
    enriquecer: Callable[[dict], dict] | None = None
    autorizar_escritura: Any = None


def build_cajas_router(*, prefix: str = "/api/cajas", opciones: OpcionesCajas | None = None) -> APIRouter:
    opt = opciones or OpcionesCajas()
    router = APIRouter(prefix=prefix, tags=["cajas"])
    # Ya viene envuelta en `Depends(...)`: se pasa tal cual, como en el cierre diario.
    escribe = [opt.autorizar_escritura] if opt.autorizar_escritura is not None else []

    def _salida(caja: dict, con_turno: set[int] | None = None) -> dict:
        """La caja con `tiene_turno_abierto` (el POS no ofrece una ocupada) y lo
        que le agregue el producto."""
        if con_turno is None:
            con_turno = db_turnos.cajas_con_turno_abierto()
        caja = {**caja, "tiene_turno_abierto": caja["id"] in con_turno}
        return opt.enriquecer(caja) if opt.enriquecer else caja

    @router.get("")
    def listar(sucursal_id: int | None = None):
        con_turno = db_turnos.cajas_con_turno_abierto()
        return [_salida(c, con_turno) for c in db_caja.get_all_cajas(sucursal_id=sucursal_id)]

    @router.get("/medios-disponibles")
    def medios_disponibles():
        return medios_pago.para_selector()

    @router.post("", dependencies=escribe)
    def crear(payload: CajaPayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        if opt.validar_alta:
            opt.validar_alta(payload)
        try:
            cid = db_caja.create_caja_config(
                nombre, payload.descripcion.strip(), payload.medios_pago,
                sucursal_id=payload.sucursal_id,
                punto_venta=payload.punto_venta,
                mp_pos_id=payload.mp_pos_id,
            )
        except PuntoDeVentaRepetido as choque:
            # 409 y no 422: el dato no está mal escrito, ya lo tiene otra caja.
            raise HTTPException(409, str(choque)) from choque
        except ExternalIdMercadoPagoInvalido as exc:
            raise HTTPException(422, str(exc)) from exc
        return _salida(db_caja.get_caja_config(cid))

    @router.put("/{cid}", dependencies=escribe)
    def actualizar(cid: int, payload: CajaUpdatePayload):
        actual = db_caja.get_caja_config(cid)
        if not actual:
            raise HTTPException(404, "Caja no encontrada")
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        if opt.validar_edicion:
            opt.validar_edicion(payload, actual)
        try:
            db_caja.update_caja_config(
                cid, nombre, payload.descripcion.strip(), payload.medios_pago,
                1 if payload.activo else 0, punto_venta=payload.punto_venta,
                mp_pos_id=payload.mp_pos_id,
            )
        except PuntoDeVentaRepetido as choque:
            raise HTTPException(409, str(choque)) from choque
        except ExternalIdMercadoPagoInvalido as exc:
            raise HTTPException(422, str(exc)) from exc
        if opt.al_desactivar and not payload.activo and actual.get("activo", 1):
            opt.al_desactivar(actual)
        return _salida(db_caja.get_caja_config(cid))

    @router.post("/{cid}/set-default", dependencies=escribe)
    def set_default(cid: int):
        if not db_caja.get_caja_config(cid):
            raise HTTPException(404, "Caja no encontrada")
        (opt.predeterminar or db_caja.set_default_caja)(cid)
        return _salida(db_caja.get_caja_config(cid))

    @router.delete("/{cid}", dependencies=escribe)
    def eliminar(cid: int):
        if not db_caja.get_caja_config(cid):
            raise HTTPException(404, "Caja no encontrada")
        try:
            db_caja.delete_caja_config(cid)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return {"ok": True}

    return router


# ── Turnos de caja ───────────────────────────────────────────────────────


class AbrirPayload(BaseModel):
    monto_inicial: float = 0
    notas: str = ""
    #: Sobre qué mostrador se abre. Opcional: sin cajas múltiples el turno se
    #: abre suelto, como siempre.
    caja_id: int | None = None


class CerrarPayload(BaseModel):
    monto_declarado: float = 0
    notas: str = ""


def _puede_ver(turno: dict, user: dict) -> bool:
    return user.get("role") == "admin" or turno["usuario_id"] == user.get("id")


def build_turnos_router(
    *,
    usuario_actual: Callable[..., Any],
    resumen_turno: Callable[[int], dict] = db_turnos.get_resumen_turno,
    cerrar_turno: Callable[[int, float, str], Any] = db_turnos.cerrar_turno,
    validar_apertura: Callable[[AbrirPayload, dict], None] | None = None,
    enriquecer: Callable[[dict], dict] | None = None,
    prefix: str = "/api/turnos",
) -> APIRouter:
    """Los turnos de caja.

    Además de las dos funciones que dependen de dónde viven las ventas, dos
    ganchos opcionales para un producto con cajas por sucursal:

    - `validar_apertura(payload, user)`: corre **antes** de abrir y decide con una
      `HTTPException`. Sin él, abrir con un turno ya abierto devuelve ese turno
      (lo de siempre); un producto que no lo quiere idempotente lo rechaza acá.
    - `enriquecer(turno) -> turno`: campos de más en cada turno de la respuesta
      (la caja y la sucursal donde está abierto).
    """
    router = APIRouter(prefix=prefix, tags=["turnos"])

    def _e(turno: dict) -> dict:
        return enriquecer(turno) if enriquecer else turno

    @router.get("")
    def listar(user: dict = Depends(usuario_actual)):
        es_admin = user.get("role") == "admin"
        activo = db_turnos.get_turno_activo(user["id"])
        return {
            "turnos": [_e(t) for t in db_turnos.get_all_turnos(usuario_id=None if es_admin else user["id"])],
            "turno_activo": _e(activo) if activo else None,
        }

    @router.get("/actual")
    def actual(user: dict = Depends(usuario_actual)):
        """El turno abierto de quien pregunta, con su resumen, o `{"turno": null}`.
        Lo consulta el POS al arrancar para saber si puede cobrar. Va antes de
        `/{tid}` para que `actual` no se lea como un id."""
        turno = db_turnos.get_turno_activo(user["id"])
        if not turno:
            return {"turno": None}
        return {"turno": _e(turno), "resumen": resumen_turno(turno["id"])}

    @router.post("/abrir")
    def abrir(payload: AbrirPayload, user: dict = Depends(usuario_actual)):
        if validar_apertura:
            validar_apertura(payload, user)
        turno_activo = db_turnos.get_turno_activo(user["id"])
        if turno_activo:
            return _e(turno_activo)
        try:
            tid = db_turnos.create_turno(user["id"], payload.monto_inicial, payload.notas.strip(),
                                         caja_id=payload.caja_id)
        except DiaCerradoError as exc:
            # El día de esa sucursal ya se cerró: no es un dato mal escrito sino
            # un estado, como el 409 del punto de venta repetido.
            raise HTTPException(409, str(exc)) from exc
        return _e(db_turnos.get_turno(tid))

    @router.get("/{tid}")
    def detalle(tid: int, user: dict = Depends(usuario_actual)):
        turno = db_turnos.get_turno(tid)
        if not turno:
            raise HTTPException(404, "Turno no encontrado")
        if not _puede_ver(turno, user):
            raise HTTPException(403, "No autorizado")
        return {"turno": _e(turno), "resumen": resumen_turno(tid)}

    @router.post("/{tid}/cerrar")
    def cerrar(tid: int, payload: CerrarPayload, user: dict = Depends(usuario_actual)):
        turno = db_turnos.get_turno(tid)
        if not turno:
            raise HTTPException(404, "Turno no encontrado")
        if not _puede_ver(turno, user):
            raise HTTPException(403, "No autorizado")
        if turno["estado"] != "abierto":
            raise HTTPException(422, "El turno ya está cerrado.")
        cerrar_turno(tid, payload.monto_declarado, payload.notas.strip())
        return _e(db_turnos.get_turno(tid))

    return router


# ── Cierre diario ────────────────────────────────────────────────────────


class CerrarDiaPayload(BaseModel):
    sucursal_id: int | None = None
    #: `''` (default) usa el día operativo de hoy en hora AR — ver
    #: `cierre_diario.cerrar_dia`. Formato `YYYY-MM-DD`.
    fecha: str = ""
    notas: str = ""


class ReabrirDiaPayload(BaseModel):
    #: Obligatorio y no vacío (tras `strip()`) — `cierre_diario.reabrir_dia`
    #: levanta `MotivoRequeridoError` si no cumple, que el router traduce a
    #: 422. Queda guardado en `motivo_anulacion` para auditoría.
    motivo: str


def _pdf(contenido: bytes, filename: str) -> Response:
    return Response(
        contenido,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


def build_cierre_diario_router(
    *,
    usuario_actual: Callable[..., Any],
    resolver_sucursal_nombre: Callable[[int | None], str] = lambda sucursal_id: "",
    autorizar_cierre: Callable[..., Any] | None = None,
    autorizar_reabrir: Any = None,
    prefix: str = "/api/cierre-diario",
) -> APIRouter:
    """Vista previa, cierre, reapertura, listado y tickets del cierre diario.

    ```python
    app.include_router(
        build_cierre_diario_router(
            usuario_actual=get_current_user_json,
            resolver_sucursal_nombre=lambda sid: db_sucursales.get_nombre(sid),
            autorizar_cierre=Depends(require_role("admin", "cajero")),
            autorizar_reabrir=Depends(require_role("admin")),
        ),
        dependencies=[_auth_json],
    )
    ```

    🔴 **`autorizar_cierre` es quien decide quién puede cerrar, y el motor no
    lo sabe.** No hay ningún `require_admin` fijo acá adentro: la decisión de
    hoy (2026-09-13) es "admin o cajero", pero el nombre de esos roles —y si
    mañana cambia la regla— es de cada producto, no de este router. Pasado
    como dependencia FastAPI, se evalúa SÓLO en `POST /cerrar`; el resto de
    los endpoints (previsualizar, listar, ver, tickets) quedan abiertos a
    cualquiera que ya haya pasado `usuario_actual` y el gate del módulo que
    el producto le ponga a `include_router(...)`. Sin `autorizar_cierre`, el
    endpoint de cierre queda con esa misma protección de módulo nada más.

    🔴 **Sin `autorizar_reabrir`, `POST /{id}/reabrir` NO SE MONTA.** Anular
    un cierre es más sensible que cerrarlo —reabre un día que ya se dio por
    auditado, y `autorizar_cierre` deja pasar a "admin o cajero" en más de un
    producto—, así que el endpoint no puede quedar con sólo la protección de
    módulo, que en VentaLibra y LibraClub también deja pasar al cajero. El
    motor no sabe cómo se llama el rol admin en cada producto: lo decide quien
    lo monta (el pedido es "sólo admin", 2026-09-17).
    Se eligió "no montar" y no un parámetro obligatorio: con uno obligatorio,
    todo consumidor que subiera el pin sin tocar su `main.py` dejaba de
    arrancar (`TypeError`), y la reapertura no es algo que todos quieran.
    Así el cambio es aditivo y cerrado por defecto: quien no lo pasa, sigue
    exactamente igual y sin la ruta.

    `resolver_sucursal_nombre` existe porque el motor no conoce sucursales
    (viven en la base del producto, ver `db/cierre_diario.py`): sin esto los
    endpoints de listado/detalle/ticket no podrían mostrar más que el
    `sucursal_id` crudo.
    """
    router = APIRouter(prefix=prefix, tags=["cierre-diario"])
    # `autorizar_cierre`/`autorizar_reabrir` ya vienen envueltos en
    # `Depends(...)` (ver el ejemplo del docstring) — es la misma forma que
    # toman los elementos de `dependencies=` en `include_router`, así que se
    # pasan tal cual y no se envuelven de nuevo.
    cerrar_deps = [autorizar_cierre] if autorizar_cierre else []

    @router.get("/preview")
    def preview(sucursal_id: int | None = None, fecha: str = "",
               user: dict = Depends(usuario_actual)):
        return db_cierre_diario.preview_cierre_dia(sucursal_id, fecha or None)

    @router.post("/cerrar", dependencies=cerrar_deps)
    def cerrar(payload: CerrarDiaPayload, user: dict = Depends(usuario_actual)):
        try:
            cierre = db_cierre_diario.cerrar_dia(
                usuario_id=user["id"], sucursal_id=payload.sucursal_id,
                fecha=payload.fecha or None, notas=payload.notas.strip(),
            )
        except db_cierre_diario.TurnosAbiertosError as e:
            raise HTTPException(422, str(e)) from e
        except db_cierre_diario.DiaYaCerradoError as e:
            raise HTTPException(409, str(e)) from e
        return cierre

    if autorizar_reabrir is not None:
        @router.post("/{cierre_id}/reabrir", dependencies=[autorizar_reabrir])
        def reabrir(cierre_id: int, payload: ReabrirDiaPayload, user: dict = Depends(usuario_actual)):
            try:
                return db_cierre_diario.reabrir_dia(
                    cierre_id=cierre_id, usuario_id=user["id"], motivo=payload.motivo,
                )
            except db_cierre_diario.CierreNoEncontradoError as e:
                raise HTTPException(404, str(e)) from e
            except db_cierre_diario.MotivoRequeridoError as e:
                raise HTTPException(422, str(e)) from e
            except (db_cierre_diario.CierreYaAnuladoError, db_cierre_diario.CierrePosteriorError) as e:
                raise HTTPException(409, str(e)) from e

    @router.get("")
    def listar(sucursal_id: int | None = None, todas: bool = False, limit: int = 50,
              user: dict = Depends(usuario_actual)):
        return db_cierre_diario.listar_cierres(sucursal_id, todas=todas, limit=limit)

    @router.get("/{cierre_id}")
    def detalle(cierre_id: int, user: dict = Depends(usuario_actual)):
        cierre = db_cierre_diario.get_cierre(cierre_id)
        if not cierre:
            raise HTTPException(404, "Cierre no encontrado")
        return cierre

    @router.get("/{cierre_id}/ticket")
    def ticket(cierre_id: int, user: dict = Depends(usuario_actual)):
        cierre = db_cierre_diario.get_cierre(cierre_id)
        if not cierre:
            raise HTTPException(404, "Cierre no encontrado")
        sucursal_nombre = resolver_sucursal_nombre(cierre["sucursal_id"])
        contenido = ticket_generator.generar_ticket_cierre_diario(cierre, sucursal_nombre)
        return _pdf(contenido, f"cierre-diario-{cierre['numero']}.pdf")

    @router.get("/turno/{turno_id}/ticket")
    def ticket_turno(turno_id: int, user: dict = Depends(usuario_actual)):
        """Por `turno_id` (`turnos_caja.id`), no por el id de la foto: el
        cajero que acaba de cerrar su turno sólo conoce el primero, y todavía
        puede no existir ningún cierre diario que lo incluya. `arqueo_de_turno`
        decide sola si usa la foto o lo calcula en vivo."""
        try:
            arqueo = db_cierre_diario.arqueo_de_turno(turno_id)
        except db_cierre_diario.TurnoAbiertoError as e:
            raise HTTPException(409, str(e)) from e
        if not arqueo:
            raise HTTPException(404, "Turno no encontrado")
        sucursal_id = db_cierre_diario.sucursal_de_caja(arqueo.get("caja_id"))
        arqueo["sucursal_nombre"] = resolver_sucursal_nombre(sucursal_id)
        contenido = ticket_generator.generar_ticket_cierre_turno(arqueo)
        return _pdf(contenido, f"cierre-turno-{turno_id}.pdf")

    return router
