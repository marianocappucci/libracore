"""Las monedas en que la familia lleva cuentas: pesos y dólares (ADR-088).

Nace con LibraInmo, que tiene contratos de alquiler pactados en dólares: la cuenta del inquilino y la del propietario
no pueden sumar pesos con dólares, así que cada asiento del libro de terceros (`cc_asientos.moneda`) y cada recibo
(`recibos.moneda`) dicen en qué moneda están. Los productos que sólo trabajan en pesos no pasan nada: el default es
`ARS` y todo sigue igual.

**No convierte.** Un saldo en dólares es un saldo en dólares; pasarlo a pesos es una cotización que elige quien la
necesita, y el motor no la inventa.
"""
from __future__ import annotations

PESOS = "ARS"
DOLARES = "USD"

#: Código ISO 4217 → (símbolo, nombre). El orden es el de las listas de la pantalla.
MONEDAS: dict[str, tuple[str, str]] = {
    PESOS: ("$", "Pesos argentinos"),
    DOLARES: ("US$", "Dólares estadounidenses"),
}


class MonedaInvalida(ValueError):
    """La moneda no es una de `MONEDAS`. El mensaje dice cuáles hay."""


def validar(moneda: str | None) -> str:
    """La moneda normalizada (`usd` → `USD`), o `MonedaInvalida`. `None` o vacío es pesos."""
    codigo = (moneda or PESOS).strip().upper()
    if codigo not in MONEDAS:
        raise MonedaInvalida(f"Moneda inválida: {moneda!r} (hay {', '.join(MONEDAS)}).")
    return codigo


def simbolo(moneda: str | None) -> str:
    """`$` para pesos, `US$` para dólares: lo que va delante del importe en un papel o una pantalla."""
    return MONEDAS[validar(moneda)][0]


def nombre(moneda: str | None) -> str:
    return MONEDAS[validar(moneda)][1]
