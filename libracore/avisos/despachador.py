"""El despachador de avisos (ADR-090): manda un mensaje por un proveedor **una sola vez** y deja el rastro.

`despachar(proveedor, mensaje, clave=...)` es toda la API de envío. Lo que garantiza:

1. **No se manda dos veces lo mismo.** `(canal, clave)` es UNIQUE y el aviso se *reserva* (`enviando`) antes de salir a la red:
   dos procesos que pasan a la vez por la misma clave, uno manda y el otro ve que ya está. La clave la pone quien avisa y dice
   *de qué hecho* es el aviso (`reserva:41:recordatorio:24`); mandar la misma clave dos veces, aunque con otro texto, no manda dos.
2. **Un fallo se reintenta, con tope.** Un aviso `fallido` vuelve a intentarse cuando quien avisa lo pide otra vez (el barrido
   siguiente) o con `reintentar_fallidos` (que espera `ESPERA_ENTRE_INTENTOS` desde el último intento), hasta `MAX_INTENTOS`.
   Un `rechazado` (el destino no sirve) no se reintenta.
3. **`registrado` sólo cierra el tema para un proveedor simulado.** Un aviso que un proveedor de prueba anotó sin mandar nada
   **se manda** cuando llega un proveedor real con esa misma clave. Es lo que permite prender WhatsApp en modo registro, mirar qué
   saldría, y recién después conectar Evolution sin perder ni duplicar nada.
4. **Un proceso que murió a mitad de un envío no deja el aviso trabado.** Pasados `VENCE_EL_ENVIO` minutos, un `enviando` se da por
   abandonado y se reintenta. Es la única ventana en que un aviso puede salir dos veces (si el proceso murió *después* de que el
   proveedor lo aceptó y *antes* de anotarlo), y es el lado correcto para equivocarse en un recordatorio: duplicado antes que
   faltante.
5. **El nodo no manda.** En un nodo de LibraEdge (`LIBRAEDGE_NODE_ID`) `despachar` no toca la red ni la tabla y contesta `en_nodo`:
   los avisos los deduce y los manda el barrido del central (ADR-090).

Cada paso se confirma solo (no hay `conn`): la reserva tiene que ser visible para los demás **antes** de mandar, que es todo el punto.

`despachar` es **síncrono y bloquea** hasta el timeout del proveedor (10 segundos Evolution, 30 el SMTP): se llama desde un cron, un
hilo o una ruta `def`, no desde un `async def` ni dentro de la transacción de quien avisa.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from libracore import fechas, nodo_offline
from libracore.avisos.mensaje import ENVIADO, FALLIDO, RECHAZADO, REGISTRADO, Mensaje, Proveedor, Resultado
from libracore.db import avisos_envios as db
from libracore.db.avisos_envios import (
    ESTADO_ENVIADO,
    ESTADO_ENVIANDO,
    ESTADO_FALLIDO,
    ESTADO_OMITIDO,
    ESTADO_RECHAZADO,
    ESTADO_REGISTRADO,
)

logger = logging.getLogger("libracore.avisos")

#: Cuántas veces se intenta mandar un aviso que falla antes de dejarlo.
MAX_INTENTOS = 3

#: Pasado este tiempo, un aviso que sigue `enviando` se da por abandonado (el proceso murió) y se puede tomar de nuevo.
#: Tiene que ser bastante más que el peor envío (`TIMEOUT_SMTP`, 30 segundos): si no, un envío lento se pisaría con su reintento.
VENCE_EL_ENVIO = timedelta(minutes=10)

#: Cuánto espera `reintentar_fallidos` desde el último intento de un `fallido` antes de volver a probarlo. Sin esto, un cron que
#: despacha y después reintenta gastaba los intentos del mismo aviso en el mismo segundo: Evolution caído una vez se llevaba dos de
#: tres. Con la espera, el orden dentro del cron no importa.
ESPERA_ENTRE_INTENTOS = timedelta(minutes=5)

#: Hasta cuándo `reintentar_fallidos` insiste con un aviso. Un recordatorio de hace tres días ya no le sirve a nadie.
REINTENTO_HASTA = timedelta(hours=24)

#: Lo que contesta `despachar` cuando no hay fila: no se mandó y tampoco se anotó.
NO_DISPONIBLE = "no_disponible"
EN_NODO = "en_nodo"

#: Los que `despachar` devuelve y no son un estado del registro.
ESTADOS_SIN_FILA = (NO_DISPONIBLE, EN_NODO)

_LARGO_MAXIMO_DE_CLAVE = 200

_RESULTADOS = (ENVIADO, REGISTRADO, FALLIDO, RECHAZADO)


@dataclass(frozen=True, slots=True)
class Envio:
    """Qué pasó con un aviso.

    - `estado`: uno de los estados del registro (`enviado`, `registrado`, `fallido`, `rechazado`, `omitido`, `enviando`) o, si no
      hubo fila, `no_disponible` / `en_nodo`.
    - `repetido`: `True` si ese aviso **ya estaba** y no se volvió a mandar (o lo tiene otro proceso en este momento).
    - `intentos`: cuántas veces se intentó (contando ésta).
    """

    estado: str
    detalle: str = ""
    intentos: int = 0
    aviso_id: int | None = None
    id_externo: str = ""
    repetido: bool = False

    @property
    def salio(self) -> bool:
        """Si el proveedor lo aceptó (o, en modo prueba, lo registró) en esta llamada o en una anterior."""
        return self.estado in (ESTADO_ENVIADO, ESTADO_REGISTRADO)


@dataclass(frozen=True, slots=True)
class Resumen:
    """Lo que hizo un barrido. Es lo que el cron imprime al log."""

    enviados: int = 0
    registrados: int = 0
    fallidos: int = 0
    rechazados: int = 0
    omitidos: int = 0
    repetidos: int = 0
    sin_proveedor: int = 0

    def sumar(self, envio: Envio) -> Resumen:
        """Un resumen nuevo con este envío contado. Es inmutable a propósito: se usa con `functools.reduce`."""
        campos = {k: getattr(self, k) for k in self.__slots__}
        if envio.repetido:
            campos["repetidos"] += 1
        elif envio.estado == ESTADO_ENVIADO:
            campos["enviados"] += 1
        elif envio.estado == ESTADO_REGISTRADO:
            campos["registrados"] += 1
        elif envio.estado == ESTADO_FALLIDO:
            campos["fallidos"] += 1
        elif envio.estado == ESTADO_RECHAZADO:
            campos["rechazados"] += 1
        elif envio.estado == ESTADO_OMITIDO:
            campos["omitidos"] += 1
        elif envio.estado in ESTADOS_SIN_FILA:
            campos["sin_proveedor"] += 1
        return Resumen(**campos)

    def __str__(self) -> str:
        partes = [f"enviados: {self.enviados}", f"fallidos: {self.fallidos}", f"omitidos: {self.omitidos}"]
        for nombre in ("registrados", "rechazados", "repetidos", "sin_proveedor"):
            if getattr(self, nombre):
                partes.append(f"{nombre}: {getattr(self, nombre)}")
        return " · ".join(partes)


def _texto(momento: datetime) -> str:
    return momento.strftime("%Y-%m-%d %H:%M:%S")


def _en_un_nodo(nodo: Callable[[], str | None] | None) -> bool:
    return (nodo or nodo_offline.nodo_desde_entorno())() is not None


def _cierra_el_tema(fila: dict, proveedor: Proveedor, max_intentos: int) -> bool:
    """Si ese aviso ya está resuelto y **no hay que volver a mandarlo** con este proveedor."""
    estado = fila["estado"]
    if estado in (ESTADO_ENVIADO, ESTADO_RECHAZADO, ESTADO_OMITIDO):
        return True
    if estado == ESTADO_REGISTRADO:
        # 🔴 La regla del ADR-029 de LibraClub: lo anotado en modo prueba no es un aviso entregado.
        return bool(proveedor.simulado)
    if estado == ESTADO_FALLIDO:
        return fila["intentos"] >= max_intentos
    return False  # `enviando`: lo decide `despachar` mirando si venció


def _validar_clave(clave: str) -> str:
    clave = (clave or "").strip()
    if not clave or len(clave) > _LARGO_MAXIMO_DE_CLAVE:
        raise ValueError(f"La clave de un aviso es un texto de 1 a {_LARGO_MAXIMO_DE_CLAVE} caracteres.")
    return clave


def _como_envio(fila: dict, *, repetido: bool) -> Envio:
    return Envio(fila["estado"], fila["detalle"], fila["intentos"], fila["id"], fila["id_externo"], repetido)


def despachar(proveedor: Proveedor, mensaje: Mensaje, *, clave: str, tipo: str = "", max_intentos: int = MAX_INTENTOS,
              nodo: Callable[[], str | None] | None = None, ahora: datetime | None = None) -> Envio:
    """Manda `mensaje` por `proveedor` si esa `clave` todavía no salió, y deja el rastro. Ver el docstring del módulo.

    `tipo` es la etiqueta de quien avisa (`recordatorio`, `confirmacion`): sólo sirve para filtrar el listado. `nodo` reemplaza la
    lectura de la identidad del nodo (default `LIBRAEDGE_NODE_ID`); `ahora`, para fijar el reloj en un test.
    """
    clave = _validar_clave(clave)
    if _en_un_nodo(nodo):
        logger.warning("Aviso %s/%s: este proceso es un nodo y no manda avisos (los manda el central).", proveedor.canal, clave)
        return Envio(EN_NODO, "Un nodo no manda avisos: los manda el central.")
    if not proveedor.disponible():
        return Envio(NO_DISPONIBLE, f"El canal {proveedor.canal} no está disponible.")

    ahora = ahora or fechas.ahora()
    destino = (mensaje.destino or "").strip()
    campos = {"tipo": tipo, "destino": destino, "asunto": mensaje.asunto or "", "cuerpo": mensaje.cuerpo or "",
              "plantilla": mensaje.plantilla or "",
              "variables": {str(k): str(v) for k, v in (mensaje.variables or {}).items()},
              "proveedor": proveedor.nombre}

    if not destino:
        db.anotar_omitido(proveedor.canal, clave, tipo=tipo, destino="", motivo="Falta el destino.")
        return _como_envio(db.obtener(proveedor.canal, clave), repetido=False)

    aviso_id, intentos = _reservar(proveedor, clave, campos, max_intentos, ahora)
    if aviso_id is None:
        fila = db.obtener(proveedor.canal, clave)
        return _como_envio(fila, repetido=True)

    resultado = _enviar(proveedor, mensaje, clave)
    db.cerrar(aviso_id, resultado.estado, detalle=resultado.detalle, id_externo=resultado.id_externo)
    logger.log(logging.INFO if resultado.estado in (ENVIADO, REGISTRADO) else logging.WARNING,
               "Aviso %s/%s por %s: %s (intento %s)", proveedor.canal, clave, proveedor.nombre, resultado.estado, intentos)
    return Envio(resultado.estado, resultado.detalle, intentos, aviso_id, resultado.id_externo)


def _reservar(proveedor: Proveedor, clave: str, campos: dict, max_intentos: int, ahora: datetime) -> tuple[int | None, int]:
    """Deja el aviso en `enviando` para este proceso. `(None, 0)` si no hay que mandarlo (ya salió, o lo tiene otro)."""
    canal = proveedor.canal
    fila = db.obtener(canal, clave)
    if fila is None:
        nuevo = db.reservar(canal, clave, **campos)
        if nuevo is not None:
            return nuevo, 1
        fila = db.obtener(canal, clave)  # lo reservó otro entre la lectura y el alta
        if fila is None:  # pragma: no cover - sólo si lo borraron en el medio
            return None, 0
    if _cierra_el_tema(fila, proveedor, max_intentos):
        return None, 0
    vencido_antes_de = None
    if fila["estado"] == ESTADO_ENVIANDO:
        vencido_antes_de = _texto(ahora - VENCE_EL_ENVIO)
        if fila["updated_at"] >= vencido_antes_de:
            return None, 0  # lo está mandando otro ahora
        if fila["intentos"] >= max_intentos:
            # Abandonado y sin intentos: queda `fallido` terminal, no `enviando` para siempre.
            db.cerrar_abandonados_sin_intentos(canal, max_intentos=max_intentos, vencido_antes_de=vencido_antes_de, clave=clave)
            return None, 0
    reiniciar = fila["estado"] == ESTADO_REGISTRADO
    if not db.tomar(fila, vencido_antes_de=vencido_antes_de, reiniciar=reiniciar, **campos):
        return None, 0
    return fila["id"], 1 if reiniciar else fila["intentos"] + 1


def _enviar(proveedor: Proveedor, mensaje: Mensaje, clave: str) -> Resultado:
    try:
        resultado = proveedor.enviar(mensaje)
        if resultado.estado not in _RESULTADOS:
            return Resultado.fallido(f"El proveedor contestó un estado desconocido ({resultado.estado!r}).")
        return resultado
    except Exception as exc:  # noqa: BLE001 - un proveedor mal escrito no puede tumbar el barrido
        # Sólo la clase, y sin traceback: el texto de una excepción puede traer una URL o una credencial.
        logger.error("Aviso %s/%s: el proveedor %s levantó %s", proveedor.canal, clave, proveedor.nombre, type(exc).__name__)
        return Resultado.fallido(f"El proveedor falló ({type(exc).__name__}).")


def omitir(canal: str, clave: str, *, motivo: str, tipo: str = "", destino: str = "",
           nodo: Callable[[], str | None] | None = None) -> Envio:
    """Anota que a ese aviso **no le corresponde salir** (el cliente no tiene teléfono, pidió no recibir avisos).

    Idempotente y sin proveedor: sirve para que el barrido no recalcule cada vez un aviso que sabe que no sale. Un aviso que ya
    existía (enviado, fallido...) no se pisa."""
    clave = _validar_clave(clave)
    if _en_un_nodo(nodo):
        return Envio(EN_NODO, "Un nodo no manda avisos: los manda el central.")
    db.anotar_omitido(canal, clave, tipo=tipo, destino=destino, motivo=motivo)
    return _como_envio(db.obtener(canal, clave), repetido=False)


def cerrado(proveedor: Proveedor, clave: str, *, max_intentos: int = MAX_INTENTOS) -> bool:
    """Si ese aviso ya no hay que despacharlo con este proveedor.

    Es lo que necesita quien **deduce** qué avisar (el barrido de LibraClub) para no armar el texto de lo que ya salió, y quien
    arma el `already_sent` de `libragenda.due_reminders`."""
    fila = db.obtener(proveedor.canal, _validar_clave(clave))
    return fila is not None and _cierra_el_tema(fila, proveedor, max_intentos)


def ya_salio(clave: str, canales, *, nodo: Callable[[], str | None] | None = None) -> Envio | None:
    """El aviso de esa `clave` en el primero de esos `canales` donde ya **salió** (`enviado`), con `repetido=True`; `None` si en ninguno.

    Es la comprobación entre canales de la política `preferir_whatsapp` (`canales.avisar`): lo que ya salió por mail no sale por
    WhatsApp porque la configuración cambió entre dos corridas, ni al revés. Sólo cuenta `enviado`: un `registrado` (modo prueba) no
    entregó nada, y un `fallido` o `rechazado` no salió. En un nodo no toca la tabla y contesta `None` (`despachar` dirá `en_nodo`)."""
    clave = _validar_clave(clave)
    if _en_un_nodo(nodo):
        return None
    for canal in canales:
        fila = db.obtener(canal, clave)
        if fila is not None and fila["estado"] == ESTADO_ENVIADO:
            return _como_envio(fila, repetido=True)
    return None


def cerradas(proveedor: Proveedor, claves, *, max_intentos: int = MAX_INTENTOS) -> set[str]:
    """Cuáles de esas claves ya no hay que despachar con este proveedor (`cerrado` para muchas, en pocas consultas).

    Es lo que usa un barrido que **deduce** candidatos de sus propias tablas para descartar los que ya salieron antes de armar el
    texto de cada uno."""
    filas = db.obtener_varios(proveedor.canal, claves)
    return {clave for clave, fila in filas.items() if _cierra_el_tema(fila, proveedor, max_intentos)}


def reintentar_fallidos(proveedor: Proveedor, *, max_intentos: int = MAX_INTENTOS, limite: int = 100,
                        antiguedad: timedelta = REINTENTO_HASTA, espera: timedelta = ESPERA_ENTRE_INTENTOS,
                        nodo: Callable[[], str | None] | None = None, ahora: datetime | None = None) -> Resumen:
    """Vuelve a mandar, con el mensaje que quedó guardado, los avisos `fallido` de ese canal que todavía tienen intentos, y los
    `enviando` que se abandonaron. Es para quien no tiene un barrido propio que repita la consulta.

    **No reenvía los `registrado`**: lo que se anotó en modo prueba pudo ser hace días y ya no tiene sentido; si importa, quien
    avisa lo deduce de nuevo y llama a `despachar`. Tampoco pasa de `antiguedad` (24 horas): un recordatorio viejo no se manda.

    **Espera entre intentos:** un `fallido` se vuelve a probar recién `espera` (5 minutos) después de su último intento. Por eso da
    igual si el cron llama a `despachar` antes o después: un fallo de Evolution no gasta dos intentos en el mismo segundo. (La
    espera es sólo de este reintento: `despachar` con la misma clave sigue probando cuando quien avisa lo pide.)

    Además cierra como `fallido` (terminal) los `enviando` abandonados a los que ya no les quedan intentos."""
    resumen = Resumen()
    if _en_un_nodo(nodo) or not proveedor.disponible():
        return resumen
    ahora = ahora or fechas.ahora()
    vencido_antes_de = _texto(ahora - VENCE_EL_ENVIO)
    cerrados = db.cerrar_abandonados_sin_intentos(proveedor.canal, max_intentos=max_intentos, vencido_antes_de=vencido_antes_de)
    if cerrados:
        logger.warning("Avisos %s: %s envío(s) abandonado(s) sin intentos quedaron fallidos.", proveedor.canal, cerrados)
    filas = db.a_reintentar(proveedor.canal, max_intentos=max_intentos, actualizado_desde=_texto(ahora - antiguedad),
                            vencido_antes_de=vencido_antes_de, fallido_antes_de=_texto(ahora - espera), limite=limite)
    for fila in filas:
        mensaje = Mensaje(fila["destino"], fila["asunto"], fila["cuerpo"], fila["plantilla"], fila["variables"])
        resumen = resumen.sumar(despachar(proveedor, mensaje, clave=fila["clave"], tipo=fila["tipo"],
                                          max_intentos=max_intentos, nodo=nodo, ahora=ahora))
    return resumen


__all__ = ["EN_NODO", "ESPERA_ENTRE_INTENTOS", "MAX_INTENTOS", "NO_DISPONIBLE", "Envio", "Resumen", "cerrado", "cerradas",
           "despachar", "omitir", "reintentar_fallidos", "ya_salio"]
