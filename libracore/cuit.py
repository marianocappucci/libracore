"""El CUIT como lo trata la familia: aquí vive la regla de «sólo los dígitos».

Un CUIT se carga como lo tipea cada quien (`20-12345678-9`, `30.70933285.2`, con espacios).
Para compararlo, buscarlo o mandarlo a ARCA hay que quitarle todo lo que no es dígito, y esa
regla es **una sola**: un producto no escribe su propia versión (`reglas/producto.md` del wiki),
porque el día que el motor cambie lo que cuenta como «un CUIT cargado» las pantallas y la
emisión dejarían de coincidir.
"""

import re


def solo_digitos(valor) -> str:
    """El valor **sólo en dígitos**, o `""`.

    Quita guiones, puntos, espacios y todo lo que no sea un dígito. `None` y vacío dan `""`;
    lo que no es texto se pasa por `str` (no lanza). No valida largo ni verificador: para eso
    están `libracore.arca_wsfe.cuit_con_verificador_valido` y `problema_del_receptor`.
    """
    return re.sub(r"\D", "", str(valor or ""))
