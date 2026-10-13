"""Registro de errores propio de la familia (ADR-045).

Hasta este módulo un 500 iba al stdout de uvicorn y se perdía en cada recreate del
contenedor. Acá queda **una línea JSON por error** en `DATA_DIR/errores/errores.jsonl`
(rotación 5 MB × 3), con lo mínimo para agruparlo, ubicarlo y avisar: sin body, sin
cookies, sin cabeceras, sin variables locales, con los datos personales enmascarados.

Una sola línea por producto instala todo (la costura análoga es `SecurityHeadersMiddleware`)::

    instalar_registro_de_errores(app, "contalibra")

- Un `Exception` sin atrapar responde `500 {"detail": "Error interno", "ref": "<8 hex>"}`
  y se registra. **No toca** `HTTPException`, `RequestValidationError` ni `ClientDisconnect`:
  los 4xx son del cliente y no son errores del sistema.
- `capturar(exc, **contexto)` registra una falla **manejada** que igual importa (un ARCA que
  no contesta, un mail que no sale) y devuelve el `ref`.
- `POST /api/errores-cliente` recibe los errores del navegador (`libra-ui`:
  `instalarReporteDeErrores`), con la misma limpieza.

🔴 **Registrar un error nunca puede ser un error.** Todo acá adentro está envuelto: si el
disco no deja escribir, la línea va al stdout; si la limpieza falla, se registra un evento
mínimo; y en ningún caso se cambia la respuesta que ya iba a salir.

Cambiar a GlitchTip/Sentry más adelante es tocar **sólo este módulo**: la forma del evento
sigue los nombres de Sentry (`release`, `environment` ≡ `entorno`, `tags`, `fingerprint` ≡
`firma`, `user.id` ≡ `usuario_id`) y `capturar` tiene la firma de `capture_exception` más
contexto. Ver `docs/errores.md`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import sysconfig
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

# ── Configuración ─────────────────────────────────────────────────────────────

#: Los entornos que entiende el vigía. Lo que no sea uno de estos es `desconocido`.
ENTORNOS = ("cliente", "demo", "dev")

NOMBRE_ARCHIVO = "errores.jsonl"
MAX_BYTES = 5 * 1024 * 1024
COPIAS = 3

#: Topes de lo que entra a una línea. El traceback es lo único grande.
MAX_TRACEBACK = 8 * 1024
MAX_MENSAJE = 2 * 1024
MAX_VALOR = 200

#: Lo que acepta `POST /api/errores-cliente`.
MAX_PAYLOAD_CLIENTE = 4 * 1024
#: Por IP: `TOPE_POR_IP` pedidos cada `VENTANA_IP` segundos. Global: `TOPE_GLOBAL` por hora.
TOPE_POR_IP = 20
VENTANA_IP = 600
TOPE_GLOBAL = 300
VENTANA_GLOBAL = 3600

#: Las claves cuyo valor no se registra jamás (se busca como subcadena, sin mayúsculas).
CLAVES_SENSIBLES = ("token", "password", "passwd", "clave", "secret", "cuit", "cert", "key", "dni", "cbu",
                    "authorization", "cookie")

_AR = timezone(timedelta(hours=-3))  # UTC-3 fijo (reglas/producto.md): Argentina no tiene horario de verano

_SIN_RUTA = "(sin ruta)"
_MASCARA = "***"

# Paquetes propios que viven en site-packages pero son «de la app» para ubicar el frame.
_FAMILIA = ("libracore/", "libraauth/", "libracommerce/", "libragenda/", "libraedge/", "libra_ui/")

_producto_global: str | None = None

# ── Limpieza ──────────────────────────────────────────────────────────────────
#
# Se aplica a TODO texto libre que entra a una línea: mensaje, traceback, query, valores de
# contexto y lo que manda el navegador. Es una lista negra y tiene que ser conservadora: se
# prefiere perder un dato de diagnóstico antes que dejar pasar uno personal.

_RX_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|\Z)", re.DOTALL)
# usuario:clave@ en cualquier URL (postgresql://, redis://, https://...).
_RX_URL_CREDENCIALES = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/\s@]*@")
# `DETAIL:  Key (cuit)=(20123456789) already exists.` — se conserva la columna, se saca el valor.
_RX_DETAIL_KEY = re.compile(r"(Key \([^)]*\)=)\(.*?\)(?=\s|$|\.|,)")
_RX_FAILING_ROW = re.compile(r"(Failing row contains )\(.*\)")
_RX_INPUT_SYNTAX = re.compile(r'(invalid input (?:syntax|value) for [^:\n]+: )"[^"\n]*"')
# SQLAlchemy: `[SQL: ...] [parameters: (...)]` — los parámetros son datos.
_RX_PARAMETROS = re.compile(r"(\[parameters:)[^\n]*")
_RX_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_RX_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_RX_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# clave=valor / clave: valor con una clave sensible. El valor llega hasta el espacio, la coma o el `&`.
_RX_CLAVE_VALOR = re.compile(
    r"(?i)(\b[\w.-]*(?:" + "|".join(CLAVES_SENSIBLES) + r")(?!Error)[\w.-]*['\"]?\s*[=:]\s*)(?:'[^']*'|\"[^\"]*\"|[^\s,;&)}\]]+)"
)
# CUIT con guiones, DNI con puntos, y cualquier corrida de 7 a 11 dígitos (CUIT/CUIL/DNI sin separadores).
_RX_CUIT_GUIONES = re.compile(r"(?<!\d)\d{2}-\d{7,8}-\d(?!\d)")
_RX_DNI_PUNTOS = re.compile(r"(?<!\d)\d{1,2}\.\d{3}\.\d{3}(?!\d)")
_RX_DIGITOS = re.compile(r"(?<!\d)\d{7,11}(?!\d)")


def limpiar_texto(texto: Any, limite: int = MAX_MENSAJE) -> str:
    """El texto sin datos personales ni secretos, truncado a `limite` caracteres.

    El orden importa: lo más grande y estructurado primero (PEM, URL con credenciales), lo
    genérico (corridas de dígitos) al final, cuando ya no puede comerse parte de otra cosa.
    """
    if texto is None:
        return ""
    s = texto if isinstance(texto, str) else str(texto)
    s = _RX_PEM.sub("[PEM]", s)
    s = _RX_URL_CREDENCIALES.sub(r"\1[credenciales]@", s)
    s = _RX_DETAIL_KEY.sub(r"\1([valor])", s)
    s = _RX_FAILING_ROW.sub(r"\1([fila])", s)
    s = _RX_INPUT_SYNTAX.sub(r'\1"[valor]"', s)
    s = _RX_PARAMETROS.sub(r"\1 [datos]", s)
    s = _RX_JWT.sub("[jwt]", s)
    s = _RX_BEARER.sub(r"\1 [token]", s)
    s = _RX_CLAVE_VALOR.sub(r"\1" + _MASCARA, s)
    s = _RX_EMAIL.sub("[email]", s)
    s = _RX_CUIT_GUIONES.sub("[CUIT]", s)
    s = _RX_DNI_PUNTOS.sub("[DNI]", s)
    s = _RX_DIGITOS.sub("[CUIT/DNI]", s)
    return _truncar(s, limite)


def _truncar(s: str, limite: int) -> str:
    if len(s) <= limite:
        return s
    if "\n" not in s:
        return s[:limite - 3] + "..."
    # En un traceback lo que importa está al principio (de dónde viene) y al final (qué falló).
    mitad = max(limite // 2 - 10, 1)
    return s[:mitad] + "\n[...]\n" + s[-mitad:]


def es_clave_sensible(clave: str) -> bool:
    c = (clave or "").lower()
    return any(p in c for p in CLAVES_SENSIBLES)


def limpiar_query(params) -> dict[str, str]:
    """Los parámetros de la query como `{clave: valor}`, las claves sensibles enmascaradas.

    `params` es un `QueryParams`/mapping. Máximo 20 claves, cada valor limpio y de ≤ 200
    caracteres: la query de una pantalla de reportes puede traer una lista entera de filtros.
    """
    salida: dict[str, str] = {}
    try:
        items = list(params.items())
    except Exception:
        return salida
    for clave, valor in items[:20]:
        k = limpiar_texto(clave, 60)
        salida[k] = _MASCARA if es_clave_sensible(clave) else limpiar_texto(valor, MAX_VALOR)
    return salida


# ── El evento ─────────────────────────────────────────────────────────────────

def _entorno() -> str:
    valor = (os.environ.get("LIBRA_ENTORNO") or "").strip().lower()
    return valor if valor in ENTORNOS else "desconocido"


def _env_o_none(nombre: str) -> str | None:
    return (os.environ.get(nombre) or "").strip() or None


def nuevo_ref() -> str:
    """Un id corto para que quien ve la pantalla de error pueda citarlo (8 hex)."""
    return secrets.token_hex(4)


def _ts() -> str:
    return datetime.now(_AR).isoformat(timespec="seconds")


def _tipo(exc: BaseException) -> str:
    t = type(exc)
    return t.__qualname__ if t.__module__ in ("builtins", "__main__") else f"{t.__module__}.{t.__qualname__}"


_STDLIB = os.path.normpath(sysconfig.get_path("stdlib") or "/usr/lib/python3")


def _es_ajeno(ruta: str) -> bool:
    """Si un archivo de un frame es código de terceros o de la librería estándar."""
    p = ruta.replace("\\", "/")
    if p.startswith("<"):  # <frozen importlib...>, <string>
        return True
    for marca in ("/site-packages/", "/dist-packages/"):
        if marca in p:
            return not p.split(marca, 1)[1].startswith(_FAMILIA)
    return os.path.normpath(p).startswith(_STDLIB + os.sep)


def _archivo_relativo(ruta: str) -> str:
    """El archivo sin lo que cambia entre máquinas: relativo al paquete o al directorio de la app."""
    p = ruta.replace("\\", "/")
    for marca in ("/site-packages/", "/dist-packages/"):
        if marca in p:
            return p.split(marca, 1)[1]
    cwd = os.getcwd().replace("\\", "/").rstrip("/") + "/"
    if p.startswith(cwd):
        return p[len(cwd):]
    return "/".join(p.split("/")[-2:])


def _frame_de_la_app(exc: BaseException) -> str:
    """`archivo:función` del frame más profundo que no es de terceros, sin número de línea.

    Cae al último frame si todo el traceback es ajeno, y a `?` si no hay traceback. Se mira el
    traceback de la excepción de afuera (la que llegó al handler), no el de su `__cause__`.
    """
    try:
        frames = traceback.extract_tb(exc.__traceback__)
    except Exception:
        return "?"
    if not frames:
        return "?"
    propios = [f for f in frames if not _es_ajeno(f.filename)]
    f = (propios or frames)[-1]
    return f"{_archivo_relativo(f.filename)}:{f.name}"


def calcular_firma(tipo: str, ruta: str, frame: str) -> str:
    """El agrupador: sha1 de tipo + ruta + `archivo:función`. Sin números de línea: un
    deploy que mueve el código unas líneas no puede partir un error en dos."""
    return hashlib.sha1(f"{tipo}|{ruta}|{frame}".encode()).hexdigest()


def _sin_ruido_del_framework(te: traceback.TracebackException, visto: set | None = None) -> None:
    """Saca del traceback los frames de terceros que van ANTES del primero propio.

    Un 500 de FastAPI arrastra ~20 frames de Starlette/anyio antes de llegar al código de la app,
    y comen el tope de 8 KB que tiene que alcanzar para ver qué falló. Los frames ajenos que
    vienen después del primero propio (un psycopg adentro de una consulta) se conservan: dicen
    por qué falló. Si no hay ningún frame propio no se toca nada.
    """
    visto = visto if visto is not None else set()
    if id(te) in visto:
        return
    visto.add(id(te))
    frames = list(te.stack)
    primero = next((i for i, f in enumerate(frames) if not _es_ajeno(f.filename)), None)
    if primero:
        te.stack = traceback.StackSummary.from_list(frames[primero:])
    for hijo in (te.__cause__, te.__context__):
        if hijo is not None:
            _sin_ruido_del_framework(hijo, visto)


def _traceback_limpio(exc: BaseException) -> str:
    try:
        te = traceback.TracebackException.from_exception(exc)
        _sin_ruido_del_framework(te)
        texto = "".join(te.format())
    except Exception:
        texto = f"{_tipo(exc)}: (traceback no disponible)"
    return limpiar_texto(texto, MAX_TRACEBACK)


def ruta_plantilla(request: Request | None) -> str:
    """La ruta como plantilla (`/api/clientes/{cid}`), nunca la URL real con sus ids.

    `scope["route"]` lo deja el router al resolver: llega al handler porque el `scope` es el
    mismo dict en toda la pila. Si el error ocurrió antes de resolver una ruta, `(sin ruta)`.
    """
    if request is None:
        return _SIN_RUTA
    try:
        ruta = getattr(request.scope.get("route"), "path", None)
        if not ruta:
            return _SIN_RUTA
        return (request.scope.get("root_path") or "").rstrip("/") + ruta
    except Exception:
        return _SIN_RUTA


def _usuario_id(request: Request | None, fuente) -> str | None:
    """El id del usuario: un valor dado, un callable `Request -> id`, o `request.state.usuario_id`."""
    try:
        if callable(fuente):
            valor = fuente(request) if request is not None else None
        elif fuente is not None:
            valor = fuente
        else:
            valor = getattr(request.state, "usuario_id", None) if request is not None else None
    except Exception:
        return None
    if valor is None or isinstance(valor, bool):
        return None
    return str(valor)[:40]


def _base(producto: str | None = None) -> dict[str, Any]:
    return {
        "ts": _ts(),
        "producto": producto or _producto_global or _env_o_none("LIBRA_PRODUCTO") or "desconocido",
        "instancia": _env_o_none("LIBRA_INSTANCIA"),
        "entorno": _entorno(),
        "release": _env_o_none("LIBRA_VERSION"),
    }


def construir_evento(exc: BaseException, request: Request | None = None, *, status: int | None = 500,
                     manejado: bool = False, ref: str | None = None, usuario_id=None,
                     tags: dict | None = None, contexto: dict | None = None,
                     producto: str | None = None) -> dict[str, Any]:
    """El evento de una excepción. No registra nada: sólo arma el dict (ya limpio)."""
    ruta = ruta_plantilla(request)
    tipo = _tipo(exc)
    frame = _frame_de_la_app(exc)
    ev = _base(producto)
    ev.update({
        "origen": "servidor",
        "manejado": manejado,
        "metodo": request.method if request is not None else None,
        "ruta": ruta,
        "status": status,
        "tipo": tipo,
        "mensaje": limpiar_texto(str(exc), MAX_MENSAJE),
        "traceback": _traceback_limpio(exc),
        "donde": frame,
        "firma": calcular_firma(tipo, ruta, frame),
        "ref": ref or nuevo_ref(),
        "usuario_id": _usuario_id(request, usuario_id),
    })
    if request is not None:
        ev["query"] = limpiar_query(request.query_params)
        ev["user_agent"] = limpiar_texto(request.headers.get("user-agent", ""), MAX_VALOR) or None
    if tags:
        ev["tags"] = {limpiar_texto(k, 40): _MASCARA if es_clave_sensible(str(k)) else limpiar_texto(v, MAX_VALOR)
                      for k, v in list(tags.items())[:20]}
    if contexto:
        ev["contexto"] = {limpiar_texto(k, 40): _MASCARA if es_clave_sensible(str(k)) else limpiar_texto(v, MAX_VALOR)
                          for k, v in list(contexto.items())[:20]}
    return ev


# ── Escritura ─────────────────────────────────────────────────────────────────

class _ManejadorRotativo(logging.handlers.RotatingFileHandler):
    """`RotatingFileHandler` que NO se come los errores de escritura (los relanza para que `registrar`
    sepa que no pudo y mande la línea al stdout) y que reabre el archivo si otro proceso lo rotó.

    Esto último es lo de `WatchedFileHandler`: con más de un worker de uvicorn, el que rota renombra el
    archivo y los demás seguirían escribiendo en el `.1`. Si dos rotan a la vez puede perderse una línea
    al renombrar; un registro de diagnóstico lo tolera."""

    def emit(self, record):
        try:
            if self.stream is not None and (os.fstat(self.stream.fileno()).st_ino != os.stat(self.baseFilename).st_ino):
                self.stream.close()
                self.stream = None
        except OSError:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
        super().emit(record)

    def handleError(self, record):  # noqa: N802 (nombre de logging)
        raise  # relanza la excepción que `emit` está atendiendo


_lock = threading.Lock()
_manejadores: dict[str, _ManejadorRotativo] = {}


def directorio_de_errores() -> str:
    """`$LIBRA_ERRORES_DIR`, o `$DATA_DIR/errores` (como `arca_ta`: mismo `DATA_DIR` que el resto)."""
    return os.environ.get("LIBRA_ERRORES_DIR") or os.path.join(os.environ.get("DATA_DIR", os.getcwd()), "errores")


def _manejador(archivo: str) -> _ManejadorRotativo:
    h = _manejadores.get(archivo)
    if h is None:
        os.makedirs(os.path.dirname(archivo), exist_ok=True)
        h = _ManejadorRotativo(archivo, maxBytes=MAX_BYTES, backupCount=COPIAS, encoding="utf-8", delay=True)
        _manejadores[archivo] = h
    return h


def registrar(evento: dict) -> bool:
    """Escribe el evento como una línea JSON. Devuelve si quedó en el archivo.

    Si no se puede escribir (disco lleno, sin permisos, `DATA_DIR` de sólo lectura) la línea
    va al stdout —que es donde iba el 100 % de los errores antes de este módulo— y nunca se
    propaga nada: el que llama ya tiene una respuesta para dar.
    """
    try:
        linea = json.dumps(evento, ensure_ascii=False, default=str)
    except Exception:
        linea = json.dumps({"ts": _ts(), "tipo": "errores.NoSerializable", "ref": evento.get("ref")})
    try:
        archivo = os.path.join(directorio_de_errores(), NOMBRE_ARCHIVO)
        with _lock:
            h = _manejador(archivo)
            h.emit(logging.makeLogRecord({"msg": linea, "levelno": logging.ERROR}))
        return True
    except Exception:
        try:
            print(linea, file=sys.stdout, flush=True)
        except Exception:
            pass
        return False


def capturar(exc: BaseException, *, request: Request | None = None, status: int | None = None,
             usuario_id=None, tags: dict | None = None, **contexto) -> str:
    """Registra una falla **manejada** que importa y devuelve su `ref`. Nunca lanza.

    Es el `sentry_sdk.capture_exception` de la familia: se llama desde un `except` que se
    traga el error pero que un humano querría saber (ARCA que no contesta, un mail que no
    sale, una tarea de fondo que falló)::

        try:
            emitir_en_arca(...)
        except Exception as e:
            ref = capturar(e, tags={"servicio": "wsfe"}, factura_id=fid)

    `tags` son etiquetas de agrupación (como las de Sentry); el resto de los argumentos con
    nombre es `contexto`. Los valores pasan por la misma limpieza que todo lo demás, y las
    claves sensibles (`cuit`, `token`...) quedan enmascaradas.
    """
    ref = nuevo_ref()
    try:
        registrar(construir_evento(exc, request, status=status, manejado=True, ref=ref, usuario_id=usuario_id,
                                   tags=tags, contexto=contexto))
    except Exception:
        try:
            registrar({**_base(), "origen": "servidor", "manejado": True, "tipo": _tipo(exc), "ref": ref})
        except Exception:
            pass
    return ref


# ── Instalación ───────────────────────────────────────────────────────────────

def instalar_registro_de_errores(app: FastAPI, producto: str, *, usuario_id: Callable | None = None,
                                 endpoint_cliente: bool = True) -> None:
    """Instala el registro en `app`. **Una línea por producto**, al construir la app.

    - Un handler de `Exception`: responde `500 {"detail": "Error interno", "ref": "..."}` y
      registra el evento. Los `HTTPException` y `RequestValidationError` que lleguen hasta acá
      (los de una ruta los resuelve FastAPI antes) se responden como siempre y **no** se
      registran; un `ClientDisconnect` tampoco.
    - `POST /api/errores-cliente` (`build_errores_cliente_router`) salvo `endpoint_cliente=False`.

    `producto` es el nombre con el que se agrupa (`contalibra`, `libracargo`...).
    `usuario_id` es un callable `Request -> id | None` para poner el id del usuario en el evento;
    sin él se mira `request.state.usuario_id`. Tiene que ser barato: no vale ir a la base.

    Idempotente. Va antes de servir el primer pedido (no se pueden sumar handlers después).

    Como todo handler de `Exception` de Starlette, deja que el servidor ASGI vuelva a ver la
    excepción después de contestar: uvicorn sigue escribiendo el traceback en su stdout.
    """
    global _producto_global
    _producto_global = producto

    async def _manejar(request: Request, exc: Exception) -> Response:
        if isinstance(exc, StarletteHTTPException):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))
        if isinstance(exc, RequestValidationError):
            return JSONResponse({"detail": jsonable_encoder(exc.errors())}, status_code=422)
        ref = nuevo_ref()
        if not isinstance(exc, ClientDisconnect):
            try:
                registrar(construir_evento(exc, request, ref=ref, usuario_id=usuario_id, producto=producto))
            except Exception:
                try:
                    registrar({**_base(producto), "origen": "servidor", "tipo": _tipo(exc), "ref": ref})
                except Exception:
                    pass
        return JSONResponse({"detail": "Error interno", "ref": ref}, status_code=500,
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    app.add_exception_handler(Exception, _manejar)
    if endpoint_cliente and not getattr(app.state, "libra_errores_cliente", False):
        app.include_router(build_errores_cliente_router(producto))
        app.state.libra_errores_cliente = True


# ── Errores del navegador ─────────────────────────────────────────────────────

class _Topes:
    """Los topes en memoria de `POST /api/errores-cliente`: por IP y global."""

    def __init__(self):
        self._lock = threading.Lock()
        self._por_ip: dict[str, deque] = {}
        self._global: deque = deque()

    def permitir(self, ip: str, ahora: float | None = None) -> bool:
        t = time.monotonic() if ahora is None else ahora
        with self._lock:
            while self._global and t - self._global[0] > VENTANA_GLOBAL:
                self._global.popleft()
            if len(self._global) >= TOPE_GLOBAL:
                return False
            cola = self._por_ip.setdefault(ip, deque())
            while cola and t - cola[0] > VENTANA_IP:
                cola.popleft()
            if len(cola) >= TOPE_POR_IP:
                return False
            cola.append(t)
            self._global.append(t)
            if len(self._por_ip) > 2000:  # una ráfaga de IPs distintas no puede agrandar el dict sin límite
                for k in [k for k, v in self._por_ip.items() if not v or t - v[-1] > VENTANA_IP]:
                    del self._por_ip[k]
            return True


_RX_SEGMENTO_ID = re.compile(r"^(?:\d+|[0-9a-fA-F]{8,}|[0-9a-fA-F-]{32,36})$")


def _pagina_plantilla(url: Any) -> str:
    """La página del navegador sin query ni fragmento y con los segmentos que parecen ids
    como `:id` (`/clientes/123/editar` → `/clientes/:id/editar`)."""
    try:
        ruta = urlsplit(str(url)).path or "/"
    except Exception:
        return _SIN_RUTA
    partes = [":id" if _RX_SEGMENTO_ID.match(p) else p for p in ruta.split("/")]
    return limpiar_texto("/".join(partes), 120)


def _ip_de(request: Request) -> str:
    # El proxy (NPM) AGREGA la IP real al final de X-Forwarded-For; lo anterior lo puede mandar cualquiera.
    xff = request.headers.get("x-forwarded-for", "")
    ultimo = xff.split(",")[-1].strip() if xff else ""
    return ultimo or (request.client.host if request.client else "?")


def _mismo_origen(request: Request) -> bool:
    origen = request.headers.get("origin")
    if not origen:
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    try:
        return urlsplit(origen).netloc.lower() == host.split(",")[0].strip().lower()
    except Exception:
        return False


def evento_del_cliente(datos: dict, producto: str | None = None) -> dict[str, Any]:
    """El evento que corresponde a lo que mandó el navegador (ya limpio)."""
    tipo = limpiar_texto(datos.get("tipo") or "error", 40)
    mensaje = limpiar_texto(datos.get("mensaje"), 500)
    ruta = _pagina_plantilla(datos.get("pagina"))
    normalizado = re.sub(r"\d+", "N", mensaje)[:120]
    ev = _base(producto)
    ev.update({
        "origen": "cliente",
        "manejado": False,
        "metodo": None,
        "ruta": ruta,
        "status": None,
        "tipo": tipo,
        "mensaje": mensaje,
        "traceback": limpiar_texto(datos.get("stack"), MAX_TRACEBACK),
        "donde": None,
        "firma": hashlib.sha1(f"cliente|{tipo}|{ruta}|{normalizado}".encode()).hexdigest(),
        "ref": nuevo_ref(),
        "usuario_id": None,
        "release_cliente": limpiar_texto(datos.get("release"), 40) or None,
    })
    return ev


def build_errores_cliente_router(producto: str | None = None) -> APIRouter:
    """`POST /api/errores-cliente`: el destino de `instalarReporteDeErrores` de `libra-ui`.

    Es **público a propósito** (un error de la pantalla de login también importa), así que lo
    único que lo defiende es lo que acá se exige: mismo origen, ≤ 4 KB, topes por IP y global
    en memoria, y que lo recibido pase por la misma limpieza que los errores del servidor. Lo
    que se guarda es lo que el servidor *decide* guardar: campos fijos, no el JSON recibido.
    """
    router = APIRouter()
    topes = _Topes()

    @router.post("/api/errores-cliente", include_in_schema=False)
    async def recibir(request: Request):
        if not _mismo_origen(request):
            return JSONResponse({"detail": "Origen no permitido"}, status_code=403)
        largo = request.headers.get("content-length")
        if largo and largo.isdigit() and int(largo) > MAX_PAYLOAD_CLIENTE:
            return JSONResponse({"detail": "Payload demasiado grande"}, status_code=413)
        cuerpo = await request.body()
        if len(cuerpo) > MAX_PAYLOAD_CLIENTE:
            return JSONResponse({"detail": "Payload demasiado grande"}, status_code=413)
        if not topes.permitir(_ip_de(request)):
            return JSONResponse({"detail": "Demasiados reportes"}, status_code=429)
        try:
            datos = json.loads(cuerpo)
        except ValueError:
            return JSONResponse({"detail": "JSON inválido"}, status_code=400)
        if not isinstance(datos, dict):
            return JSONResponse({"detail": "JSON inválido"}, status_code=400)
        try:
            registrar(evento_del_cliente(datos, producto))
        except Exception:
            pass
        return Response(status_code=204)

    return router
