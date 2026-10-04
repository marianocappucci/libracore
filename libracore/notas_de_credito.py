"""La nota de crédito de la familia: UNA implementación, normalizada para todos los productos.

Decidido por el humano el 2026-10-04: **las notas de crédito salen del motor y son iguales para todos los
productos.** No hay una nota «de LibraCargo» ni una «de Contalibra»: lo que se decide una vez vale para los siete
que emiten por ARCA (`reglas/producto.md` del wiki: el arreglo de fondo vive siempre en el motor).

Qué vive acá, porque **no depende del modelo de cada producto**:

- qué nota corresponde a qué comprobante (`TIPO_NC`: la letra y si es FCE se heredan);
- las **guardas**: la factura no se acredita dos veces, una nota que quedó sin CAE no se duplica, el receptor
  sirve para ARCA, y dos pedidos simultáneos emiten una sola nota;
- el **armado** de la nota: fecha de hoy (no se elige), el comprobante asociado (`cbte_asoc_*`) que la ata a su
  factura ante ARCA, la marca de la FCE;
- el **orden de las operaciones**: numerar → registrar → pedir el CAE.

Qué NO vive acá, porque sí depende del modelo, y cada producto lo aporta como **costura** (un callable):
cómo se carga el original, dónde se guarda la nota y qué pasa con lo suyo cuando se acredita (las órdenes de
LibraCargo, la venta de VentaLibra, el asiento de cuenta corriente). Un producto que no tiene la costura que
necesita **la agrega acá**; no escribe su propia versión de la lógica.

Fase 1 (esta): nota de crédito **total**. La nota parcial y la validación del tope acumulado vienen después y se
enchufan en `validar_nota_de_credito` sin cambiar a los productos.

Medido contra ARCA de homologación (2026-10-03 y 2026-10-04): ARCA **no** lleva el saldo de una factura común
(acepta una segunda nota total y una nota por más del importe), así que estas guardas son la única defensa; la
fecha de la nota tiene que ser la de hoy (ARCA exige fechas no decrecientes por tipo y punto de venta); y una
nota de crédito A a un CUIT inexistente, asociada a la factura que se le emitió, sí se autoriza.
"""

from __future__ import annotations

import contextlib
import datetime
import inspect
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from libracore import arca_wsfe
from libracore import tipos_comprobante as tipos

#: De qué factura sale qué nota de crédito. La letra se conserva y también si es FCE.
TIPO_NC = tipos.TIPO_NC


class NotaNoPermitida(Exception):
    """El motivo por el que la nota no se puede emitir, dicho para una persona.

    Lleva un `codigo` para que cada producto lo traduzca a su respuesta (un router lo mapea a 400, 409 o 422;
    un servicio sin HTTP lo muestra como quiera). El motor no sabe de HTTP acá a propósito.
    """

    TIPO = "tipo"                    # el comprobante no admite nota de crédito (ya es una nota, o no es de ARCA)
    YA_TIENE_NOTA = "ya_tiene_nota"  # la factura ya fue acreditada
    NOTA_SIN_CAE = "nota_sin_cae"    # ya hay una nota, pero quedó sin CAE: se autoriza o se borra
    EN_CURSO = "en_curso"            # otro pedido está emitiendo la nota de esta misma factura
    RECEPTOR = "receptor"            # el CUIT del receptor no sirve para ARCA

    def __init__(self, mensaje: str, codigo: str):
        super().__init__(mensaje)
        self.codigo = codigo


def nombre_de_tipo(tipo: int) -> str:
    """`Factura C`, `Nota de Crédito FCE A`… como se lee en un papel."""
    return tipos.NOMBRE.get(int(tipo), "comprobante")


def etiqueta(comprobante: dict) -> str:
    """`Factura C 0001-00000123`: lo que el humano busca en su listado."""
    return (f"{nombre_de_tipo(comprobante['tipo'])} "
            f"{str(comprobante['punto_venta']).zfill(4)}-{str(comprobante['numero']).zfill(8)}")


def tipo_de_nota_de_credito(tipo_original: int) -> int:
    """El tipo de la nota que acredita un comprobante de este tipo."""
    nota = TIPO_NC.get(int(tipo_original))
    if nota is None:
        raise NotaNoPermitida("Tipo de comprobante no admite nota de crédito", NotaNoPermitida.TIPO)
    return nota


# ── El candado ─────────────────────────────────────────────────────────────

#: Los comprobantes que tienen una nota **en camino** en este proceso. Ver `una_nota_a_la_vez`.
_EN_CURSO: set[Any] = set()
_EN_CURSO_LOCK = threading.Lock()


def en_curso(clave: Any) -> bool:
    """¿Hay una nota en camino para esta clave? (Lo usan los tests.)"""
    with _EN_CURSO_LOCK:
        return clave in _EN_CURSO


@contextlib.contextmanager
def una_nota_a_la_vez(clave: Any):
    """Un solo pedido de nota de crédito por comprobante a la vez.

    🔴 **Sin esto la consulta «¿ya tiene una nota?» no alcanza**: dos pedidos que llegan juntos —un doble
    clic, o dos admins— la contestan los dos antes de que ninguno haya creado la nota, y los dos emiten. La nota
    tarda: pide número a ARCA, crea la fila y pide el CAE.

    Alcanza con un candado **en memoria** porque el motor corre con un solo proceso por instancia (`uvicorn` sin
    `--workers`). Una restricción única en la base sería más fuerte, pero una base con duplicados históricos no
    podría crearla, y esta guarda no puede fallar el arranque de un producto en producción.

    `clave` es lo que identifica al comprobante **en ese producto** (su id, o una tupla si hace falta distinguir
    tablas); el motor no le da otro significado.
    """
    with _EN_CURSO_LOCK:
        if clave in _EN_CURSO:
            raise NotaNoPermitida(
                "Ya hay una nota de crédito en curso para esta factura: esperá a que termine antes de pedir otra.",
                NotaNoPermitida.EN_CURSO,
            )
        _EN_CURSO.add(clave)
    try:
        yield
    finally:
        with _EN_CURSO_LOCK:
            _EN_CURSO.discard(clave)


# ── Las guardas ────────────────────────────────────────────────────────────

def validar_nota_de_credito(original: dict, previas: Sequence[dict]) -> int:
    """Dice, antes de pedirle nada a ARCA, si esta factura se puede acreditar. Devuelve el tipo de la nota.

    `original` es el comprobante a acreditar, con las claves que usa toda la familia (`tipo`, `punto_venta`,
    `numero`, `cliente_cuit`, `cliente_razon`…). `previas` son las notas de crédito que **ya cuelgan** de él,
    cualquiera sea su estado.

    La nota de crédito de esta fase es **total**: copia el original entero, así que una segunda acredita dos veces
    lo mismo. ARCA no lo frena, y cada nota registra un abono por el importe completo: una factura a cuenta
    corriente quedaba con el saldo en −total. Cuenta **cualquier** nota previa, también la que quedó sin CAE:
    esa ya tiene número, y lo que corresponde es autorizarla o borrarla, no pedir otra encima.
    """
    tipo_nota = tipo_de_nota_de_credito(original["tipo"])
    if previas:
        nota = previas[0]
        nombre = etiqueta(nota)
        if not nota.get("cae") or nota["cae"] == "PENDIENTE":
            raise NotaNoPermitida(
                f"Esta factura ya tiene la {nombre}, que todavía no tiene CAE: autorizala o eliminala; "
                "no se pide otra nota encima.",
                NotaNoPermitida.NOTA_SIN_CAE,
            )
        raise NotaNoPermitida(
            f"Esta factura ya tiene la {nombre}: una factura se acredita una sola vez.",
            NotaNoPermitida.YA_TIENE_NOTA,
        )
    problema = arca_wsfe.problema_del_receptor({
        "tipo": tipo_nota,
        "cliente_cuit": original.get("cliente_cuit") or "",
        "cliente_razon": original.get("cliente_razon") or "",
    })
    if problema:
        raise NotaNoPermitida(problema, NotaNoPermitida.RECEPTOR)
    return tipo_nota


# ── El armado ──────────────────────────────────────────────────────────────

def armar_nota(
    original: dict, tipo_nota: int, *, hoy: datetime.date | None = None,
    prefijo: str = "Anula", motivo: str = "",
) -> dict:
    """El diccionario de la nota (sin número todavía), con las claves que usa toda la familia.

    🔑 **Copia los importes —y los ítems, si el producto los tiene— del original tal cual.** Una nota que anula
    tiene que decir exactamente lo mismo que anula: si los recalculara —con la tasa de hoy, o con un precio que
    cambió— anularía un importe distinto del que se facturó, y ante ARCA quedarían dos comprobantes que no cierran.

    🔑 **La fecha es la de hoy y no se elige.** ARCA exige fechas no decrecientes por tipo y punto de venta: una
    nota con fecha futura bloquea a todas las siguientes de ese tipo hasta esa fecha, y no hay cómo deshacerlo.

    `cbte_asoc_*` es lo que ata la nota a su factura ante ARCA; sin eso es un comprobante suelto. Una nota de FCE
    exige además la fecha del asociado (`10158`) y decir si anula la factura (opcional 22): `N` siempre, porque `S`
    sólo la acepta ARCA si el comprador rechazó la factura (`10154`), y eso no lo decide quien emite.
    """
    fecha = (hoy or datetime.date.today()).isoformat()
    nota = {
        "tipo": tipo_nota,
        "punto_venta": original["punto_venta"],
        "fecha": fecha,
        "cliente_cuit": original.get("cliente_cuit") or "",
        "cliente_razon": original.get("cliente_razon") or "",
        "cliente_iva_cond": original.get("cliente_iva_cond") or 0,
        "subtotal": original["subtotal"],
        "iva_amount": original["iva_amount"],
        "total": original["total"],
        "concepto": original.get("concepto", 1),
        "observaciones": f"{prefijo} {etiqueta(original)}" + (f" — {motivo}" if motivo else ""),
        "cliente_domicilio": original.get("cliente_domicilio", ""),
        "fch_serv_desde": original.get("fch_serv_desde", ""),
        "fch_serv_hasta": original.get("fch_serv_hasta", ""),
        "fch_vto_pago": fecha,
        "cbte_asoc_tipo": original["tipo"],
        "cbte_asoc_pv": original["punto_venta"],
        "cbte_asoc_nro": original["numero"],
        "cbte_asoc_fecha": original["fecha"],
        "fce_anulacion": "N" if tipo_nota in tipos.FCE_NOTA else "",
    }
    if "items" in original:
        nota["items"] = original["items"]
    return nota


# ── El orden de las operaciones ────────────────────────────────────────────

@dataclass(frozen=True)
class NotaEmitida:
    """Lo que devolvió cada paso, para que el producto cierre lo suyo (órdenes, venta, cuenta corriente)."""

    nota: dict          # la nota armada, con su número
    registro: Any       # lo que devolvió `registrar` (el id de la fila, el objeto del ORM…)
    resultado: Any      # lo que devolvió `pedir_cae`


async def _esperar(valor: Any) -> Any:
    """Acepta tanto una función común como una corrutina: el producto elige."""
    return await valor if inspect.isawaitable(valor) else valor


async def emitir_nota_de_credito(
    original: dict,
    *,
    clave: Any,
    cargar_previas: Callable[[], Sequence[dict]],
    numerar: Callable[[int, int], Any],
    registrar: Callable[[dict, Any], Any],
    pedir_cae: Callable[[Any, dict, Any], Any],
    hoy: datetime.date | None = None,
    prefijo: str = "Anula",
    motivo: str = "",
) -> NotaEmitida:
    """Emite la nota de crédito **total** de un comprobante. El orden y las guardas son los mismos en todos los productos.

    Las **costuras** que aporta cada producto:

    - `cargar_previas()`: las notas de crédito que ya cuelgan del original (de **su** modelo). Se llama
      **adentro del candado**: si el producto las leyera antes, dos pedidos simultáneos la contestarían vacía los dos.
    - `numerar(tipo_nota, punto_venta)` → `(numero, contexto)`: el número que sigue (de ARCA si emite por ARCA).
      El `contexto` es opaco para el motor (el ticket de WSAA, la configuración…) y se lo devuelve a los otros pasos.
    - `registrar(nota, contexto)` → registro: guarda la nota con su número, todavía sin CAE (de **su** modelo).
    - `pedir_cae(registro, nota, contexto)` → resultado: pide el CAE y completa el registro. Si ARCA rechaza, que
      levante: el motor no atrapa nada, así el producto decide qué queda (en una transacción, nada).

    Lo que pasa con lo suyo (las órdenes, la venta, la cuenta corriente) lo hace el producto **con el resultado**,
    después de que esto vuelva.

    Levanta `NotaNoPermitida` si la nota no corresponde, sin haberle pedido nada a ARCA.
    """
    with una_nota_a_la_vez(clave):
        tipo_nota = validar_nota_de_credito(original, cargar_previas())
        nota = armar_nota(original, tipo_nota, hoy=hoy, prefijo=prefijo, motivo=motivo)
        numero, contexto = await _esperar(numerar(tipo_nota, original["punto_venta"]))
        nota["numero"] = numero
        registro = await _esperar(registrar(nota, contexto))
        resultado = await _esperar(pedir_cae(registro, nota, contexto))
        return NotaEmitida(nota=nota, registro=registro, resultado=resultado)
