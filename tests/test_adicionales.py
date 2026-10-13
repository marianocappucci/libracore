"""Los adicionales fuera de la escalera (ADR-048): el catálogo, el abono por líneas, el dato de la instancia y el router.

Tests de tabla, como `test_abono.py` y `test_limites.py`: el `plans.py` del producto se reemplaza por un módulo falso, y la instancia por un
`config.json` en un directorio temporal. La parte que necesita PostgreSQL (el tipo real de `modulos.habilitado`) está en
`tests/db/test_adicionales_postgres.py`.
"""
import importlib
import json
import types

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore.abono import calcular_abono

TOKEN = "token-de-servicio-de-prueba"
SERVICIO = {"x-internal-auth": TOKEN}
ADMIN = {"x-rol": "admin"}


# ── los productos de juguete ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _plans(**atributos):
    m = types.ModuleType("plans")
    m.PLANES = ["basico", "pro"]
    m.PLAN_MODULOS = {"basico": {"ventas"}, "pro": {"ventas", "stock"}}
    for k, v in atributos.items():
        setattr(m, k, v)
    return m


def ventalibra():
    """Como VentaLibra: un plan, sucursales a $19.950 con una incluida, y `resguardo_externo`."""
    return _plans(
        PLANES=["unico"], PLAN_MODULOS={"unico": {"facturacion"}}, PLAN_PRECIOS={"unico": 39900}, ADDONS={"resguardo_externo"},
        ADICIONALES={
            "sucursal": {"label": "Sucursales", "precio": 19950, "unidad": "sucursal", "incluidas": {"unico": 1}, "limite": "sucursales"},
            "resguardo_externo": {"label": "Resguardo externo", "precio": None},
        })


def restolibra():
    """Como RestoLibra: offline por sucursal, sin precio fijado, en cualquier plan."""
    return _plans(
        PLAN_PRECIOS={"basico": 27000, "pro": 69000}, ADDONS={"resguardo_externo"},
        ADICIONALES={"offline": {"label": "Modo offline", "precio": None, "modulos": {"offline"}, "unidad": "sucursal", "limite": "sucursales_offline"}})


@pytest.fixture
def ad(tmp_path, monkeypatch):
    """El módulo `libracore.adicionales` con un `DATA_DIR` propio (el `config.json` de la instancia)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.adicionales as modulo
    import libracore.config_manager as cm
    importlib.reload(cm)
    importlib.reload(modulo)
    modulo.cm = cm
    return modulo


def _escribir_config(ad, datos):
    with open(ad.cm.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(datos, f)


def _leer_config(ad):
    with open(ad.cm.CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── el catálogo sintetizado: compatibilidad total ────────────────────────────────────────────────────────────────────────────────────

def test_un_producto_sin_nada_declarado_no_tiene_adicionales(ad):
    assert ad.catalogo(_plans()) == {}
    assert ad.modulos_de_adicionales(_plans()) == set()


def test_los_addons_se_sintetizan_como_si_no_sin_precio(ad):
    cat = ad.catalogo(_plans(ADDONS={"mayorista", "resguardo_externo"}))
    assert sorted(cat) == ["mayorista", "resguardo_externo"]
    m = cat["mayorista"]
    assert (m.precio, m.modulos, m.unidad, m.planes) == (None, frozenset({"mayorista"}), None, None)
    assert m.label == "Mayorista" and cat["resguardo_externo"].label == "Resguardo externo"


def test_las_unidades_por_plan_se_sintetizan_como_el_adicional_de_su_unidad(ad):
    cat = ad.catalogo(_plans(PLAN_UNIDADES_ADICIONALES={"pro": {"unidad": "sucursal", "incluidas": 1, "precio": 19950}}))
    s = cat["sucursal"]
    assert (s.unidad, s.precio, s.incluidas, s.limite, s.planes, s.modulos) == (
        "sucursal", 19950, {"pro": 1}, "sucursales", frozenset({"pro"}), frozenset())
    assert s.label == "Sucursales adicionales"


def test_la_unidad_con_dos_precios_segun_el_plan_no_se_inventa(ad):
    plans = _plans(PLAN_UNIDADES_ADICIONALES={"basico": {"unidad": "caja", "incluidas": 1, "precio": 10}, "pro": {"unidad": "caja", "incluidas": 2, "precio": 20}})
    with pytest.raises(ValueError, match="precios distintos"):
        ad.catalogo(plans)


def test_lo_declarado_pisa_a_lo_sintetizado_y_el_resto_se_conserva(ad):
    plans = _plans(ADDONS={"resguardo_externo", "mayorista"}, ADICIONALES={"mayorista": {"label": "Venta mayorista", "precio": 5000}})
    cat = ad.catalogo(plans)
    assert cat["mayorista"].precio == 5000 and cat["mayorista"].label == "Venta mayorista"
    assert cat["resguardo_externo"].precio is None


def test_modulos_de_adicionales_es_un_superconjunto_de_addons(ad):
    plans = restolibra()
    assert ad.modulos_de_adicionales(plans) == {"resguardo_externo", "offline"}
    assert plans.ADDONS <= ad.modulos_de_adicionales(plans)


def test_un_adicional_por_unidades_sin_modulos_no_aporta_ninguno(ad):
    assert ad.modulos_de_adicionales(ventalibra()) == {"resguardo_externo"}


# ── las declaraciones mal formadas se ven al primer uso ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("entrada, texto", [
    ({"x": {"precio": -1}}, "entero >= 0"),
    ({"x": {"precio": True}}, "entero >= 0"),
    ({"x": {"precio": "mucho"}}, "entero >= 0"),
    ({"x": {"precio": 1, "cantidad": 3}}, "claves desconocidas"),
    ({"x": {"incluidas": {"basico": 1}}}, "falta 'unidad'"),
    ({"x": {"limite": "tope"}}, "falta 'unidad'"),
    ({"x": {"unidad": "caja", "incluidas": {"basico": -1}}}, "entero >= 0"),
    ({"x": {"unidad": "caja", "incluidas": {"platino": 1}}}, "no existe"),
    ({"x": {"planes": {"platino"}}}, "no existe"),
    ({"x": {"modulos": {"ventas"}}}, "ya pertenecen a un plan"),
    ({"x": []}, "diccionario"),
])
def test_una_declaracion_invalida_es_value_error(ad, entrada, texto):
    with pytest.raises(ValueError, match=texto):
        ad.catalogo(_plans(ADICIONALES=entrada))


# ── resolver: módulos, límites y líneas ──────────────────────────────────────────────────────────────────────────────────────────────

def test_resolver_sin_contratar_nada_apaga_todo_y_no_cobra(ad):
    r = ad.resolver(restolibra(), "basico", {})
    assert r == {"modulos": {"offline": False, "resguardo_externo": False}, "limites": {}, "lineas_abono": [], "incompatibles": []}


def test_resolver_un_si_no_prende_su_modulo_y_deja_una_linea(ad):
    r = ad.resolver(restolibra(), "pro", {"resguardo_externo": True})
    assert r["modulos"] == {"offline": False, "resguardo_externo": True}
    assert [ln["clave"] for ln in r["lineas_abono"]] == ["resguardo_externo"]
    assert r["lineas_abono"][0]["cantidad"] == 1 and r["lineas_abono"][0]["a_cotizar"] is True


def test_resolver_unidades_con_modulo_prende_el_modulo_y_fija_el_limite(ad):
    r = ad.resolver(restolibra(), "basico", {"offline": 3})
    assert r["modulos"]["offline"] is True
    assert r["limites"] == {"sucursales_offline": 3}
    (linea,) = r["lineas_abono"]
    assert (linea["cantidad"], linea["incluidas"], linea["cobrables"], linea["precio"], linea["subtotal"], linea["a_cotizar"]) == (3, 0, 3, None, None, True)


@pytest.mark.parametrize("valor", [0, -1, True, False, None, "3", 2.5, 1001])
def test_resolver_una_cantidad_invalida_es_no_contratar(ad, valor):
    r = ad.resolver(restolibra(), "basico", {"offline": valor})
    assert r["modulos"]["offline"] is False and r["limites"] == {} and r["lineas_abono"] == []


def test_resolver_un_si_no_exige_true_y_no_un_numero(ad):
    assert ad.resolver(restolibra(), "basico", {"resguardo_externo": 1})["modulos"]["resguardo_externo"] is False


def test_resolver_una_clave_desconocida_es_error(ad):
    with pytest.raises(ValueError, match="desconocido"):
        ad.resolver(restolibra(), "basico", {"cohetes": True})


def test_resolver_marca_los_incompatibles_con_el_plan(ad):
    plans = _plans(ADICIONALES={"solo_pro": {"precio": 100, "planes": {"pro"}}, "libre": {"precio": 50}})
    r = ad.resolver(plans, "basico", {"solo_pro": True, "libre": True})
    assert r["incompatibles"] == ["solo_pro"]
    assert ad.resolver(plans, "pro", {"solo_pro": True, "libre": True})["incompatibles"] == []
    assert ad.incompatibles(plans, "basico", {"solo_pro": False, "libre": True}) == []  # lo no contratado no estorba


# ── el abono por líneas ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("contratadas", [1, 2, 3, 10])
def test_las_sucursales_dan_el_mismo_total_que_calcular_abono(ad, contratadas):
    """El test de regresión del ADR-048: el camino nuevo no cambia un peso del abono de VentaLibra (ADR-041)."""
    plans = ventalibra()
    r = ad.resolver(plans, "unico", {"sucursal": contratadas})
    nuevo = ad.calcular_abono_total(39900, r["lineas_abono"])
    assert nuevo["total"] == calcular_abono(39900, 1, 19950, contratadas)["total"]
    assert nuevo["total"] == 39900 + (contratadas - 1) * 19950
    assert nuevo["a_cotizar"] == []


def test_tres_sucursales_son_la_base_mas_dos_adicionales(ad):
    r = ad.resolver(ventalibra(), "unico", {"sucursal": 3})
    (linea,) = r["lineas_abono"]
    assert (linea["cantidad"], linea["incluidas"], linea["cobrables"], linea["precio"], linea["subtotal"]) == (3, 1, 2, 19950, 39900)
    assert ad.calcular_abono_total(39900, r["lineas_abono"])["total"] == 79800


def test_las_sucursales_incluidas_no_dejan_linea(ad):
    r = ad.resolver(ventalibra(), "unico", {"sucursal": 1})
    assert r["lineas_abono"] == []
    assert r["limites"] == {"sucursales": 1}
    assert ad.calcular_abono_total(39900, [])["total"] == 39900


def test_un_adicional_sin_precio_no_suma_y_queda_a_cotizar(ad):
    """`precio=None` ⇒ «a cotizar»: el total es lo que se sabe, y la pantalla dice qué falta cotizar."""
    r = ad.resolver(ventalibra(), "unico", {"sucursal": 2, "resguardo_externo": True})
    abono = ad.calcular_abono_total(39900, r["lineas_abono"])
    assert abono["extra"] == 19950 and abono["total"] == 59850
    assert abono["a_cotizar"] == ["resguardo_externo"]
    assert [ln["clave"] for ln in abono["lineas"]] == ["resguardo_externo", "sucursal"]


def test_offline_en_dos_sucursales_es_todo_a_cotizar(ad):
    r = ad.resolver(restolibra(), "basico", {"offline": 2})
    abono = ad.calcular_abono_total(27000, r["lineas_abono"])
    assert abono == {"base": 27000, "lineas": r["lineas_abono"], "extra": 0, "total": 27000, "a_cotizar": ["offline"]}


def test_un_plan_sin_precio_deja_el_total_en_none(ad):
    assert ad.calcular_abono_total(None, [])["total"] is None


def test_calcular_abono_total_no_comparte_las_lineas_con_quien_las_pasa(ad):
    lineas = ad.resolver(ventalibra(), "unico", {"sucursal": 2})["lineas_abono"]
    ad.calcular_abono_total(39900, lineas)["lineas"][0]["subtotal"] = 0
    assert lineas[0]["subtotal"] == 19950


# ── el dato de la instancia: cantidades en el config.json ────────────────────────────────────────────────────────────────────────────

def test_sin_dato_no_hay_cantidad(ad):
    assert ad.cantidad_contratada("offline") is None


def test_fijar_y_leer_una_cantidad_no_pisa_el_resto_de_la_config(ad):
    _escribir_config(ad, {"empresa_nombre": "Almacén de prueba"})
    assert ad.fijar_cantidad("offline", 2) == 2
    assert ad.cantidad_contratada("offline") == 2
    cfg = _leer_config(ad)
    assert cfg["adicionales"] == {"offline": 2} and cfg["empresa_nombre"] == "Almacén de prueba"


def test_quitar_la_ultima_cantidad_borra_la_clave_adicionales(ad):
    ad.fijar_cantidad("offline", 2)
    ad.fijar_cantidad("offline", None)
    assert "adicionales" not in _leer_config(ad)


def test_la_sucursal_sigue_en_limite_sucursales_y_no_en_adicionales(ad):
    """ADR-041: ninguna instancia existente cambia de lugar su dato."""
    _escribir_config(ad, {"limite_sucursales": 3})
    assert ad.cantidad_contratada("sucursal") == 3
    ad.fijar_cantidad("sucursal", 5)
    cfg = _leer_config(ad)
    assert cfg["limite_sucursales"] == 5 and "adicionales" not in cfg
    ad.fijar_cantidad("sucursal", None)
    assert "limite_sucursales" not in _leer_config(ad)


@pytest.mark.parametrize("basura", ["mucho", 0, -3, True, False, 1.5, [], {}, None, 1001])
def test_basura_en_una_cantidad_se_lee_como_sin_dato(ad, basura):
    _escribir_config(ad, {"adicionales": {"offline": basura}, "limite_sucursales": basura})
    assert ad.cantidad_contratada("offline") is None
    assert ad.cantidad_contratada("sucursal") is None


@pytest.mark.parametrize("basura", ["texto", 7, ["offline"], None, True])
def test_un_adicionales_que_no_es_un_diccionario_se_lee_como_sin_dato(ad, basura):
    _escribir_config(ad, {"adicionales": basura})
    assert ad.cantidad_contratada("offline") is None
    ad.fijar_cantidad("offline", 1)  # y escribir encima lo repara
    assert _leer_config(ad)["adicionales"] == {"offline": 1}


@pytest.mark.parametrize("malo", [0, -1, 1001, "2", 1.5, True, False])
def test_fijar_una_cantidad_invalida_es_value_error_y_no_escribe(ad, malo):
    with pytest.raises(ValueError):
        ad.fijar_cantidad("offline", malo)
    assert ad.cantidad_contratada("offline") is None


def test_estado_de_un_si_no_depende_del_modulo(ad):
    a = ad.catalogo(restolibra())["resguardo_externo"]
    assert ad.estado_de(a, modulos={"resguardo_externo": True}) == {"habilitado": True, "cantidad": None}
    assert ad.estado_de(a, modulos={}) == {"habilitado": False, "cantidad": None}


def test_estado_de_unidades_con_modulo_pide_las_dos_cosas(ad):
    a = ad.catalogo(restolibra())["offline"]
    ad.fijar_cantidad("offline", 2)
    assert ad.estado_de(a, modulos={"offline": True}) == {"habilitado": True, "cantidad": 2}
    assert ad.estado_de(a, modulos={"offline": False}) == {"habilitado": False, "cantidad": 2}


def test_estado_de_unidades_sin_modulo_es_que_haya_dato(ad):
    a = ad.catalogo(ventalibra())["sucursal"]
    assert ad.estado_de(a) == {"habilitado": False, "cantidad": None}
    ad.fijar_cantidad("sucursal", 2)
    assert ad.estado_de(a) == {"habilitado": True, "cantidad": 2}


def test_contratados_de_la_instancia_alimenta_a_resolver(ad):
    plans = restolibra()
    ad.fijar_cantidad("offline", 2)
    c = ad.contratados_de_la_instancia(plans, modulos={"offline": True, "resguardo_externo": True})
    assert c == {"offline": 2, "resguardo_externo": True}
    assert ad.resolver(plans, "basico", c)["limites"] == {"sucursales_offline": 2}
    # el módulo apagado con una cantidad que quedó: no cuenta como contratado
    assert ad.contratados_de_la_instancia(plans, modulos={})["offline"] is None


# ── hacer cumplir ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_verificar_alta_sin_el_modulo_prendido_es_no_contratado(ad):
    with pytest.raises(ad.AdicionalNoContratado, match="no incluye «Modo offline»"):
        ad.verificar_alta(restolibra(), "offline", 0, modulos={})


def test_verificar_alta_justo_en_el_limite_pasa_y_uno_mas_falla(ad):
    plans = restolibra()
    ad.fijar_cantidad("offline", 2)
    mods = {"offline": True}
    ad.verificar_alta(plans, "offline", 0, modulos=mods)
    ad.verificar_alta(plans, "offline", 1, modulos=mods)  # la 2ª, que es la última contratada
    with pytest.raises(ad.AdicionalExcedido, match=r"para 2 sucursales\. Para sumar otra, comunicate con nosotros") as e:
        ad.verificar_alta(plans, "offline", 2, modulos=mods)
    assert (e.value.contratadas, e.value.usadas, e.value.clave) == (2, 2, "offline")


def test_el_mensaje_concuerda_en_numero(ad):
    a = ad.catalogo(restolibra())["offline"]
    assert "para 1 sucursal." in ad.mensaje_de_exceso(a, 1)
    assert "para 3 sucursales." in ad.mensaje_de_exceso(a, 3)


def test_verificar_alta_con_el_modulo_pero_sin_numero_no_limita(ad):
    ad.verificar_alta(restolibra(), "offline", 50, modulos={"offline": True})


def test_verificar_alta_de_un_adicional_sin_modulo_y_sin_dato_no_limita(ad):
    """ADR-041: sin dato cargado de sucursales, no hay límite."""
    ad.verificar_alta(ventalibra(), "sucursal", 50)
    ad.fijar_cantidad("sucursal", 2)
    with pytest.raises(ad.AdicionalExcedido):
        ad.verificar_alta(ventalibra(), "sucursal", 2)


def test_verificar_alta_de_una_clave_que_no_existe_es_error(ad):
    with pytest.raises(ValueError, match="desconocido"):
        ad.verificar_alta(restolibra(), "cohetes", 0)


def test_verificar_por_limite_generico(ad):
    a = ad.catalogo(restolibra())["offline"]
    ad.verificar_por_limite(a, 5, contratadas=None)
    ad.verificar_por_limite(a, 2, contratadas=3)
    ad.verificar_por_limite(a, 0, contratadas=3, sumar=3)
    with pytest.raises(ad.AdicionalExcedido):
        ad.verificar_por_limite(a, 1, contratadas=3, sumar=3)


# ── el router ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _guarda_lectura(x_internal_auth: str = Header(default=""), x_rol: str = Header(default="")):
    if x_internal_auth == TOKEN or x_rol == "admin":
        return
    raise HTTPException(403 if x_rol else 401, "no autorizado")


def _guarda_escritura(x_internal_auth: str = Header(default=""), x_rol: str = Header(default="")):
    """SÓLO el token de servicio. Un admin de la instancia es 403."""
    if x_internal_auth == TOKEN:
        return
    raise HTTPException(403 if x_rol else 401, "no autorizado")


@pytest.fixture
def modulos():
    """La tabla `modulos` de la instancia, en memoria."""
    return {}


@pytest.fixture
def en_uso():
    return {"sucursal": 1, "offline": 0}


@pytest.fixture
def client(ad, modulos, en_uso):
    import libracore.adicionales_router as router_mod
    importlib.reload(router_mod)
    app = FastAPI()
    app.include_router(router_mod.build_adicionales_router(
        _guarda_lectura, _guarda_escritura, medir={k: (lambda k=k: en_uso[k]) for k in en_uso}, plans=_ambos(),
        leer_modulos=lambda: dict(modulos), escribir_modulo=lambda m, v: modulos.__setitem__(m, v)))
    return TestClient(app)


def _ambos():
    """Un producto que tiene los tres tipos a la vez, para ejercitar el router entero."""
    plans = restolibra()
    plans.PLANES = ["unico"]
    plans.PLAN_MODULOS = {"unico": {"facturacion"}}
    plans.ADICIONALES = {**ventalibra().ADICIONALES, **restolibra().ADICIONALES}
    return plans


def _item(client, clave):
    return next(i for i in client.get("/api/adicionales", headers=SERVICIO).json()["adicionales"] if i["clave"] == clave)


@pytest.mark.parametrize("metodo, ruta, cuerpo", [("get", "/api/adicionales", None), ("put", "/api/adicionales/offline", {"cantidad": 1})])
def test_sin_credenciales_no_entra(client, metodo, ruta, cuerpo):
    assert getattr(client, metodo)(ruta, **({"json": cuerpo} if cuerpo else {})).status_code == 401


def test_el_admin_de_la_instancia_lee_pero_no_puede_prenderse_un_adicional(client, modulos, ad):
    """Un adicional es un límite comercial: si el admin del cliente pudiera cargarlo, contratar no costaría nada."""
    assert client.get("/api/adicionales", headers=ADMIN).status_code == 200
    for ruta, cuerpo in [("/api/adicionales/offline", {"cantidad": 99}), ("/api/adicionales/resguardo_externo", {"habilitado": True}),
                         ("/api/adicionales/sucursal", {"cantidad": 99})]:
        assert client.put(ruta, headers=ADMIN, json=cuerpo).status_code == 403
    assert modulos == {}
    assert ad.cantidad_contratada("offline") is None and ad.cantidad_contratada("sucursal") is None


def test_un_rol_que_no_es_admin_no_lee_ni_escribe(client):
    assert client.get("/api/adicionales", headers={"x-rol": "cajero"}).status_code == 403
    assert client.put("/api/adicionales/offline", headers={"x-rol": "cajero"}, json={"cantidad": 1}).status_code == 403


def test_el_get_trae_el_catalogo_lo_contratado_lo_usado_y_los_limites(client, en_uso, ad):
    en_uso.update(sucursal=3, offline=1)
    ad.fijar_cantidad("sucursal", 4)
    cuerpo = client.get("/api/adicionales", headers=SERVICIO).json()
    assert [i["clave"] for i in cuerpo["adicionales"]] == ["offline", "resguardo_externo", "sucursal"]
    offline = next(i for i in cuerpo["adicionales"] if i["clave"] == "offline")
    assert offline == {
        "clave": "offline", "label": "Modo offline", "precio": None, "modulos": ["offline"], "unidad": "sucursal", "incluidas": {},
        "limite": "sucursales_offline", "planes": None, "habilitado": False, "cantidad": None, "usadas": 1}
    sucursal = next(i for i in cuerpo["adicionales"] if i["clave"] == "sucursal")
    assert (sucursal["precio"], sucursal["incluidas"], sucursal["cantidad"], sucursal["usadas"]) == (19950, {"unico": 1}, 4, 3)
    assert next(i for i in cuerpo["adicionales"] if i["clave"] == "resguardo_externo")["usadas"] is None
    assert cuerpo["limites"] == {"sucursales": 4}


def test_put_cantidad_prende_el_modulo_y_get_lo_devuelve(client, modulos, ad):
    r = client.put("/api/adicionales/offline", headers=SERVICIO, json={"cantidad": 2})
    assert r.status_code == 200
    assert (r.json()["habilitado"], r.json()["cantidad"]) == (True, 2)
    assert modulos == {"offline": True}
    assert _item(client, "offline")["cantidad"] == 2
    assert _leer_config(ad)["adicionales"] == {"offline": 2}


def test_put_cantidad_null_es_la_baja_y_apaga_el_modulo(client, modulos, ad):
    client.put("/api/adicionales/offline", headers=SERVICIO, json={"cantidad": 2})
    r = client.put("/api/adicionales/offline", headers=SERVICIO, json={"cantidad": None})
    assert r.status_code == 200 and (r.json()["habilitado"], r.json()["cantidad"]) == (False, None)
    assert modulos == {"offline": False}
    assert "adicionales" not in _leer_config(ad)


def test_put_habilitado_false_en_un_adicional_por_unidades_tambien_es_la_baja(client, modulos):
    client.put("/api/adicionales/offline", headers=SERVICIO, json={"cantidad": 2})
    assert client.put("/api/adicionales/offline", headers=SERVICIO, json={"habilitado": False}).status_code == 200
    assert modulos == {"offline": False} and _item(client, "offline")["cantidad"] is None


def test_put_habilitado_true_en_un_adicional_por_unidades_pide_la_cantidad(client, modulos):
    r = client.put("/api/adicionales/offline", headers=SERVICIO, json={"habilitado": True})
    assert r.status_code == 422 and "cantidad" in r.json()["detail"]
    assert modulos == {}


def test_put_si_no_prende_y_apaga(client, modulos):
    assert client.put("/api/adicionales/resguardo_externo", headers=SERVICIO, json={"habilitado": True}).json()["habilitado"] is True
    assert modulos == {"resguardo_externo": True}
    assert client.put("/api/adicionales/resguardo_externo", headers=SERVICIO, json={"habilitado": None}).json()["habilitado"] is False
    assert modulos == {"resguardo_externo": False}


def test_put_cantidad_en_un_si_no_es_422(client, modulos):
    r = client.put("/api/adicionales/resguardo_externo", headers=SERVICIO, json={"cantidad": 2})
    assert r.status_code == 422 and "sí/no" in r.json()["detail"]
    assert modulos == {}


def test_put_la_sucursal_escribe_limite_sucursales_y_no_toca_modulos(client, modulos, ad):
    r = client.put("/api/adicionales/sucursal", headers=SERVICIO, json={"cantidad": 3})
    assert r.status_code == 200 and r.json()["cantidad"] == 3
    cfg = _leer_config(ad)
    assert cfg["limite_sucursales"] == 3 and "adicionales" not in cfg
    assert modulos == {}


@pytest.mark.parametrize("cuerpo", [{}, {"habilitado": True, "cantidad": 2}, {"cantidad": None, "habilitado": None}])
def test_put_exige_exactamente_un_campo(client, cuerpo, modulos):
    assert client.put("/api/adicionales/offline", headers=SERVICIO, json=cuerpo).status_code == 422
    assert modulos == {}


@pytest.mark.parametrize("malo", [0, -1, 1001, "mucho", 1.5, True, False, [], {}])
def test_put_rechaza_una_cantidad_que_no_es_un_entero_valido(client, malo, modulos):
    assert client.put("/api/adicionales/offline", headers=SERVICIO, json={"cantidad": malo}).status_code == 422
    assert modulos == {}


def test_put_de_una_clave_que_no_existe_es_404(client):
    r = client.put("/api/adicionales/cohetes", headers=SERVICIO, json={"habilitado": True})
    assert r.status_code == 404 and "cohetes" in r.json()["detail"]


def test_basura_en_el_config_no_rompe_el_get(client, ad):
    _escribir_config(ad, {"adicionales": "roto", "limite_sucursales": "muchas"})
    cuerpo = client.get("/api/adicionales", headers=SERVICIO).json()
    assert all(i["cantidad"] is None for i in cuerpo["adicionales"])
    assert cuerpo["limites"] == {}


def test_el_alias_de_limites_y_el_adicional_sucursal_ven_el_mismo_numero(client, ad):
    """`/api/limites/sucursales` (ADR-041) sigue escribiendo la misma clave: los dos caminos no se desfasan."""
    import libracore.limites as lim
    importlib.reload(lim)
    lim.config_manager.save({**lim.config_manager.load()})
    lim.fijar_sucursales_contratadas(5)
    assert _item(client, "sucursal")["cantidad"] == 5
    client.put("/api/adicionales/sucursal", headers=SERVICIO, json={"cantidad": 2})
    assert lim.sucursales_contratadas() == 2
