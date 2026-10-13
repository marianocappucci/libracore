# La nota de débito de la familia: por importe, del motor

Decidido el 2026-10-13 (ADR-049). Es a la nota de débito lo que [`notas-de-credito.md`](notas-de-credito.md) es a la de
crédito: **UNA** implementación, `libracore.notas_de_debito`, que reutiliza el núcleo de aquella (`armar_nota`,
`repartir_importe`, el candado «una a la vez», `NotaEmitida`) y deja lo que depende del modelo de cada producto en
costuras. Quien necesite emitir una nota de débito contra ARCA la usa; si le falta algo, lo agrega al motor.

> `POST /api/facturas/{id}/nota-debito` del router de facturas (Contalibra, Restolibra, LibraClub) **no pasa por acá**:
> copia la factura entera, sin importe ni motivo. Siguen siendo dos caminos hasta que se decida unirlos.

## En qué se distingue de la nota de crédito

| | Nota de crédito | Nota de débito |
|---|---|---|
| Importe | total (sin `importe`) o parcial | **siempre** por un importe, con IVA, hasta dos decimales |
| Tope | la suma de las notas con CAE nunca supera el total de la factura | **ninguno**: agrega plata, puede superar el total |
| Cuántas por factura | una total, o varias parciales hasta el total | todas las que haga falta |
| Original | lo chequea el producto | **tiene que tener CAE** (`ORIGINAL_SIN_CAE`) |
| Tipo | `TIPO_NC` (`3/8/13`, `203/208/213`) | `TIPO_ND` (`2/7/12`, `202/207/212`) |
| Qué la frena | una previa sin CAE, el receptor, un pedido en curso | una previa de débito sin CAE, el receptor, un pedido en curso |

La letra y si es FCE se heredan de la factura. La fecha es la de hoy y no se elige (ARCA exige fechas no decrecientes
por tipo y punto de venta). El neto y el IVA salen del importe con la alícuota de la factura, y la nota lleva un solo
ítem (`Débito s/ Factura A 0001-00000007`) y el motivo en las observaciones. Una clase C no discrimina IVA.

## Cómo se enchufa

```python
from libracore import notas_de_debito as nd

emitida = await nd.emitir_nota_de_debito(
    original,                                    # dict con tipo, punto_venta, numero, fecha, cae, cliente_cuit, ...
    clave=("mi_producto", "debito", comprobante.id),   # distinta de la de la nota de crédito
    importe=Decimal("1500.00"), motivo="Intereses por mora",
    numerar=mi_numerar, registrar=mi_registrar, pedir_cae=mi_pedir_cae,
    cargar_previas=lambda: mis_notas_de_debito_de(comprobante),    # opcional
    conn=conexion_de_la_transaccion,             # el candado de la base (varios procesos, ADR-044)
)
```

`NotaNoPermitida.codigo` → HTTP sugerido: `tipo` 400; `original_sin_cae`, `nota_sin_cae` y `en_curso` 409; `receptor` e
`importe` 422.

## Lo que falta medir

El armado es el de la nota de crédito parcial (medido en homologación el 2026-10-04) con otros tipos. Sin certificado
en la máquina de desarrollo **no se probó contra ARCA**: queda por confirmar que acepta la marca de anulación `N` en la
nota de débito de una FCE (`202/207/212`). Hasta entonces, un producto que ofrezca la nota de débito de una FCE tiene
que decirlo como no medido.
