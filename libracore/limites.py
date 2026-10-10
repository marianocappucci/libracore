"""El tope de sucursales que el cliente CONTRATÓ, y cómo se hace cumplir (ADR-041).

El abono es `precio base + sucursales adicionales × precio` (`libracore.abono`) y se cobra **lo contratado**. La cantidad contratada es un
dato de la INSTANCIA —la clave `limite_sucursales` de su `config.json`, el mismo mecanismo que el tema (`libracore.tema_router`)—, y no un
archivo del host: así la instancia puede bloquear un alta sin salir de su proceso, y el backoffice (que la carga con el token de servicio) y
el cliente ven el mismo número.

## Tres decisiones del dueño (2026-10-10)

1. **Se cobra lo contratado**: el número lo carga el backoffice; el sistema calcula el abono y muestra «activas vs contratadas».
2. **Pasarse bloquea el alta** (y la reactivación): con la sucursal nueva, las activas no pueden superar las contratadas. El producto llama a
   `verificar_alta_de_sucursal` desde su gancho de alta y de reactivación; acá no se toca ninguna tabla del dominio.
3. **Sin dato cargado, no hay límite**: ninguna instancia existente cambia de comportamiento hasta que alguien cargue un número. `GET` devuelve
   `contratadas: null` y el backoffice muestra «Sin cargar».

## Lo que NO hace

No da de baja nada: si se baja el límite por debajo de las activas (el cliente achicó el contrato), las sucursales que ya existen siguen
andando; lo único que cambia es que no se puede sumar otra. Tampoco decide quién puede llamar: **las guardas las pone el producto**,
como en `resumen_router`. Leer es de admin **o** del token de servicio del backoffice (`X-Internal-Auth`); **escribir es sólo del token de
servicio**: el tope es un límite comercial y el admin del cliente no puede subírselo.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from libracore import config_manager
from libracore.validacion import sin_booleanos

#: La clave del `config.json` donde vive la cantidad de sucursales contratadas.
CLAVE_CONFIG = "limite_sucursales"

#: Una cota para que un PUT no cargue un número absurdo (un cero de más). Ninguna instancia de la familia se acerca.
MAXIMO = 1000


class LimiteExcedido(Exception):
    """El alta (o la reactivación) de una sucursal superaría lo contratado. `str(exc)` es el mensaje para el cliente, en castellano."""

    def __init__(self, contratadas: int, activas: int):
        self.contratadas = contratadas
        self.activas = activas
        super().__init__(mensaje_de_limite(contratadas))


def mensaje_de_limite(contratadas: int) -> str:
    plural = "sucursal" if contratadas == 1 else "sucursales"
    return f"Tu plan incluye {contratadas} {plural}. Para sumar otra, comunicate con nosotros."


def sucursales_contratadas(cfg: dict | None = None) -> int | None:
    """Lo contratado, o `None` si no hay dato (= sin límite). **Nunca lanza**: un `config.json` editado a mano, con un texto o un cero, no puede
    ni romper la lectura ni bloquear una instancia; un valor que no sea un entero ≥ 1 se lee como «sin dato»."""
    if cfg is None:
        cfg = config_manager.load()
    valor = cfg.get(CLAVE_CONFIG)
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
        return None
    return valor


def fijar_sucursales_contratadas(valor: int | None) -> int | None:
    """Guarda lo contratado (entero ≥ 1) o lo quita con `None`. Devuelve lo que quedó. `ValueError` si no es un entero entre 1 y `MAXIMO`."""
    if valor is not None and (isinstance(valor, bool) or not isinstance(valor, int) or not 1 <= valor <= MAXIMO):
        raise ValueError(f"La cantidad de sucursales contratadas tiene que ser un entero entre 1 y {MAXIMO}.")
    cfg = config_manager.load()
    if valor is None:
        cfg.pop(CLAVE_CONFIG, None)
    else:
        cfg[CLAVE_CONFIG] = valor
    config_manager.save(cfg)
    return valor


def verificar_alta_de_sucursal(activas_actuales: int, *, contratadas: int | None = None) -> None:
    """Levanta `LimiteExcedido` si dar de alta (o reactivar) UNA sucursal más dejaría `activas_actuales + 1` por encima de lo contratado.

    `activas_actuales` son las activas **antes** de la nueva (en una reactivación, sin contar la que se reactiva). `contratadas` es opcional: sin
    pasarlo se lee del `config.json` de la instancia; un test o un script que ya lo tiene lo pasa. Sin dato (`None`) no limita."""
    if contratadas is None:
        contratadas = sucursales_contratadas()
    if contratadas is not None and activas_actuales + 1 > contratadas:
        raise LimiteExcedido(contratadas, activas_actuales)


class LimiteIn(BaseModel):
    """El cuerpo del `PUT`: `contratadas` es un entero ≥ 1, o `null` para quitar el dato (la instancia queda sin límite)."""

    contratadas: int | None = Field(..., ge=1, le=MAXIMO)

    _no_son_booleanos = sin_booleanos("contratadas")


class LimiteOut(BaseModel):
    contratadas: int | None
    activas: int


def build_limites_router(*, guard_lectura: Callable, guard_escritura: Callable, medir_activas: Callable[[], int],
                         prefix: str = "/api/limites") -> APIRouter:
    """`GET`/`PUT {prefix}/sucursales`: lo contratado y lo activo, y la carga de lo contratado.

    Las guardas vienen **inyectadas a propósito** (como en `resumen_router`): LibraCore no depende del motor de auth. Son DOS porque
    quien mira el contrato y quien lo cambia no son los mismos:

    - `guard_lectura`: admin de la instancia **o** el token de servicio del backoffice. El cliente ve «activas vs contratadas».
    - `guard_escritura`: **sólo el token de servicio.** 🔴 El tope es un límite comercial: si el admin del cliente pudiera cargar su propio
      número, el bloqueo no bloquearía nada. Una guarda que acepte a un admin de la instancia acá desarma la decisión 2 del ADR-041.

    `medir_activas()` devuelve cuántas sucursales activas tiene la instancia (el producto sabe dónde viven; acá no se asume ninguna tabla)."""
    router = APIRouter(prefix=prefix, tags=["limites"])

    def _estado() -> LimiteOut:
        return LimiteOut(contratadas=sucursales_contratadas(), activas=int(medir_activas()))

    @router.get("/sucursales", response_model=LimiteOut, dependencies=[Depends(guard_lectura)])
    def obtener() -> LimiteOut:
        return _estado()

    @router.put("/sucursales", response_model=LimiteOut, dependencies=[Depends(guard_escritura)])
    def fijar(cuerpo: LimiteIn) -> LimiteOut:
        fijar_sucursales_contratadas(cuerpo.contratadas)
        return _estado()

    return router
