"""Los ganchos de la caja: lo que un producto engancha para subir la caja de un nodo al central (ADR-076).

Un nodo de LibraEdge abre y cierra turnos, anota ingresos y egresos, anula movimientos y cierra el día **sin ver al
central**. La decisión del humano (2026-10-21) es que todo eso sube. El motor no sabe de LibraEdge ni de su outbox
(ADR-074): lo que ofrece son estos cinco puntos de extensión, con el mismo patrón que `PuertoDeVentas.al_facturar` y
que los ganchos de venta de `libracommerce.erp.hooks`. El producto (o `libracommerce`) los usa para encolar la operación
en el outbox del nodo.

```python
ganchos = GanchosDeCaja(
    al_abrir_turno=lambda conn, datos: OUTBOX.encolar(conn, "turno.abierto", datos),
    al_cerrar_turno=lambda conn, datos: OUTBOX.encolar(conn, "turno.cerrado", datos),
    al_registrar_movimiento=...,
    al_anular_movimiento=...,
    al_cerrar_dia=...,
)
app.include_router(build_caja_router(usuario_actual=..., ganchos_de_caja=ganchos))
app.include_router(build_turnos_router(usuario_actual=..., ganchos_de_caja=ganchos))
app.include_router(build_cierre_diario_router(usuario_actual=..., ganchos_de_caja=ganchos))
```

🔴 **Cada gancho recibe la MISMA conexión del cambio y corre adentro de su transacción.** Es lo que fijan los ganchos
de venta (`libracommerce.erp.hooks`): si el gancho levanta, el cambio no queda, y si el cambio no se confirma, la
operación no queda en el outbox. Un gancho que abriera su propia conexión rompería las dos cosas sin que nadie lo vea.
Por la misma razón **un gancho que falla frena el cambio**: un turno que se abre en el nodo y no sube no es un turno
a medias, es una caja que el central no ve.

**Qué dispara cada uno** (y qué no):

| Gancho | Cuándo | Datos |
|---|---|---|
| `al_abrir_turno(conn, datos)` | después de insertar el turno | `datos_de_turno_abierto` |
| `al_cerrar_turno(conn, datos)` | después de cerrar el turno, con el **arqueo** (declarado, esperado, diferencia y medios) | `datos_de_turno_cerrado` |
| `al_registrar_movimiento(conn, datos)` | después de insertar un movimiento de caja. **No** si la referencia ya existía (no hubo fila nueva) | `datos_de_movimiento` |
| `al_anular_movimiento(conn, datos)` | después de marcar `anulado = 1`. **No** si ya estaba anulado o no existe | `datos_de_movimiento` |
| `al_cerrar_dia(conn, datos)` | después de grabar el cierre diario y su foto | `datos_de_cierre_diario` |

No hay un gancho de «arqueo» aparte: el arqueo de un turno **no es un acto**, es el cierre del turno (el ticket
`arqueo_de_turno` sólo lee), y el de la sucursal es el cierre del día. Un retiro de caja es un egreso (`tipo =
"egreso"`); no tiene tipo propio.

🔑 **Los ganchos se pasan por llamada, no se registran globalmente.** Así sólo disparan las acciones que decide una
persona en la caja (los routers de este motor) o las que el producto marca a propósito. Los movimientos que nacen de
una **venta** los reproduce el central al aplicar la venta; si además subieran como movimientos, quedarían duplicados.
`create_caja_movimiento(..., ganchos=)` está para quien quiera subir otros.

Todos los `datos` son JSON (`dict` de textos, números, `None`, listas): los montos son `float` (como los escribe
`create_caja_movimiento`) y los ids son **los locales del nodo**. El central los guarda como `origen_id` junto con el
`origen` (el id del nodo): ver las columnas de `turnos_caja`, `caja_movimientos` y `cierres_diarios` y
`validar_origen`. `VERSION` sube si cambia la forma de algún `datos`.

🔑 **Lo propio del producto viaja en `extra` (ADR-080).** `create_caja_movimiento(..., extra=)` y
`anular_caja_movimiento(..., extra=)` aceptan un `dict` JSON que el motor no lee ni interpreta: va en `datos["extra"]` de
`al_registrar_movimiento` / `al_anular_movimiento` y, del otro lado, lo recibe el gancho del central
(`libracore.caja_del_nodo`). Sin `extra` el `datos` es **idéntico** al de antes (la clave ni aparece), así que `VERSION`
sigue en 1: un central anterior ignora la clave.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

#: La versión de la forma de los `datos` de todos los ganchos. Si cambia, sube: el central rechaza lo que no entiende.
VERSION = 1

#: Un gancho: `(conn, datos)`. `conn` es la conexión del cambio; no confirma ni cierra nada.
GanchoDeCaja = Callable[[Any, dict], None]


def _nada(conn: Any, datos: dict) -> None:
    return None


@dataclass(frozen=True)
class GanchosDeCaja:
    """El conjunto de ganchos de la caja de un producto. Inmutable, con un no-op por defecto en cada uno: sin pasar
    ninguno, todo queda como estaba."""

    al_abrir_turno: GanchoDeCaja = _nada
    al_cerrar_turno: GanchoDeCaja = _nada
    al_registrar_movimiento: GanchoDeCaja = _nada
    al_anular_movimiento: GanchoDeCaja = _nada
    al_cerrar_dia: GanchoDeCaja = _nada


#: Ningún gancho enganchado: el comportamiento de siempre.
SIN_GANCHOS = GanchosDeCaja()


def validar_origen(origen: str | None, origen_id: int | None) -> None:
    """`origen` y `origen_id` van juntos: o los dos o ninguno. Levanta `ValueError` si viene uno solo."""
    if (origen is None) != (origen_id is None):
        raise ValueError("origen y origen_id van juntos: o los dos o ninguno.")
    if origen is not None and not str(origen).strip():
        raise ValueError("origen no puede estar vacío.")


def validar_extra(extra: Mapping | None) -> dict | None:
    """El `extra` de un movimiento, listo para viajar: un `dict` JSON, o `None` si no hay nada que llevar.

    `None` y un mapeo vacío son lo mismo: no viaja. Levanta `ValueError` si no es un mapeo de claves de texto o si algo
    adentro no entra en JSON (un `Decimal`, una fecha, un `set`): se avisa **antes** de escribir, no cuando el gancho
    lo serializa con el movimiento ya insertado."""
    if extra is None:
        return None
    if not isinstance(extra, Mapping):
        raise ValueError("extra tiene que ser un diccionario (JSON).")
    if not extra:
        return None
    if not all(isinstance(clave, str) for clave in extra):
        raise ValueError("las claves de extra tienen que ser texto.")
    try:
        json.dumps(extra, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"extra tiene que ser JSON (textos, números, None, listas y diccionarios): {exc}") from exc
    return dict(extra)


# ── Los datos que viajan ─────────────────────────────────────────────────


def _numero(valor) -> float | None:
    """Un monto como `float`, o `None`. PostgreSQL devuelve `Decimal` en las columnas `NUMERIC`: el JSON no lo sabe."""
    return None if valor is None else float(valor)


def datos_de_turno_abierto(turno: Mapping, *, sucursal_id: int | None) -> dict:
    """`{"version", "turno_id", "usuario_id", "caja_id", "sucursal_id", "apertura", "monto_inicial", "notas"}`.

    `apertura` es el texto `AAAA-MM-DD HH:MM:SS` en hora de Argentina que escribió el nodo."""
    return {
        "version": VERSION,
        "turno_id": turno["id"],
        "usuario_id": turno["usuario_id"],
        "caja_id": turno["caja_id"],
        "sucursal_id": sucursal_id,
        "apertura": turno["apertura"],
        "monto_inicial": _numero(turno["monto_inicial"]),
        "notas": turno["notas"] or "",
    }


def datos_de_turno_cerrado(turno: Mapping, *, sucursal_id: int | None, medios: list[dict]) -> dict:
    """`datos_de_turno_abierto` más el arqueo: `{"cierre", "monto_declarado", "monto_esperado", "diferencia", "medios"}`.

    `diferencia = declarado - esperado` (el signo de `cierre_diario._diferencia`); `medios` es lo que se cobró y se
    pagó por medio de pago, `[{"medio_pago", "ingresos", "egresos", "neto"}]` (el desglose del ticket del arqueo)."""
    declarado = _numero(turno["monto_declarado_cierre"]) or 0.0
    esperado = _numero(turno["monto_esperado_cierre"]) or 0.0
    return {
        **datos_de_turno_abierto(turno, sucursal_id=sucursal_id),
        "cierre": turno["cierre"],
        "monto_declarado": declarado,
        "monto_esperado": esperado,
        "diferencia": round(declarado - esperado, 2),
        "medios": [
            {"medio_pago": m["medio_pago"], "ingresos": _numero(m["ingresos"]), "egresos": _numero(m["egresos"]),
             "neto": _numero(m["neto"])}
            for m in medios
        ],
    }


def datos_de_movimiento(mov: Mapping, *, sucursal_id: int | None, extra: Mapping | None = None) -> dict:
    """`{"version", "movimiento_id", "fecha", "tipo", "concepto", "monto", "referencia", "factura_id", "usuario_id",
    "caja_id", "sucursal_id", "medio_pago", "turno_id", "cc_pago_id", "anulado"}`, más `"extra"` si el producto llevó algo.

    `tipo` es `"ingreso"` o `"egreso"` (un retiro es un egreso). `anulado` es `0` al registrar y `1` al anular. Los
    `factura_id` y `cc_pago_id` son ids **del nodo**: el central puede no tener esa factura o ese pago. `extra` (ADR-080)
    es lo propio del producto, tal cual lo pasó (`validar_extra`); sin él la clave no está."""
    datos = {
        "version": VERSION,
        "movimiento_id": mov["id"],
        "fecha": mov["fecha"],
        "tipo": mov["tipo"],
        "concepto": mov["concepto"],
        "monto": _numero(mov["monto"]),
        "referencia": mov["referencia"] or "",
        "factura_id": mov["factura_id"],
        "usuario_id": mov["usuario_id"],
        "caja_id": mov["caja_id"],
        "sucursal_id": sucursal_id,
        "medio_pago": mov["medio_pago"] or "",
        "turno_id": mov["turno_id"],
        "cc_pago_id": mov["cc_pago_id"],
        "anulado": int(mov["anulado"] or 0),
    }
    llevado = validar_extra(extra)
    if llevado is not None:
        datos["extra"] = llevado
    return datos


def datos_de_cierre_diario(cierre: Mapping, turnos: list[Mapping], medios: list[Mapping]) -> dict:
    """`{"version", "cierre_id", "sucursal_id", "fecha", "numero", "usuario_id", "notas", "monto_esperado_total",
    "monto_declarado_total", "diferencia_total", "turnos", "medios"}`.

    `turnos` es el arqueo de cada turno que entró al cierre —`[{"turno_id", "caja_id", "monto_inicial",
    "monto_esperado", "monto_declarado", "diferencia"}]`, los `turno_id` son los del nodo— y `medios` el total del día
    por medio de pago."""
    return {
        "version": VERSION,
        "cierre_id": cierre["id"],
        "sucursal_id": cierre["sucursal_id"],
        "fecha": cierre["fecha"],
        "numero": cierre["numero"],
        "usuario_id": cierre["usuario_id"],
        "notas": cierre["notas"] or "",
        "monto_esperado_total": _numero(cierre["monto_esperado_total"]),
        "monto_declarado_total": _numero(cierre["monto_declarado_total"]),
        "diferencia_total": _numero(cierre["diferencia_total"]),
        "turnos": [
            {"turno_id": t["id"], "caja_id": t["caja_id"], "monto_inicial": _numero(t["monto_inicial"]),
             "monto_esperado": _numero(t["monto_esperado_cierre"]),
             "monto_declarado": _numero(t["monto_declarado_cierre"]), "diferencia": _numero(t["diferencia"])}
            for t in turnos
        ],
        "medios": [
            {"medio_pago": m["medio_pago"], "ingresos": _numero(m["ingresos"]), "egresos": _numero(m["egresos"]),
             "neto": _numero(m["neto"])}
            for m in medios
        ],
    }
