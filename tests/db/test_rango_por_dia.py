"""Los filtros de rango del motor miden por **día** (ADR-037), con la fila fechada con o sin hora.

🔴 Las columnas `fecha` son TEXT libre y los routers aceptan `fecha: str` sin validar, así que
llega `'2026-10-09 13:00:00'` o `'2026-10-09T13:00'`. `fecha <= '2026-10-09'` las dejaba afuera:
el último día del rango desaparecía del listado, del reporte o del libro IVA sin ningún error.

Cada test siembra, por tabla, el mismo día escrito de tres maneras más dos vecinos que NO
tienen que entrar (el día anterior a las 23:59 y el siguiente a las 00:00), y pide el rango
de ese único día. Corre en SQLite y, si hay `LIBRACORE_POSTGRES_URL`, en una base propia de
PostgreSQL (la comparación es sólo de texto: tiene que dar lo mismo en los dos).
"""
import pytest

from libracore.db import (
    caja,
    core,
    dashboard,
    egresos,
    facturas,
    libro_de_terceros,
    libros_iva,
    logs,
    recibos,
    reportes,
    resumen,
    stock,
    tesoreria,
    ventas,
)
from libracore.db.schema import init_core_schema

DIA = "2026-10-09"
#: El día en sus tres escrituras: sin hora, con hora y con `T`.
DEL_DIA = (DIA, DIA + " 13:00:00", DIA + "T13:00")
#: Vecinos que quedan afuera de un rango de un solo día.
ANTERIOR = "2026-10-08 23:59:59"
SIGUIENTE = "2026-10-10 00:00:00"
TODAS = DEL_DIA + (ANTERIOR, SIGUIENTE)


@pytest.fixture(params=["sqlite", "postgres"])
def conn(request, tmp_path):
    if request.param == "sqlite":
        destino = str(tmp_path / "rango.db")
    else:
        # Base propia del test (nunca la de LIBRACORE_POSTGRES_URL); se saltea sin la variable.
        destino = request.getfixturevalue("bases").nueva()
    core.configure(db_path=destino)
    c = core.get_connection()
    init_core_schema(c)
    c.commit()
    yield c
    c.close()
    core._db_path = None
    core._database_url = None


def test_ventas_del_ultimo_dia_con_hora(conn):
    for i, fecha in enumerate(TODAS):
        ventas.create_venta(f"V-{i}", fecha, [], 100, 0, 100, None, "Cli", None)

    filas = ventas.get_all_ventas(desde=DIA, hasta=DIA)
    assert sorted(v["fecha"] for v in filas) == sorted(DEL_DIA)

    r = reportes.get_reporte_resumen(DIA, DIA)
    assert r["ventas_cantidad"] == 3
    assert r["ventas_total"] == 300


def test_caja_reportes_y_resumen_del_ultimo_dia_con_hora(conn):
    for fecha in TODAS:
        conn.execute("INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, medio_pago) "
                     "VALUES (?, 'ingreso', 'Venta', 10, 'efectivo')", (fecha,))
    conn.commit()

    assert caja.get_caja_resumen(DIA, DIA)["ingresos"] == 30
    assert len(caja.get_caja_movimientos(DIA, DIA)) == 3
    assert resumen.get_resumen_core(DIA, DIA)["cobrado"] == 30
    assert dashboard.get_dashboard_data(DIA, DIA)["cobrado_mes"] == 30
    assert sum(f["total"] for f in reportes.get_reporte_caja_medios(DIA, DIA)) == 30
    assert reportes.get_reporte_resumen(DIA, DIA)["caja_saldo"] == 30


def test_tesoreria_del_ultimo_dia_con_hora_y_con_T(conn):
    # El parche viejo (`hasta + " 23:59:59"`) dejaba afuera la fila con `T`.
    cuenta = tesoreria.create_cuenta_tesoreria("Banco", "banco")
    for fecha in TODAS:
        tesoreria.create_movimiento_tesoreria(fecha, cuenta, "ingreso", 10, concepto=fecha)

    filas = tesoreria.get_movimientos_tesoreria(desde=DIA, hasta=DIA)
    assert sorted(m["fecha"] for m in filas) == sorted(DEL_DIA)


def test_egresos_y_libro_iva_compras_del_ultimo_dia_con_hora(conn):
    for fecha in TODAS:
        egresos.create_egreso(fecha, "Compra", 121, tipo_comprobante="factura")

    assert sorted(e["fecha"] for e in egresos.get_all_egresos(DIA, DIA)) == sorted(DEL_DIA)
    assert egresos.get_resumen_egresos(DIA, DIA)["total_periodo"] == 363
    assert sorted(e["fecha"] for e in libros_iva.get_egresos_para_iva(DIA, DIA)) == sorted(DEL_DIA)


def test_stock_del_ultimo_dia_con_hora(conn):
    pid = conn.execute("INSERT INTO productos (codigo, nombre, unidad, tipo, activo) "
                       "VALUES ('P', 'Prod', 'u', 'producto', 1)").lastrowid
    conn.commit()
    for fecha in TODAS:
        stock.add_movimiento_stock(pid, "compra", 1, fecha=fecha)

    filas = stock.get_movimientos_stock(desde=DIA, hasta=DIA)
    assert sorted(m["fecha"] for m in filas) == sorted(DEL_DIA)


def test_log_de_actividad_del_ultimo_dia_con_hora(conn):
    # El filtro va sobre la UNION de ocho SELECT: `fecha` es la columna de cada parte.
    for i, fecha in enumerate(TODAS):
        ventas.create_venta(f"V-{i}", fecha, [], 100, 0, 100, None, "Cli", None)

    filas = [f for f in logs.get_actividad_log(tipos=["venta"], desde=DIA, hasta=DIA)]
    assert sorted(f["fecha"] for f in filas) == sorted(DEL_DIA)


def test_un_rango_de_varios_dias_sigue_incluyendo_los_dos_extremos(conn):
    # Fechas sin hora: la semántica de siempre, [d, h] incluye ambos días.
    for i, fecha in enumerate(("2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10")):
        ventas.create_venta(f"V-{i}", fecha, [], 100, 0, 100, None, "Cli", None)

    filas = ventas.get_all_ventas(desde="2026-10-08", hasta="2026-10-09")
    assert sorted(v["fecha"] for v in filas) == ["2026-10-08", "2026-10-09"]


def test_facturas_del_ultimo_dia_con_hora(conn):
    # Listado, libro IVA ventas, resumen del panel y dashboard: cuatro consultas, un mismo criterio.
    for i, fecha in enumerate(TODAS):
        conn.execute("INSERT INTO facturas (tipo, punto_venta, numero, fecha, items, subtotal, iva_amount, total) "
                     "VALUES (6, 1, ?, ?, '[]', 100, 0, 100)", (i + 1, fecha))
    conn.commit()

    assert sorted(f["fecha"] for f in facturas.get_facturas_filtradas(DIA, DIA)["items"]) == sorted(DEL_DIA)
    assert sorted(f["fecha"] for f in libros_iva.get_facturas_para_iva(DIA, DIA)) == sorted(DEL_DIA)
    assert resumen.get_resumen_core(DIA, DIA)["facturado"] == 300
    assert dashboard.get_dashboard_data(DIA, DIA)["facturado_mes"] == 300
    assert dashboard.get_dashboard_data(DIA, DIA)["cant_facturas_mes"] == 3


def test_recibos_del_ultimo_dia_con_hora(conn):
    for i, fecha in enumerate(TODAS):
        recibos.create_recibo(fecha, "Cliente", "venta", 100, [{"medio": "efectivo", "monto": 100}], numero=i + 1)

    assert sorted(r["fecha"] for r in recibos.get_recibos(DIA, DIA)) == sorted(DEL_DIA)
    assert recibos.contar_recibos(DIA, DIA) == 3


def test_libro_de_terceros_del_ultimo_dia_con_hora(conn):
    for fecha in TODAS:
        libro_de_terceros.asentar(1, "cliente", fecha, "Venta", debe=10)

    assert libro_de_terceros.saldo(1, "cliente", hasta=DIA) == 40  # todo hasta el 9 inclusive: 4 de 5
    extracto = libro_de_terceros.extracto(1, "cliente", desde=DIA, hasta=DIA)
    assert sorted(a["fecha"] for a in extracto["asientos"]) == sorted(DEL_DIA)
    assert extracto["saldo_anterior"] == 10  # sólo el del día anterior


def test_el_extracto_con_hora_en_desde_no_cuenta_dos_veces(conn):
    # `desde` con hora: el saldo anterior es el de antes del DÍA, no de antes de esa hora. Si no, el
    # asiento de la mañana del 9 estaría en el saldo anterior y también en el período.
    libro_de_terceros.asentar(1, "cliente", DIA + " 08:00:00", "Mañana", debe=10)
    libro_de_terceros.asentar(1, "cliente", ANTERIOR, "Ayer", debe=5)

    extracto = libro_de_terceros.extracto(1, "cliente", desde=DIA + " 13:00:00", hasta=DIA)
    assert extracto["saldo_anterior"] == 5
    assert [a["concepto"] for a in extracto["asientos"]] == ["Mañana"]
    assert extracto["asientos"][0]["saldo"] == 15
