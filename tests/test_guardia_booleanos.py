"""La guardia contra booleanos en campos numéricos (ADR-013): `libracore.testing.campos_numericos_que_aceptan_booleano`.

Tres cosas. (1) **El motor entero está limpio**: se arma una app con TODAS las factories de router de libracore (con las opciones que suman rutas prendidas) y la lista de campos que aceptan
`true`/`false` tiene que ser VACÍA, sin excepciones. (2) **La guardia no es muda**: con `sin_booleanos`
anulado (en otro proceso, antes de importar los routers) ve los campos que ADR-013 arregló, así que un `[]` de (1) significa algo. (3) **La función en sí**, con routers de juguete: detecta un
campo `int`/`float`/`list[int]`/`dict[str, float]`/anidado sin el validador, no marca los que lo tienen, ni `bool`, `Decimal`, `StrictInt` ni `Literal`; las excepciones `ignorar`; los parámetros de
query y de path; y que recorre `include_router` con prefijo, los `Mount` y los payloads heredados.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter, Depends, FastAPI, Query
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator

from libracore import (
    arca_router,
    caja_router,
    clientes_router,
    comprobantes_router,
    config_router,
    consultar_cuit_router,
    cuenta_corriente_router,
    dashboard_router,
    egresos_router,
    facturas_router,
    feriados,
    geografia,
    libros_iva_router,
    logs_router,
    mp_bandeja_router,
    mp_config_router,
    mp_webhook,
    pre_facturas_router,
    presupuestos_router,
    recibos_router,
    remitos_router,
    reportes_router,
    resguardo_enlace,
    resumen_router,
    smtp_router,
    tema_router,
    tesoreria_router,
    ventas_cobro_router,
)
from libracore.respaldo import Instancia
from libracore.testing import campos_numericos_que_aceptan_booleano
from libracore.validacion import sin_booleanos
from libracore.venta_facturacion import PuertoDeVentas

RAIZ = Path(__file__).resolve().parent.parent

#: Lo que `sin_booleanos` arregla en cada ruta (el relevamiento de ADR-013): sin el helper, la guardia tiene que verlos todos. Los primeros 23 son los que encontró la guardia de libracommerce corrida
#: sobre VentaLibra; el resto (y `facturas_router`) lo sumó el relevamiento de todos los routers de libracore. Son 60 en total.
ESPERADOS_SIN_EL_VALIDADOR = {
    "POST /api/cajas": ["punto_venta", "sucursal_id"],
    "PUT /api/cajas/{cid}": ["punto_venta", "sucursal_id"],
    "POST /api/cierre-diario/cerrar": ["sucursal_id"],
    "POST /api/turnos/abrir": ["caja_id", "monto_inicial"],
    "POST /api/turnos/{tid}/cerrar": ["monto_declarado"],
    "POST /api/cuenta-corriente/{cliente_id}/pagar": ["caja_id", "facturas[]", "monto"],
    "POST /api/egresos": ["iva_pct", "monto_neto", "proveedor_id"],
    "POST /api/egresos/{eid}/pagar": ["caja_id", "monto"],
    "POST /api/tesoreria/cuentas": ["saldo_inicial"],
    "PUT /api/tesoreria/cuentas/{cid}": ["saldo_inicial"],
    "POST /api/tesoreria/cuentas/{cid}/movimiento": ["monto"],
    "POST /api/tesoreria/transferencia": ["cuenta_destino_id", "cuenta_origen_id", "monto"],
    "PUT /config/arca": ["punto_venta"],
    # Lo que sumó el relevamiento de todos los routers.
    "POST /api/caja": ["caja_id", "factura_id", "monto"],
    "POST /api/comprobantes-pendientes": ["cliente_id", "items[].iva_rate", "items[].qty", "items[].unit_price"],
    "POST /api/comprobantes-pendientes/facturar-prefill": ["ids[]"],
    "POST /api/comprobantes-pendientes/marcar-facturado": ["factura_id", "ids[]"],
    "POST /api/mp-bandeja/sincronizar": ["dias"],
    "POST /api/mp-bandeja/demo/sembrar": ["items[].monto"],
    "POST /api/presupuestos": ["client_id", "items[].qty", "items[].unit_price", "tax_rate"],
    "PUT /api/presupuestos/{pres_id}": ["client_id", "items[].qty", "items[].unit_price", "tax_rate"],
    "POST /api/remitos": ["client_id", "items[].qty"],
    # `facturas_router`: `tipo`, `concepto` y `punto_venta` viajan al comprobante que se pide a ARCA.
    "POST /api/facturas": ["tipo", "concepto", "punto_venta", "client_id", "tax_rate", "items[].qty", "items[].unit_price"],
    "POST /api/facturas/borrador-pdf": ["tipo", "concepto", "punto_venta", "client_id", "tax_rate", "items[].qty", "items[].unit_price"],
    "POST /api/facturas/{factura_id}/cobrar": ["caja_id"],
}


def _usuario():
    return {"id": 1, "role": "admin"}


def _sin_gate():
    return None


def _nada(*args, **kwargs):
    return None


def _routers_del_motor(carpeta: pathlib.Path):
    """Las factories `build_*_router` de libracore, con las opciones que agregan rutas o campos prendidas: `permitir_siembra_de_demo` (la ruta de siembra de la bandeja de MercadoPago sólo existe en una
    demo), `autorizar_reabrir` (el cierre diario sólo monta `/reabrir` con él) y `con_recibos`."""
    return [
        arca_router.build_arca_router(usuario_actual=_usuario),
        caja_router.build_caja_router(usuario_actual=_usuario),
        caja_router.build_cajas_router(),
        caja_router.build_turnos_router(usuario_actual=_usuario),
        caja_router.build_cierre_diario_router(usuario_actual=_usuario, autorizar_reabrir=Depends(_sin_gate)),
        clientes_router.build_clientes_router(),
        comprobantes_router.build_comprobantes_ingesta_router(),
        comprobantes_router.build_comprobantes_bandeja_router(),
        config_router.build_empresa_router(),
        config_router.build_empresa_admin_router(),
        config_router.build_backup_router(Instancia(nombre="x", bases=[carpeta / "x.db"]), carpeta / "backups"),
        consultar_cuit_router.build_consultar_cuit_router(usuario_actual=_usuario),
        cuenta_corriente_router.build_cuenta_corriente_router(usuario_actual=_usuario, solo_admin=_sin_gate),
        cuenta_corriente_router.build_cuenta_corriente_router(usuario_actual=_usuario, solo_admin=_sin_gate, con_recibos=True, prefix="/api/cc-con-recibos"),
        dashboard_router.build_dashboard_router(usuario_actual=_usuario),
        egresos_router.build_proveedores_router(),
        egresos_router.build_egresos_router(usuario_actual=_usuario),
        facturas_router.build_comprobantes_router(usuario_actual=_usuario, solo_admin=_sin_gate),
        facturas_router.build_comprobantes_pdf_router(usuario_actual=_usuario),
        libros_iva_router.build_libros_iva_router(),
        libros_iva_router.build_libros_iva_export_router(solo_admin=_sin_gate),
        logs_router.build_logs_router(usuarios=lambda: []),
        logs_router.build_logs_export_router(solo_admin=_sin_gate),
        mp_bandeja_router.build_mp_bandeja_router(permitir_siembra_de_demo=True),
        mp_config_router.build_mp_config_router(),
        mp_webhook.build_mp_webhook_router(),
        pre_facturas_router.build_pre_facturas_router(origen_producto="x"),
        presupuestos_router.build_presupuestos_router(usuario_actual=_usuario, generar_pdf=_nada, convertir_a_remito=_nada, smtp_configurado=_nada,
                                                      enviar_comprobante=_nada, moneda=str),
        recibos_router.build_recibos_router(usuario_actual=_usuario, solo_admin=_sin_gate),
        remitos_router.build_remitos_router(usuario_actual=_usuario, generar_pdf=_nada),
        reportes_router.build_reportes_router(),
        reportes_router.build_reportes_export_router(sesion=_sin_gate),
        resumen_router.build_resumen_router(identidad=dict, guard=_sin_gate),
        smtp_router.build_smtp_probe_router(_nada),
        tema_router.build_tema_router(),
        tema_router.build_tema_admin_router(),
        tesoreria_router.build_tesoreria_router(usuario_actual=_usuario),
        ventas_cobro_router.build_cobro_de_ventas_router(ventas=MagicMock(spec=PuertoDeVentas), usuario_actual=_usuario),
        geografia.build_geo_router(),
        feriados.build_feriados_router(),
        resguardo_enlace.build_resguardo_enlace_router(carpeta / "backups"),
    ]


def _app_del_motor(carpeta: pathlib.Path) -> FastAPI:
    app = FastAPI()
    for router in _routers_del_motor(carpeta):
        app.include_router(router)
    return app


# ═══════════════════════════════════════════════════ (1) El motor entero


def test_todas_las_factories_del_motor_estan_en_la_app_de_prueba(tmp_path):
    """Si alguien agrega un módulo con un `build_*_router` y no lo suma a `_routers_del_motor`, la guardia no lo mide: este test lo avisa."""
    import importlib
    import inspect
    import pkgutil

    import libracore

    montados = {ruta.endpoint.__module__ for router in _routers_del_motor(tmp_path) for ruta in router.routes if hasattr(ruta, "endpoint")}
    con_factory = set()
    for info in pkgutil.iter_modules(libracore.__path__):
        if info.ispkg:
            continue
        modulo = importlib.import_module(f"libracore.{info.name}")
        for nombre, funcion in inspect.getmembers(modulo, inspect.isfunction):
            if nombre.startswith("build_") and nombre.endswith("_router") and funcion.__module__ == modulo.__name__:
                con_factory.add(modulo.__name__)
    assert con_factory - montados == set(), sorted(con_factory - montados)


def test_ninguna_factory_del_motor_acepta_un_booleano_en_un_campo_numerico(tmp_path):
    app = _app_del_motor(tmp_path)
    assert campos_numericos_que_aceptan_booleano(app) == []


def test_la_guardia_ve_los_campos_que_adr_013_arreglo_si_se_anula_sin_booleanos(tmp_path):
    """Sin esto, el `[]` de arriba valdría igual con una guardia que no mide nada. Otro proceso (los routers se importan una vez) con `sin_booleanos` convertido en un no-op: tienen que aparecer
    los campos que ADR-013 arregló."""
    codigo = (
        "import json, pathlib, sys\n"
        "import libracore.validacion as v\n"
        "v.sin_booleanos = lambda *campos: None\n"
        "sys.path.insert(0, 'tests')\n"
        "from test_guardia_booleanos import _app_del_motor\n"
        "from libracore.testing import campos_numericos_que_aceptan_booleano as g\n"
        f"print(json.dumps(g(_app_del_motor(pathlib.Path({str(tmp_path)!r})))))\n"
    )
    entorno = {**os.environ, "PYTHONPATH": os.pathsep.join([str(RAIZ), str(RAIZ / "tests"), os.environ.get("PYTHONPATH", "")]), "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, cwd=RAIZ, env=entorno, timeout=120)
    assert r.returncode == 0, r.stderr
    hallados = {tuple(x) for x in json.loads(r.stdout.strip().splitlines()[-1])}
    for sitio, campos in ESPERADOS_SIN_EL_VALIDADOR.items():
        for campo in campos:
            assert any(h[0] == sitio and h[1] == campo for h in hallados), (sitio, campo)
    assert sum(len(v) for v in ESPERADOS_SIN_EL_VALIDADOR.values()) == 60


# ═══════════════════════════════════════════════════ (2) La función, con routers de juguete


class Anidado(BaseModel):
    valor: int


class AnidadoBueno(BaseModel):
    valor: int
    _no_son_booleanos = sin_booleanos("valor")


class Malo(BaseModel):
    """Todo lo que acepta un booleano: int, float, list[int], dict[str, float], un modelo anidado y una lista de modelos."""
    n: int
    f: float = 0
    opcional: int | None = None
    ids: list[int] = []
    precios: dict[str, float] = {}
    hijo: Anidado | None = None
    hijos: list[Anidado] = []
    con_rango: Annotated[int, Field(ge=2)] = 2


class Bueno(BaseModel):
    n: int
    f: float = 0
    opcional: int | None = None
    ids: list[int] = []
    precios: dict[str, float] = {}
    hijo: AnidadoBueno | None = None
    hijos: list[AnidadoBueno] = []
    con_rango: Annotated[int, Field(ge=2)] = 2
    _no_son_booleanos = sin_booleanos("n", "f", "opcional", "ids", "precios", "con_rango")


class QueNoSonNumerosSueltos(BaseModel):
    """Lo que la guardia no tiene que marcar nunca."""
    activo: bool = True
    estricto: StrictBool = True
    entero_estricto: StrictInt = 1
    tipo: Literal["a", "b"] = "a"
    monto: Decimal = Decimal(1)
    texto: str = "x"
    montos: list[Decimal] = []


def _router_malo() -> APIRouter:
    router = APIRouter()

    @router.post("/malo")
    def crear(payload: Malo):
        return {}

    return router


def _router_bueno() -> APIRouter:
    router = APIRouter()

    @router.post("/bueno")
    def crear(payload: Bueno):
        return {}

    @router.post("/otros")
    def otros(payload: QueNoSonNumerosSueltos):
        return {}

    return router


def _app(*routers, prefijos=None) -> FastAPI:
    app = FastAPI()
    for i, router in enumerate(routers):
        app.include_router(router, prefix=(prefijos or {}).get(i, ""))
    return app


def test_un_router_que_acepta_booleanos_se_detecta_campo_por_campo():
    hallados = campos_numericos_que_aceptan_booleano(_app(_router_malo()))
    assert hallados == [
        ("POST /malo", "con_rango", "int"),
        ("POST /malo", "f", "float"),
        ("POST /malo", "hijo.valor", "int"),
        ("POST /malo", "hijos[].valor", "int"),
        ("POST /malo", "ids[]", "int"),
        ("POST /malo", "n", "int"),
        ("POST /malo", "opcional", "int"),
        ("POST /malo", "precios{valor}", "float"),
    ]


def test_un_router_con_sin_booleanos_no_se_marca_y_tampoco_bool_decimal_strict_ni_literal():
    assert campos_numericos_que_aceptan_booleano(_app(_router_bueno())) == []


def test_ignorar_acepta_la_ruta_con_o_sin_metodo_y_no_oculta_lo_demas():
    app = _app(_router_malo())
    todo = campos_numericos_que_aceptan_booleano(app)
    assert ("POST /malo", "n", "int") in todo
    sin_n = campos_numericos_que_aceptan_booleano(app, ignorar={("POST /malo", "n")})
    assert sin_n == [x for x in todo if x[1] != "n"]
    sin_f = campos_numericos_que_aceptan_booleano(app, ignorar={("/malo", "f")})        # sin el método: vale para todos
    assert sin_f == [x for x in todo if x[1] != "f"]
    assert campos_numericos_que_aceptan_booleano(app, ignorar={("GET /malo", "n")}) == todo     # otro método: no coincide


def test_ignorar_mal_formado_es_un_error_y_no_se_ignora_en_silencio():
    app = _app(_router_malo())
    for roto in ({"n"}, {("POST /malo",)}, {("POST /malo", 1)}):
        with pytest.raises(ValueError, match="ignorar"):
            campos_numericos_que_aceptan_booleano(app, ignorar=roto)


def test_el_prefijo_del_include_router_y_un_mount_forman_parte_de_la_ruta():
    app = _app(_router_malo(), prefijos={0: "/api"})
    assert ("POST /api/malo", "n", "int") in campos_numericos_que_aceptan_booleano(app)
    sub = FastAPI()
    sub.include_router(_router_malo())
    raiz = FastAPI()
    raiz.mount("/otra", sub)
    assert ("POST /otra/malo", "n", "int") in campos_numericos_que_aceptan_booleano(raiz)


def test_un_apirouter_suelto_tambien_se_puede_medir():
    assert ("POST /malo", "n", "int") in campos_numericos_que_aceptan_booleano(_router_malo())


class ProductoDelProducto(BaseModel):
    """Lo que hace un producto: hereda el payload del motor y le suma un número propio sin `sin_booleanos`."""
    nombre: str = "x"
    precio_venta: float = 0
    _no_son_booleanos = sin_booleanos("precio_venta")


class ProductoConExtra(ProductoDelProducto):
    orden: int = 0


def test_un_payload_heredado_con_un_campo_nuevo_sin_el_validador_se_detecta():
    router = APIRouter()

    @router.post("/productos")
    def crear(payload: ProductoConExtra):
        return {}

    assert campos_numericos_que_aceptan_booleano(_app(router)) == [("POST /productos", "orden", "int")]
    assert campos_numericos_que_aceptan_booleano(_app(router), ignorar={("POST /productos", "orden")}) == []


def test_el_payload_de_caja_que_hereda_la_edicion_conserva_el_validador():
    """`CajaUpdatePayload` hereda de `CajaPayload`: los campos numéricos del padre siguen protegidos en la edición."""
    for modelo in (caja_router.CajaPayload, caja_router.CajaUpdatePayload):
        for campo in ("punto_venta", "sucursal_id"):
            with pytest.raises(ValueError, match=f"{campo} tiene que ser un número, no un booleano"):
                modelo(nombre="x", **{campo: True})


class SinElValidadorOtroMecanismo(BaseModel):
    """Rechaza el booleano por su cuenta (no con `sin_booleanos`): tampoco se marca. La guardia mide el resultado, no el mecanismo."""
    cantidad: float

    @field_validator("cantidad", mode="before")
    @classmethod
    def _numero(cls, valor):
        if isinstance(valor, bool):
            raise ValueError("no booleano")
        return valor


def test_se_mide_el_resultado_y_no_el_mecanismo():
    router = APIRouter()

    @router.post("/otro")
    def crear(payload: SinElValidadorOtroMecanismo):
        return {}

    assert campos_numericos_que_aceptan_booleano(_app(router)) == []


def test_query_path_y_cuerpo_suelto_se_miden_con_el_texto_true():
    """Un parámetro numérico de query o de path llega como texto y pydantic no convierte «true» en número: no se marca. Un `Query` con restricciones y una lista de enteros, tampoco."""
    router = APIRouter()

    @router.get("/x/{producto_id}")
    def leer(producto_id: int, limite: Annotated[int, Query(gt=0)] = 10, ids: Annotated[list[int] | None, Query()] = None,
             factor: float = 1.0):
        return {}

    assert campos_numericos_que_aceptan_booleano(_app(router)) == []


def test_un_parametro_de_dependencia_tambien_se_recorre():
    """El cuerpo que declara una dependencia (`Depends`) llega a la ruta igual que el propio."""
    def pagina(payload: Malo):
        return payload

    router = APIRouter()

    @router.post("/con-dependencia")
    def crear(pag=Depends(pagina)):
        return {}

    assert ("POST /con-dependencia", "n", "int") in campos_numericos_que_aceptan_booleano(_app(router))
