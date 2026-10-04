# La nota de crédito de la familia: una sola, del motor

**Decidido por el humano el 2026-10-04: las notas de crédito salen del motor y son iguales para todos los
productos.** No hay una nota «de LibraCargo» ni una «de Contalibra». Quien necesite emitir una nota de crédito
contra ARCA usa `libracore.notas_de_credito`; si le falta algo, **lo agrega al motor** (regla del 2026-10-03: el
arreglo de fondo vive siempre en el motor, nunca en un producto).

Cubre hoy la nota de crédito **total**. La nota parcial se enchufa después en `validar_nota_de_credito`, sin
cambiar a los productos.

## Qué pone el motor y qué pone el producto

| El motor (`libracore.notas_de_credito`) | El producto (una costura = un callable) |
|---|---|
| Qué nota corresponde a cada comprobante: `TIPO_NC` (la letra y si es FCE se heredan) | `cargar_previas()`: las notas de crédito que ya cuelgan del original, **de su modelo** |
| Las **guardas**: no se acredita dos veces, una nota sin CAE no se duplica, el receptor sirve, dos pedidos simultáneos emiten una sola | `numerar(tipo_nota, punto_venta)` → `(numero, contexto)`: el número que sigue |
| El **armado**: importes copiados del original, fecha de hoy, comprobante asociado (`cbte_asoc_*`), marca de la FCE | `registrar(nota, contexto)`: guarda la nota con su número, todavía sin CAE |
| El **orden**: previas → numerar → registrar → pedir el CAE | `pedir_cae(registro, nota, contexto)`: pide el CAE y completa el registro |
| El candado y los códigos de error (`NotaNoPermitida.codigo`) | Lo suyo después: las órdenes, la venta, el asiento de cuenta corriente, la pantalla |

## Cómo se enchufa

```python
from libracore import notas_de_credito as nc

try:
    emitida = await nc.emitir_nota_de_credito(
        original,                                   # dict con tipo, punto_venta, numero, fecha, cliente_cuit,
                                                    # cliente_razon, subtotal, iva_amount, total (y items si los hay)
        clave=("mi_producto", comprobante.id),      # identifica al comprobante en ESTE producto
        cargar_previas=lambda: mis_notas_de(comprobante),
        numerar=mi_numerar, registrar=mi_registrar, pedir_cae=mi_pedir_cae,
    )
except nc.NotaNoPermitida as e:
    ...  # e.codigo: "tipo", "ya_tiene_nota", "nota_sin_cae", "en_curso" o "receptor"
```

Las costuras pueden ser funciones comunes o corrutinas. **`cargar_previas` se llama adentro del candado**: si el
producto las leyera antes, dos pedidos simultáneos las contestarían vacías los dos. Si `pedir_cae` levanta (ARCA
rechazó), el motor no atrapa nada: el producto decide qué queda (en una transacción, nada).

`NotaNoPermitida.codigo` → respuesta sugerida: `tipo` 400, `ya_tiene_nota` / `nota_sin_cae` / `en_curso` 409,
`receptor` 422. El motor no sabe de HTTP acá a propósito.

## Lo que se midió contra ARCA de homologación

- **ARCA no lleva el saldo de una factura común** (2026-10-03): acepta una segunda nota total, y una nota por más
  del importe sólo con la observación `10237`. Por eso la guarda es nuestra.
- **La fecha tiene que ser la de hoy** (`10016`): ARCA exige fechas no decrecientes por tipo y punto de venta, y una
  nota con fecha futura bloquea a las siguientes. `armar_nota` no deja elegirla.
- **La letra se hereda** (`10040`): una nota B no se asocia a una factura A. Sin comprobante asociado, `10197`.
- **FCE**: la marca de anulación `S` sólo la acepta ARCA si el comprador rechazó la factura (`10154`); una nota total
  sin anulación se rechaza por superar el saldo (`10184`). Por eso la nota de una FCE sale siempre con `N`, y una FCE
  sólo se puede acreditar **parcialmente** mientras el comprador no la rechace.
- **Una nota a un CUIT inexistente se autoriza** (2026-10-04), igual que la factura que se emitió a ese CUIT (ARCA
  la autoriza con el aviso `10238` y pide justamente la nota). La guarda del receptor no puede impedir corregirla.

## Qué falta (fases siguientes)

La nota **parcial** y el tope acumulado; el **motivo** obligatorio y guardado; guardar y mostrar las observaciones
de ARCA; y que `anular_venta` use esto cuando la venta tiene factura con CAE.
