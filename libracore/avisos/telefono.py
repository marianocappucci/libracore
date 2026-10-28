"""El teléfono como lo piden los proveedores de WhatsApp (ADR-090). Subido de LibraClub (ADR-029)."""
import re


def telefono_para_whatsapp(texto: str | None) -> str:
    """El teléfono como lo piden los proveedores de WhatsApp: sólo dígitos, con país. Vacío si no se sabe.

    `11 5555-1234`, `(011) 5555-1234` y `+54 9 11 5555 1234` son el mismo celular y salen como `5491155551234`.
    **Ante la duda devuelve vacío** y el aviso queda `omitido` con su motivo: un número mal armado es un mensaje a un
    desconocido, que es peor que un mensaje que no sale. Por eso sólo se reconocen formas sin ambigüedad:

    - con el 54 de Argentina (con o sin el 9 del celular);
    - 10 dígitos nacionales (característica + número), con o sin el 0 de adelante. La característica empieza con 11, 2 o 3: un
      «15 5555-1234» —el 15 sin característica— no es un número completo;
    - el «15» que se marca entre la característica y el número, **sólo con la característica 11**: en el interior la
      característica tiene 2, 3 o 4 dígitos y el «15» no se puede ubicar solo;
    - otro país, **sólo si se escribió con `+` o `00`**: sin ese prefijo un número largo puede ser un argentino con el 15
      puesto, y mandárselo a un desconocido es lo que se quiere evitar.
    """
    crudo = (texto or "").strip()
    digitos = re.sub(r"\D", "", crudo)
    internacional = crudo.startswith("+") or crudo.startswith("00")
    if crudo.startswith("00"):
        digitos = digitos[2:]

    if internacional:
        if digitos.startswith("54"):
            resto = digitos[2:]
            if resto.startswith("9"):
                return digitos if len(resto) == 11 else ""
            return f"549{resto}" if len(resto) == 10 else ""
        return digitos if 8 <= len(digitos) <= 15 and not digitos.startswith("0") else ""

    if digitos.startswith("0"):
        digitos = digitos[1:]
    if len(digitos) == 12 and digitos.startswith("1115"):
        digitos = "11" + digitos[4:]
    if len(digitos) == 10 and (digitos.startswith("11") or digitos[0] in "23"):
        return f"549{digitos}"
    return ""


#: Un teléfono ya normalizado: sólo dígitos, sin 0 de adelante, con país (lo que devuelve `telefono_para_whatsapp`).
_E164 = re.compile(r"[1-9]\d{7,14}")


def es_telefono_normalizado(texto: str) -> bool:
    """Si `texto` ya es un teléfono con país, listo para el proveedor.

    Es la última puerta antes de la red y por eso es conservadora: un número argentino **sin** el 549 (10 dígitos que empiezan con
    11, 2 o 3) tiene la forma de uno de otro país —«1155551234» se leería como un número de Estados Unidos— y mandarlo es
    escribirle a un desconocido. Se rechaza, y quien avisa lo pasa antes por `telefono_para_whatsapp`."""
    if not _E164.fullmatch(texto or ""):
        return False
    return not (len(texto) == 10 and (texto.startswith("11") or texto[0] in "23"))
