"""El vocabulario de medios de pago de la familia, en un solo lugar.

Hasta el 2026-08-24 esta lista estaba declarada **28 veces en 11 repos**, y ya
divergía en seis formas distintas. El inventario, con nombres y archivos, está
en `wiki/concepts/medios-de-pago-familia-libra.md`. Lo que importa acá es por
qué eso no era un problema cosmético:

- `cheque` existía en `libra-ui/src/facturas.ts` y en los mapas de etiquetas de
  Contalibra y Restolibra, **pero no en la lista canónica** — así que el
  selector no lo ofrecía y los reportes sí lo sabían dibujar.
- `tarjeta` existía en LibraDesk, LibraClub, MedLibra y Gestiolibra, cada uno
  por su cuenta, y en `pdf_generator` de este mismo motor. Ninguna de las cuatro
  lo podía mapear a una condición de venta de ARCA.
- VentaLibra **escribía** `mercado_pago`, con guión bajo, en todo su stack.
  Es la única histórica que ya se retiró: ver "Cómo sale una grafía de
  `HISTORICOS`", más abajo.
- `qr` aparece **sólo dentro de un `WHERE ... IN (...)`** de tres repos, sin
  estar declarado en ninguna lista.
- `ticket_generator._MEDIOS_LABEL` y `pdf_generator._MEDIOS_LABEL` —mismo
  nombre, mismo repo— tenían contenidos distintos.

## 🔴 Dos listas, y la diferencia es la que importa

`ELEGIBLES` es lo que se puede **elegir hoy**: lo que puebla un selector y lo
único que se acepta al escribir. `HISTORICOS` es lo que hay que saber **leer**:
grafías que quedaron en filas reales de bases reales y que ningún reporte puede
dejar en blanco.

Es el mismo criterio que ya llevaba `MEDIOS_CUENTA_CORRIENTE` en `db/caja.py`
con sus dos grafías —*"no sacar ninguna de las dos: hay movimientos históricos
con cada una, y perder una cambiaría saldos ya calculados"*—, generalizado.

**Un histórico no se borra nunca.** Sacarlo de acá no borra la fila: la deja sin
etiqueta, y un cierre de caja que muestra un bucket vacío es peor que uno que
muestra un nombre viejo.

## Cómo sale una grafía de `HISTORICOS`

"Nunca" quiere decir *nunca sin migrar los datos primero*, no *nunca jamás*. El
único camino, recorrido una vez —`mercado_pago`, el 2026-08-25— es éste y en
este orden:

1. **Medir** cuántas filas la tienen, en todas las instancias reales y
   recorriendo TODAS las columnas de texto. No alcanza con filtrar las columnas
   por nombre: la de VentaLibra vivía además adentro del JSON de
   `recibos.pagos`, en una columna que no se llama nada parecido a "medio".
2. **Migrar** esas filas a la grafía canónica, y dejar corriendo el mecanismo
   que las vuelve a normalizar si alguien restaura un backup viejo.
3. **Verificar cero** después de desplegar, no antes.
4. Recién entonces sacarla de acá, de `EQUIVALENTE_CANONICO` y de
   `MEDIOS_ELECTRONICOS` — de las tres, porque una grafía que ya no se conoce y
   sigue nombrada en un `IN (...)` es ruido que el próximo lector va a
   interpretar como que todavía existe.

El trinquete de `tests/test_medios_pago.py` se pone rojo en el paso 4. **Ese
rojo es la pregunta "¿hiciste los tres pasos anteriores?"**, no un obstáculo a
esquivar editando la lista del test.

## Por qué la tarjeta va partida en dos

`tarjeta` a secas no se puede declarar ante ARCA. `CONDICIONES_VENTA` distingue
**Tarjeta de Débito** de **Tarjeta de Crédito**, y son dos condiciones de venta
distintas en el comprobante. Un solo `tarjeta` obliga a adivinar o a caer en
"Otra", que es declarar de menos. VentaLibra ya las tenía partidas por su
cuenta; ahora esa distinción es la de la familia.
"""

#: Los medios que se pueden **elegir** hoy. Es lo que puebla cualquier selector
#: y lo único que se acepta al registrar un pago nuevo.
#:
#: El orden es el de la pantalla, no alfabético: primero lo que más se usa en un
#: mostrador argentino, y `cuenta_corriente` último porque no es un cobro.
ELEGIBLES: dict[str, str] = {
    "efectivo":         "Efectivo",
    "transferencia":    "Transferencia",
    "tarjeta_debito":   "Tarjeta de débito",
    "tarjeta_credito":  "Tarjeta de crédito",
    "mercadopago":      "Mercado Pago",
    "cuenta_dni":       "Cuenta DNI",
    "billetera":        "Otras billeteras",
    "cheque":           "Cheque",
    "cuenta_corriente": "Cuenta corriente",
}

#: Grafías que **ya existen en bases reales** y hay que poder leer, pero que no
#: se ofrecen más. Cada una dice de dónde vino, porque el día que se quiera
#: migrar los datos hay que saber a quién avisarle.
#:
#: 🔴 **Nunca sacar una de acá.** Sacarla no borra la fila que la tiene: la deja
#: sin etiqueta, y un cierre de caja con un bucket sin nombre es peor que uno
#: con un nombre viejo.
HISTORICOS: dict[str, str] = {
    # LibraDesk, LibraClub, MedLibra y Gestiolibra, cada uno por su cuenta.
    "tarjeta":          "Tarjeta",
    # LibraClub, en su `enums.MedioPago`.
    "debito":           "Tarjeta de débito",
    "credito":          "Tarjeta de crédito",
    # Sólo dentro de un `WHERE medio IN (...)` de tres repos. Nunca estuvo
    # declarado en ninguna lista, así que si hay filas con este valor no vinieron
    # de un selector.
    "qr":               "QR",
    # LibraCargo y LibraClub, como cajón de sastre de sus enums.
    "otro":             "Otro",
    # La grafía vieja de cuenta corriente, con espacio. La escriben los
    # movimientos de la emisión. Ver `db/caja.MEDIOS_CUENTA_CORRIENTE`.
    "cuenta corriente": "Cuenta corriente",
}

#: Todo lo que este motor sabe nombrar: lo elegible más lo histórico. Es lo que
#: tiene que consultar cualquier cosa que **muestre** un medio.
CONOCIDOS: dict[str, str] = {**ELEGIBLES, **HISTORICOS}

#: A qué medio elegible corresponde cada histórico. Es el mapa que usaría una
#: migración de datos, y el que permite que un reporte agrupe `tarjeta` y
#: `tarjeta_credito` en la misma fila en vez de mostrar dos.
#:
#: `qr` y `otro` no tienen equivalente y quedan afuera **a propósito**: elegir
#: uno sería inventar información que la fila no tiene.
EQUIVALENTE_CANONICO: dict[str, str] = {
    "tarjeta":          "tarjeta_credito",
    "debito":           "tarjeta_debito",
    "credito":          "tarjeta_credito",
    "cuenta corriente": "cuenta_corriente",
}


#: Los medios que se cobran **por QR o billetera electrónica**, o sea aquellos a
#: los que MercadoPago le puede poner una referencia de pago.
#:
#: 🔴 Estaba escrito como un `IN ('mercadopago','billetera','cuenta_dni','qr')`
#: inline, **repetido en tres repos**, y era el único lugar de la familia donde
#: `qr` existía: no estaba declarado en ninguna lista, así que si hay filas con
#: ese valor no salieron de ningún selector.
#:
#: Hasta el 2026-08-25 incluía también `mercado_pago`, la grafía vieja de
#: VentaLibra — sin ella, ahí la referencia del pago de MercadoPago nunca se
#: actualizaba y nadie lo notaba, porque el pago entra igual. Salió junto con la
#: grafía, y sólo después de que no quedara ninguna fila con ese valor.
MEDIOS_ELECTRONICOS = (
    "mercadopago", "billetera", "cuenta_dni", "qr",
)

_LISTA_ELECTRONICOS = ",".join(f"'{m}'" for m in MEDIOS_ELECTRONICOS)


def sql_es_electronico(columna: str = "medio") -> str:
    """Fragmento SQL: el pago se hizo por un medio electrónico.

    Mismo criterio que `sql_es_cuenta_corriente` de `db/caja.py`, y por la misma
    razón: son valores fijos de este módulo, nunca entrada de usuario, y van
    interpolados porque se concatenan dentro de consultas que ya llevan sus
    propios `?` posicionales.
    """
    return f"{columna} IN ({_LISTA_ELECTRONICOS})"


class MedioDePagoInvalido(ValueError):
    """El medio no es uno de los que se pueden elegir hoy."""


def label(medio: str) -> str:
    """Cómo se muestra un medio. **Nunca devuelve vacío.**

    Un medio desconocido se devuelve tal cual vino, y no `"-"` ni `""`: si
    aparece uno que este motor no conoce, verlo escrito es la única forma de
    enterarse. Taparlo con un guión lo esconde justo en la pantalla donde
    alguien podría notarlo.
    """
    return CONOCIDOS.get(medio, medio)


def canonico(medio: str) -> str:
    """El medio elegible que le corresponde, o el mismo si ya lo es.

    Para agrupar en reportes: sin esto, `tarjeta` y `tarjeta_credito` salen
    como dos filas distintas de la misma cosa.
    """
    return EQUIVALENTE_CANONICO.get(medio, medio)


def es_elegible(medio: str) -> bool:
    """Si se puede elegir hoy. Los históricos dan `False`: se leen, no se
    escriben."""
    return medio in ELEGIBLES


def validar(medio: str) -> str:
    """El medio, o `MedioDePagoInvalido`. **Falla cerrado, a propósito.**

    Hasta hoy Contalibra y Restolibra aceptaban cualquier string en
    `PagoPayload.medio`, y `add_venta_pago()` tampoco validaba: la lista sólo
    existía para poblar el `<Select>`. Un medio inventado entraba, creaba su
    movimiento de caja y aparecía en el cierre como un bucket suelto con el
    nombre crudo. Nadie se enteraba, porque la plata estaba bien contada — lo
    que estaba mal era el reparto.
    """
    if medio not in ELEGIBLES:
        raise MedioDePagoInvalido(
            f"«{medio}» no es un medio de pago válido. "
            f"Los que se pueden elegir son: {', '.join(ELEGIBLES)}."
        )
    return medio


def para_selector(*, incluir_cuenta_corriente: bool = True) -> list[dict]:
    """La lista `[{id, label}]` que espera un `<Select>`.

    `incluir_cuenta_corriente=False` es para las pantallas que **cobran**: la
    cuenta corriente no es un medio de cobro, es la marca de que la operación se
    hizo a crédito. Ofrecerla ahí deja registrar un cobro que no cobra nada.
    """
    return [
        {"id": clave, "label": etiqueta}
        for clave, etiqueta in ELEGIBLES.items()
        if incluir_cuenta_corriente or clave != "cuenta_corriente"
    ]
