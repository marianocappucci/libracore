"""De dónde salen los clientes que MercadoPago factura.

## Por qué esto es un puerto y no una tabla

El módulo de MercadoPago del motor nació extraído de [[contalibra]], y se trajo
puesta una suposición de allá: que **los clientes viven en `libracore.db.clients`**.
Es cierto en Contalibra y en Restolibra —ahí esa tabla *es* el registro de
clientes, porque son sistemas contables y el cliente es su dominio— y es falso
en el resto de la familia:

| Productos | De dónde sale el cliente |
|---|---|
| [[contalibra]], [[restolibra]] | `libracore.db.clients` |
| [[gestiolibra]], [[medlibra]] | `libragenda.Client` + una fila de extensión local |
| [[libraclub]], [[ventalibra]] | su propio dominio |

> 🔑 **Y eso no es una falta de normalización: es la normalización correcta.**
> Un producto de turnos saca el cliente de su motor de agenda, que es de donde
> cuelgan `appointments` y —en MedLibra— la historia clínica entera. Unificar
> todo en `libracore.clients` rompería seis claves foráneas y cambiaría la
> identidad del cliente de `String(100)` a `INTEGER`. Está analizado y decidido
> el 2026-08-12; ver `wiki/analyses/clientes-transversal-familia-libra.md`.

Lo que **sí** tiene que ser transversal es el **flujo**: la firma del webhook, la
idempotencia, la ingesta única que comparten la bandeja y el cron, los cuatro
caminos pasando por un solo punto de resolución. Nada de eso depende de dónde
esté guardado el cliente.

Así que el módulo lo recibe. Sin registro explícito usa el de LibraCore, que es
el comportamiento que Contalibra y Restolibra ya tenían: para ellos esto no
cambia nada.

## El contrato

Un cliente es un `dict` con estas claves. Son las que el comprobante necesita, y
el producto es responsable de traducir las suyas:

| Clave | Para qué |
|---|---|
| `id` | Vincular el pago. Puede ser `int` o `str` — el motor no lo interpreta |
| `name` | Razón social del comprobante. **La única obligatoria** |
| `cuit_dni` | El CUIT del receptor |
| `iva_condition` | Se traduce al código que exige ARCA |
| `address` | Domicilio del receptor |
| `email` | A dónde se manda el comprobante |
| `auto_facturar` | Si se le factura sin que nadie mire |

## Los alias son del registro, no del motor

`resolver()` es responsable de **el alias primero y el match directo después**.
No está partido en dos métodos a propósito: la regla *"nunca resuelvas por tu
cuenta"* es justamente lo que se rompió una vez y costó dos comprobantes al CUIT
equivocado, y con un solo método no hay dónde saltearse el alias.

⚠️ `facturacion_alias.cliente_id` es `INTEGER`, así que la tabla de alias de
LibraCore **no sirve** para un registro cuya identidad es texto. Un producto así
trae su propio almacenamiento de alias, o no tiene alias — es su decisión, y por
eso vive detrás del puerto.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from libracore.db import clients as db_clients
from libracore.db import core
from libracore.db import mp as db_mp


@runtime_checkable
class RegistroDeClientes(Protocol):
    """Lo que el módulo de MercadoPago necesita saber de un registro."""

    def resolver(self, payer_email: str, payer_cuit: str) -> dict | None:
        """A quién corresponde este pago. **Alias primero, match directo
        después.** `None` si no hay nadie."""
        ...

    def crear(self, *, nombre: str, email: str = "", cuit_dni: str = "",
              iva_condition: str = "Consumidor Final", address: str = "") -> dict:
        """Da de alta el cliente que no estaba y lo devuelve."""
        ...

    def buscar_muchos(self, emails: set[str], cuits: set[str]) -> tuple[dict, dict]:
        """Para la bandeja: `(por_email, por_cuit)` en **dos consultas**, no una
        por fila. Las claves de `por_cuit` vienen normalizadas sin guiones."""
        ...


class RegistroDeLibraCore:
    """El registro de los productos contables, sobre `libracore.db.clients`.

    Es el comportamiento que Contalibra y Restolibra ya tenían; se escribe acá
    para que sea *una* implementación del puerto y no el caso especial que el
    motor asume.
    """

    def resolver(self, payer_email: str, payer_cuit: str) -> dict | None:
        return db_mp.resolver_cliente_pago(payer_email, payer_cuit)

    def crear(self, *, nombre: str, email: str = "", cuit_dni: str = "",
              iva_condition: str = "Consumidor Final", address: str = "") -> dict:
        cliente_id = db_clients.create_client(
            name=nombre, email=email, cuit_dni=cuit_dni,
            iva_condition=iva_condition, address=address,
        )
        return db_clients.get_client(cliente_id)

    def buscar_muchos(self, emails: set[str], cuits: set[str]) -> tuple[dict, dict]:
        por_email: dict = {}
        por_cuit: dict = {}
        if not emails and not cuits:
            return por_email, por_cuit
        with core.get_connection() as conn:
            if emails:
                hueco = ",".join("?" * len(emails))
                filas = conn.execute(
                    f"SELECT * FROM clients WHERE email IN ({hueco})", tuple(emails)
                ).fetchall()
                por_email = {dict(f)["email"]: dict(f) for f in filas}
            if cuits:
                hueco = ",".join("?" * len(cuits))
                filas = conn.execute(
                    f"SELECT * FROM clients WHERE REPLACE(cuit_dni, '-', '') IN ({hueco})",
                    tuple(cuits),
                ).fetchall()
                for fila in filas:
                    d = dict(fila)
                    norm = (d.get("cuit_dni") or "").replace("-", "").strip()
                    if norm:
                        por_cuit[norm] = d
        return por_email, por_cuit


#: El que se usa cuando nadie pasa otro. Mantiene intacto el comportamiento de
#: los dos productos que ya usaban este módulo.
POR_OMISION = RegistroDeLibraCore()


def el_registro(registro: RegistroDeClientes | None) -> RegistroDeClientes:
    return registro if registro is not None else POR_OMISION
