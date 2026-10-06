"""Quién emite el documento que sale en el PDF: **un solo lugar** que lo resuelve (ADR-031).

Todos los PDF del motor (`pdf_generator`: factura, nota de crédito y de débito, FCE, recibo,
presupuesto, remito, pre factura, resumen de cuenta) arman el membrete con un `dict` `empresa`:
`nombre`, `cuit`, `direccion`, `telefono`, `email`, `iva_condition`, `iibb`, `inicio_actividades`,
`logo_path` y `logo_bytes`. Hasta acá salía **siempre** de la configuración global de la instancia
(`config.json`), que supone un solo emisor. Un producto con varias razones sociales (LibraCargo,
ADR-021) emitía con el CUIT de una y el membrete de otra.

`emisor_para(documento)` arma ese `dict` por **capas**, cada una pisa a la anterior:

1. **la configuración de la instancia** (`pdf_generator._empresa()`): lo de siempre. Quien no hace
   nada, no cambia nada;
2. **`documento["emisor_id"]`**, si lo tiene: `nombre` y `cuit` de esa fila de `arca_config`
   (`empresa` y `cuit`), con la que se emitió el comprobante. Esa tabla no guarda domicilio, condición
   de IVA ni logo: esos los pone la capa siguiente;
3. **el resolvedor del producto**: un callable `(documento: dict) -> dict | None` con lo que sólo el
   producto sabe (`direccion`, `iva_condition`, `iibb`, `inicio_actividades`, `logo_bytes`...). Sus
   claves pisan las anteriores; una clave en `None` **no** pisa («no tengo dato, queda el de abajo»);
   un texto vacío sí;
4. **`empresa=`**, lo que pasa quien llama a un generador puntual (la pre factura lo tiene desde su
   primera versión): gana sobre todo lo demás.

## El resolvedor, de dos maneras

- **Registrado** una vez al arrancar, `registrar_resolvedor(fn)`: lo usan todos los PDF del proceso,
  también los que el motor genera por su cuenta (el PDF que se guarda al autorizar, el de
  `venta_facturacion`, el de la bandeja de MercadoPago). Es el mismo criterio que
  `libro_de_clientes.registrar_origen_de_ventas`: una decisión del producto que vale para todo el
  proceso y que el motor no puede pedir en cada llamada porque no es él quien llama.
- **Por parámetro**, `resolvedor=` de cada generador y `emisor_del_pdf=` de los routers: **reemplaza**
  al registrado para esa llamada. Sirve para probar y para un router que se monta con otra regla.

Un producto que no registra nada ni pasa nada **no cambia**: la capa 1, y la 2 sólo si el comprobante
tiene `emisor_id`.

## Qué recibe el resolvedor

El `dict` del documento tal cual lo tiene el generador: una fila de `facturas`, un recibo, un
presupuesto, un remito, una pre factura, o —en el resumen de cuenta— el cliente. **Leé con `.get`**: no
todos tienen las mismas claves.

🔴 **Lo que el resolvedor levante, sube.** No se traga: caer al membrete de la instancia ante un error
sería imprimir un comprobante con los datos de otra razón social, y eso es peor que no imprimirlo.
"""
from __future__ import annotations

from collections.abc import Callable

from libracore.db.arca_config import EmisorDesconocido
from libracore.db.core import Conexion
from libracore.db.facturas import _con

#: `(documento) -> dict | None`: los datos del emisor de ese documento, o `None` si no tiene nada que agregar.
Resolvedor = Callable[[dict], "dict | None"]

_resolvedor: Resolvedor | None = None


def registrar_resolvedor(resolvedor: Resolvedor | None) -> None:
    """Registra (o, con `None`, quita) el resolvedor del producto para **todos** los PDF del proceso.

    Se llama una vez al arrancar. Registrar de nuevo reemplaza al anterior.
    """
    global _resolvedor
    _resolvedor = resolvedor


def resolvedor_registrado() -> Resolvedor | None:
    return _resolvedor


def datos_de_arca_config(emisor_id: int, *, conn: Conexion | None = None) -> dict:
    """`nombre` y `cuit` de la fila de `arca_config` con la que se emitió un comprobante.

    Busca **también entre las inactivas**: dar de baja una razón social no cambia con quién se emitió
    lo que ya salió, y reimprimirlo tiene que seguir diciendo lo mismo. Un id que no existe levanta
    `EmisorDesconocido` en vez de caer a otro emisor: sería imprimir el CUIT equivocado (ADR-021).
    """
    with _con(conn) as c:
        fila = c.execute("SELECT empresa, cuit FROM arca_config WHERE id=?", (emisor_id,)).fetchone()
    if not fila:
        raise EmisorDesconocido(f"No hay una configuración de ARCA con id {emisor_id}.")
    return {"nombre": fila[0], "cuit": fila[1]}


def _sin_nulos(datos: dict | None) -> dict:
    return {k: v for k, v in (datos or {}).items() if v is not None}


def emisor_para(
    documento: dict | None = None, *, resolvedor: Resolvedor | None = None,
    empresa: dict | None = None, conn: Conexion | None = None,
) -> dict:
    """El `dict` `empresa` que dibuja el membrete de `documento`. Ver el docstring del módulo.

    `resolvedor` reemplaza al registrado con `registrar_resolvedor`; `empresa` pisa al final;
    `conn` es la conexión (y la transacción) de quien llama, para leer `arca_config` desde ahí.
    """
    # Import tardío: `pdf_generator` importa este módulo, y `_empresa` es de él.
    from libracore import pdf_generator

    documento = documento or {}
    emp = pdf_generator._empresa()
    if documento.get("emisor_id") is not None:
        emp.update(datos_de_arca_config(documento["emisor_id"], conn=conn))
    resolver = resolvedor if resolvedor is not None else _resolvedor
    if resolver is not None:
        emp.update(_sin_nulos(resolver(documento)))
    emp.update(_sin_nulos(empresa))
    return emp
