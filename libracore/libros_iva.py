"""Libros IVA: los resúmenes por alícuota y los generadores REGINFO (P9-M4,
2026-09-07). Viene de `web/routers/libros_iva.py`, que Contalibra y Restolibra
tenían igual (la copia de Restolibra traía dos helpers sin uso y un comentario
sobre el neto del archivo de comprobantes, que se conserva). Los nombres con
guion bajo se mantienen porque los tests de los dos productos los importan.
"""

from __future__ import annotations

import datetime

# ── Constantes ARCA/REGINFO ───────────────────────────────────────────────────

_TIPO_C = {11, 12, 13}   # Factura C, ND C, NC C — Monotributista / Consumidor Final
_NC = {3, 8, 13}         # Notas de Crédito (importes negativos)

# Código alícuota ARCA → código REGINFO
_ALIC_CODE = {0.0: "3", 2.5: "9", 5.0: "8", 10.5: "4", 21.0: "5", 27.0: "6"}
_ALIC_PCT = {0.0: "0.00", 2.5: "2.50", 5.0: "5.00", 10.5: "10.50", 21.0: "21.00", 27.0: "27.00"}

# Tipo de comprobante en egresos → código AFIP
_TIPO_EGRESO_AFIP = {
    "factura": "01",  # Factura A (por defecto; sin dato específico)
    "ticket": "11",
    "recibo": "11",
    "otro": "11",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _default_periodo():
    hoy = datetime.date.today()
    return hoy.replace(day=1).isoformat(), hoy.isoformat()


def _strip_cuit(cuit: str) -> str:
    return (cuit or "").replace("-", "").replace(" ", "")


def _cod_doc(cuit_limpio: str) -> str:
    if len(cuit_limpio) == 11:
        return "80"   # CUIT
    if len(cuit_limpio) == 8:
        return "96"   # DNI
    return "99"       # Sin identificar


def _fmt(v) -> str:
    return f"{float(v or 0):.2f}"


def _get_iva_rate(factura: dict) -> float:
    """Determina la tasa de IVA de una factura (de items o calculada)."""
    for item in factura.get("items", []):
        if item.get("iva_pct") is not None:
            return float(item["iva_pct"])
    sub = float(factura.get("subtotal") or 0)
    iva = float(factura.get("iva_amount") or 0)
    if sub > 0 and iva > 0:
        raw = round(iva / sub * 100, 1)
        # Redondear a la alícuota ARCA más cercana
        return min(_ALIC_CODE, key=lambda x: abs(x - raw))
    return 0.0


def _alicuota_de_egreso(neto: float, iva_monto: float) -> float:
    """Alícuota de una compra, en puntos porcentuales (21.0, no 0.21).

    🔴 **No se lee `iva_pct` de la fila.** En egresos ese campo guarda una
    *fracción* — el alta hace `monto_neto * iva_pct` sin dividir por 100 y el
    detalle lo muestra con `iva_pct * 100` — mientras que `_ALIC_CODE` y
    `_ALIC_PCT` están indexados en puntos. Usarlo directo no matcheaba nunca y
    el export caía siempre al default: una compra al 10,5% salía declarada
    como 21% (código 5) con el neto y el IVA reales al lado, o sea un archivo
    que ARCA rechaza.

    Se deriva del importe, igual que en el lado de ventas (`_get_iva_rate`):
    es independiente de en qué unidad haya quedado guardado el campo, así que
    también acierta sobre las filas viejas.
    """
    if neto <= 0 or iva_monto <= 0:
        return 0.0
    raw = round(iva_monto / neto * 100, 1)
    return min(_ALIC_CODE, key=lambda x: abs(x - raw))


def _parse_num_egreso(numero: str):
    """'0001-00001234' → (1, 1234). Retorna (1, 0) si no parsea."""
    numero = (numero or "").strip()
    if "-" in numero:
        parts = numero.split("-", 1)
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            pass
    try:
        return 1, int(numero)
    except ValueError:
        return 1, 0


# ── Generadores REGINFO ───────────────────────────────────────────────────────

def _ventas_cbte(facturas: list) -> str:
    lines = []
    for f in facturas:
        tipo = int(f.get("tipo", 11))
        is_c = tipo in _TIPO_C
        is_nc = tipo in _NC
        sub = float(f.get("subtotal") or 0)
        iva = float(f.get("iva_amount") or 0)
        total = float(f.get("total") or 0)

        if is_nc:
            total = -abs(total)
            sub = -abs(sub)
            iva = -abs(iva)

        cuit_c = _strip_cuit(f.get("cliente_cuit") or "")
        fecha = (f.get("fecha") or "").replace("-", "")
        pv = str(f.get("punto_venta") or 1).zfill(5)
        nro = str(f.get("numero") or 0).zfill(8)
        denom = (f.get("cliente_razon") or "")[:30]

        if is_c or iva == 0:
            cant_alic = 0
            cod_op = "N"
        else:
            cant_alic = 1
            cod_op = " "

        lines.append("|".join([
            fecha,                  # 1  Fecha
            str(tipo).zfill(2),     # 2  Tipo
            pv,                     # 3  PV
            nro,                    # 4  Nro desde
            nro,                    # 5  Nro hasta
            _cod_doc(cuit_c),       # 6  Cód doc comprador
            cuit_c or "0",          # 7  Nro doc comprador
            denom,                  # 8  Denominación
            _fmt(total),            # 9  Importe total
            _fmt(0),                # 10 No gravado
            _fmt(0),                # 11 Percepción NC
            _fmt(0),                # 12 Exento
            _fmt(0),                # 13 Percepción IIBB
            _fmt(0),                # 14 Percepción municipal
            _fmt(0),                # 15 Imp. internos
            "PES",                  # 16 Moneda
            "1.000000",             # 17 Tipo de cambio
            str(cant_alic),         # 18 Cant. alícuotas
            cod_op,                 # 19 Cód. operación
            _fmt(0),                # 20 Crédito fiscal computable
            _fmt(0),                # 21 Otros tributos
            "",                     # 22 CUIT emisor
            "",                     # 23 Denominación emisor ext.
            "",                     # 24 ID despacho
        ]))
    return "\r\n".join(lines)


def _ventas_alicuotas(facturas: list) -> str:
    lines = []
    for f in facturas:
        tipo = int(f.get("tipo", 11))
        iva = float(f.get("iva_amount") or 0)
        if tipo in _TIPO_C or iva == 0:
            continue

        is_nc = tipo in _NC
        sub = float(f.get("subtotal") or 0)
        if is_nc:
            sub = -abs(sub)
            iva = -abs(iva)

        rate = _get_iva_rate(f)
        pv = str(f.get("punto_venta") or 1).zfill(5)
        nro = str(f.get("numero") or 0).zfill(8)

        lines.append("|".join([
            str(tipo).zfill(2),        # 1 Tipo
            pv,                        # 2 PV
            nro,                       # 3 Número
            _ALIC_CODE.get(rate, "5"), # 4 Cód. alícuota
            _fmt(sub),                 # 5 Neto gravado
            _ALIC_PCT.get(rate, "21.00"),  # 6 Alícuota %
            _fmt(iva),                 # 7 IVA liquidado
        ]))
    return "\r\n".join(lines)


def _compras_cbte(egresos: list) -> str:
    lines = []
    for e in egresos:
        tc_raw = e.get("tipo_comprobante") or "factura"
        tipo = _TIPO_EGRESO_AFIP.get(tc_raw, "01")
        total = float(e.get("total") or 0)

        cuit_p = _strip_cuit(e.get("proveedor_cuit") or "")
        fecha = (e.get("fecha") or "").replace("-", "")
        pv_raw, nro_raw = _parse_num_egreso(e.get("numero") or "")
        pv = str(pv_raw).zfill(5)
        nro = str(nro_raw).zfill(8)
        denom = (e.get("proveedor_nombre") or "")[:30]

        # El neto NO va en este archivo: el registro de COMPROBANTES lleva el
        # importe total, y el neto gravado por alícuota va en el archivo de
        # ALICUOTAS (`_compras_alicuotas`), que es donde ARCA lo espera.
        iva_m = float(e.get("iva_monto") or 0)
        cant_alic = 1 if iva_m > 0 else 0
        cod_op = " " if iva_m > 0 else "N"

        lines.append("|".join([
            fecha,                  # 1  Fecha
            tipo,                   # 2  Tipo
            pv,                     # 3  PV
            nro,                    # 4  Nro desde
            nro,                    # 5  Nro hasta
            _cod_doc(cuit_p),       # 6  Cód doc proveedor
            cuit_p or "0",          # 7  CUIT proveedor
            denom,                  # 8  Denominación
            _fmt(total),            # 9  Importe total
            _fmt(0),                # 10 No gravado
            _fmt(0),                # 11 IVA no computable
            _fmt(0),                # 12 Exento
            _fmt(0),                # 13 Percepción IVA
            _fmt(0),                # 14 Percepción IIBB
            _fmt(0),                # 15 Percepción municipal
            _fmt(0),                # 16 Imp. internos
            "PES",                  # 17 Moneda
            "1.000000",             # 18 Tipo de cambio
            str(cant_alic),         # 19 Cant. alícuotas
            cod_op,                 # 20 Cód. operación
            _fmt(iva_m),            # 21 Crédito fiscal computable
            _fmt(0),                # 22 Otros tributos
            "",                     # 23 CUIT emisor
            "",                     # 24 Denominación emisor ext.
            "",                     # 25 ID despacho
        ]))
    return "\r\n".join(lines)


def _compras_alicuotas(egresos: list) -> str:
    lines = []
    for e in egresos:
        iva_m = float(e.get("iva_monto") or 0)
        if iva_m == 0:
            continue

        tc_raw = e.get("tipo_comprobante") or "factura"
        tipo = _TIPO_EGRESO_AFIP.get(tc_raw, "01")
        neto = float(e.get("monto_neto") or 0)
        pct = _alicuota_de_egreso(neto, iva_m)
        pv_raw, nro_raw = _parse_num_egreso(e.get("numero") or "")
        pv = str(pv_raw).zfill(5)
        nro = str(nro_raw).zfill(8)

        lines.append("|".join([
            tipo,                           # 1 Tipo
            pv,                             # 2 PV
            nro,                            # 3 Número
            _ALIC_CODE.get(pct, "5"),       # 4 Cód. alícuota
            _fmt(neto),                     # 5 Neto gravado
            _ALIC_PCT.get(pct, "21.00"),    # 6 Alícuota %
            _fmt(iva_m),                    # 7 IVA liquidado
        ]))
    return "\r\n".join(lines)


# ── Resumen por alícuota ──────────────────────────────────────────────────────

def _resumen_ventas(facturas: list) -> dict:
    total_cbtes = len(facturas)
    total_neto = 0.0
    total_iva = 0.0
    total_total = 0.0
    por_tasa = {}

    for f in facturas:
        tipo = int(f.get("tipo", 11))
        is_nc = tipo in _NC
        sign = -1 if is_nc else 1
        sub = float(f.get("subtotal") or 0) * sign
        iva = float(f.get("iva_amount") or 0) * sign
        tot = float(f.get("total") or 0) * sign
        total_neto += sub
        total_iva += iva
        total_total += tot

        rate = _get_iva_rate(f) if iva != 0 else 0.0
        if rate not in por_tasa:
            por_tasa[rate] = {"neto": 0.0, "iva": 0.0, "cbtes": 0}
        por_tasa[rate]["neto"] += sub
        por_tasa[rate]["iva"] += iva
        por_tasa[rate]["cbtes"] += 1

    return {
        "cbtes": total_cbtes, "neto": total_neto,
        "iva": total_iva, "total": total_total,
        "por_tasa": dict(sorted(por_tasa.items())),
    }


def _resumen_compras(egresos: list) -> dict:
    total_cbtes = len(egresos)
    total_neto = sum(float(e.get("monto_neto") or 0) for e in egresos)
    total_iva = sum(float(e.get("iva_monto") or 0) for e in egresos)
    total_total = sum(float(e.get("total") or 0) for e in egresos)
    por_tasa = {}

    for e in egresos:
        iva = float(e.get("iva_monto") or 0)
        neto = float(e.get("monto_neto") or 0)
        # En puntos, igual que el resumen de ventas: agrupar por el campo crudo
        # partía el resumen en una tasa "0.21" que la tarjeta muestra como
        # "0.21%", y separaba las filas viejas de las nuevas.
        pct = _alicuota_de_egreso(neto, iva)
        if pct not in por_tasa:
            por_tasa[pct] = {"neto": 0.0, "iva": 0.0, "cbtes": 0}
        por_tasa[pct]["neto"] += neto
        por_tasa[pct]["iva"] += iva
        por_tasa[pct]["cbtes"] += 1

    return {
        "cbtes": total_cbtes, "neto": total_neto,
        "iva": total_iva, "total": total_total,
        "por_tasa": dict(sorted(por_tasa.items())),
    }
