"""`GET /api/adicionales` y `PUT /api/adicionales/{clave}`: lo contratado fuera de la escalera, visto y cargado en la instancia (ADR-048).

Mismo diseño que `libracore.limites` (ADR-041), generalizado: el dato vive en la INSTANCIA —los sí/no en la tabla `modulos`, las cantidades en su
`config.json`— para que ella pueda hacer cumplir un tope sin llamar al backoffice, y el backoffice (que lo carga con el token de servicio) y el
cliente ven el mismo número. El catálogo (qué existe, cuánto cuesta) sale del `plans.py` del producto (`libracore.adicionales`).

`/api/limites/sucursales` (`libracore.limites.build_limites_router`) **sigue funcionando** y escribe la misma clave del `config.json`
(`limite_sucursales`): es el alias de `PUT /api/adicionales/sucursal` para los backoffices que todavía no conocen el catálogo.

## Quién puede qué

Las guardas las inyecta el producto y son DOS, como en `limites`: leer es de admin **o** del token de servicio del backoffice; **escribir es sólo
del token de servicio**. 🔴 Un adicional es un límite comercial: si el admin del cliente pudiera prenderse uno o subirse su cantidad, contratar
no costaría nada. Una guarda que acepte a un admin de la instancia en `guard_escritura` desarma el ADR-048.

## El cuerpo del PUT

Exactamente UNO de los dos campos:

- `{"habilitado": true | false | null}` para un sí/no. `false` y `null` son la baja.
- `{"cantidad": n | null}` para un adicional por unidades (`1..1000`). `null` es la baja. Sobre un adicional con módulo (offline) la cantidad
  prende o apaga el módulo en el mismo paso: así no queda un módulo prendido sin número, ni un número sin módulo.

Para un adicional por unidades, `{"habilitado": false | null}` también da de baja; `{"habilitado": true}` es 422 («indicá la cantidad»).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from libracore import adicionales as ad
from libracore import config_manager
from libracore.validacion import sin_booleanos


class AdicionalIn(BaseModel):
    """Exactamente uno de `habilitado` (sí/no) o `cantidad` (por unidades); `null` es la baja."""

    habilitado: bool | None = None
    cantidad: int | None = Field(default=None, ge=1, le=ad.MAXIMO)

    _no_son_booleanos = sin_booleanos("cantidad")


def _modulos_por_defecto() -> tuple[Callable[[], dict], Callable[[str, bool], None]]:
    from libracore.db.modulos import get_modulos, set_addon
    return get_modulos, set_addon


def build_adicionales_router(guard_lectura: Callable, guard_escritura: Callable, medir: dict[str, Callable[[], int]] | None = None, *,
                             plans=None, leer_modulos: Callable[[], dict] | None = None,
                             escribir_modulo: Callable[[str, bool], None] | None = None, prefix: str = "/api/adicionales") -> APIRouter:
    """El router de adicionales de la instancia.

    - `guard_lectura` / `guard_escritura`: ver arriba. Las pone el producto; LibraCore no depende de su motor de auth.
    - `medir`: `{clave: callable}` con lo que está en uso de cada adicional por unidades (`"sucursal"`: las sucursales activas; `"offline"`: las
      sucursales con un nodo). Un adicional sin medidor devuelve `usadas: null` («no se sabe»).
    - `plans`: el módulo `plans` del producto; sin pasarlo se importa `plans` (como hace `apply_plan`).
    - `leer_modulos` / `escribir_modulo`: cómo leer y escribir la tabla `modulos` de ESTA instancia. Por defecto `libracore.db.modulos`
      (`get_modulos`, `set_addon`), que sirve a los productos que configuraron `libracore.db.core`; los de SQLAlchemy pasan los de su
      `app.database`."""
    router = APIRouter(prefix=prefix, tags=["adicionales"])
    medir = medir or {}

    def _plans():
        if plans is not None:
            return plans
        import plans as p
        return p

    def _leer() -> dict:
        return (leer_modulos or _modulos_por_defecto()[0])()

    def _escribir(modulo: str, valor: bool) -> None:
        (escribir_modulo or _modulos_por_defecto()[1])(modulo, valor)

    def _usadas(a: ad.Adicional) -> int | None:
        medidor = medir.get(a.clave)
        return int(medidor()) if medidor else None

    def _item(a: ad.Adicional, cfg: dict, modulos: dict) -> dict:
        return {**a.como_dict(), **ad.estado_de(a, cfg=cfg, modulos=modulos), "usadas": _usadas(a) if a.por_unidades else None}

    def _estado() -> dict:
        cat = ad.catalogo(_plans())
        cfg = config_manager.load()
        modulos = _leer() if any(a.modulos for a in cat.values()) else {}
        items = [_item(cat[c], cfg, modulos) for c in sorted(cat)]
        limites = {i["limite"]: i["cantidad"] for i in items if i["limite"] and i["cantidad"] is not None}
        return {"adicionales": items, "limites": limites}

    @router.get("", dependencies=[Depends(guard_lectura)])
    def listar() -> dict:
        return _estado()

    @router.put("/{clave}", dependencies=[Depends(guard_escritura)])
    def fijar(clave: str, cuerpo: AdicionalIn) -> dict:
        cat = ad.catalogo(_plans())
        a = cat.get(clave)
        if a is None:
            raise HTTPException(404, f"No hay ningún adicional {clave!r}. Los del producto: {sorted(cat)}.")
        pedidos = cuerpo.model_fields_set & {"habilitado", "cantidad"}
        if len(pedidos) != 1:
            raise HTTPException(422, "Indicá exactamente uno: «habilitado» (sí/no) o «cantidad» (por unidades); null es la baja.")
        if a.por_unidades:
            if "habilitado" in pedidos and cuerpo.habilitado:
                raise HTTPException(422, f"«{a.label}» se cuenta por {a.unidad}: indicá la cantidad.")
            cantidad = cuerpo.cantidad if "cantidad" in pedidos else None  # habilitado false/null = baja
            ad.fijar_cantidad(clave, cantidad)
            for m in sorted(a.modulos):
                _escribir(m, cantidad is not None)
        else:
            if "cantidad" in pedidos:
                raise HTTPException(422, f"«{a.label}» es un sí/no: indicá «habilitado», no una cantidad.")
            for m in sorted(a.modulos):
                _escribir(m, bool(cuerpo.habilitado))
        return _item(a, config_manager.load(), _leer() if a.modulos else {})

    return router
