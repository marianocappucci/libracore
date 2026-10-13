# Adicionales fuera de la escalera de planes

ADR-048. Un **adicional** es algo que se contrata aparte del plan —un módulo sí/no, o una cantidad de unidades— y que **ningún cambio de plan toca**. Esta guía es para quien agrega uno a un producto.

## Declararlo

En el `plans.py` del producto (todo opcional salvo lo que se quiera cobrar):

```python
ADICIONALES = {
    "offline": {
        "label": "Modo offline",            # texto de pantalla
        "precio": None,                     # None = «a cotizar»; un entero en pesos (IVA incluido) si se fijó
        "modulos": {"offline"},             # módulos de la tabla `modulos` que prende
        "unidad": "sucursal",               # si está, se cuenta por unidades; si no, es un sí/no
        "incluidas": {},                    # {plan: n}: unidades que el plan trae sin cargo
        "limite": "sucursales_offline",     # nombre del límite que se hace cumplir
        "planes": {"basico", "estandar"},   # dónde se puede contratar; sin esto, todos
    },
}
```

Sin declarar nada, `libracore.adicionales.catalogo(plans)` lo **sintetiza** desde `ADDONS` (cada uno, un sí/no sin precio) y `PLAN_UNIDADES_ADICIONALES` (las unidades del ADR-041). Un módulo de adicional no puede estar también en `PLAN_MODULOS`.

## Dónde vive el dato

| Qué | Dónde |
|---|---|
| sí/no | tabla `modulos`, `plan="addon"` (lo que lee `require_module`) |
| cantidad | `config.json` → `adicionales: {clave: n}` (`sucursal` sigue en `limite_sucursales`) |
| copia informativa | `cliente.json` del host → `adicionales` (si difiere, gana la instancia) |

`apply_plan` / `apply_plan_modules` saltean `modulos_de_adicionales(plans)`; `set_plan` responde 409 si un adicional contratado no cabe en el plan nuevo.

## Montarlo en el producto

```python
from libracore.adicionales_router import build_adicionales_router
app.include_router(build_adicionales_router(
    guard_lectura, guard_escritura,            # lectura: admin o token de servicio; escritura: SÓLO el token de servicio
    medir={"offline": sucursales_con_nodo},    # cuánto hay en uso de cada adicional por unidades
))
```

- `GET /api/adicionales` → `{adicionales: [{clave, label, precio, modulos, unidad, incluidas, limite, planes, habilitado, cantidad, usadas}], limites: {nombre: n}}`.
- `PUT /api/adicionales/{clave}` → `{"habilitado": bool|null}` (sí/no) o `{"cantidad": 1..1000|null}` (unidades); `null` es la baja. 404 si la clave no existe, 422 si el cuerpo no trae exactamente uno o no corresponde al tipo.

## Hacerlo cumplir

```python
from libracore.adicionales import AdicionalError, verificar_alta
verificar_alta(plans, "offline", usadas_antes=sucursales_con_nodo_antes_de_la_nueva)
```

Levanta `AdicionalNoContratado` (módulo apagado) o `AdicionalExcedido` (sobre la cantidad); `str(exc)` es el mensaje para el cliente. Un módulo prendido sin cantidad cargada no limita (sin dato, sin límite: ADR-041).

## El abono

```python
r = resolver(plans, plan, contratados)          # contratados = {clave: True | cantidad}
abono = calcular_abono_total(PLAN_PRECIOS[plan], r["lineas_abono"])
# {base, lineas, extra, total, a_cotizar}: una línea sin precio no suma y su clave va a `a_cotizar`
```
