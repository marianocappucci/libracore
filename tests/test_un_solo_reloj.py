"""Un solo reloj para «hoy» y «ahora»: el cobro, el turno y el cierre caen el mismo día de Argentina (ADR-070).

El motor tenía dos relojes. `datetime.date.today()` (el del **proceso**: depende de la `TZ` del contenedor) fechaba el
cobro, la factura, el recibo, el egreso y el movimiento de stock; `_ar_now()` (UTC-3) fechaba el turno y el cierre.
Con el contenedor en UTC, entre las 21:00 y la medianoche de Argentina daban días distintos: un cobro con fecha de
mañana dentro de un turno de hoy. La suite no lo veía porque corre con la `TZ` de la máquina y a la hora que sea.

Estos tests fijan las dos cosas que lo causan, sin depender de la máquina:

- la `TZ` del proceso en UTC (`time.tzset()`), que es lo que corre un contenedor sin `TZ`;
- el instante: 23:30 de Argentina del 2026-10-17, que son las 02:30 UTC del 2026-10-18.

El instante se fija en `fechas._ahora_utc`, lo único del motor que mira el reloj del sistema. Y el último test
recorre el código de producción buscando relojes sueltos (`date.today()`, `datetime.now()` sin zona): el defecto
vive en cada lugar que decide una fecha, así que no alcanza con arreglar los que se conocían.
"""

from __future__ import annotations

import ast
import datetime
import os
import time
from pathlib import Path

import pytest
from test_cobros import FACTURA_CONTADO, Escrituras

from libracore import cobros, fechas
from libracore.db import cierre_diario as cd
from libracore.db import core
from libracore.db import turnos as db_turnos
from libracore.db.schema import init_core_schema

#: Las 23:30 del 2026-10-17 en Argentina: ya es el 2026-10-18 en UTC.
INSTANTE = datetime.datetime(2026, 10, 18, 2, 30, tzinfo=datetime.UTC)
DIA_AR = "2026-10-17"
DIA_UTC = "2026-10-18"


@pytest.fixture
def noche_en_un_contenedor_en_utc(monkeypatch):
    """El proceso en UTC y el reloj clavado a las 23:30 de Argentina."""
    anterior = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    monkeypatch.setattr(fechas, "_ahora_utc", lambda: INSTANTE)
    try:
        yield
    finally:
        if anterior is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = anterior
        time.tzset()


def test_el_escenario_es_el_del_defecto(noche_en_un_contenedor_en_utc):
    """Control del test: para el reloj del proceso ya es mañana, y para Argentina todavía es hoy."""
    assert datetime.datetime.fromtimestamp(INSTANTE.timestamp()).date().isoformat() == DIA_UTC
    assert fechas.hoy().isoformat() == DIA_AR


def test_hoy_ahora_y_ar_now_dan_el_mismo_instante_argentino(noche_en_un_contenedor_en_utc):
    assert fechas.ahora().isoformat() == "2026-10-17T23:30:00-03:00"
    assert fechas.hoy() == datetime.date(2026, 10, 17)
    assert core._ar_now() == "2026-10-17 23:30:00"
    assert core._ar_now()[:10] == fechas.hoy().isoformat()


def test_ahora_sin_fijar_es_el_reloj_real_en_argentina():
    antes = datetime.datetime.now(datetime.UTC)
    ahora = fechas.ahora()
    despues = datetime.datetime.now(datetime.UTC)
    assert ahora.utcoffset() == datetime.timedelta(hours=-3)
    assert antes <= ahora <= despues


@pytest.fixture
def base(tmp_path):
    core.configure(db_path=str(tmp_path / "un_reloj.db"))
    c = core.get_connection()
    init_core_schema(c)
    cd.crear_tablas(c)
    cur = c.execute(
        "INSERT INTO usuarios (username, nombre, email, password_hash, role, activo)"
        " VALUES ('cajero', 'Cajero', '', 'sin-hash-real--este-test-no-autentica', 'admin', TRUE)")
    c.commit()
    yield cur.lastrowid
    c.close()
    core._db_path = None


def test_un_cobro_un_turno_y_un_cierre_caen_el_mismo_dia_de_argentina(noche_en_un_contenedor_en_utc, base):
    """El cruce que fallaba: a las 23:30 de Argentina, con el proceso en UTC, el cobro salía con fecha de mañana."""
    turno_id = db_turnos.create_turno(base, 0.0)
    turno = db_turnos.get_turno(turno_id)

    # Sin `fecha=`: la fecha del cobro la pone el motor, que es lo que hace el router.
    esc = Escrituras()
    cobros.registrar_cobro_factura(
        FACTURA_CONTADO, [{"medio_id": "efectivo", "monto": 90000.0}], caja_id=1, usuario_id=base,
        crear_movimiento=esc.crear_movimiento, crear_cc_pago=esc.crear_cc_pago,
        resolver_cliente=esc.resolver_cliente, leer_estado=esc.leer_estado, tipo_label="FACTURA C")
    dia_del_cobro = esc.movimientos[0]["fecha"]

    previa = cd.preview_cierre_dia(None)
    dia_del_cierre = previa["fecha"]

    assert (dia_del_cobro, turno["apertura"][:10], dia_del_cierre) == (DIA_AR, DIA_AR, DIA_AR)
    # y el cierre de ese día ve el turno de ese día
    assert [t["id"] for t in previa["turnos_abiertos"]] == [turno_id]


def test_el_cobro_dentro_del_turno_tambien_es_del_dia_de_argentina(noche_en_un_contenedor_en_utc):
    """Con turno abierto el cobro ignora la `fecha` del pedido y usa la del motor (cobros.py, rama del turno)."""
    esc = Escrituras()
    cobros.registrar_cobro_factura(
        FACTURA_CONTADO, [{"medio_id": "efectivo", "monto": 90000.0}], fecha="1999-01-01", caja_id=1, usuario_id=2,
        crear_movimiento=esc.crear_movimiento, crear_cc_pago=esc.crear_cc_pago,
        resolver_cliente=esc.resolver_cliente, leer_estado=esc.leer_estado, tipo_label="FACTURA C",
        turno_de=lambda c: {"id": 7, "caja_id": 3, "usuario_id": 2})
    assert esc.movimientos[0]["fecha"] == DIA_AR


@pytest.mark.parametrize("modulo, funcion", [
    ("libracore.recibos", "_hoy"),
    ("libracore.egresos_router", "_hoy"),
])
def test_los_hoy_de_cada_modulo_salen_del_reloj_unico(noche_en_un_contenedor_en_utc, modulo, funcion):
    import importlib

    assert getattr(importlib.import_module(modulo), funcion)() == DIA_AR


def test_un_movimiento_de_stock_sin_fecha_se_fecha_en_argentina(noche_en_un_contenedor_en_utc, base):
    from libracore.db import stock
    from libracore.db.productos import create_producto

    pid = create_producto("Bulto")
    stock.add_movimiento_stock(pid, "ajuste", 1.0)
    with core.get_connection() as c:
        fecha = c.execute("SELECT fecha FROM movimientos_stock WHERE producto_id=?", (pid,)).fetchone()[0]
    assert fecha == DIA_AR


# ── El patrón: ningún reloj suelto en el código de producción ────────────────────────────────────────────────────

RAIZ = Path(__file__).resolve().parents[1] / "libracore"

#: Relojes que NO deciden una fecha de negocio: marcas técnicas (nombre de un archivo, `built-at` de una imagen, la
#: antigüedad de un respaldo). Cada entrada dice por qué se queda con el reloj del proceso.
MARCAS_TECNICAS = {
    "respaldo.py": "nombre del archivo de respaldo y mtime de los archivos del disco",
    "admin/services.py": "nombre del archivo de respaldo",
    "resguardo_estado.py": "antigüedad de la última copia externa: ambos extremos son relojes del proceso",
    "provisioning/__init__.py": "etiqueta de versión de la imagen y su `built-at`",
    "provisioning/panel_admin.py": "nombres de volcados, corte de retención y `desplegado_at` del panel",
    "provisioning/resguardo_externo.py": "marca de la subida a la nube y retención de copias",
}


#: Tests que leen el reloj del proceso a propósito: `test_config_router` arma un `cuando` técnico, y este mismo
#: archivo clava el instante a mano.
TESTS_CON_RELOJ_PROPIO = {"test_config_router.py", "test_un_solo_reloj.py"}


def _relojes_sueltos(arbol: ast.AST) -> list[int]:
    """Líneas con `X.today()`, `X.utcnow()` o `X.now()` sin zona: el reloj del proceso (o uno UTC naive)."""
    lineas = []
    for nodo in ast.walk(arbol):
        if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)):
            continue
        nombre = nodo.func.attr
        if nombre in ("today", "utcnow") or (nombre == "now" and not nodo.args and not nodo.keywords):
            lineas.append(nodo.lineno)
    return lineas


def test_ningun_modulo_de_produccion_decide_la_fecha_con_el_reloj_del_proceso():
    sueltos = {}
    for ruta in sorted(RAIZ.rglob("*.py")):
        relativa = ruta.relative_to(RAIZ).as_posix()
        if relativa in MARCAS_TECNICAS or relativa.startswith(("migrations/versions/", "testing/")):
            continue
        lineas = _relojes_sueltos(ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta)))
        if lineas:
            sueltos[relativa] = lineas
    assert not sueltos, (
        "Estos módulos leen el reloj del proceso (depende de la TZ del contenedor). Para una fecha de negocio usá "
        f"`libracore.fechas.hoy()` / `ahora()` (ADR-070); si es una marca técnica, anotala en MARCAS_TECNICAS: {sueltos}")


def test_los_tests_tampoco_comparan_contra_el_reloj_del_proceso():
    """Un test que arma su «hoy» con `date.today()` falla en el CI (UTC) entre las 21:00 y la medianoche de Argentina."""
    sueltos = {}
    for ruta in sorted(RAIZ.parent.joinpath("tests").rglob("*.py")):
        lineas = [n for n in _relojes_sueltos(ast.parse(ruta.read_text(encoding="utf-8"))) if n]
        if lineas and ruta.name not in TESTS_CON_RELOJ_PROPIO:
            sueltos[ruta.name] = lineas
    assert not sueltos, f"Usá `libracore.fechas.hoy()` o `tests/fechas.py` (ADR-070): {sueltos}"


def test_el_huso_de_argentina_se_define_una_sola_vez():
    """`timezone(timedelta(hours=-3))` vive en `fechas.ZONA_AR`; los demás módulos lo importan."""
    copias = []
    for ruta in sorted(RAIZ.rglob("*.py")):
        relativa = ruta.relative_to(RAIZ).as_posix()
        if relativa in ("fechas.py", "migrations/versions/0003_created_at_en_hora_de_argentina.py"):
            continue
        for nodo in ast.walk(ast.parse(ruta.read_text(encoding="utf-8"))):
            if (isinstance(nodo, ast.Call) and getattr(nodo.func, "id", getattr(nodo.func, "attr", "")) == "timedelta"
                    and any(isinstance(k.value, ast.UnaryOp) and isinstance(k.value.operand, ast.Constant)
                            and k.value.operand.value == 3 for k in nodo.keywords if k.arg == "hours")):
                copias.append(f"{relativa}:{nodo.lineno}")
    assert not copias, f"Otra definición de UTC-3; usá libracore.fechas.ZONA_AR: {copias}"


def test_la_lista_de_marcas_tecnicas_no_tiene_archivos_de_mas():
    """Una entrada que ya no tiene reloj suelto es una excepción que sobra: se saca."""
    de_mas = [
        r for r in MARCAS_TECNICAS
        if not _relojes_sueltos(ast.parse((RAIZ / r).read_text(encoding="utf-8")))
    ]
    assert not de_mas, f"Estos archivos ya no leen el reloj del proceso, sacalos de MARCAS_TECNICAS: {de_mas}"
