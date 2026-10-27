"""El libro de cuenta corriente de terceros: asientos de debe y haber por (tercero, rol). ADR-026.

Es **opcional y aparte** de la cuenta corriente de clientes de `cuenta_corriente`, que
sigue calculando el saldo desde los documentos (ventas, facturas, débitos y pagos) y
que los siete productos que la usan no cambian. Este libro es para un producto que
lleva cuentas **como libro**: cada hecho es un asiento, y el saldo es la suma del
debe menos la del haber (LibraCargo: clientes, fleteros y proveedores, con el gasto
de dos patas —el proveedor al debe y el fletero al haber en la misma transacción—).

- `tercero_id` no tiene FK y `rol` es texto: el tercero y sus roles son del
  producto. El motor no sabe qué es un fletero, y no tiene por qué.
- Un asiento mueve el debe o el haber, nunca los dos (lo dicen la base y `asentar`).
  Lo migrado de un sistema viejo (`origen_legado`) puede traer los dos en cero.
- **Corregir no es anular.** `corregir` cambia un asiento en el lugar, que es lo que
  hace un producto al editar el documento que lo originó (el gasto, el cobro).
  `contraasentar` deja el original y agrega otro con las columnas invertidas, que
  es como se revierte algo que ya pasó: la cuenta impresa antes se puede
  reconstruir después.

Todas las funciones aceptan `conn=` (ADR-025): con ella trabajan en la transacción
de quien llama y no confirman nada, así el producto asienta junto con su documento.

## La moneda (ADR-088)

Cada asiento dice en qué moneda está (`ARS` por defecto, `libracore.monedas`). **Una
cuenta no suma pesos con dólares**: `saldo` y `extracto` filtran por la moneda que se
les pide, y `saldos` devuelve una fila por tercero, rol **y moneda**. Sin `moneda`,
`saldo` y `extracto` toman todo, que es lo de siempre para quien sólo asienta en pesos;
quien lleva cuentas en más de una moneda la pasa siempre.
"""
from libracore import monedas
from libracore.db.core import Conexion
from libracore.db.facturas import _con
from libracore.fechas import dia_iso, rango_por_dia

#: Lo que `corregir` deja cambiar: todo lo que cambia cuando se edita el documento
#: que originó el asiento (también la cuenta: un cobro que pasa a ser de otro
#: tercero, o de otro rol). El origen en el legado y de qué asiento es contrapartida
#: no se corrigen: cambiarlos es otro asiento.
CORREGIBLES = frozenset({"fecha", "tercero_id", "rol", "concepto", "descripcion", "debe",
                         "haber", "factura_id"})


class AsientoInvalido(ValueError):
    """El asiento no se puede escribir así. El mensaje dice por qué."""


def _validar_importes(debe, haber, origen_legado) -> tuple[float, float]:
    debe, haber = debe or 0, haber or 0
    if debe < 0 or haber < 0:
        raise AsientoInvalido("El debe y el haber no son negativos: la columna dice el signo.")
    if origen_legado is None and not ((debe > 0) != (haber > 0)):
        raise AsientoInvalido("Un asiento mueve el debe o el haber, y uno solo.")
    return debe, haber


def asentar(tercero_id: int, rol: str, fecha: str, concepto: str, *, debe=0, haber=0,
            descripcion: str | None = None, factura_id: int | None = None,
            origen_legado: str | None = None, usuario_id: int | None = None,
            origen: str | None = None, moneda: str = monedas.PESOS,
            conn: Conexion | None = None) -> int:
    """Escribe un asiento y devuelve su id. `fecha` es `AAAA-MM-DD`.

    `origen` es el hecho que lo originó (`cc_pago:12`): lo usa la cuenta de clientes
    del motor (`libro_de_clientes`). Un producto que lleva su propio libro no lo pasa.
    """
    if not (rol or "").strip():
        raise AsientoInvalido("El asiento necesita el rol de la cuenta (cliente, proveedor...).")
    if not (concepto or "").strip():
        raise AsientoInvalido("El asiento necesita un concepto.")
    debe, haber = _validar_importes(debe, haber, origen_legado)
    try:
        moneda = monedas.validar(moneda)
    except monedas.MonedaInvalida as e:
        raise AsientoInvalido(str(e)) from None
    with _con(conn) as c:
        cur = c.execute(
            "INSERT INTO cc_asientos (fecha, tercero_id, rol, concepto, descripcion, debe, haber, "
            "factura_id, origen_legado, usuario_id, origen, moneda) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (fecha, tercero_id, rol.strip(), concepto.strip(), descripcion, debe, haber,
             factura_id, origen_legado, usuario_id, origen, moneda),
        )
        return cur.lastrowid


def get_asiento(asiento_id: int, *, conn: Conexion | None = None) -> dict | None:
    with _con(conn) as c:
        fila = c.execute("SELECT * FROM cc_asientos WHERE id = ?", (asiento_id,)).fetchone()
        return dict(fila) if fila else None


def corregir(asiento_id: int, *, conn: Conexion | None = None, **campos) -> dict:
    """Cambia un asiento **en el lugar** y lo devuelve. Sólo los campos de `CORREGIBLES`.

    Es lo que corresponde cuando se edita el documento que lo originó. Para revertir
    algo que ya pasó, `contraasentar`.
    """
    desconocidos = set(campos) - CORREGIBLES
    if desconocidos:
        raise AsientoInvalido(f"No se corrige {sorted(desconocidos)}: es otro asiento.")
    with _con(conn) as c:
        actual = get_asiento(asiento_id, conn=c)
        if actual is None:
            raise AsientoInvalido(f"No existe el asiento {asiento_id}.")
        nuevo = actual | campos
        if not str(nuevo["rol"] or "").strip():
            raise AsientoInvalido("El asiento necesita el rol de la cuenta (cliente, proveedor...).")
        _validar_importes(nuevo["debe"], nuevo["haber"], nuevo["origen_legado"])
        if campos:
            asignaciones = ", ".join(f"{k} = ?" for k in campos)
            c.execute(f"UPDATE cc_asientos SET {asignaciones} WHERE id = ?",
                      (*campos.values(), asiento_id))
        return get_asiento(asiento_id, conn=c)


def borrar(asiento_id: int, *, conn: Conexion | None = None) -> None:
    """Saca un asiento. Es lo que corresponde cuando el documento que lo originó **deja de
    mover la cuenta** al editarlo (un cobro que se queda sin tercero, una comisión que
    pasa a cero): un asiento de $0 no significa nada, y la base no lo admite.

    🔴 **No es la forma de anular.** Algo que ya pasó se revierte con `contraasentar`.
    Un asiento que tiene contrapartida no se borra: dejaría la reversión sin original.
    """
    with _con(conn) as c:
        if c.execute("SELECT 1 FROM cc_asientos WHERE contrapartida_de = ?",
                     (asiento_id,)).fetchone():
            raise AsientoInvalido(f"El asiento {asiento_id} tiene contrapartida: no se borra.")
        c.execute("DELETE FROM cc_asientos WHERE id = ?", (asiento_id,))


def contraasentar(asiento_id: int, *, fecha: str | None = None, concepto: str | None = None,
                  usuario_id: int | None = None, conn: Conexion | None = None) -> int:
    """Revierte un asiento con otro de columnas invertidas, y devuelve el id del nuevo.

    🔑 **Por defecto con la fecha del original**, no la de hoy: la reversión de algo
    que no debió pasar se fecha cuando pasó, o la cuenta mostraría entre el cargo y su
    reversión una deuda que ningún total del período reconoce (es lo que hace
    LibraCargo al anular un comprobante sin CAE o un gasto). Quien revierte un hecho
    nuevo —un cobro que se devuelve hoy— pasa la fecha.

    Un asiento se revierte una vez, y una contrapartida no se revierte. La reversión
    lleva el `origen` y la `moneda` del original: los dos son del mismo hecho.
    """
    with _con(conn) as c:
        original = get_asiento(asiento_id, conn=c)
        if original is None:
            raise AsientoInvalido(f"No existe el asiento {asiento_id}.")
        if original["contrapartida_de"] is not None:
            raise AsientoInvalido("Una contrapartida no se revierte: se revierte el original.")
        ya = c.execute("SELECT 1 FROM cc_asientos WHERE contrapartida_de = ?",
                       (asiento_id,)).fetchone()
        if ya:
            raise AsientoInvalido(f"El asiento {asiento_id} ya tiene contrapartida.")
        cur = c.execute(
            "INSERT INTO cc_asientos (fecha, tercero_id, rol, concepto, descripcion, debe, haber, "
            "factura_id, contrapartida_de, origen_legado, usuario_id, origen, moneda) "
            "VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,?)",
            (fecha or original["fecha"], original["tercero_id"], original["rol"],
             concepto or f"Reversión: {original['concepto']}", original["descripcion"],
             original["haber"], original["debe"], original["factura_id"], asiento_id, usuario_id,
             original.get("origen"), original.get("moneda") or monedas.PESOS),
        )
        return cur.lastrowid


def _rango(desde: str | None, hasta: str | None) -> tuple[str, tuple]:
    conds, params = rango_por_dia("fecha", desde, hasta)
    return "".join(f" AND {c}" for c in conds), tuple(params)


def _de_moneda(moneda: str | None) -> tuple[str, tuple]:
    """El filtro por moneda: vacío si no se pide (todas, lo de siempre)."""
    if moneda is None:
        return "", ()
    try:
        return " AND moneda = ?", (monedas.validar(moneda),)
    except monedas.MonedaInvalida as e:
        raise AsientoInvalido(str(e)) from None


def saldo(tercero_id: int, rol: str, *, hasta: str | None = None, moneda: str | None = None,
          conn: Conexion | None = None) -> float:
    """El debe menos el haber de la cuenta, hasta una fecha inclusive si se pide, y de una
    moneda si se pide (ADR-088: con más de una moneda en la cuenta, se pide siempre)."""
    filtro, params = _rango(None, hasta)
    filtro_moneda, params_moneda = _de_moneda(moneda)
    filtro, params = filtro + filtro_moneda, params + params_moneda
    with _con(conn) as c:
        fila = c.execute(
            "SELECT COALESCE(SUM(debe), 0) - COALESCE(SUM(haber), 0) FROM cc_asientos "
            f"WHERE tercero_id = ? AND rol = ?{filtro}", (tercero_id, rol, *params)).fetchone()
        return float(fila[0] or 0)


def extracto(tercero_id: int, rol: str, *, desde: str | None = None, hasta: str | None = None,
             moneda: str | None = None, conn: Conexion | None = None) -> dict:
    """Los asientos de la cuenta en el rango, con el saldo anterior y el saldo corrido.

    Devuelve `{"saldo_anterior": ..., "asientos": [... con "saldo"]}`, en orden de fecha
    y de alta. El saldo anterior es el de antes de `desde`: con él, un extracto de un
    mes cierra con el saldo real de la cuenta y no con la suma de ese mes.

    Con `moneda`, sólo los asientos de esa moneda, también en el saldo anterior (ADR-088).
    """
    filtro, params = _rango(desde, hasta)
    filtro_moneda, params_moneda = _de_moneda(moneda)
    filtro, params = filtro + filtro_moneda, params + params_moneda
    with _con(conn) as c:
        anterior = 0.0
        if desde:
            # «Antes de desde» es antes del DÍA de desde: el rango de abajo arranca en ese día
            # (`rango_por_dia`), y con la hora de `desde` en la comparación el mismo asiento
            # entraría en el saldo anterior y también en el período.
            dia = dia_iso(desde)
            fila = c.execute(
                "SELECT COALESCE(SUM(debe), 0) - COALESCE(SUM(haber), 0) FROM cc_asientos "
                f"WHERE tercero_id = ? AND rol = ? AND fecha < ?{filtro_moneda}",
                (tercero_id, rol, dia.isoformat() if dia else desde, *params_moneda)).fetchone()
            anterior = float(fila[0] or 0)
        filas = c.execute(
            f"SELECT * FROM cc_asientos WHERE tercero_id = ? AND rol = ?{filtro} ORDER BY fecha, id",
            (tercero_id, rol, *params)).fetchall()
    corrido, asientos = anterior, []
    for f in filas:
        d = dict(f)
        corrido += float(d["debe"] or 0) - float(d["haber"] or 0)
        asientos.append(d | {"saldo": round(corrido, 2)})
    return {"saldo_anterior": round(anterior, 2), "asientos": asientos}


def saldos(rol: str | None = None, *, incluir_en_cero: bool = False, moneda: str | None = None,
           conn: Conexion | None = None) -> list[dict]:
    """El saldo de cada cuenta, del rol pedido o de todos: `[{tercero_id, rol, moneda, saldo}]`.

    🔑 **Una fila por moneda** (ADR-088): un tercero con asientos en pesos y en dólares
    tiene dos saldos, nunca uno sumado. Con `moneda`, sólo los de esa moneda.
    """
    conds, params = [], []
    if rol:
        conds.append("rol = ?")
        params.append(rol)
    filtro_moneda, params_moneda = _de_moneda(moneda)
    if filtro_moneda:
        conds.append(filtro_moneda.removeprefix(" AND "))
        params.extend(params_moneda)
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    with _con(conn) as c:
        filas = c.execute(
            "SELECT tercero_id, rol, moneda, COALESCE(SUM(debe), 0) - COALESCE(SUM(haber), 0) AS saldo "
            f"FROM cc_asientos {where} GROUP BY tercero_id, rol, moneda ORDER BY rol, tercero_id, moneda",
            tuple(params)).fetchall()
    salida = [{"tercero_id": f[0], "rol": f[1], "moneda": f[2], "saldo": round(float(f[3] or 0), 2)}
              for f in filas]
    return salida if incluir_en_cero else [s for s in salida if s["saldo"] != 0]
