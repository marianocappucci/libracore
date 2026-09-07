"""Reportes de negocio (P9-M4, 2026-09-07): el puerto con el que un producto
dice de dónde salen sus reportes, y el pivot de caja por medio de cobro.

Los reportes de **caja** son de este motor (`libracore.db.reportes`); los de
**ventas, medios de pago, productos, stock bajo y el resumen** dependen de
dónde viven las ventas y el stock. El default lee las tablas de LibraCore; los
productos con LibraCommerce pasan los de `libracommerce.erp.reportes`.

`MEDIO_LABEL`, `_pivot_caja_medios` y `_totales_por_medio` vienen de
`web/routers/reportes.py`, que los dos productos tenían igual.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from libracore import medios_pago
from libracore.db import reportes as db_reportes

# `sin_especificar` NO es un medio: es lo que el SQL del reporte pone cuando la
# columna viene nula, así que se resuelve acá y no en el vocabulario de la
# familia — meterlo allá lo haría elegible en un selector.
_SIN_MEDIO = {"sin_especificar": "Sin especificar", "": "Sin especificar"}

#: El mapa completo que consume la SPA (`medio_label`). Lleva **los históricos
#: también**: un reporte mira meses para atrás, y ahí hay filas con `tarjeta`,
#: `mercado_pago` y `cuenta corriente` con espacio.
MEDIO_LABEL = {**medios_pago.CONOCIDOS, **_SIN_MEDIO}


def medio_label(medio: str) -> str:
    return MEDIO_LABEL.get(medio, medio)


@dataclass(frozen=True)
class PuertoDeReportes:
    """Las cinco consultas que cambian según dónde viven las ventas y el stock.
    Firmas: `ventas(desde, hasta, agrupacion)`, `medios_pago(desde, hasta)`,
    `productos_top(desde, hasta, limit=20)`, `stock_bajo()`, `resumen(desde, hasta)`."""

    ventas: Callable[..., list[dict]] = field(default=db_reportes.get_reporte_ventas)
    medios_pago: Callable[..., list[dict]] = field(default=db_reportes.get_reporte_medios_pago)
    productos_top: Callable[..., list[dict]] = field(default=db_reportes.get_reporte_productos_top)
    stock_bajo: Callable[..., list[dict]] = field(default=db_reportes.get_reporte_stock_bajo)
    resumen: Callable[..., dict] = field(default=db_reportes.get_reporte_resumen)


#: Los reportes sobre las tablas de este motor.
REPORTES_LIBRACORE = PuertoDeReportes()


def pivot_caja_medios(rows: list) -> list:
    """Convierte las filas planas en una lista de cajas con medios pivoteados.
    Cada caja: {id, nombre, medios: {medio: {ingresos, ingresos_ops, egresos, egresos_ops}},
    total_ingresos, total_egresos, saldo}."""
    cajas: dict = {}
    for r in rows:
        cid = r["caja_id"]
        if cid not in cajas:
            cajas[cid] = {"nombre": r["caja_nombre"], "medios": {}}
        medio = r["medio"]
        if medio not in cajas[cid]["medios"]:
            cajas[cid]["medios"][medio] = {"ingresos": 0.0, "ingresos_ops": 0, "egresos": 0.0, "egresos_ops": 0}
        if r["tipo"] == "ingreso":
            cajas[cid]["medios"][medio]["ingresos"] += float(r["total"])
            cajas[cid]["medios"][medio]["ingresos_ops"] += int(r["operaciones"])
        else:
            cajas[cid]["medios"][medio]["egresos"] += float(r["total"])
            cajas[cid]["medios"][medio]["egresos_ops"] += int(r["operaciones"])

    result = []
    for cid, data in cajas.items():
        ti = sum(m["ingresos"] for m in data["medios"].values())
        te = sum(m["egresos"] for m in data["medios"].values())
        result.append({
            "id": cid, "nombre": data["nombre"], "medios": data["medios"],
            "total_ingresos": ti, "total_egresos": te, "saldo": ti - te,
        })
    return result


def totales_por_medio(cajas_pivot: list) -> dict:
    """Suma de ingresos/egresos por medio a través de todas las cajas."""
    totales: dict = {}
    for caja in cajas_pivot:
        for medio, vals in caja["medios"].items():
            if medio not in totales:
                totales[medio] = {"ingresos": 0.0, "ingresos_ops": 0, "egresos": 0.0, "egresos_ops": 0}
            totales[medio]["ingresos"] += vals["ingresos"]
            totales[medio]["ingresos_ops"] += vals["ingresos_ops"]
            totales[medio]["egresos"] += vals["egresos"]
            totales[medio]["egresos_ops"] += vals["egresos_ops"]
    return dict(sorted(totales.items()))


# Los nombres con guion bajo que los dos productos importaban de su router.
_pivot_caja_medios = pivot_caja_medios
_totales_por_medio = totales_por_medio
_medio_label = medio_label
