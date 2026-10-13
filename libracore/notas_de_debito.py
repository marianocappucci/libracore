"""La nota de débito de la familia: UNA implementación, la misma para todos los productos (ADR-049).

Hasta acá la nota de débito vivía en `facturas_router._crear_nota`, metida en un router y atada a la tabla
`facturas`: **copiaba los importes de la factura entera** (`POST /api/facturas/{id}/nota-debito`, sin cuerpo). Eso
sirve para repetir un cobro, pero no para lo que una nota de débito es en la práctica: cobrar **un monto nuevo**
sobre una factura ya emitida —intereses por mora, un gasto que se olvidó, una diferencia de precio— con su motivo.

Este módulo es a la nota de débito lo que `libracore.notas_de_credito` es a la de crédito, y **reusa lo suyo**
(`armar_nota`, `repartir_importe`, el candado «una a la vez» y `NotaEmitida`): qué nota corresponde (`TIPO_ND`: la
letra y si es FCE se heredan), las guardas, el armado con el comprobante asociado y el orden *numerar → registrar →
pedir el CAE*. Lo que depende del modelo de cada producto entra por costuras (callables), igual que allá.

## En qué se distingue de la nota de crédito

- 🔑 **Va siempre por un importe** (con IVA, hasta dos decimales). No hay «nota de débito total»: no copia la factura.
- 🔑 **No tiene tope.** Una nota de crédito nunca supera lo que queda de la factura; una de débito agrega plata y
  puede ser por más que el total original. Por eso no hay saldo, ni `SUPERA_SALDO`, ni «una factura se debita una
  sola vez»: una factura admite varias.
- **El original tiene que tener CAE** (`ORIGINAL_SIN_CAE`): ARCA exige el comprobante asociado, y asociar uno que no
  conoce es un rechazo después de haber tomado el número.
- Una nota previa (de débito) que quedó **sin CAE** frena a la siguiente, como en la de crédito: ya tiene número, y lo
  que corresponde es autorizarla o borrarla.
- El receptor se valida como en la de crédito: una nota hereda el receptor de una factura que ARCA ya autorizó, así
  que no se bloquea por el dígito verificador (ver `arca_wsfe.problema_del_receptor`); una clase A o una FCE exigen
  CUIT de 11 dígitos.

## Cómo se enchufa

```python
from libracore import notas_de_debito as nd

emitida = await nd.emitir_nota_de_debito(
    original,                                   # dict con tipo, punto_venta, numero, fecha, cae, cliente_cuit, ...
    clave=("mi_producto", comprobante.id),
    importe=Decimal("1500.00"), motivo="Intereses por mora",
    numerar=mi_numerar, registrar=mi_registrar, pedir_cae=mi_pedir_cae,
    cargar_previas=lambda: mis_notas_de_debito_de(comprobante),   # opcional
    conn=conexion_de_la_transaccion,
)
```

`NotaNoPermitida.codigo` → respuesta sugerida: `tipo` 400; `original_sin_cae`, `nota_sin_cae` y `en_curso` 409;
`receptor` e `importe` 422 (los mismos códigos que la nota de crédito).

> ⚠️ **Sin medir contra ARCA de homologación** al escribir esto (el motor no tiene certificado en la máquina de
> desarrollo): el armado es el de la nota de crédito parcial, que sí está medido, con otro tipo (`2/7/12`,
> `202/207/212`). Lo que falta confirmar con un ensayo es que ARCA acepte la marca de anulación `N` en la nota de
> débito de una FCE (`_opcionales_fce` la manda para todas las notas de FCE).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from libracore import arca_wsfe
from libracore import tipos_comprobante as tipos
from libracore.notas_de_credito import (
    NotaEmitida,
    NotaNoPermitida,
    _es_cae,
    _esperar,
    armar_nota,
    etiqueta,
    importe_valido,
    una_nota_a_la_vez,
)

#: De qué factura sale qué nota de débito. La letra se conserva y también si es FCE.
TIPO_ND = tipos.TIPO_ND


def tipo_de_nota_de_debito(tipo_original: int) -> int:
    """El tipo de la nota de débito que se asocia a un comprobante de este tipo."""
    nota = TIPO_ND.get(int(tipo_original))
    if nota is None:
        raise NotaNoPermitida("Tipo de comprobante no admite nota de débito", NotaNoPermitida.TIPO)
    return nota


def validar_nota_de_debito(original: dict, importe: Any, previas: Sequence[dict] = ()) -> int:
    """Dice, antes de pedirle nada a ARCA, si a esta factura se le puede emitir esta nota. Devuelve el tipo de la nota.

    `original` es la factura, con las claves de toda la familia (`tipo`, `punto_venta`, `numero`, `cae`,
    `cliente_cuit`, `cliente_razon`…). `previas` son las notas de débito que **ya cuelgan** de ella (cada una con
    `cae`): una sin CAE frena. No hay tope de importe: ver el docstring del módulo.
    """
    tipo_nota = tipo_de_nota_de_debito(original["tipo"])
    if not _es_cae(original.get("cae")):
        raise NotaNoPermitida(
            f"El comprobante {etiqueta(original)} no tiene CAE: ARCA no lo conoce, así que no hay nada a lo que "
            "asociar una nota de débito.",
            NotaNoPermitida.ORIGINAL_SIN_CAE,
        )
    if importe is None:
        raise NotaNoPermitida(
            "La nota de débito va por un importe: indicá cuánto se debita (con IVA).", NotaNoPermitida.IMPORTE)
    importe_valido(importe)
    for nota in previas:
        if not _es_cae(nota.get("cae")):
            raise NotaNoPermitida(
                f"Esta factura ya tiene la {etiqueta(nota)}, que todavía no tiene CAE: autorizala o eliminala; "
                "no se pide otra nota encima.",
                NotaNoPermitida.NOTA_SIN_CAE,
            )
    problema = arca_wsfe.problema_del_receptor({
        "tipo": tipo_nota,
        "cliente_cuit": original.get("cliente_cuit") or "",
        "cliente_razon": original.get("cliente_razon") or "",
    })
    if problema:
        raise NotaNoPermitida(problema, NotaNoPermitida.RECEPTOR)
    return tipo_nota


def armar_nota_de_debito(
    original: dict, tipo_nota: int, *, importe: Any, motivo: str = "", hoy: datetime.date | None = None,
) -> dict:
    """El diccionario de la nota de débito (sin número todavía), con las claves que usa toda la familia.

    Es `armar_nota` por `importe`: el neto y el IVA salen de `repartir_importe` con la alícuota de la factura (el IVA
    es la resta, así `neto + iva == importe`; una clase C no discrimina), lleva **un solo ítem** con la leyenda y el
    comprobante asociado que la ata a su factura ante ARCA. **La fecha es la de hoy y no se elige** (ARCA exige
    fechas no decrecientes por tipo y punto de venta).
    """
    return armar_nota(
        original, tipo_nota, hoy=hoy, motivo=motivo, importe=importe, por_importe=True,
        leyenda=f"Débito s/ {etiqueta(original)}",
    )


async def emitir_nota_de_debito(
    original: dict,
    *,
    clave: Any,
    importe: Any,
    numerar: Callable[[int, int], Any],
    registrar: Callable[[dict, Any], Any],
    pedir_cae: Callable[[Any, dict, Any], Any],
    cargar_previas: Callable[[], Sequence[dict]] | None = None,
    hoy: datetime.date | None = None,
    motivo: str = "",
    conn: Any = None,
) -> NotaEmitida:
    """Emite una nota de débito por `importe` (con IVA) sobre un comprobante con CAE. El orden y las guardas son los
    mismos en todos los productos.

    Las costuras son las de `emitir_nota_de_credito` (`numerar`, `registrar`, `pedir_cae`, y `conn` para el candado de la
    base), más `cargar_previas()` —opcional— con las notas de débito que ya cuelgan del original, que se llama
    **adentro del candado**. Si ARCA rechaza, que `pedir_cae` levante: el motor no atrapa nada.

    El candado es por `clave`, que el producto elige distinta de la de la nota de crédito del mismo comprobante: un doble
    clic emite una sola nota, pero una de crédito en camino no frena a una de débito.
    """
    with una_nota_a_la_vez(clave, conn, clase="débito"):
        previas = cargar_previas() if cargar_previas is not None else ()
        tipo_nota = validar_nota_de_debito(original, importe, previas)
        nota = armar_nota_de_debito(
            original, tipo_nota, importe=Decimal(str(importe)), motivo=motivo, hoy=hoy)
        numero, contexto = await _esperar(numerar(tipo_nota, original["punto_venta"]))
        nota["numero"] = numero
        registro = await _esperar(registrar(nota, contexto))
        resultado = await _esperar(pedir_cae(registro, nota, contexto))
        return NotaEmitida(nota=nota, registro=registro, resultado=resultado)
