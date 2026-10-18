"""A dónde va la plata cuando se acredita una factura **al contado ya cobrada** (ADR-065).

Hasta acá la nota de crédito de una factura al contado **no movía plata**: ARCA quedaba al día, pero la caja no
registraba la devolución ni la cuenta del cliente el saldo a favor, y lo cobrado de la factura quedaba «de más»
sin que nada lo explicara. La de una factura **a cuenta corriente** sí tiene su destino (abona la cuenta, ver
`notas_de_credito.referencia_cc_de_nota`) y sigue igual.

Decidido por el humano el 2026-10-16: **preguntar al emitir.** Quien emite la nota elige:

- `devolver`: un **egreso de caja** por el importe, con el medio de pago con el que se devuelve (por omisión, el
  único con el que se cobró la factura);
- `a_favor`: un **abono en la cuenta corriente** del cliente (sólo si la factura tiene un cliente identificado:
  sin cliente no hay cuenta donde dejarlo).

Sin elegir nada (el default, para no romper a ningún producto que no manda el campo) no se mueve plata, como
antes.

## Cuánto se mueve

No el importe de la nota a ciegas: **lo que la nota deja cobrado de más**. Lo cobrado neto de la factura
(`cobrado − lo ya devuelto o dejado a favor por otras notas`) menos lo que el cliente todavía debe después de las
notas (`total − acreditado`), con tope en el importe de la nota y piso en cero::

    a_mover = max(0, min(importe_de_la_nota, cobrado_neto − max(0, total − acreditado)))

- Cobrada completa (1000) y nota por 300: se mueven 300.
- Cobrada a medias (400 de 1000) y nota total: se mueven 400 (no se devuelve lo que no entró); lo que excede
  (600) queda en el texto del movimiento y en la respuesta (`excedente`).
- Cobrada a medias (400 de 1000) y nota por 300: **se mueve 0**: el cliente sigue debiendo 300 de los 700 que
  quedan, y ya había pagado 400; no hay nada de más que devolverle.

Si no se mueve nada (nada cobrado, o nada de más), **no se pide destino** y el que llegue se ignora.

## Cuándo se escribe, respecto del CAE

**Después de que la nota tiene CAE, nunca antes.** La emisión de la nota (numerar → registrar → pedir el CAE) no
corre en una transacción —cada función de `libracore.db` abre y cierra la suya—, y `solicitar_cae` **no levanta**
si ARCA rechaza: deja el motivo en la nota y sigue. Por eso el router mira el CAE de la nota ya emitida:

- con CAE: escribe el movimiento, **en una transacción propia**, con la factura bloqueada, que mira lo cobrado y
  lo ya devuelto y escribe;
- sin CAE: **no mueve plata** y lo dice en la respuesta (`movido: false`, `pendiente: true`), pero **anota el
  destino** (`reintegros_pendientes`, una fila por nota; ADR-067) y el `motivo` cuenta qué va a pasar. La nota queda
  sin CAE como siempre (se autoriza con `/autorizar` o se borra):
  - **se autoriza**: `/autorizar` mira la tabla **antes** de pedirle el CAE a ARCA (`planear_pendiente`: sin turno
    abierto, en un producto que lo exige, o sin cliente para dejar a favor, frena con 409 / 422 como el alta) y,
    obtenido el CAE, mueve la plata con el destino anotado (`aplicar_pendiente`: la misma `aplicar`, en una
    transacción que además borra la fila). Si la plata no se pudo mover, la fila queda y volver a llamar a
    `/autorizar` sobre la nota —ya con CAE— lo reintenta;
  - **se borra**: la fila se va con la nota (`ON DELETE CASCADE`) y no se mueve nada.

Entre el CAE y el movimiento puede morir el proceso: la nota queda autorizada y sin movimiento. Es el mismo
hueco que el del abono de las facturas a cuenta corriente, y se arregla igual (a mano, con la marca) salvo que
la nota haya salido sin CAE: ahí la fila sigue y `/autorizar` lo termina.

## Que no se devuelva dos veces

El movimiento lleva `referencia = nc:factura:<id>:<nota_id>` (la misma marca que el abono de una factura a cuenta
corriente). `aplicar` busca esa marca antes de escribir y, si ya está, no escribe otra; el egreso lleva además el
`factura_id`, así que `create_caja_movimiento` también descarta por repetido el de la misma referencia.
`notas_de_credito.devuelto_por_notas` y `cc_acreditado_por_notas` dicen cuánto salió por cada camino: **quien anula
la venta de esa factura tiene que restarlo de lo que revierte de la caja**.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from libracore import cobros, medios_pago
from libracore import notas_de_credito as nc
from libracore.notas_de_credito import NotaNoPermitida, _dec

logger = logging.getLogger(__name__)

DEVOLVER = "devolver"
A_FAVOR = "a_favor"
DESTINOS = (DEVOLVER, A_FAVOR)

CONDICION_CUENTA_CORRIENTE = cobros.CONDICION_CUENTA_CORRIENTE

MENSAJE_SIN_TURNO = "No hay una caja abierta. Abrí el turno antes de devolver."
MENSAJE_SIN_TURNO_AL_AUTORIZAR = (
    "Esta nota devuelve plata de la caja y no hay una caja abierta. Abrí el turno antes de autorizarla.")


def reintegrable(total: Any, cobrado: Any, acreditado: Any, ya_reintegrado: Any, importe: Any) -> Decimal:
    """Cuánta plata deja de más la nota por `importe` (ver el docstring del módulo). Puro: no toca la base.

    `acreditado` es lo que acreditan las notas con CAE **incluida ésta**; `ya_reintegrado`, lo que otras notas de
    la factura ya devolvieron o dejaron a favor."""
    cobrado_neto = _dec(cobrado) - _dec(ya_reintegrado)
    deuda = max(_dec(total) - _dec(acreditado), Decimal("0.00"))
    return max(min(_dec(importe), cobrado_neto - deuda), Decimal("0.00"))


@dataclass(frozen=True)
class Estado:
    """Lo que dice la base de la plata de una factura, con la misma cuenta que el resto del motor (ADR-059)."""

    cobrado: Decimal          # ingresos de caja que cuentan (`sql_cobrado`)
    acreditado: Decimal       # notas de crédito con CAE y vigentes (`sql_acreditado`)
    ya_reintegrado: Decimal   # lo que otras notas ya devolvieron (egresos) o dejaron a favor (abonos)
    medios: tuple[str, ...]   # con qué se cobró, de más a menos plata


def leer_estado(factura_id: int, *, conn: Any = None) -> Estado:
    """El estado de la plata de la factura. Con `conn`, lo ve como lo ve esa transacción y no confirma nada."""
    from libracore.db import caja as db_caja
    from libracore.db import facturas as db_facturas
    from libracore.db.core import get_connection

    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        cobrado, acreditado = db_facturas.get_cobrado_y_acreditado(factura_id, conn=c) or (0.0, 0.0)
        por_medio: dict[str, Decimal] = {}
        for cobro in db_caja.get_cobros_factura(factura_id, conn=c):
            medio = (cobro.get("medio_pago") or "").strip()
            if medio:
                por_medio[medio] = por_medio.get(medio, Decimal("0")) + _dec(cobro["monto"])
        reintegrado = nc.devuelto_por_notas(c, factura_id) + nc.cc_acreditado_por_notas(c, factura_id)
    medios = tuple(sorted(por_medio, key=lambda m: (-por_medio[m], m)))
    return Estado(_dec(cobrado), _dec(acreditado), reintegrado, medios)


@dataclass(frozen=True)
class Plan:
    """Lo que se va a hacer con la plata, ya validado **antes** de pedirle el CAE a ARCA."""

    destino: str
    medio_pago: str | None        # `devolver`: con qué sale; `a_favor`: nada
    cliente_id: int | None        # `a_favor`: de quién es la cuenta
    turno: dict | None            # `devolver`: el turno abierto de quien emite, si el producto lleva la caja por turno


def planear(
    original: dict, importe: Any, destino: str | None, medio_pago: str | None = None, *,
    turno_de: Callable[[], dict | None] | None = None, exigir_turno: bool = False,
    leer: Callable[[int], Estado] | None = None,
) -> Plan | None:
    """Decide, antes de emitir nada, qué se hace con la plata. `None` = no se mueve plata.

    `importe` es el de la nota (el total de la factura si es total). Levanta `NotaNoPermitida`:

    - `DESTINO` (422): un destino que no existe; un destino en una factura a cuenta corriente (ahí la nota abona
      la cuenta sola); `a_favor` sin cliente identificado o con un medio de pago; `devolver` sin medio cuando se
      cobró con varios (no hay cuál elegir por la persona) o con un medio que no es de plata;
    - `SIN_TURNO` (409): hay que devolver y el producto lleva la caja por turno y quien emite no tiene uno abierto.

    Todo se dice **antes** del CAE: una nota autorizada no se deshace.

    `turno_de()` devuelve el turno abierto de quien emite (o `None`); sólo se llama al devolver y si el producto lo
    pasa. `leer(factura_id)` reemplaza a `leer_estado` (para probar sin base)."""
    if destino is None:
        if medio_pago:
            raise NotaNoPermitida("El medio de pago va junto con el destino «devolver».", NotaNoPermitida.DESTINO)
        return None
    if destino not in DESTINOS:
        raise NotaNoPermitida(
            f"El destino {destino!r} no existe: elegí «devolver» o «a_favor».", NotaNoPermitida.DESTINO)
    if original.get("condicion_venta") == CONDICION_CUENTA_CORRIENTE:
        raise NotaNoPermitida(
            "Una factura a cuenta corriente no lleva destino: la nota abona la cuenta del cliente sola.",
            NotaNoPermitida.DESTINO)
    if destino == A_FAVOR and medio_pago:
        raise NotaNoPermitida(
            "El medio de pago sólo va con «devolver»: dejar a favor no mueve plata de la caja.",
            NotaNoPermitida.DESTINO)

    estado = (leer or (lambda fid: leer_estado(fid)))(original["id"])
    acreditado_con_esta = estado.acreditado + _dec(importe)
    if reintegrable(original["total"], estado.cobrado, acreditado_con_esta, estado.ya_reintegrado, importe) <= 0:
        return None   # nada cobrado, o nada de más: no se pide destino y no se mueve plata

    if destino == A_FAVOR:
        from libracore.db import clients as db_clients

        cliente = db_clients.get_client_by_cuit(original.get("cliente_cuit") or "")
        if not cliente:
            raise NotaNoPermitida(
                "Esta factura no tiene un cliente identificado: no hay una cuenta corriente donde dejar el saldo a "
                "favor. Devolvé la plata, o dalo de alta como cliente antes.", NotaNoPermitida.DESTINO)
        return Plan(A_FAVOR, None, cliente["id"], None)

    medio = _medio_de_devolucion(medio_pago, estado)
    turno = turno_de() if turno_de is not None else None
    if turno is None and exigir_turno:
        raise NotaNoPermitida(MENSAJE_SIN_TURNO, NotaNoPermitida.SIN_TURNO)
    return Plan(DEVOLVER, medio, None, turno)


def _medio_de_devolucion(pedido: str | None, estado: Estado) -> str:
    """El medio con el que sale la plata: el pedido, o el único con el que se cobró."""
    medio = (pedido or "").strip()
    if not medio:
        if len(estado.medios) == 1:
            return estado.medios[0]
        raise NotaNoPermitida(
            "La factura se cobró con más de un medio de pago: elegí con cuál devolver."
            if estado.medios else "Elegí con qué medio de pago se devuelve.",
            NotaNoPermitida.DESTINO)
    if cobros.es_medio_cuenta_corriente(medio):
        raise NotaNoPermitida(
            "«Cuenta corriente» no es un medio con el que salga plata de la caja: para dejarla en la cuenta del "
            "cliente elegí «Dejar a favor del cliente».", NotaNoPermitida.DESTINO)
    ofrecidos = medios_de_devolucion(estado)
    if medio not in ofrecidos:
        raise NotaNoPermitida(
            f"«{medio}» no es un medio de pago válido. Los que se pueden elegir son: {', '.join(ofrecidos)}.",
            NotaNoPermitida.DESTINO)
    return medio


def medios_de_devolucion(estado: Estado) -> list[str]:
    """Los medios con los que puede salir la plata: los que se eligen hoy (menos la cuenta corriente, que no es plata
    del cajón) más los de los cobros de esta factura, aunque sean históricos (`tarjeta`, `qr`…): se puede devolver por
    el mismo con el que se cobró."""
    elegibles = [m for m in medios_pago.ELEGIBLES if m != "cuenta_corriente"]
    return elegibles + [m for m in estado.medios if m not in elegibles and not cobros.es_medio_cuenta_corriente(m)]


def sin_mover(plan: Plan, motivo: str, *, pendiente: bool = False) -> dict:
    """La respuesta cuando había que mover plata y no se movió (la nota no obtuvo CAE).

    `pendiente`: el destino quedó anotado y la plata se mueve sola cuando la nota se autorice."""
    return {"destino": plan.destino, "medio_pago": plan.medio_pago, "movido": False, "monto": 0.0,
            "excedente": 0.0, "referencia": None, "motivo": motivo, **({"pendiente": True} if pendiente else {})}


def dejar_pendiente(plan: Plan, original: dict, nota: dict) -> dict:
    """La nota no obtuvo CAE: anota el destino para moverlo cuando se autorice (ADR-067) y devuelve el `reintegro`
    de la respuesta, cuyo `motivo` dice qué va a pasar.

    Anotarlo **no puede tumbar la emisión** (la nota ya existe): si la base falla, la respuesta vuelve al
    «hacelo a mano con la marca» de antes."""
    from libracore.db import reintegros_pendientes

    referencia = nc.referencia_cc_de_nota(original["id"], nota["id"])
    a_mano = ("La nota todavía no tiene CAE, así que la plata no se movió: autorizala y hacé el movimiento "
              f"a mano con la marca {referencia}.")
    try:
        estado = leer_estado(original["id"])
        importe = _dec(nota["total"])
        monto = reintegrable(original["total"], estado.cobrado, estado.acreditado + importe, estado.ya_reintegrado, importe)
        reintegros_pendientes.anotar(nota["id"], original["id"], plan.destino, plan.medio_pago or "")
    except Exception:
        logger.exception("No se pudo anotar el destino de la plata de la nota %s (factura %s).",
                         nota.get("id"), original.get("id"))
        return sin_mover(plan, a_mano)
    cuenta = cobros._pesos(cobros.centavos(monto))
    que_pasa = (f"van a salir {cuenta} de la caja ({medios_pago.label(plan.medio_pago or '')})"
                if plan.destino == DEVOLVER else f"van a quedar {cuenta} a favor del cliente, en su cuenta corriente")
    return sin_mover(
        plan, "La nota todavía no tiene CAE, así que la plata no se movió. Quedó anotado el destino que elegiste: "
              f"cuando la autorices {que_pasa}. Si en cambio la borrás, no se mueve nada.", pendiente=True)


def planear_pendiente(
    pendiente: dict, original: dict, *, turno_de: Callable[[], dict | None] | None = None, exigir_turno: bool = False,
) -> Plan:
    """El `Plan` de una nota con el destino anotado (`reintegros_pendientes.de_nota`), a la hora de autorizarla.

    Se llama **antes** de pedirle el CAE a ARCA, por lo mismo que `planear`: una nota autorizada no se deshace.
    Levanta `NotaNoPermitida`: `SIN_TURNO` (409) si hay que devolver, el producto lleva la caja por turno y quien
    autoriza no tiene uno abierto; `DESTINO` (422) si hay que dejar a favor y la factura ya no tiene un cliente."""
    if pendiente["destino"] == A_FAVOR:
        from libracore.db import clients as db_clients

        cliente = db_clients.get_client_by_cuit(original.get("cliente_cuit") or "")
        if not cliente:
            raise NotaNoPermitida(
                "Esta nota deja la plata a favor del cliente y la factura ya no tiene un cliente identificado: "
                "dalo de alta como cliente antes de autorizarla.", NotaNoPermitida.DESTINO)
        return Plan(A_FAVOR, None, cliente["id"], None)
    turno = turno_de() if turno_de is not None else None
    if turno is None and exigir_turno:
        raise NotaNoPermitida(MENSAJE_SIN_TURNO_AL_AUTORIZAR, NotaNoPermitida.SIN_TURNO)
    return Plan(DEVOLVER, pendiente["medio_pago"] or None, None, turno)


def aplicar_pendiente(plan: Plan, original: dict, nota: dict, usuario_id: int | None) -> dict:
    """Mueve la plata de una nota **que acaba de obtener CAE** con el destino anotado y borra la anotación.

    Todo en una sola transacción: o queda el movimiento y la anotación borrada, o ninguna de las dos cosas (y un
    nuevo `/autorizar` lo reintenta). `aplicar` no escribe dos veces la misma marca, así que dos autorizaciones
    a la vez, o una repetida, no duplican nada."""
    from libracore.db import reintegros_pendientes
    from libracore.db.core import get_connection

    with get_connection() as c:
        reintegro = aplicar(plan, original, nota, usuario_id, conn=c)
        reintegros_pendientes.borrar(nota["id"], conn=c)
    if not reintegro["movido"] and not reintegro.get("ya_estaba"):
        reintegro["motivo"] = nada_que_mover(plan.destino)["motivo"]
    return reintegro


def ofrecer(
    original: dict, importe: Any, *, turno_de: Callable[[], dict | None] | None = None, exigir_turno: bool = False,
    leer: Callable[[int], Estado] | None = None,
) -> dict:
    """Lo que una pantalla necesita para **preguntar** qué hacer con la plata de una nota por `importe` (ADR-065):
    `GET /{factura_id}/nota-credito/destino`. No escribe nada.

    - `aplica`: hay plata que mover (factura al contado, vigente, con algo cobrado de más para ese importe). Si es
      `false`, la pantalla no pregunta y la nota sale como siempre;
    - `monto` y `excedente`: cuánto se mueve y cuánto de la nota no estaba cobrado;
    - `medios` (`[{id, label}]`) y `medio_sugerido` (el único con el que se cobró, o `null`): para `devolver`;
    - `cliente_identificado`: sin él `a_favor` no se puede;
    - `sin_turno`: el producto exige turno y quien pide no tiene uno abierto (`devolver` daría 409)."""
    sin_nada = {"aplica": False, "monto": 0.0, "excedente": 0.0, "medios": [], "medio_sugerido": None,
                "cliente_identificado": False, "sin_turno": False}
    if original.get("condicion_venta") == CONDICION_CUENTA_CORRIENTE or original.get("anulada_en"):
        return sin_nada
    estado = (leer or (lambda fid: leer_estado(fid)))(original["id"])
    importe = _dec(importe)
    monto = reintegrable(original["total"], estado.cobrado, estado.acreditado + importe, estado.ya_reintegrado, importe)
    if monto <= 0:
        return sin_nada
    from libracore.db import clients as db_clients

    turno = turno_de() if turno_de is not None else None
    return {
        "aplica": True, "monto": float(monto), "excedente": float(importe - monto),
        "medios": [{"id": m, "label": medios_pago.label(m)} for m in medios_de_devolucion(estado)],
        "medio_sugerido": estado.medios[0] if len(estado.medios) == 1 else None,
        "cliente_identificado": db_clients.get_client_by_cuit(original.get("cliente_cuit") or "") is not None,
        "sin_turno": exigir_turno and turno is None,
    }


def nada_que_mover(destino: str) -> dict:
    """La respuesta cuando llegó un destino y la factura no tenía nada cobrado de más: la nota sale y no se mueve plata."""
    return {"destino": destino, "medio_pago": None, "movido": False, "monto": 0.0, "excedente": 0.0,
            "referencia": None, "motivo": "La factura no tenía nada cobrado de más: no hay plata que mover."}


def aplicar(plan: Plan, original: dict, nota: dict, usuario_id: int | None, *, conn: Any = None) -> dict:
    """Escribe el movimiento de una nota **que ya tiene CAE**. Devuelve qué hizo (el cuerpo de `reintegro`).

    Una sola transacción —la de `conn`, o una propia—: bloquea la factura, mira lo cobrado y lo ya devuelto con lo
    que hay en la base **ahora**, y escribe. Si la marca de esta nota ya está escrita no escribe otra (reintento).
    Si ya no queda nada de más (cambió desde que se planeó), no escribe nada y lo dice (`monto: 0`)."""
    from libracore.db.core import get_connection
    from libracore.db.reversiones import MEDIO_CUENTA_CORRIENTE, reintegrar_devolucion

    referencia = nc.referencia_cc_de_nota(original["id"], nota["id"])
    importe = _dec(nota["total"])
    cm = contextlib.nullcontext(conn) if conn is not None else get_connection()
    with cm as c:
        cobros._bloquear_factura(c, original["id"])
        previo = _movimiento_previo(c, original["id"], referencia, plan.destino)
        if previo is not None:
            return _respuesta(plan, referencia, _dec(previo), importe, ya_estaba=True)

        estado = leer_estado(original["id"], conn=c)
        monto = reintegrable(original["total"], estado.cobrado, estado.acreditado, estado.ya_reintegrado, importe)
        if monto <= 0:
            return _respuesta(plan, referencia, monto, importe)

        excedente = importe - monto
        cuenta = cobros._pesos
        cual = f"{nc.etiqueta(nota)} (anula {nc.etiqueta(original)})"
        resto = (f" — {cuenta(cobros.centavos(monto))} de {cuenta(cobros.centavos(importe))}: el resto no estaba cobrado"
                 if excedente > 0 else "")
        turno = plan.turno
        reintegrar_devolucion(
            venta_id=original["id"], numero=nc.etiqueta(original), fecha=datetime.date.today().isoformat(),
            monto=float(monto),
            medio_pago=MEDIO_CUENTA_CORRIENTE if plan.destino == A_FAVOR else plan.medio_pago,
            referencia=referencia, cliente_id=plan.cliente_id,
            # La caja, el día y el dueño del movimiento son los del turno, como en el cobro (ADR-060).
            usuario_id=turno["usuario_id"] if turno else usuario_id,
            turno_id=turno["id"] if turno else None,
            conn=c, factura_id=original["id"],
            concepto=(f"Saldo a favor por {cual}{resto}" if plan.destino == A_FAVOR
                      else f"Devolución {cual}{resto}"),
        )
    return _respuesta(plan, referencia, monto, importe)


def _movimiento_previo(c: Any, factura_id: int, referencia: str, destino: str) -> Any:
    """El monto del movimiento que esta nota ya escribió, o `None`."""
    if destino == A_FAVOR:
        fila = c.execute("SELECT monto FROM cc_pagos WHERE referencia = ?", (referencia,)).fetchone()
    else:
        fila = c.execute(
            "SELECT monto FROM caja_movimientos WHERE referencia = ? AND factura_id = ? AND tipo = 'egreso'",
            (referencia, factura_id)).fetchone()
    return None if fila is None else fila[0]


def _respuesta(plan: Plan, referencia: str, monto: Decimal, importe: Decimal, *, ya_estaba: bool = False) -> dict:
    return {
        "destino": plan.destino, "medio_pago": plan.medio_pago, "movido": monto > 0, "monto": float(monto),
        "excedente": float(importe - monto), "referencia": referencia if monto > 0 else None,
        **({"ya_estaba": True} if ya_estaba else {}),
    }
