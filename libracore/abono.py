"""El abono mensual de una instancia: precio base + unidades adicionales contratadas (ADR-041).

Es **puro**: no lee la config, ni la base, ni el plan. Quien lo llama le pasa los números (el precio y las unidades incluidas salen del
`plans.py` del producto vía `libracore.admin.services.planes_info()`; las contratadas, de la instancia). Eso lo hace el mismo cálculo para el
backoffice, para una pantalla del cliente o para un reporte, y lo vuelve trivial de probar con una tabla.

La regla: el plan incluye `incluidas` unidades y cada una de más cuesta `precio_adicional`. **Se cobra lo contratado, no lo usado.**
`contratadas=None` es «sin dato cargado»: no hay cuántas cobrar de más, así que el total es el precio base, y se devuelve `contratadas: None`
para que quien lo muestre distinga «1 contratada» de «sin cargar».
"""

from __future__ import annotations


def calcular_abono(precio_base, incluidas: int, precio_adicional, contratadas: int | None) -> dict:
    """`{contratadas, incluidas, base, adicionales, precio_adicional, extra, total}`.

    `adicionales = max(0, contratadas - incluidas)`; `extra = adicionales * precio_adicional`; `total = base + extra`. Con `contratadas`
    en `None`, en 0 o por debajo de las incluidas no hay adicionales y `total == base`. Los precios se devuelven como vinieron (enteros
    en pesos, IVA incluido, en toda la familia): no se redondea ni se convierte."""
    adicionales = max(0, contratadas - incluidas) if contratadas is not None else 0
    extra = adicionales * precio_adicional
    return {
        "contratadas": contratadas,
        "incluidas": incluidas,
        "base": precio_base,
        "adicionales": adicionales,
        "precio_adicional": precio_adicional,
        "extra": extra,
        "total": precio_base + extra,
    }
