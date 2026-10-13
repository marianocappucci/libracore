"""Los adicionales FUERA de la escalera de planes (ADR-048): un módulo o una cantidad que se contrata aparte y que ningún cambio de plan toca.

## Qué resuelve

La escalera de cada producto (`plans.py`: `PLANES`, `PLAN_MODULOS`…) sólo sabe decir «este plan trae estos módulos». Un adicional es otra cosa: se
vende aparte, con un precio propio, y sobrevive a subir o bajar de plan. Había dos formas sueltas de expresarlo —`plans.ADDONS` (un módulo sí/no) y
`plans.PLAN_UNIDADES_ADICIONALES` (las sucursales de VentaLibra, ADR-041)— y una tercera que no cabía en ninguna: el módulo offline de RestoLibra se
cobra **por sucursal con offline**, es decir un sí/no *y* una cantidad. Acá hay UN catálogo que las cubre a todas.

## Cómo se declara

El `plans.py` del producto puede declarar `ADICIONALES = {clave: {...}}`. Todas las claves de cada entrada son opcionales salvo el precio, que puede
ser `None` («a cotizar»: no se inventa un precio que no se fijó):

    "label"     texto para la pantalla (default: la clave).
    "precio"    entero en pesos (IVA incluido, como en toda la familia) o `None` = a cotizar. Es por unidad si hay `unidad`.
    "modulos"   los módulos de la tabla `modulos` que el adicional prende. Default: `{clave}` si es un sí/no, nada si cuenta unidades.
    "unidad"    si está, el adicional se cuenta por unidades (`"sucursal"`); si no, es un sí/no.
    "incluidas" `{plan: n}`: las unidades que el plan ya trae sin cargo (el abono cobra desde la n+1).
    "limite"    el nombre del límite que se hace cumplir (`"sucursales"`, `"sucursales_offline"`).
    "planes"    los planes donde se puede contratar. Default: todos.

**Sin declarar nada, el catálogo se sintetiza** desde lo que el producto ya tiene: cada `ADDONS` es un sí/no sin precio, y cada
`PLAN_UNIDADES_ADICIONALES` es el adicional por unidades con el nombre de su unidad. Es compatibilidad total: los productos que no declaran
`ADICIONALES` siguen igual. Lo declarado pisa a lo sintetizado con la misma clave.

## Dónde vive el dato de cada instancia

- **Sí/no**: la tabla `modulos` (`plan="addon"`), que es lo que ya lee `require_module`. Ningún `apply_plan` la toca (`modulos_de_adicionales`).
- **Cantidad**: la clave `adicionales: {clave: n}` del `config.json` de la instancia. La única excepción es `sucursal`, que sigue en
  `limite_sucursales` (ADR-041): ninguna instancia existente cambia de lugar su dato.
- Un adicional con módulo *y* cantidad (offline) guarda las dos cosas, y la cantidad manda: `n >= 1` prende el módulo, quitarla lo apaga.

Un valor ilegible del `config.json` (un texto, un cero, un booleano, un `adicionales` que no es un diccionario) se lee como «sin dato»: nunca rompe la
lectura ni bloquea una instancia.

Este módulo es de datos y de cuentas; el HTTP está en `libracore.adicionales_router`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from libracore import config_manager

#: La clave del `config.json` donde viven las cantidades contratadas.
CLAVE_CONFIG = "adicionales"

#: La cota de una cantidad (un cero de más no tiene que volverse un contrato). Ninguna instancia de la familia se acerca.
MAXIMO = 1000

#: Los adicionales cuya cantidad NO vive en `adicionales` sino en una clave propia del `config.json` (ADR-041). Es el único caso: las sucursales
#: ya tenían su lugar antes de que existiera el catálogo y mover el dato obligaría a migrar cada instancia.
CLAVES_DE_CONFIG_PROPIAS = {"sucursal": "limite_sucursales"}

_CLAVES_VALIDAS = {"label", "precio", "modulos", "unidad", "incluidas", "limite", "planes"}


# ── errores ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

class AdicionalError(Exception):
    """El alta o el uso de un adicional no está permitido. `str(exc)` es el mensaje para el cliente, en castellano."""

    def __init__(self, clave: str, mensaje: str):
        self.clave = clave
        super().__init__(mensaje)


class AdicionalNoContratado(AdicionalError):
    """El adicional no está habilitado en esta instancia."""


class AdicionalExcedido(AdicionalError):
    """Sumar una unidad más superaría lo contratado."""

    def __init__(self, clave: str, contratadas: int, usadas: int, mensaje: str):
        self.contratadas = contratadas
        self.usadas = usadas
        super().__init__(clave, mensaje)


# ── el catálogo ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Adicional:
    clave: str
    label: str
    precio: int | None = None
    modulos: frozenset = field(default_factory=frozenset)
    unidad: str | None = None
    incluidas: dict = field(default_factory=dict)
    limite: str | None = None
    planes: frozenset | None = None

    @property
    def por_unidades(self) -> bool:
        return self.unidad is not None

    def en_plan(self, plan: str) -> bool:
        return self.planes is None or plan in self.planes

    def incluidas_en(self, plan: str) -> int:
        return int(self.incluidas.get(plan, 0))

    def como_dict(self) -> dict:
        """Para el JSON de la API y de `planes_info()`: listas ordenadas en vez de conjuntos."""
        return {
            "clave": self.clave, "label": self.label, "precio": self.precio, "modulos": sorted(self.modulos),
            "unidad": self.unidad, "incluidas": dict(self.incluidas), "limite": self.limite,
            "planes": None if self.planes is None else sorted(self.planes),
        }


def _entero(valor: Any, donde: str, *, minimo: int = 0) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < minimo:
        raise ValueError(f"{donde}: tiene que ser un entero >= {minimo}, no {valor!r}.")
    return valor


def _humanizar(clave: str) -> str:
    return clave.replace("_", " ").capitalize()


def _plural(unidad: str) -> str:
    """El plural de una unidad en castellano («sucursal» → «sucursales», «caja» → «cajas»)."""
    return unidad + ("s" if unidad[-1] in "aeiou" else "es")


def _normalizar(clave: str, crudo: dict) -> Adicional:
    if not isinstance(clave, str) or not clave:
        raise ValueError(f"ADICIONALES: la clave {clave!r} tiene que ser un texto no vacío.")
    if not isinstance(crudo, dict):
        raise ValueError(f"ADICIONALES[{clave!r}]: tiene que ser un diccionario.")
    sobran = set(crudo) - _CLAVES_VALIDAS
    if sobran:
        raise ValueError(f"ADICIONALES[{clave!r}]: claves desconocidas {sorted(sobran)} (las válidas: {sorted(_CLAVES_VALIDAS)}).")
    unidad = crudo.get("unidad")
    if unidad is not None and (not isinstance(unidad, str) or not unidad):
        raise ValueError(f"ADICIONALES[{clave!r}]['unidad']: tiene que ser un texto no vacío.")
    precio = crudo.get("precio")
    if precio is not None:
        precio = _entero(precio, f"ADICIONALES[{clave!r}]['precio']")
    incluidas = crudo.get("incluidas") or {}
    if incluidas and unidad is None:
        raise ValueError(f"ADICIONALES[{clave!r}]: 'incluidas' sólo tiene sentido en un adicional por unidades (falta 'unidad').")
    if not isinstance(incluidas, dict):
        raise ValueError(f"ADICIONALES[{clave!r}]['incluidas']: tiene que ser {{plan: cantidad}}.")
    incluidas = {p: _entero(n, f"ADICIONALES[{clave!r}]['incluidas'][{p!r}]") for p, n in incluidas.items()}
    limite = crudo.get("limite")
    if limite is not None and unidad is None:
        raise ValueError(f"ADICIONALES[{clave!r}]: 'limite' sólo tiene sentido en un adicional por unidades (falta 'unidad').")
    modulos = crudo.get("modulos")
    modulos = frozenset(modulos) if modulos is not None else (frozenset() if unidad else frozenset({clave}))
    if not all(isinstance(m, str) and m for m in modulos):
        raise ValueError(f"ADICIONALES[{clave!r}]['modulos']: tiene que ser un conjunto de nombres de módulo.")
    planes = crudo.get("planes")
    return Adicional(
        clave=clave, label=crudo.get("label") or _humanizar(clave), precio=precio, modulos=modulos, unidad=unidad,
        incluidas=incluidas, limite=limite, planes=None if planes is None else frozenset(planes),
    )


def _sintetizar_unidades(plans) -> dict[str, Adicional]:
    """Lo que declaraba el ADR-041: `PLAN_UNIDADES_ADICIONALES = {plan: {"unidad", "incluidas", "precio"}}`, agrupado por unidad."""
    por_unidad: dict[str, dict] = {}
    for plan, datos in (getattr(plans, "PLAN_UNIDADES_ADICIONALES", None) or {}).items():
        if not datos:
            continue
        unidad = datos["unidad"]
        grupo = por_unidad.setdefault(unidad, {"precio": datos.get("precio"), "incluidas": {}, "planes": set()})
        if grupo["precio"] != datos.get("precio"):
            raise ValueError(
                f"PLAN_UNIDADES_ADICIONALES: la unidad {unidad!r} tiene precios distintos según el plan; declará ADICIONALES para expresarlo.")
        grupo["incluidas"][plan] = int(datos.get("incluidas", 0))
        grupo["planes"].add(plan)
    return {
        unidad: Adicional(
            clave=unidad, label=f"{_humanizar(_plural(unidad))} adicionales", precio=g["precio"], unidad=unidad,
            incluidas=g["incluidas"], limite=_plural(unidad), planes=frozenset(g["planes"]),
        )
        for unidad, g in por_unidad.items()
    }


def catalogo(plans) -> dict[str, Adicional]:
    """Los adicionales del producto, `{clave: Adicional}`. Si `plans` no declara `ADICIONALES`, se sintetiza desde `ADDONS` y
    `PLAN_UNIDADES_ADICIONALES` (compatibilidad total); lo declarado pisa a lo sintetizado con la misma clave. Levanta `ValueError` ante una
    declaración mal formada: un error de tipeo en el `plans.py` se ve al primer uso y no como un cobro equivocado."""
    resultado: dict[str, Adicional] = {}
    for clave in sorted(getattr(plans, "ADDONS", None) or ()):
        resultado[clave] = Adicional(clave=clave, label=_humanizar(clave), modulos=frozenset({clave}))
    resultado.update(_sintetizar_unidades(plans))
    declarados = getattr(plans, "ADICIONALES", None) or {}
    for clave, crudo in declarados.items():
        resultado[clave] = _normalizar(clave, crudo)
    if declarados:
        _validar_contra_la_escalera(plans, [resultado[c] for c in declarados])
    return resultado


def _validar_contra_la_escalera(plans, declarados: list[Adicional]) -> None:
    """Un módulo no puede estar a la vez en un plan y en un adicional: el plan lo prendería o apagaría solo y el adicional dejaría de ser
    contratable. Y un plan inexistente en `incluidas`/`planes` es un typo."""
    en_planes = set().union(*(set(m) for m in (getattr(plans, "PLAN_MODULOS", None) or {}).values()))
    planes = set(getattr(plans, "PLANES", None) or ()) | set(getattr(plans, "PLANES_RETIRADOS", None) or ())
    for a in declarados:
        choque = a.modulos & en_planes
        if choque:
            raise ValueError(f"ADICIONALES[{a.clave!r}]: los módulos {sorted(choque)} ya pertenecen a un plan; un adicional es lo que NO está en la escalera.")
        for p in [*a.incluidas, *(a.planes or ())]:
            if planes and p not in planes:
                raise ValueError(f"ADICIONALES[{a.clave!r}]: el plan {p!r} no existe (los de la escalera: {sorted(planes)}).")


def modulos_de_adicionales(plans) -> set[str]:
    """Todos los módulos que son de un adicional (superconjunto de `ADDONS`). Es lo que `apply_plan` y `apply_plan_modules` dejan sin tocar."""
    modulos = set(getattr(plans, "ADDONS", None) or ())
    for a in catalogo(plans).values():
        modulos |= a.modulos
    return modulos


def adicionales_del_plan(plans, plan: str) -> list[Adicional]:
    """Los adicionales que se pueden contratar con `plan`, en orden de clave."""
    return [a for _, a in sorted(catalogo(plans).items()) if a.en_plan(plan)]


# ── el abono por líneas ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _cantidad_valida(valor) -> int | None:
    """Una cantidad contratada, o `None` si no es un entero entre 1 y `MAXIMO` (= sin dato). Nunca lanza."""
    if isinstance(valor, bool) or not isinstance(valor, int) or not 1 <= valor <= MAXIMO:
        return None
    return valor


def _contratado(a: Adicional, valor) -> int | None:
    """Cuánto hay contratado de `a` según `valor`: para un sí/no, 1 si es `True`; para unidades, la cantidad. `None` = no contratado."""
    if a.por_unidades:
        return _cantidad_valida(valor)
    return 1 if valor is True else None


def _linea(a: Adicional, plan: str, cantidad: int) -> dict | None:
    """La línea del abono de un adicional contratado, o `None` si no se cobra nada de más (las unidades contratadas entran todas en el plan)."""
    incluidas = a.incluidas_en(plan) if a.por_unidades else 0
    cobrables = max(0, cantidad - incluidas)
    if a.por_unidades and cobrables == 0:
        return None
    return {
        "clave": a.clave, "label": a.label, "unidad": a.unidad, "cantidad": cantidad, "incluidas": incluidas, "cobrables": cobrables,
        "precio": a.precio, "subtotal": None if a.precio is None else a.precio * cobrables, "a_cotizar": a.precio is None,
    }


def incompatibles(plans, plan: str, contratados: dict) -> list[str]:
    """Las claves contratadas que `plan` no admite (`planes` del adicional), en orden. Un adicional no contratado no estorba."""
    cat = catalogo(plans)
    return sorted(c for c, v in contratados.items() if c in cat and _contratado(cat[c], v) and not cat[c].en_plan(plan))


def resolver(plans, plan: str, contratados: dict) -> dict:
    """Lo que implica contratar `contratados` (`{clave: True | cantidad | None}`) sobre `plan`:

    - `modulos`: `{módulo: bool}` de **todos** los módulos de adicionales (prendido si algún adicional contratado lo trae).
    - `limites`: `{nombre_del_limite: cantidad}` de los adicionales por unidades contratados que declaran `limite`.
    - `lineas_abono`: una línea por adicional contratado que cobra algo (ver `calcular_abono_total`); `precio=None` sale `a_cotizar`.
    - `incompatibles`: los contratados que `plan` no admite. No es una excepción: quien cambia de plan decide (el backoffice responde 409).

    Una clave que el catálogo no tiene es `ValueError`: contratar algo que no se vende es un error de quien llama."""
    cat = catalogo(plans)
    desconocidos = sorted(set(contratados) - set(cat))
    if desconocidos:
        raise ValueError(f"Adicional desconocido: {desconocidos}. Los del producto: {sorted(cat)}.")
    modulos = {m: False for a in cat.values() for m in a.modulos}
    limites: dict[str, int] = {}
    lineas: list[dict] = []
    for clave in sorted(cat):
        a = cat[clave]
        cantidad = _contratado(a, contratados.get(clave))
        if cantidad is None:
            continue
        for m in a.modulos:
            modulos[m] = True
        if a.por_unidades and a.limite:
            limites[a.limite] = cantidad
        linea = _linea(a, plan, cantidad)
        if linea:
            lineas.append(linea)
    return {"modulos": modulos, "limites": limites, "lineas_abono": lineas, "incompatibles": incompatibles(plans, plan, contratados)}


def calcular_abono_total(base, lineas: list[dict]) -> dict:
    """El abono: el precio base del plan más las líneas de `resolver(...)["lineas_abono"]`. Puro, como `libracore.abono.calcular_abono`.

    `{base, lineas, extra, total, a_cotizar}`. Una línea sin precio NO suma (no hay número que sumar) y su clave va a `a_cotizar`: quien lo
    muestre dice «a cotizar» y no un total que parece completo. Con `base=None` (plan sin precio) el `total` es `None`."""
    extra = sum(ln["subtotal"] for ln in lineas if ln.get("subtotal") is not None)
    return {
        "base": base, "lineas": [dict(ln) for ln in lineas], "extra": extra,
        "total": None if base is None else base + extra, "a_cotizar": [ln["clave"] for ln in lineas if ln.get("subtotal") is None],
    }


# ── el dato de la instancia ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def cantidad_contratada(clave: str, cfg: dict | None = None) -> int | None:
    """La cantidad de `clave` en el `config.json` de la instancia, o `None` si no hay dato. **Nunca lanza**: un texto, un cero, un booleano o un
    `adicionales` que no es un diccionario se leen como «sin dato»."""
    if cfg is None:
        cfg = config_manager.load()
    propia = CLAVES_DE_CONFIG_PROPIAS.get(clave)
    if propia:
        return _cantidad_valida(cfg.get(propia))
    todas = cfg.get(CLAVE_CONFIG)
    if not isinstance(todas, dict):
        return None
    return _cantidad_valida(todas.get(clave))


def fijar_cantidad(clave: str, cantidad: int | None) -> int | None:
    """Guarda la cantidad de `clave` (entero entre 1 y `MAXIMO`) o la quita con `None`. Devuelve lo que quedó. `ValueError` si no es válida.
    Lee-modifica-guarda el `config.json` (atómico, `config_manager.save`): no pisa el resto de la configuración."""
    if cantidad is not None and (isinstance(cantidad, bool) or not isinstance(cantidad, int) or not 1 <= cantidad <= MAXIMO):
        raise ValueError(f"La cantidad contratada de {clave!r} tiene que ser un entero entre 1 y {MAXIMO}.")
    cfg = config_manager.load()
    propia = CLAVES_DE_CONFIG_PROPIAS.get(clave)
    if propia:
        if cantidad is None:
            cfg.pop(propia, None)
        else:
            cfg[propia] = cantidad
    else:
        todas = cfg.get(CLAVE_CONFIG)
        todas = dict(todas) if isinstance(todas, dict) else {}
        if cantidad is None:
            todas.pop(clave, None)
        else:
            todas[clave] = cantidad
        if todas:
            cfg[CLAVE_CONFIG] = todas
        else:
            cfg.pop(CLAVE_CONFIG, None)
    config_manager.save(cfg)
    return cantidad


def estado_de(a: Adicional, *, cfg: dict | None = None, modulos: dict | None = None) -> dict:
    """`{habilitado, cantidad}` de un adicional en esta instancia.

    - Sí/no: `habilitado` si todos sus módulos están prendidos; `cantidad` es `None`.
    - Por unidades **sin** módulo (sucursales): `cantidad` es lo cargado; `habilitado` es que haya dato. Sin dato no hay límite (ADR-041).
    - Por unidades **con** módulo (offline): `habilitado` es el módulo; `cantidad` es lo cargado y puede ser `None` si el módulo se prendió sin
      cargar un número (sin número no hay tope)."""
    cantidad = cantidad_contratada(a.clave, cfg) if a.por_unidades else None
    if a.modulos:
        habilitado = all(bool((modulos or {}).get(m)) for m in a.modulos)
    else:
        habilitado = cantidad is not None
    return {"habilitado": habilitado, "cantidad": cantidad}


def contratados_de_la_instancia(plans, *, cfg: dict | None = None, modulos: dict | None = None) -> dict:
    """`{clave: True | cantidad | None}` de lo contratado en esta instancia, en la forma que espera `resolver`. Un adicional con módulo y cantidad
    sin cargar cuenta como no contratado para el abono (no hay número que cobrar) aunque el módulo esté prendido."""
    if cfg is None:
        cfg = config_manager.load()
    resultado = {}
    for clave, a in catalogo(plans).items():
        e = estado_de(a, cfg=cfg, modulos=modulos)
        resultado[clave] = (e["cantidad"] if e["habilitado"] else None) if a.por_unidades else e["habilitado"]
    return resultado


# ── hacer cumplir ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def mensaje_de_exceso(a: Adicional, contratadas: int) -> str:
    unidad = a.unidad or "unidad"
    plural = unidad if contratadas == 1 else _plural(unidad)
    return f"Tenés contratado «{a.label}» para {contratadas} {plural}. Para sumar otra, comunicate con nosotros."


def mensaje_de_no_contratado(a: Adicional) -> str:
    return f"Tu plan no incluye «{a.label}». Para contratarlo, comunicate con nosotros."


def verificar_por_limite(a: Adicional, usadas_antes: int, *, contratadas: int | None, sumar: int = 1) -> None:
    """Levanta `AdicionalExcedido` si sumar `sumar` unidades dejaría `usadas_antes + sumar` por encima de `contratadas`. `contratadas=None` es «sin
    límite» y no bloquea. Es el verificador genérico; el alta de sucursales sigue usando el suyo (`libracore.limites`), con su propio mensaje."""
    if contratadas is not None and usadas_antes + sumar > contratadas:
        raise AdicionalExcedido(a.clave, contratadas, usadas_antes, mensaje_de_exceso(a, contratadas))


def verificar_alta(plans, clave: str, usadas_antes: int, *, sumar: int = 1, cfg: dict | None = None, modulos: dict | None = None) -> None:
    """La guarda de un producto antes de dar de alta una unidad de un adicional (un nodo offline, una caja extra…).

    - Un adicional con módulo que no está prendido: `AdicionalNoContratado` (no se contrató, o se dio de baja).
    - Un adicional por unidades: si `usadas_antes + sumar` supera la cantidad cargada, `AdicionalExcedido`. Sin cantidad cargada, no limita.

    `usadas_antes` son las unidades en uso **antes** de la nueva (para offline: las sucursales distintas que ya tienen nodo; quien llama NO cuenta
    la nueva si cae en una sucursal que ya tenía). `modulos` es el `{módulo: bool}` de la instancia; sin pasarlo se lee de `libracore.db.modulos`."""
    cat = catalogo(plans)
    if clave not in cat:
        raise ValueError(f"Adicional desconocido: {clave!r}.")
    a = cat[clave]
    if a.modulos and modulos is None:
        from libracore.db.modulos import get_modulos
        modulos = get_modulos()
    e = estado_de(a, cfg=cfg, modulos=modulos)
    if a.modulos and not e["habilitado"]:
        raise AdicionalNoContratado(clave, mensaje_de_no_contratado(a))
    if a.por_unidades:
        verificar_por_limite(a, usadas_antes, contratadas=e["cantidad"], sumar=sumar)
