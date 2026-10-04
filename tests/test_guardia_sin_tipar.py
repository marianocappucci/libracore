"""La guardia de cuerpos sin tipar (ADR-015): hermana de la de booleanos (ADR-013), que sólo ve los campos con tipo.

Dos mitades, como todas las guardias: sobre una app de juguete con cada caso (los que **tienen** que aparecer y los tipados que **no**), y sobre la app con todas las factories del motor, donde se
fija el conjunto conocido para que un cuerpo sin tipar nuevo obligue a mirarlo.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

import pytest
from fastapi import APIRouter, Body, Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, JsonValue
from test_guardia_booleanos import _app_del_motor
from typing_extensions import TypedDict

from libracore.testing import campos_numericos_que_aceptan_booleano, cuerpos_sin_tipar

# ═══════════════════════════════════════════════════ (1) Una app de juguete con cada caso


class Tipado(BaseModel):
    nombre: str
    monto: float = 0


class ItemTipado(BaseModel):
    qty: int


class ConLista(BaseModel):
    items: list[ItemTipado]
    notas: dict[str, str] = {}
    pesos: dict[str, float] = {}


class Abierto(BaseModel):
    """`extra="allow"`: cada producto le suma claves propias, sin tipo."""
    model_config = ConfigDict(extra="allow")
    nombre: str


class AbiertoPorHerencia(Abierto):
    otro: int = 0


class Cerrado(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nombre: str


class ConDictAdentro(BaseModel):
    nombre: str
    pagos: list[dict]
    extra: dict[str, Any] = {}
    sub: Abierto | None = None
    bien: list[ItemTipado] = []


class ConAbiertosEnLista(BaseModel):
    filas: list[Abierto]


class Recursivo(BaseModel):
    hijos: list[Recursivo] = []
    carga: Any = None


class ConJson(BaseModel):
    carga: JsonValue
    otro: list[JsonValue] = []


class ConTypedDict(TypedDict):
    nombre: str


def _dependencia_con_request(request: Request):
    return None


def _dependencia_con_cuerpo(payload: Tipado):
    return payload


def _router() -> APIRouter:
    r = APIRouter()

    # ── Los que tienen que aparecer ──
    @r.post("/dict")
    def a(payload: dict): ...

    @r.post("/lista-desnuda")
    def b(payload: list): ...

    @r.post("/lista-de-dict")
    def c(payload: list[dict]): ...

    @r.post("/dict-any")
    def d(payload: dict[str, Any]): ...

    @r.post("/any")
    def e(payload: Any = Body(...)): ...

    @r.post("/object")
    def f(payload: object = Body(...)): ...

    @r.post("/json")
    def g(payload: JsonValue): ...

    @r.post("/opcional")
    def h(payload: Optional[dict] = None): ...  # noqa: UP045

    @r.post("/dict-de-lista-de-dict")
    def i(payload: dict[str, list[dict]]): ...

    @r.post("/extra-allow")
    def j(payload: Abierto): ...

    @r.post("/extra-allow-heredado")
    def k(payload: AbiertoPorHerencia): ...

    @r.put("/dentro-de-un-modelo")
    def modelo_con_dict(payload: ConDictAdentro): ...

    @r.patch("/abiertos-en-lista")
    def m(payload: ConAbiertosEnLista): ...

    @r.post("/json-en-un-modelo")
    def json_en_un_modelo(payload: ConJson): ...

    @r.post("/recursivo")
    def n(payload: Recursivo): ...

    @r.post("/dos-cuerpos")
    def o(uno: dict, dos: Tipado): ...

    @r.post("/embebido")
    def p(payload: Annotated[dict, Body(embed=True)]): ...

    @r.get("/get-con-cuerpo")
    def q(filtro: Annotated[dict, Body()] = {}): ...  # noqa: B006

    # ── Los endpoints que declaran Request y no tienen cuerpo tipado ──
    @r.post("/request")
    def r1(request: Request): ...

    @r.delete("/request-delete")
    def r2(request: Request): ...

    @r.put("/request-con-path/{x}")
    def r3(x: int, request: Request): ...

    # ── Los tipados, que NO tienen que aparecer ──
    @r.post("/tipado")
    def t1(payload: Tipado): ...

    @r.post("/lista-tipada")
    def t2(payload: list[Tipado]): ...

    @r.post("/lista-de-int")
    def t3(payload: list[int]): ...

    @r.post("/dict-de-str-a-int")
    def t4(payload: dict[str, int]): ...

    @r.post("/modelo-con-mapas-tipados")
    def t5(payload: ConLista): ...

    @r.post("/cerrado")
    def t6(payload: Cerrado): ...

    @r.post("/literal-y-union")
    def t7(modo: Literal["a", "b"], payload: Tipado | None = None): ...

    @r.post("/typeddict")
    def t8(payload: ConTypedDict): ...

    @r.post("/request-con-cuerpo-tipado")
    def t9(payload: Tipado, request: Request): ...

    @r.get("/get-con-request")
    def t10(request: Request): ...

    @r.post("/dependencia-con-request", dependencies=[Depends(_dependencia_con_request)])
    def t11(payload: Tipado): ...

    @r.post("/request-pero-la-dependencia-trae-el-cuerpo")
    def t12(request: Request, cuerpo: Tipado = Depends(_dependencia_con_cuerpo)): ...

    @r.post("/sin-cuerpo")
    def t13(x: int = 0): ...

    @r.post("/pesos")
    def t14(payload: dict[str, float]): ...

    return r


def _app(*routers, prefijos=None) -> FastAPI:
    app = FastAPI()
    for i, r in enumerate(routers):
        app.include_router(r, prefix=(prefijos or {}).get(i, ""))
    return app


def _hallazgos(app, **kw) -> dict[str, list[tuple[str, str]]]:
    """`{metodo y ruta: [(campo, tipo)]}`, para leer el resultado por ruta."""
    salida: dict[str, list[tuple[str, str]]] = {}
    for sitio, campo, tipo in cuerpos_sin_tipar(app, **kw):
        salida.setdefault(sitio, []).append((campo, tipo))
    return salida


def test_cada_caso_sin_tipar_aparece_con_su_campo_y_su_tipo():
    h = _hallazgos(_app(_router()))
    assert h["POST /dict"] == [("payload", "dict")]
    assert h["POST /lista-desnuda"] == [("payload", "list")]
    assert h["POST /lista-de-dict"] == [("payload", "list[dict]")]
    assert h["POST /dict-any"] == [("payload", "dict[str, Any]")]
    assert h["POST /any"] == [("payload", "Any")]
    assert h["POST /object"] == [("payload", "object")]
    assert h["POST /json"] == [("payload", "JsonValue")]
    assert h["POST /opcional"] == [("payload", "dict | None")]
    assert h["POST /dict-de-lista-de-dict"] == [("payload", "dict[str, list[dict]]")]
    assert h["POST /embebido"] == [("payload", "dict")]
    assert h["GET /get-con-cuerpo"] == [("filtro", "dict")]


def test_extra_allow_se_nombra_en_el_tipo_y_se_ve_tambien_si_se_hereda():
    h = _hallazgos(_app(_router()))
    assert h["POST /extra-allow"] == [("payload", 'Abierto(extra="allow")')]
    assert h["POST /extra-allow-heredado"] == [("payload", 'AbiertoPorHerencia(extra="allow")')]


def test_baja_por_los_modelos_anidados_y_marca_el_campo_de_adentro():
    h = _hallazgos(_app(_router()))
    assert sorted(h["PUT /dentro-de-un-modelo"]) == [
        ("extra", "dict[str, Any]"),
        ("pagos", "list[dict]"),
        ("sub", 'Abierto(extra="allow") | None'),
    ]
    # Un modelo con extra="allow" dentro de una lista: el campo es `filas` y el tipo dice cuál es.
    assert h["PATCH /abiertos-en-lista"] == [("filas", 'list[Abierto(extra="allow")]')]


def test_json_value_se_ve_como_parametro_y_como_campo_de_un_modelo():
    """FastAPI expande el `JsonValue` de un parámetro a una unión con una marca de pydantic: se lo reconoce por ella, y también tal cual dentro de un modelo."""
    assert _hallazgos(_app(_router()))["POST /json-en-un-modelo"] == [("carga", "JsonValue"), ("otro", "list[JsonValue]")]


def test_un_modelo_recursivo_no_cuelga_la_guardia():
    assert _hallazgos(_app(_router()))["POST /recursivo"] == [("carga", "Any")]


def test_con_dos_cuerpos_el_nombre_del_parametro_va_delante_y_el_tipado_no_aparece():
    assert _hallazgos(_app(_router()))["POST /dos-cuerpos"] == [("uno", "dict")]


def test_un_endpoint_con_request_y_sin_cuerpo_tipado_se_marca_en_post_put_patch_y_delete():
    h = _hallazgos(_app(_router()))
    marca = "request-sin-cuerpo-tipado"
    assert h["POST /request"] == [("request", marca)]
    assert h["DELETE /request-delete"] == [("request", marca)]
    assert h["PUT /request-con-path/{x}"] == [("request", marca)]


@pytest.mark.parametrize("sitio", [
    "POST /tipado", "POST /lista-tipada", "POST /lista-de-int", "POST /dict-de-str-a-int", "POST /modelo-con-mapas-tipados", "POST /cerrado",
    "POST /literal-y-union", "POST /typeddict", "POST /request-con-cuerpo-tipado", "GET /get-con-request", "POST /dependencia-con-request",
    "POST /request-pero-la-dependencia-trae-el-cuerpo", "POST /sin-cuerpo", "POST /pesos",
])
def test_lo_tipado_no_aparece(sitio):
    """Las dos mitades de cada caso: sin estos, la guardia pasaría igual informando todo."""
    assert sitio not in _hallazgos(_app(_router()))


def test_el_resultado_sale_ordenado_y_sin_repetidos_y_son_tuplas_de_tres_textos():
    r = _router()
    app = _app(r, r)  # el mismo router dos veces: la misma ruta no se repite
    salida = cuerpos_sin_tipar(app)
    assert salida == sorted(set(salida))
    assert all(isinstance(x, tuple) and len(x) == 3 and all(isinstance(y, str) for y in x) for x in salida)


def test_el_prefijo_del_include_router_y_un_mount_forman_parte_de_la_ruta():
    interna = FastAPI()
    interna.include_router(_router())
    app = FastAPI()
    app.include_router(_router(), prefix="/api")
    app.mount("/sub", interna)
    h = _hallazgos(app)
    assert "POST /api/dict" in h and "POST /dict" not in h
    assert "POST /sub/dict" in h and "POST /sub/request" in h


def test_un_apirouter_suelto_tambien_se_puede_medir():
    assert ("POST /dict", "payload", "dict") in cuerpos_sin_tipar(_router())


def test_ignorar_acepta_la_ruta_con_o_sin_metodo_y_no_oculta_lo_demas():
    app = _app(_router())
    todo = cuerpos_sin_tipar(app)
    con_metodo = cuerpos_sin_tipar(app, ignorar={("POST /dict", "payload")})
    sin_metodo = cuerpos_sin_tipar(app, ignorar={("/dict", "payload")})
    assert con_metodo == sin_metodo == [x for x in todo if x[:2] != ("POST /dict", "payload")]
    assert ("POST /dict", "payload", "dict") in todo
    # El Request también se ignora con el nombre del parámetro.
    assert ("POST /request", "request", "request-sin-cuerpo-tipado") not in cuerpos_sin_tipar(app, ignorar={("POST /request", "request")})
    # Una entrada que no coincide con nada no avisa y no cambia nada.
    assert cuerpos_sin_tipar(app, ignorar={("POST /no-existe", "x")}) == todo


def test_ignorar_mal_formado_es_un_error_y_no_se_ignora_en_silencio():
    app = _app(_router())
    for malo in ({("POST /dict",)}, {"POST /dict"}, {("POST /dict", 1)}, {("a", "b", "c")}):
        with pytest.raises(ValueError):
            cuerpos_sin_tipar(app, ignorar=malo)


def test_una_app_sin_nada_sin_tipar_da_una_lista_vacia():
    r = APIRouter()

    @r.post("/x")
    def x(payload: Tipado): ...

    @r.get("/y")
    def y(n: int = 0): ...

    assert cuerpos_sin_tipar(_app(r)) == []


def test_comparte_el_recorrido_con_la_guardia_de_booleanos():
    """Mismas rutas con el mismo mecanismo: lo que la guardia de booleanos mide (`dict[str, float]`) la de cuerpos sin tipar no lo informa (no está sin tipar) y lo que ésta informa
    (`dict`) la otra no lo puede medir. Se complementan, no se pisan."""
    r = APIRouter()

    @r.post("/pesos")
    def a(payload: dict[str, float]): ...

    @r.post("/libre")
    def b(payload: dict): ...

    app = _app(r)
    assert [x[:2] for x in campos_numericos_que_aceptan_booleano(app)] == [("POST /pesos", "payload{valor}")]
    assert [x[:2] for x in cuerpos_sin_tipar(app)] == [("POST /libre", "payload")]


# ═══════════════════════════════════════════════════ (2) El motor entero

#: Lo que hoy hay sin tipar en las factories de libracore, **cada uno con su porqué**. Un cuerpo sin tipar nuevo rompe `test_los_cuerpos_sin_tipar_del_motor_son_los_conocidos` y obliga a mirarlo:
#: o se tipa (con `sin_booleanos` en lo numérico, ADR-013) o se agrega acá con su justificación.
CONOCIDOS_DEL_MOTOR = [
    # `FacturaPayload` con `extra="allow"` a propósito (ver su docstring): cada producto le suma sus campos propios (`categoria`, `sucursal`…) y el motor no los conoce. Los campos que el motor sí
    # usa (`tipo`, `concepto`, `client_id`, `tax_rate`, `items[]`...) están tipados y con `sin_booleanos`; lo de más no se lee ni numérico ni con efecto en el comprobante.
    ("POST /api/facturas", "payload", 'FacturaPayload(extra="allow")'),
    ("POST /api/facturas/borrador-pdf", "payload", 'FacturaPayload(extra="allow")'),
    # `CobroPayload.pagos` es `list[dict]`: el hueco que encontró el barrido de ADR-013. NO es aceptable sin más: lo cubre `rechazar_booleanos` en el modelo y en
    # `cobros.registrar_cobro_factura` (`{"monto": true}` da 422). Queda en la lista porque el tipo sigue siendo libre: lo que valide hay que mantenerlo a mano.
    ("POST /api/facturas/{factura_id}/cobrar", "pagos", "list[dict]"),
    # El webhook lo llama MercadoPago con el JSON que quiera: lo lee a mano a propósito (la firma se verifica sobre sus cabeceras y el id sale del cuerpo). Tolera cualquier forma (400/200,
    # nunca 500: ADR-015, test_mp_webhook) y no confía en el contenido: el importe y el estado salen de la API de MercadoPago con el id.
    ("POST /webhooks/mercadopago", "request", "request-sin-cuerpo-tipado"),
    # Declara `Request` sólo para armar la URL de vuelta del enlace OAuth (`_redirect_uri`): no lee ningún cuerpo. Falso positivo conocido.
    ("POST /api/config/resguardo-externo/enlace/{proveedor}", "request", "request-sin-cuerpo-tipado"),
]


def test_los_cuerpos_sin_tipar_del_motor_son_los_conocidos(tmp_path):
    """Un cuerpo sin tipar **nuevo** en cualquier `build_*_router` rompe esto: mirarlo, y tiparlo o sumarlo a `CONOCIDOS_DEL_MOTOR` con su justificación. Que desaparezca uno también lo rompe:
    se actualiza la lista (y se celebra)."""
    assert cuerpos_sin_tipar(_app_del_motor(tmp_path)) == sorted(CONOCIDOS_DEL_MOTOR)


def test_ignorar_los_conocidos_deja_la_app_del_motor_en_blanco(tmp_path):
    """La otra forma de usarla: `ignorar` con los conocidos y un `== []`. Las dos mitades: sin `ignorar` hay cinco, con él ninguno."""
    app = _app_del_motor(tmp_path)
    assert len(cuerpos_sin_tipar(app)) == len(CONOCIDOS_DEL_MOTOR) == 5
    assert cuerpos_sin_tipar(app, ignorar={(sitio, campo) for sitio, campo, _ in CONOCIDOS_DEL_MOTOR}) == []

