"""Los Términos y Condiciones PROPIOS de esta instancia: lo que un revendedor de marca blanca carga para sus clientes (ADR-093).

Hermano de `marca_router` (ADR-092): lo que se guarda vive en la instancia —la versión y la vigencia en la clave `terminos_propios` del
`config.json`, el texto en `$DATA_DIR/marca/terminos.md`— y **no** en la base de `usuarios`, así que no hay migración. Quien los **usa**
es `libraauth` (`TerminosRepository(contrato_propio=leer_terminos_propios, exigir_propio=marca_router.es_marca_propia)`, libraauth
ADR-030): este módulo sólo los guarda y los entrega. `libracore` no depende de `libraauth` ni al revés; el producto los une.

## Qué expone

| Ruta (dentro de `build_marca_admin_router`) | Quién | Qué |
|---|---|---|
| `GET /api/marca/terminos` | admin / servicio | si hay términos cargados: versión, vigencia, huella y largo. **No** devuelve el texto (lo sirve `libraauth`, `GET /terminos?texto=1`) |
| `PUT /api/marca/terminos` | admin / servicio | carga o reemplaza: `{version, vigente_desde, texto}` |
| `DELETE /api/marca/terminos` | admin / servicio | los saca. Con la instancia como marca propia, queda frenada hasta que se cargue otros |

No hay ruta pública: el texto lo ve quien entra a la instancia, por la pantalla de aceptación.

## 🔑 Decisiones que no se ven en el código

- **Una versión es UN texto.** Publicar otra vez la misma versión con otro texto es 409: hay que subir la versión. `libraauth` ya no daría por
  aceptado un texto distinto (la prueba es versión y hash), pero dos textos con el mismo número en el historial de un cliente es justo lo
  que una prueba legal no tiene que tener. Publicar el mismo texto otra vez es idempotente. El historial `versión -> huella` queda en
  `terminos_propios_historial` aunque se borren los términos.
- **Las reglas de forma son las de `libraauth.terminos.Contrato.propio_desde`** (versión, vigencia, tope del texto). Se duplican a
  propósito porque no hay dependencia entre los dos motores; `tests/test_terminos_propios.py` fija los mismos límites.
- **El texto se guarda normalizado a LF** (como `libraauth.texto_vigente`): la huella no depende de los finales de línea de quien lo cargó.
- **El caché compara el CONTENIDO, no la fecha ni el inodo.** Se lee el archivo en cada llamada (~30 KB, decenas de microsegundos) y, si es
  igual a lo último leído, se devuelve el mismo objeto. Una clave de fecha, largo e inodo parecía suficiente y no lo es: la fecha tiene la
  resolución del reloj del kernel (unos milisegundos) y ext4 reusa los inodos liberados, así que dos escrituras seguidas del mismo largo
  (un archivo restaurado de un respaldo, una edición a mano) pueden dejar la misma clave con otro texto. Con el contenido no hay clave que
  adivinar. Lo que se ahorra es lo caro de verdad: que `libraauth` vuelva a armar y a hashear el contrato en cada pedido.
- **Sin términos no es un error:** `leer_terminos_propios()` devuelve `None` y `libraauth` decide (la instancia común sigue con los de la
  familia; la de un revendedor queda frenada).
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from libracore import config_manager

#: La clave del `config.json` donde viven la versión y la vigencia.
CLAVE_CONFIG = "terminos_propios"
#: Qué huella tuvo cada versión que se publicó alguna vez: `{version: sha256}`. Es lo que impide reusar un número con otro texto.
CLAVE_HISTORIAL = "terminos_propios_historial"

ARCHIVO = "terminos.md"

#: Mismas reglas que `libraauth.terminos.Contrato.propio_desde`.
MAX_TEXTO = 300_000
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,19}")


def _ruta() -> str:
    return os.path.join(config_manager.MARCA_DIR, ARCHIVO)


def normalizar_texto(texto: str) -> str:
    return texto.replace("\r\n", "\n").replace("\r", "\n")


def huella(texto: str) -> str:
    """sha256 hex del texto normalizado: la misma que calcula `libraauth.terminos.Contrato.hash`."""
    return hashlib.sha256(normalizar_texto(texto).encode("utf-8")).hexdigest()


# ── Lo guardado ────────────────────────────────────────────────────────────

#: Lo último que se leyó. Se reusa sólo si la versión, la vigencia Y el texto son iguales (ver el docstring del módulo).
_ultimo: dict | None = None


def leer_terminos_propios() -> dict | None:
    """Los términos de esta instancia como `{"version", "vigente_desde", "texto"}`, o `None` si no tiene.

    Es el gancho `contrato_propio` de `libraauth`: se llama en CADA pedido gateado. Devuelve el **mismo objeto** mientras la versión, la
    vigencia y el texto no cambien (libraauth no vuelve a armar ni a hashear el contrato). Sin términos cargados no toca el disco. Nunca
    lanza: un `config.json` o un archivo en mal estado es «no tiene».
    """
    global _ultimo
    meta = config_manager.load().get(CLAVE_CONFIG)
    if not isinstance(meta, dict):
        return None
    version, desde = meta.get("version"), meta.get("vigente_desde")
    if not isinstance(version, str) or not isinstance(desde, str):
        return None
    try:
        with open(_ruta(), encoding="utf-8", newline="") as f:
            texto = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    previo = _ultimo
    if previo is not None and previo["version"] == version and previo["vigente_desde"] == desde and previo["texto"] == texto:
        return previo
    valor = {"version": version, "vigente_desde": desde, "texto": texto}
    _ultimo = valor
    return valor


# ── Lo que llega ───────────────────────────────────────────────────────────

class TerminosIn(BaseModel):
    version: str
    vigente_desde: str
    texto: str

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not _VERSION.fullmatch(v):
            raise ValueError("version: hasta 20 caracteres (letras, números, punto, guion)")
        return v

    @field_validator("vigente_desde")
    @classmethod
    def _vigente_desde(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%d-%m-%Y")
        except ValueError:
            raise ValueError("vigente_desde: se espera una fecha dd-mm-aaaa") from None
        return v

    @field_validator("texto")
    @classmethod
    def _texto(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("texto: no puede estar vacío")
        if len(v) > MAX_TEXTO:
            raise ValueError(f"texto: máximo {MAX_TEXTO} caracteres")
        return v


class TerminosOut(BaseModel):
    cargados: bool
    version: str = ""
    vigente_desde: str = ""
    #: sha256 hex del texto normalizado (la huella de la cláusula 30.3 de ESTE contrato).
    hash: str = ""
    caracteres: int = 0


def _vista() -> TerminosOut:
    propios = leer_terminos_propios()
    if propios is None:
        return TerminosOut(cargados=False)
    return TerminosOut(
        cargados=True, version=propios["version"], vigente_desde=propios["vigente_desde"],
        hash=huella(propios["texto"]), caracteres=len(normalizar_texto(propios["texto"])),
    )


def quitar_terminos_propios() -> None:
    """Saca los términos de la instancia (el archivo y la clave). El historial de versiones queda."""
    cfg = config_manager.load()
    cfg.pop(CLAVE_CONFIG, None)
    config_manager.save(cfg)
    try:
        os.unlink(_ruta())
    except FileNotFoundError:
        pass


def agregar_rutas(router: APIRouter, *, prefix: str = "") -> None:
    """Registra las tres rutas de los términos propios sobre `router`, bajo `prefix`.

    🔑 **Directo sobre el router y no con `include_router`**: esta versión de FastAPI deja un router anidado como un objeto perezoso que
    no es una lista de rutas, y ni las guardas del repo ni quien recorra `router.routes` lo ven. Así quedan planas, como las demás."""

    @router.get(prefix, response_model=TerminosOut)
    def obtener_terminos() -> TerminosOut:
        return _vista()

    # `def` y no `async def`: es disco, y con un solo proceso de uvicorn frenaría el loop mientras dura.
    @router.put(prefix, response_model=TerminosOut)
    def guardar_terminos(cuerpo: TerminosIn) -> TerminosOut:
        texto = normalizar_texto(cuerpo.texto)
        h = huella(texto)
        cfg = config_manager.load()
        historial = cfg.get(CLAVE_HISTORIAL)
        historial = dict(historial) if isinstance(historial, dict) else {}
        previa = historial.get(cuerpo.version)
        if previa is not None and previa != h:
            raise HTTPException(
                409,
                f"La versión {cuerpo.version} ya se publicó con otro texto. Una versión es un texto: usá otro número de versión.",
            )
        # El archivo primero y la clave después: si algo corta entre los dos, queda un archivo sin clave (la instancia sigue sin
        # términos propios), nunca una clave que apunta a un texto que no está.
        config_manager.escribir_atomico(_ruta(), texto.encode("utf-8"))
        historial[cuerpo.version] = h
        cfg[CLAVE_HISTORIAL] = historial
        cfg[CLAVE_CONFIG] = {"version": cuerpo.version, "vigente_desde": cuerpo.vigente_desde}
        config_manager.save(cfg)
        return _vista()

    @router.delete(prefix, response_model=TerminosOut)
    def borrar_terminos() -> TerminosOut:
        quitar_terminos_propios()
        return _vista()


def build_terminos_propios_router(*, prefix: str = "/terminos") -> APIRouter:
    """Un router con sólo las rutas de los términos propios. `build_marca_admin_router` ya las trae (bajo `/api/marca/terminos`): un
    producto no las monta aparte. Van detrás del gate de admin del producto, que tiene que aceptar también el token de servicio."""
    router = APIRouter(prefix=prefix, tags=["marca"])
    agregar_rutas(router)
    return router
