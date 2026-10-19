"""La factura que emitió un nodo offline, de subida al central (ADR-074).

El nodo factura con internet (decisión 3 del humano): el comprobante nace en la base del nodo, con el número y el CAE
que dio ARCA. El central lo tiene que tener —libro IVA, cuenta corriente, reportes—, y LibraEdge sólo sube lo que
alguien encola. Este módulo es la mitad del motor de ese viaje:

- `payload_factura(factura)`: lo que viaja, en JSON (sin PDF, sin usuario: el del nodo no existe en el central).
- `aplicar_factura_del_nodo(payload, conn=)`: la escribe en el central **con el mismo número y CAE**, idempotente.

La otra mitad es del producto (o de `libracommerce.erp.offline`): encolar la operación en `PuertoDeVentas.al_facturar`
y, del lado del central, vincular la factura a la venta que subió. El motor no sabe de ventas ni de LibraEdge.

**Idempotencia.** La clave es la del comprobante ante ARCA: `(tipo, punto_venta, numero, ambiente, emisor)`. Si ya
está con el mismo total y el mismo CAE (o el central lo tiene sin CAE y llega con CAE: se autorizó después en el
nodo), no duplica. Si está con **otro** total u **otro** CAE, `FacturaEnConflicto` (un `ValueError`: el receptor de
LibraEdge la manda a revisión manual). Pasa si el central emitió por el mismo punto de venta que el nodo: **el punto
de venta de la sucursal con nodo lo usa sólo el nodo**.
"""

from __future__ import annotations

import json

from libracore.db import facturas as db_facturas
from libracore.db.core import Conexion

#: La versión de la forma de `payload_factura`. Si cambia, sube: el central rechaza lo que no entiende.
VERSION = 1

_CAMPOS = (
    "tipo", "punto_venta", "numero", "fecha", "cliente_cuit", "cliente_razon", "cliente_iva_cond", "items",
    "subtotal", "iva_amount", "total", "concepto", "cae", "cae_vto", "observaciones", "cliente_domicilio",
    "fch_serv_desde", "fch_serv_hasta", "fch_vto_pago", "cbte_asoc_tipo", "cbte_asoc_pv", "cbte_asoc_nro",
    "condicion_venta", "ambiente", "emisor_id",
)


class FacturaEnConflicto(ValueError):
    """El central ya tiene ese número de comprobante con otro total u otro CAE. No es un duplicado: revisión manual."""


def payload_factura(factura: dict) -> dict:
    """Lo que viaja al central de una factura del nodo. Plata como número (ya está redondeada a 2 decimales)."""
    datos = {campo: factura.get(campo) for campo in _CAMPOS}
    items = datos.get("items")
    datos["items"] = json.loads(items) if isinstance(items, str) else list(items or [])
    datos["id_en_el_nodo"] = factura.get("id")
    datos["version"] = VERSION
    return datos


def aplicar_factura_del_nodo(payload: dict, *, conn: Conexion | None = None) -> tuple[int, bool]:
    """Escribe en el central la factura que emitió el nodo. Devuelve `(factura_id, creada)`.

    Con `conn`, en la transacción del receptor (no confirma). Ver el docstring del módulo para la idempotencia."""
    if int(payload.get("version") or 0) != VERSION:
        raise ValueError(f"versión de factura del nodo no soportada: {payload.get('version')!r}")
    tipo, pv, numero = int(payload["tipo"]), int(payload["punto_venta"]), int(payload["numero"])
    ambiente = str(payload.get("ambiente") or "").strip().lower()
    cae = str(payload.get("cae") or "")
    existente = db_facturas.get_factura_por_tipo_pv_nro(
        tipo, pv, numero, emisor_id=payload.get("emisor_id"), ambiente=ambiente, conn=conn)
    if existente:
        rotulo = f"{pv:04d}-{numero:08d} (tipo {tipo})"
        if abs(float(existente["total"]) - float(payload["total"])) > 0.005:
            raise FacturaEnConflicto(
                f"El comprobante {rotulo} ya está en el central por {existente['total']} y el nodo lo emitió por "
                f"{payload['total']}.")
        cae_central = str(existente.get("cae") or "")
        if cae_central and cae and cae_central != cae:
            raise FacturaEnConflicto(f"El comprobante {rotulo} ya está en el central con otro CAE.")
        if cae and not cae_central:
            db_facturas.update_factura_cae(existente["id"], cae, payload.get("cae_vto") or "", conn=conn)
        return existente["id"], False

    factura_id = db_facturas.registrar_del_nodo(
        tipo=tipo, punto_venta=pv, numero=numero, ambiente=ambiente, fecha=payload["fecha"],
        cliente_cuit=payload.get("cliente_cuit") or "", cliente_razon=payload.get("cliente_razon") or "",
        cliente_iva_cond=payload.get("cliente_iva_cond"), items=payload.get("items") or [],
        subtotal=payload["subtotal"], iva_amount=payload["iva_amount"], total=payload["total"],
        concepto=payload.get("concepto") or 1, cae=cae, cae_vto=payload.get("cae_vto") or "",
        observaciones=payload.get("observaciones") or "", cliente_domicilio=payload.get("cliente_domicilio") or "",
        fch_serv_desde=payload.get("fch_serv_desde") or "", fch_serv_hasta=payload.get("fch_serv_hasta") or "",
        fch_vto_pago=payload.get("fch_vto_pago") or "", cbte_asoc_tipo=payload.get("cbte_asoc_tipo") or 0,
        cbte_asoc_pv=payload.get("cbte_asoc_pv") or 0, cbte_asoc_nro=payload.get("cbte_asoc_nro") or 0,
        condicion_venta=payload.get("condicion_venta") or "", emisor_id=payload.get("emisor_id"), conn=conn,
    )
    return factura_id, True
