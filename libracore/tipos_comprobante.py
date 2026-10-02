"""Los tipos de comprobante de ARCA que la familia emite, en **un** lugar.

Hasta acá cada módulo —listados, cuenta corriente, libro IVA, dashboard, PDF—
tenía su propia copia de `(1, 6, 11)` y `(3, 8, 13)`. Sumar la Factura de
Crédito Electrónica MiPyME (FCE) a ocho copias es de donde sale la que se
olvida, y un comprobante que falta en una consulta **desaparece** de ese
listado sin error.

La FCE tiene los mismos tres tipos que una factura común, con otros códigos:
`201/202/203` son A (factura, débito, crédito), `206/207/208` son B y
`211/212/213` son C.
"""

FACTURAS = (1, 6, 11, 201, 206, 211)
NC = (3, 8, 13, 203, 208, 213)
ND = (2, 7, 12, 202, 207, 212)

#: Las que son FCE: la factura, y sus notas.
FCE_FACTURA = frozenset({201, 206, 211})
FCE_NOTA = frozenset({202, 203, 207, 208, 212, 213})
FCE = FCE_FACTURA | FCE_NOTA

#: Clase C (factura, débito y crédito): todo el importe es neto, sin alícuotas.
C = frozenset({11, 12, 13, 211, 212, 213})

#: De qué factura sale qué nota. La letra se conserva y también si es FCE: la
#: nota de una FCE es una nota de FCE, no una común.
TIPO_NC = {1: 3, 6: 8, 11: 13, 201: 203, 206: 208, 211: 213}
TIPO_ND = {1: 2, 6: 7, 11: 12, 201: 202, 206: 207, 211: 212}

LETRA = {
    1: "A", 6: "B", 11: "C", 3: "A", 8: "B", 13: "C", 2: "A", 7: "B", 12: "C",
    201: "A", 202: "A", 203: "A", 206: "B", 207: "B", 208: "B",
    211: "C", 212: "C", 213: "C",
}


def en_sql(tipos) -> str:
    """`1,6,11` para un `IN (...)`. Son enteros de este módulo, nunca entrada de usuario."""
    return ",".join(str(int(t)) for t in tipos)
