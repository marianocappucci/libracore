"""La cola de comprobantes del nodo offline (ADR-079): el producto deja el pedido sin red, el nodo lo emite solo al volver
y el central recibe el resultado. En SQLite y PostgreSQL.

Los emisores de los tests son los de un producto de verdad en chico: el de `documento` numera con el ticket prestado,
crea la factura y pide el CAE (con ARCA simulada); el de `cpe` pide el ticket de `wscpe`. La base del motor es real.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from libracore import arca_facturacion, arca_wsaa, arca_wsfe, candados, cola_arca, nodo_offline
from libracore import pdf_generator as pdf_gen
from libracore.arca_wscpe import EmisionIncierta, ErrorWscpe
from libracore.cola_arca import (
    ColaDeArca,
    ConfirmacionRequerida,
    EmisionEnCurso,
    FueraDeUnNodo,
    PedidoNoEncontrado,
    aplicar_pedido_del_nodo,
    payload_de_pedido,
)
from libracore.cola_arca_router import build_cola_del_nodo_router
from libracore.db import arca_config as db_arca
from libracore.db import cola_arca as db
from libracore.db import core
from libracore.db import facturas as db_facturas
from libracore.db.schema import init_core_schema
from libracore.nodo_offline import ConexionDelNodo, NodoSinArca, RechazoDelCentral, SinConexion

NODO = "nodo-cargo-01"
PRODUCTO = "libracargo"
USUARIO = {"id": 1, "username": "oficina", "role": "admin"}


@pytest.fixture(params=["sqlite", "postgres"])
def entorno(request, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    import libracore.config_manager as cm
    importlib.reload(cm)
    monkeypatch.setattr(pdf_gen, "FACTURAS_PDF_DIR", str(tmp_path / "pdf"))
    bases = request.getfixturevalue("bases") if request.param == "postgres" else None
    destino = str(tmp_path / "nodo.db") if bases is None else bases.nueva()
    core.configure(db_path=destino)
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    conn.close()
    cm.save({"empresa_iva_condition": "Monotributista"})
    yield {"cm": cm, "bases": bases, "tmp": tmp_path, "destino": destino}
    core._db_path = None
    core._database_url = None


class Red:
    """La conexión del nodo, a mano."""

    def __init__(self, en_linea=True):
        self.en_linea = en_linea

    def conexion(self) -> ConexionDelNodo:
        return ConexionDelNodo(nodo=lambda: NODO, en_linea=lambda: self.en_linea)


@pytest.fixture
def red():
    return Red()


@pytest.fixture
def con_arca(entorno, monkeypatch):
    """ARCA simulada: el último número autorizado es el 7 y el CAE sale siempre."""
    monkeypatch.delenv("ENV", raising=False)
    db_arca.crear_arca_config("Comercio Ficticio", "20000000001", 3, "", "", ambiente="homologacion")
    pedidos = {"numero": [], "cae": 0}

    async def ultimo(pv, tipo, cuit, token, sign, ambiente):
        pedidos["numero"].append((pv, tipo, token))
        return 7

    async def cae(factura, cuit, token, sign, ambiente):
        pedidos["cae"] += 1
        return {"cae": "70000000000002", "cae_vto": "20261030"}

    monkeypatch.setattr(arca_wsfe, "ultimo_numero_autorizado", ultimo)
    monkeypatch.setattr(arca_wsfe, "solicitar_cae", cae)
    return pedidos


class Producto:
    """Un producto en chico: lo que LibraCargo y LibraClub van a registrar (PRs 9 y 11)."""

    def __init__(self, red):
        self.red = red
        self.llamadas: list[tuple] = []     # (origen_tipo, origen_id), en el orden en que se emitió
        self.avisos: list[dict] = []        # lo que recibió `al_cambiar`
        self.tickets: list[tuple] = []      # (servicio) de cada ticket pedido
        self.falla = None                   # una excepción para levantar en el emisor, o una función (pedido) -> None
        self.estado_visto: list[tuple] = []  # (estado, intentos) que el emisor vio en la fila mientras emitía

    async def ticket(self, arca, servicio="wsfe"):
        self.tickets.append(servicio)
        return {"token": "T", "sign": "S", "servicio": servicio, "ambiente": "homologacion"}

    def _fallar(self, pedido):
        if callable(self.falla):
            self.falla(pedido)
        elif self.falla is not None:
            raise self.falla

    async def emitir_documento(self, pedido, ctx):
        self.llamadas.append((pedido["origen_tipo"], pedido["origen_id"]))
        fila = db.get_pedido(pedido["id"])
        self.estado_visto.append((fila["estado"], fila["intentos"]))
        self._fallar(pedido)
        # Idempotente: si la orden ya tiene su factura, la devuelve sin emitir otra.
        with core.get_connection() as conn:
            previa = conn.execute("SELECT id, cae FROM facturas WHERE observaciones=?",
                                  (f"Orden {pedido['origen_id']}",)).fetchone()
        if previa:
            return {"factura_id": previa["id"], "cae": previa["cae"]}
        punto_venta, tipo = 3, 11
        numero, ta, arca = await ctx.numerar(punto_venta, tipo)
        ambiente = arca_facturacion.ambiente_de(arca)
        fecha = ctx.fecha_de_la_factura(punto_venta, tipo, ambiente)
        total = float(pedido["datos"]["total"])
        factura_id = db_facturas.create_factura(
            tipo, punto_venta, numero, fecha.isoformat(), "", "Cliente Ficticio", 5,
            [{"description": "Flete", "qty": 1, "unit_price": total, "subtotal": total}], total, 0.0, total,
            observaciones=f"Orden {pedido['origen_id']}", ambiente=ambiente)
        factura = db_facturas.get_factura(factura_id)
        factura = await arca_facturacion.solicitar_cae(factura_id, factura, ta, arca)
        return {"factura_id": factura_id, "cae": factura.get("cae") or "", "fecha": fecha.isoformat()}

    async def emitir_cpe(self, pedido, ctx):
        self.llamadas.append((pedido["origen_tipo"], pedido["origen_id"]))
        fila = db.get_pedido(pedido["id"])
        self.estado_visto.append((fila["estado"], fila["intentos"]))
        self._fallar(pedido)
        ta, _ = await ctx.ticket("wscpe")
        return {"ctg": 10100000000 + int(pedido["origen_id"]), "ambiente": ta["ambiente"]}

    def cola(self, **extra) -> ColaDeArca:
        datos = dict(
            producto=PRODUCTO,
            emisores={"documento": self.emitir_documento, "cpe": self.emitir_cpe},
            conexion=self.red.conexion(), obtener_ta=self.ticket, al_cambiar=self.avisos.append)
        datos.update(extra)
        return ColaDeArca(**datos)


@pytest.fixture
def producto(entorno, red):
    return Producto(red)


def _facturas() -> int:
    with core.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM facturas").fetchone()[0]


def _doc(cola, origen_id, total=1210.0, **extra):
    return cola_arca.encolar(cola, "documento", origen_id, datos={"total": total}, **extra)


def _cpe(cola, origen_id, **extra):
    return cola_arca.encolar(cola, "cpe", origen_id, datos={"titular": "30000000019"}, **extra)


def _correr(cola, **kw):
    return asyncio.run(cola_arca.procesar_cola(cola, **kw))


# ── Encolar: idempotente por origen ──────────────────────────────────────────


def test_sin_conexion_encola_una_sola_vez(producto, red):
    red.en_linea = False
    cola = producto.cola()
    pedido, creado = _doc(cola, "A-17", total=1000.0, fecha_ref="2026-10-20", concepto=2)
    assert creado and pedido["estado"] == "esperando" and pedido["intentos"] == 0 and pedido["revision"] == 1
    assert (pedido["origen_producto"], pedido["origen_instancia"], pedido["origen_tipo"], pedido["origen_id"]) == (
        PRODUCTO, NODO, "documento", "A-17")
    assert pedido["datos"] == {"total": 1000.0} and pedido["fecha_ref"] == "2026-10-20" and pedido["concepto"] == 2

    # Pedir lo mismo de nuevo no duplica: queda UNA fila y manda lo último que dijo el producto.
    otra, creado = _doc(cola, "A-17", total=1500.0)
    assert not creado and otra["id"] == pedido["id"] and otra["datos"] == {"total": 1500.0}
    assert otra["revision"] == 2   # el cambio sube la revisión: el central se entera
    assert len(db.listar()) == 1

    # La misma entidad en otro tipo, o la misma orden de otro nodo, son otras filas.
    _cpe(cola, "A-17")
    assert len(db.listar()) == 2
    # Sin conexión no se sale a ARCA: no hay ticket, número ni factura.
    assert producto.tickets == [] and producto.llamadas == [] and _facturas() == 0


def test_un_reenvio_de_algo_que_ya_no_esta_esperando_no_lo_pisa(producto, red, con_arca):
    cola = producto.cola()
    pedido, _ = _doc(cola, "A-1")
    _correr(cola)
    assert db.get_pedido(pedido["id"])["estado"] == "emitido"
    with pytest.raises(db.PedidoYaResuelto) as e:
        _doc(cola, "A-1", total=9.0)
    assert e.value.estado == "emitido" and e.value.pedido_id == pedido["id"]
    assert db.get_pedido(pedido["id"])["datos"] == {"total": 1210.0}


def test_el_alta_valida_el_tipo_el_producto_y_el_origen(producto):
    cola = producto.cola()
    with pytest.raises(ValueError, match="registró un emisor"):
        cola_arca.encolar(ColaDeArca(producto=PRODUCTO, emisores={"cpe": producto.emitir_cpe},
                                     conexion=producto.red.conexion()), "documento", "1", datos={})
    with pytest.raises(ValueError, match="origen_tipo"):
        db.encolar(PRODUCTO, NODO, "venta", "1", datos={})
    with pytest.raises(ValueError, match="origen_id"):
        db.encolar(PRODUCTO, NODO, "cpe", " ", datos={})
    with pytest.raises(ValueError, match="origen_producto"):
        db.encolar("", NODO, "cpe", "1", datos={})
    with pytest.raises(TypeError, match="datos"):
        cola_arca.encolar(cola, "cpe", "1", datos="no es un dict")   # type: ignore[arg-type]
    with pytest.raises(ValueError, match="emisores"):
        ColaDeArca(producto=PRODUCTO, emisores={"venta": producto.emitir_cpe})
    assert db.listar() == []


def test_encolar_en_la_transaccion_del_producto(producto):
    """Con `conn`, el pedido queda o se va con la transacción de quien llama, y no se avisa antes de confirmar."""
    cola = producto.cola()

    class Revertir(Exception):
        pass

    with pytest.raises(Revertir), core.get_connection() as conn:
        pedido, _ = _doc(cola, "T-1", conn=conn)
        assert db.get_pedido(pedido["id"], conn=conn)["estado"] == "esperando"
        raise Revertir
    assert db.listar() == [] and producto.avisos == []

    with core.get_connection() as conn:
        _doc(cola, "T-2", conn=conn)
    assert [p["origen_id"] for p in db.listar()] == ["T-2"]
    assert producto.avisos == []        # con `conn` el aviso lo encola el producto, con lo que devolvió `encolar`
    _doc(cola, "T-3")                   # sin `conn`: confirma y avisa
    assert [a["origen_id"] for a in producto.avisos] == ["T-3"]


def test_el_estado_solo_admite_los_seis(entorno):
    with pytest.raises(Exception):  # noqa: B017,PT011 - el CHECK de la base: IntegrityError en SQLite y en PostgreSQL
        with core.get_connection() as conn:
            conn.execute("INSERT INTO cola_arca (origen_producto, origen_tipo, origen_id, estado) "
                         "VALUES ('p', 'cpe', '1', 'volando')")


# ── Al volver: una vez y en orden ────────────────────────────────────────────


def test_al_volver_emite_una_sola_vez_y_en_orden(producto, red, con_arca):
    red.en_linea = False
    cola = producto.cola()
    _doc(cola, "A-3")
    _cpe(cola, "2")
    _doc(cola, "A-1")
    _cpe(cola, "9")
    sin_red = _correr(cola)
    assert sin_red["en_linea"] is False and sin_red["emitidos"] == [] and sin_red["quedan"] == 4
    assert producto.llamadas == []

    red.en_linea = True
    hecho = _correr(cola)
    # Los más viejos primero, sea cual sea el tipo: el orden en que se encolaron.
    assert producto.llamadas == [("documento", "A-3"), ("cpe", "2"), ("documento", "A-1"), ("cpe", "9")]
    assert [e["origen_id"] for e in hecho["emitidos"]] == ["A-3", "2", "A-1", "9"]
    assert hecho["quedan"] == 0 and hecho["a_revisar"] == hecho["inciertos"] == hecho["errores"] == []
    assert _facturas() == 2
    assert {p["estado"] for p in db.listar()} == {"emitido"}

    # Una segunda tanda no emite nada de nuevo.
    again = _correr(cola)
    assert again["emitidos"] == [] and len(producto.llamadas) == 4 and _facturas() == 2

    # La numeración vino de ARCA (el último autorizado, 7, más uno) con el ticket prestado y no local.
    facturas = [db_facturas.get_factura(p["resultado"]["factura_id"]) for p in db.listar(origen_tipo="documento")]
    assert [f["numero"] for f in facturas] == [8, 9]
    assert {f["cae"] for f in facturas} == {"70000000000002"}
    assert producto.tickets.count("wsfe") == 2 and producto.tickets.count("wscpe") == 2


def test_el_emisor_ve_el_pedido_emitiendo_y_con_el_intento_contado(producto, red, con_arca):
    """Se confirma `emitiendo` ANTES de llamar al emisor: si el proceso muere ahí, queda rastro."""
    cola = producto.cola()
    _cpe(cola, "1")
    _correr(cola)
    assert producto.estado_visto == [("emitiendo", 1)]
    fila = db.listar()[0]
    assert fila["estado"] == "emitido" and fila["intentos"] == 1 and fila["resuelto_at"]
    assert fila["resultado"] == {"ctg": 10100000001, "ambiente": "homologacion"}


def test_el_resultado_llega_al_central_y_el_desorden_no_lo_retrocede(producto, red, con_arca):
    cola = producto.cola()
    _cpe(cola, "5")
    _correr(cola)
    avisos = producto.avisos
    assert [a["estado"] for a in avisos] == ["esperando", "emitido"]
    assert [a["revision"] for a in avisos] == [1, 3]   # esperando(1) → emitiendo(2) → emitido(3)
    emitido = avisos[1]
    assert emitido["resultado"]["ctg"] == 10100000005 and emitido["version"] == cola_arca.VERSION
    assert emitido["id_en_el_nodo"] and emitido["origen_instancia"] == NODO

    # El central: otra base. Se simula con un nodo distinto en la misma (la clave incluye al nodo).
    en_central = {**emitido, "origen_instancia": "nodo-otro"}
    viejo = {**avisos[0], "origen_instancia": "nodo-otro"}
    cid, aplicada = aplicar_pedido_del_nodo(viejo)
    assert aplicada and db.get_pedido(cid)["estado"] == "esperando"
    assert aplicar_pedido_del_nodo(en_central) == (cid, True)
    assert db.get_pedido(cid)["estado"] == "emitido" and db.get_pedido(cid)["resultado"]["ctg"] == 10100000005
    # Reenviado, o llegado tarde (revisión menor): no pisa.
    assert aplicar_pedido_del_nodo(en_central) == (cid, False)
    assert aplicar_pedido_del_nodo(viejo) == (cid, False)
    assert db.get_pedido(cid)["estado"] == "emitido" and db.get_pedido(cid)["revision"] == 3
    # Y el nodo de verdad sigue con su fila: son dos.
    assert len(db.listar(origen_tipo="cpe")) == 2
    with pytest.raises(ValueError, match="versión"):
        aplicar_pedido_del_nodo({**emitido, "version": 99})
    with pytest.raises(ValueError, match="origen_instancia"):
        aplicar_pedido_del_nodo({**emitido, "origen_instancia": ""})


# ── Errores: la red no consume, el rechazo se marca, lo incierto no se reintenta ─


@pytest.mark.parametrize("falla", [ConnectionError("sin ruta"), TimeoutError(), SinConexion("central caído")],
                         ids=["conexion", "tiempo", "sin_conexion"])
def test_un_error_de_red_no_consume_el_pedido(producto, red, con_arca, falla):
    cola = producto.cola()
    _doc(cola, "A-1")
    _doc(cola, "A-2")
    producto.falla = falla
    corte = _correr(cola)
    # Se corta en el primero: no se prueba el segundo con la red caída.
    assert corte["en_linea"] is False and corte["emitidos"] == [] and corte["quedan"] == 2
    assert producto.llamadas == [("documento", "A-1")]
    primero = db.get_pedido(db.listar()[0]["id"])
    assert primero["estado"] == "esperando" and primero["intentos"] == 0   # el intento no se cuenta
    assert primero["ultimo_error"]   # queda dicho por qué espera
    assert _facturas() == 0

    producto.falla = None
    hecho = _correr(cola)
    assert [e["origen_id"] for e in hecho["emitidos"]] == ["A-1", "A-2"] and _facturas() == 2


def test_un_5xx_del_central_tambien_es_un_corte(producto, red, con_arca):
    import httpx

    cola = producto.cola()
    _doc(cola, "A-1")
    pedido = httpx.Request("GET", "https://central.invalid/sync/v1/ticket-arca")
    producto.falla = httpx.HTTPStatusError("x", request=pedido, response=httpx.Response(503, request=pedido))
    assert _correr(cola)["quedan"] == 1
    assert db.listar()[0]["estado"] == "esperando"


def test_un_rechazo_de_arca_lo_marca_para_revisar_con_el_mensaje_y_sigue(producto, red, con_arca):
    cola = producto.cola()
    _cpe(cola, "1")
    _doc(cola, "A-1")
    producto.falla = lambda pedido: (_ for _ in ()).throw(
        ErrorWscpe("949: el campo productor no se debe informar", [(949, "productor")])) if pedido["origen_tipo"] == "cpe" else None
    hecho = _correr(cola)
    # La CPE rechazada no frena a la factura que está detrás.
    assert [e["origen_id"] for e in hecho["emitidos"]] == ["A-1"]
    assert hecho["a_revisar"][0]["origen_id"] == "1" and "949" in hecho["a_revisar"][0]["error"]
    cpe = db.get_por_origen(PRODUCTO, NODO, "cpe", "1")
    assert cpe["estado"] == "a_revisar" and "949" in cpe["ultimo_error"] and cpe["intentos"] == 1
    assert cpe["resultado"] is None
    assert hecho["quedan"] == 0
    # No se reintenta sola: la tanda siguiente no la toca.
    producto.falla = None
    again = _correr(cola)
    assert again["emitidos"] == [] and db.get_pedido(cpe["id"])["estado"] == "a_revisar"

    # Una persona corrige los datos y la reabre: sale en la próxima tanda.
    reabierta = cola_arca.reintentar(cola, cpe["id"], datos={"titular": "30000000019", "corregido": True})
    assert reabierta["estado"] == "esperando" and reabierta["ultimo_error"] == ""
    assert reabierta["datos"]["corregido"] is True
    assert [e["origen_id"] for e in _correr(cola)["emitidos"]] == ["1"]
    assert db.get_pedido(cpe["id"])["estado"] == "emitido"


def test_un_documento_sin_cae_va_a_revisar_con_su_factura(producto, red, con_arca, monkeypatch):
    async def sin_cae(factura, cuit, token, sign, ambiente):
        raise RuntimeError("ARCA no autorizó: 10016 fecha anterior a la del último comprobante")

    monkeypatch.setattr(arca_wsfe, "solicitar_cae", sin_cae)
    cola = producto.cola()
    _doc(cola, "A-1")
    hecho = _correr(cola)
    assert hecho["emitidos"] == [] and hecho["a_revisar"][0]["origen_id"] == "A-1"
    pedido = db.listar()[0]
    assert pedido["estado"] == "a_revisar" and pedido["resultado"]["factura_id"]
    assert "no autorizó" in pedido["ultimo_error"] or "CAE" in pedido["ultimo_error"]
    assert _facturas() == 1
    # La factura borrador existe una sola vez: el emisor idempotente la reconoce al reintentar.
    cola_arca.reintentar(cola, pedido["id"])
    monkeypatch.setattr(arca_wsfe, "solicitar_cae", lambda *a: None)   # no se vuelve a pedir CAE
    _correr(cola)
    assert _facturas() == 1


def test_una_emision_incierta_no_se_reintenta_a_ciegas(producto, red, con_arca):
    cola = producto.cola()
    _cpe(cola, "1")
    producto.falla = EmisionIncierta("ARCA no contestó al autorizar", sucursal=1, nro_orden=77, tipo_cpe=74)
    hecho = _correr(cola)
    assert [e["origen_id"] for e in hecho["inciertos"]] == ["1"] and hecho["emitidos"] == []
    pedido = db.listar()[0]
    assert pedido["estado"] == "incierto" and "no contestó" in pedido["ultimo_error"]

    # Ni la tanda ni el botón vuelven a emitirla.
    producto.falla = None
    assert _correr(cola)["emitidos"] == [] and len(producto.llamadas) == 1
    assert asyncio.run(cola_arca.emitir_pedido(cola, pedido["id"]))["estado"] == "incierto"
    assert len(producto.llamadas) == 1

    # Reabrirla pide decir que se consultó a ARCA y no salió.
    with pytest.raises(ConfirmacionRequerida):
        cola_arca.reintentar(cola, pedido["id"])
    assert db.get_pedido(pedido["id"])["estado"] == "incierto"
    cola_arca.reintentar(cola, pedido["id"], confirmo_que_no_salio=True)
    assert [e["origen_id"] for e in _correr(cola)["emitidos"]] == ["1"]


def test_lo_marcado_como_incierto_por_el_producto_tambien_cuenta(producto, red, con_arca):
    class Dudosa(Exception):
        emision_incierta = True

    cola = producto.cola()
    _doc(cola, "A-1")
    producto.falla = Dudosa("el CAE pudo haber salido")
    assert _correr(cola)["inciertos"][0]["origen_id"] == "A-1"


def test_una_persona_comprueba_que_si_salio(producto, red, con_arca):
    cola = producto.cola()
    _cpe(cola, "1")
    producto.falla = EmisionIncierta("ARCA no contestó", sucursal=1, nro_orden=77, tipo_cpe=74)
    _correr(cola)
    pedido = db.listar()[0]
    with pytest.raises(ValueError, match="resultado"):
        cola_arca.marcar_emitido(cola, pedido["id"], {})
    hecho = cola_arca.marcar_emitido(cola, pedido["id"], {"ctg": 10100000099})
    assert hecho["estado"] == "emitido" and hecho["resultado"] == {"ctg": 10100000099} and hecho["ultimo_error"] == ""
    with pytest.raises(ValueError, match="sólo se marca emitido"):
        cola_arca.marcar_emitido(cola, pedido["id"], {"ctg": 1})   # ya está emitido
    producto.falla = None
    assert _correr(cola)["emitidos"] == [] and len(producto.llamadas) == 1


def test_un_pedido_a_medias_pasa_a_incierto_y_no_se_vuelve_a_emitir(producto, red, con_arca):
    """El proceso murió con el pedido `emitiendo`: no se sabe si ARCA llegó a emitirlo."""
    cola = producto.cola()
    pedido, _ = _cpe(cola, "1")
    assert db.tomar_para_emitir(pedido["id"])       # …y se cayó la luz
    hecho = _correr(cola)
    assert producto.llamadas == []                  # no se llamó al emisor
    assert [i["origen_id"] for i in hecho["inciertos"]] == ["1"]
    fila = db.get_pedido(pedido["id"])
    assert fila["estado"] == "incierto" and "a mitad de la emisión" in fila["ultimo_error"]


def test_un_emisor_que_devuelve_cualquier_cosa_no_pasa_como_emitido(producto, red, con_arca):
    cola = producto.cola()
    _cpe(cola, "1")
    _doc(cola, "A-1")

    async def mal(pedido, ctx):
        return None

    async def sin_factura(pedido, ctx):
        return {"cae": "123"}

    cola = producto.cola(emisores={"cpe": mal, "documento": sin_factura})
    hecho = _correr(cola)
    assert hecho["emitidos"] == [] and {i["origen_id"] for i in hecho["a_revisar"]} == {"1", "A-1"}
    assert all("tiene que devolver" in p["ultimo_error"] for p in db.listar())


def test_un_emisor_sincronico_tambien_sirve(producto, red, con_arca):
    cola = producto.cola(emisores={"cpe": lambda pedido, ctx: {"ctg": 1}})
    _cpe(cola, "1")
    assert _correr(cola)["emitidos"][0]["resultado"] == {"ctg": 1}


# ── El central presta el ticket (o no) ───────────────────────────────────────


def test_un_ticket_que_el_central_no_presta_deja_esperando_y_corta_solo_ese_tipo(producto, red, con_arca):
    async def sin_wscpe(arca, servicio="wsfe"):
        if servicio == "wscpe":
            raise RechazoDelCentral("servicio que no presta")
        return await producto.ticket(arca, servicio)

    cola = producto.cola(obtener_ta=sin_wscpe)
    _cpe(cola, "1")
    _cpe(cola, "2")
    _doc(cola, "A-1")
    hecho = _correr(cola)
    # La primera CPE falla y corta a las CPE; la factura sigue.
    assert [e["origen_id"] for e in hecho["emitidos"]] == ["A-1"]
    assert len(hecho["errores"]) == 1 and hecho["errores"][0]["origen_id"] == "1" and hecho["quedan"] == 2
    cpe = db.get_por_origen(PRODUCTO, NODO, "cpe", "1")
    assert cpe["estado"] == "esperando" and cpe["intentos"] == 0
    assert "no presta" in cpe["ultimo_error"]
    assert db.get_por_origen(PRODUCTO, NODO, "cpe", "2")["ultimo_error"] == ""   # no se probó


def test_un_ticket_de_otro_servicio_es_un_rechazo_no_una_emision_con_el_ticket_equivocado(producto, red, con_arca):
    """Un LibraEdge viejo ignora el `servicio` y devuelve el de facturación: ARCA contestaría con un error mudo."""
    async def viejo(arca, servicio="wsfe"):
        return {"token": "T", "sign": "S"}      # sin `servicio`: es el de wsfe

    cola = producto.cola(obtener_ta=viejo)
    _cpe(cola, "1")
    hecho = _correr(cola)
    assert hecho["emitidos"] == [] and "actualizá LibraEdge" in hecho["errores"][0]["error"]
    assert db.listar()[0]["estado"] == "esperando"
    assert producto.llamadas == [("cpe", "1")]


def test_el_ticket_de_facturacion_sigue_pidiendose_como_siempre(producto, red, con_arca):
    """El cliente del ticket de ADR-074 (un solo parámetro) sigue sirviendo para `wsfe`."""
    async def de_siempre(arca):
        return {"token": "T", "sign": "S"}

    cola = producto.cola(obtener_ta=de_siempre)
    _doc(cola, "A-1")
    assert _correr(cola)["emitidos"][0]["origen_id"] == "A-1"


def test_sin_red_nunca_se_numera_local(producto, red, con_arca, monkeypatch):
    async def local(*args, **kw):
        raise AssertionError("se numeró local")

    monkeypatch.setattr(arca_facturacion, "get_next_numero_with_arca", local)

    # 1) El central no responde al pedir el ticket.
    async def central_caido(arca, servicio="wsfe"):
        raise ConnectionError("sin ruta")

    cola = producto.cola(obtener_ta=central_caido)
    _doc(cola, "A-1")
    assert _correr(cola)["quedan"] == 1 and _facturas() == 0

    # 2) ARCA no da el número.
    async def arca_caida(*args):
        raise TimeoutError

    monkeypatch.setattr(arca_wsfe, "ultimo_numero_autorizado", arca_caida)
    assert _correr(producto.cola())["quedan"] == 1 and _facturas() == 0

    # 3) El numerador del producto cayó a la numeración local (no devolvió ticket): no pasa como número bueno.
    async def numerador_local(pv, tipo):
        return 99, None, {"cuit": "20000000001", "ambiente": "homologacion"}

    cola = producto.cola(numerar_comprobante=numerador_local)
    assert _correr(cola)["quedan"] == 1 and _facturas() == 0

    # 4) Sin con qué autenticarse: ni se intenta.
    sin_nada = ColaDeArca(producto=PRODUCTO, emisores={"documento": producto.emitir_documento},
                          conexion=red.conexion())
    hecho = _correr(sin_nada)
    assert "con qué autenticarse" in hecho["errores"][0]["error"] and _facturas() == 0
    assert {p["estado"] for p in db.listar()} == {"esperando"}


# ── El candado: una emisión, aunque corran dos a la vez ──────────────────────


def test_el_candado_con_el_pedido_tomado_no_se_emite(producto, red, con_arca):
    cola = producto.cola()
    pedido, _ = _doc(cola, "A-1")
    nombre = cola_arca._nombre_del_candado(cola, pedido["id"])
    with core.get_connection() as conn, candados.candado_de_base(conn, nombre) as tomado:
        assert tomado
        with pytest.raises(EmisionEnCurso):
            asyncio.run(cola_arca.emitir_pedido(cola, pedido["id"]))
        assert _correr(cola)["emitidos"] == []          # la tanda lo saltea
        assert producto.llamadas == [] and _facturas() == 0
    assert db.get_pedido(pedido["id"])["estado"] == "esperando"
    assert [e["origen_id"] for e in _correr(cola)["emitidos"]] == ["A-1"]


def test_dos_a_la_vez_emiten_una_sola_vez(producto, red, con_arca):
    """La tanda automática y el botón a la vez: una emisión, y el pedido queda `emitido` (no `incierto`)."""
    entrada = threading.Barrier(2)

    async def lento(pedido, ctx):
        producto.llamadas.append((pedido["origen_tipo"], pedido["origen_id"]))
        await asyncio.sleep(0.4)
        return {"ctg": 1}

    cola = producto.cola(emisores={"cpe": lento})
    pedido, _ = _cpe(cola, "1")
    resultados = []

    def correr():
        entrada.wait()
        try:
            resultados.append(asyncio.run(cola_arca.emitir_pedido(cola, pedido["id"]))["estado"])
        except EmisionEnCurso:
            resultados.append("en_curso")

    hilos = [threading.Thread(target=correr) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(10)
    assert len(producto.llamadas) == 1
    assert sorted(resultados) == ["emitido", "en_curso"]
    assert db.get_pedido(pedido["id"])["estado"] == "emitido"


def test_la_transicion_a_emitiendo_es_atomica(entorno):
    pedido_id, _ = db.encolar(PRODUCTO, NODO, "cpe", "1", datos={})
    assert db.tomar_para_emitir(pedido_id) is True
    assert db.tomar_para_emitir(pedido_id) is False   # el segundo no pasa: ya no está esperando
    assert db.get_pedido(pedido_id)["intentos"] == 1


# ── Fuera de un nodo, nada cambia ────────────────────────────────────────────


def test_fuera_de_un_nodo_nada_cambia(producto, red, con_arca):
    cola = ColaDeArca(producto=PRODUCTO, emisores={"cpe": producto.emitir_cpe}, al_cambiar=producto.avisos.append)
    assert not cola.conexion.es_nodo()
    with pytest.raises(FueraDeUnNodo):
        _cpe(cola, "1")
    assert db.listar() == []
    hecho = _correr(cola)
    assert hecho == {"es_nodo": False, "en_linea": True, "emitidos": [], "a_revisar": [], "inciertos": [],
                     "errores": [], "quedan": 0}
    llamadas = []
    assert cola_arca.arrancar_cola_al_volver(cola, en_cada_vuelta=lambda: llamadas.append(1)) is None
    assert llamadas == [] and producto.llamadas == [] and producto.avisos == []
    assert cola_arca.resumen(cola)["cuentas"] == dict.fromkeys(db.ESTADOS, 0)
    # Y el resto del motor no cambió: una factura de siempre sale igual.
    assert db_facturas.get_factura(db_facturas.create_factura(
        11, 3, 1, "2026-10-20", "", "X", 5, [], 1.0, 0.0, 1.0, ambiente="homologacion"))["numero"] == 1


def test_un_pedido_de_otro_producto_o_de_otro_nodo_no_se_toca(producto, red, con_arca):
    cola = producto.cola()
    ajeno, _ = db.encolar("libraclub", NODO, "cpe", "1", datos={})
    otro_nodo, _ = db.encolar(PRODUCTO, "nodo-otro", "cpe", "1", datos={})
    _cpe(cola, "1")
    _correr(cola)
    assert producto.llamadas == [("cpe", "1")]
    assert db.get_pedido(ajeno)["estado"] == db.get_pedido(otro_nodo)["estado"] == "esperando"
    with pytest.raises(PedidoNoEncontrado):
        asyncio.run(cola_arca.emitir_pedido(cola, ajeno))
    with pytest.raises(PedidoNoEncontrado):
        cola_arca.descartar(cola, otro_nodo)
    assert cola_arca.resumen(cola)["cuentas"]["esperando"] == 0


# ── El aviso al producto ─────────────────────────────────────────────────────


def test_un_error_del_aviso_no_deshace_lo_emitido_ni_deja_datos_en_el_log(producto, red, con_arca, caplog):
    def revienta(pedido):
        raise RuntimeError("token-secreto-que-no-va-al-log")

    cola = producto.cola(al_cambiar=revienta)
    with caplog.at_level("ERROR", logger=cola_arca.logger.name):
        _cpe(cola, "1")
        hecho = _correr(cola)
    assert hecho["emitidos"][0]["origen_id"] == "1"       # el comprobante ya salió
    assert db.listar()[0]["estado"] == "emitido"
    assert "RuntimeError" in caplog.text and "token-secreto" not in caplog.text


def test_el_hilo_emite_al_volver_y_reconcilia(producto, red, con_arca):
    cola = producto.cola()
    red.en_linea = False
    _cpe(cola, "1")
    pasos = []
    parar = cola_arca.arrancar_cola_al_volver(cola, intervalo=0.02, en_cada_vuelta=lambda: pasos.append("vuelta"))
    try:
        assert _esperar(lambda: len(pasos) >= 3)
        assert db.listar()[0]["estado"] == "esperando"     # sin conexión no emite
        red.en_linea = True
        assert _esperar(lambda: db.listar()[0]["estado"] == "emitido")
    finally:
        parar.set()
    assert pasos[0] == "vuelta" and producto.llamadas == [("cpe", "1")]


def test_un_error_no_mata_el_hilo(producto, red, con_arca, monkeypatch, caplog):
    vueltas = {"tanda": 0, "reconciliar": 0}

    async def tanda(cola, **kw):
        vueltas["tanda"] += 1
        raise RuntimeError("token-secreto-que-no-va-al-log")

    def reconciliar():
        vueltas["reconciliar"] += 1
        raise RuntimeError("otro-dato-que-no-va-al-log")

    monkeypatch.setattr(cola_arca, "procesar_cola", tanda)
    with caplog.at_level("ERROR", logger=cola_arca.logger.name):
        parar = cola_arca.arrancar_cola_al_volver(producto.cola(), intervalo=0.02, en_cada_vuelta=reconciliar)
        try:
            assert _esperar(lambda: vueltas["tanda"] >= 3 and vueltas["reconciliar"] >= 4)
        finally:
            parar.set()
    assert "RuntimeError" in caplog.text
    assert "token-secreto" not in caplog.text and "otro-dato" not in caplog.text


def _esperar(condicion, segundos=5.0):
    limite = time.monotonic() + segundos
    while not condicion() and time.monotonic() < limite:
        time.sleep(0.01)
    return condicion()


def test_encolar_y_emitir_sale_en_el_momento_con_red_y_espera_sin_ella(producto, red, con_arca):
    cola = producto.cola()
    ya = asyncio.run(cola_arca.encolar_y_emitir(cola, "documento", "A-1", datos={"total": 100.0}))
    assert ya["estado"] == "emitido" and _facturas() == 1

    red.en_linea = False
    despues = asyncio.run(cola_arca.encolar_y_emitir(cola, "documento", "A-2", datos={"total": 100.0}))
    assert despues["estado"] == "esperando" and _facturas() == 1

    # Con red pero el central se cae a mitad: queda esperando, no se pierde ni explota.
    red.en_linea = True
    producto.falla = ConnectionError("sin ruta")
    cortada = asyncio.run(cola_arca.encolar_y_emitir(cola, "documento", "A-3", datos={"total": 100.0}))
    assert cortada["estado"] == "esperando"
    producto.falla = None
    assert len(_correr(cola)["emitidos"]) == 2 and _facturas() == 3


# ── La fecha de la factura ───────────────────────────────────────────────────


def test_la_factura_al_volver_lleva_la_fecha_del_hecho_si_arca_la_admite(producto, red, con_arca):
    from libracore import fechas

    hoy = fechas.hoy()
    cola = producto.cola()
    _doc(cola, "A-1", fecha_ref=(hoy - timedelta(days=8)).isoformat(), concepto=2)      # servicios: ARCA admite 10 días
    _doc(cola, "A-2", fecha_ref=(hoy - timedelta(days=3)).isoformat())                  # productos, dentro de los 5 días
    _doc(cola, "A-3", fecha_ref=(hoy - timedelta(days=30)).isoformat())                 # pasado el plazo: hoy
    _doc(cola, "A-4", fecha_ref=(hoy - timedelta(days=1)).isoformat())                  # admitida, pero la serie ya llegó a hoy
    _doc(cola, "A-5", fecha_ref=(hoy - timedelta(days=8)).isoformat(), concepto=1)      # productos a 8 días: hoy
    _doc(cola, "A-6")                                                                    # sin fecha: hoy
    emitidas = {e["origen_id"]: e["resultado"]["fecha"] for e in _correr(cola)["emitidos"]}
    assert emitidas == {
        "A-1": (hoy - timedelta(days=8)).isoformat(),
        "A-2": (hoy - timedelta(days=3)).isoformat(),
        "A-3": hoy.isoformat(),
        # 🔴 Nunca antes que el último comprobante de la serie: ARCA rechaza una fecha anterior a la del último autorizado.
        "A-4": hoy.isoformat(),
        "A-5": hoy.isoformat(),
        "A-6": hoy.isoformat(),
    }


def test_la_fecha_de_la_serie_respeta_el_emisor(entorno):
    from libracore.venta_facturacion import fecha_de_la_factura_en_la_serie

    hoy = date(2026, 10, 23)
    emisor = db_arca.crear_arca_config("Otra Razon Ficticia", "20000000002", 3, "", "", ambiente="homologacion")
    db_facturas.create_factura(11, 3, 1, "2026-10-22", "", "X", 5, [], 1.0, 0.0, 1.0, ambiente="homologacion",
                               emisor_id=emisor)
    # La serie de ese emisor llega al 22: una fecha del 20 sube al 22. La del emisor por defecto no tiene piso.
    assert fecha_de_la_factura_en_la_serie(
        "2026-10-20", 3, 11, "homologacion", emisor_id=emisor, hoy=hoy) == date(2026, 10, 22)
    assert fecha_de_la_factura_en_la_serie("2026-10-20", 3, 11, "homologacion", hoy=hoy) == date(2026, 10, 20)
    assert fecha_de_la_factura_en_la_serie(
        "2026-10-20", 3, 11, "produccion", emisor_id=emisor, hoy=hoy) == date(2026, 10, 20)


# ── ta_para_el_nodo con servicio ─────────────────────────────────────────────


@pytest.fixture
def wsaa(monkeypatch):
    pedidos = []

    async def autenticar(cert, clave, ambiente="homologacion", servicio="wsfe"):
        pedidos.append((cert, clave, ambiente, servicio))
        return {"token": f"T-{servicio}", "sign": "S", "expiracion": "2099-01-01T00:00:00"}

    monkeypatch.setattr(arca_wsaa, "autenticar", autenticar)
    return pedidos


def _par(tmp_path, nombre):
    cert, clave = tmp_path / f"{nombre}.crt", tmp_path / f"{nombre}.key"
    cert.write_text("c")
    clave.write_text("k")
    return str(cert), str(clave)


def test_ta_para_el_nodo_de_facturacion_sigue_igual_y_dice_su_servicio(entorno, wsaa, monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    cert, clave = _par(entorno["tmp"], "wsfe")
    db_arca.crear_arca_config("Comercio Ficticio", "20000000001", 3, clave, cert, ambiente="produccion")
    ta = asyncio.run(nodo_offline.ta_para_el_nodo())
    assert ta["token"] == "T-wsfe" and ta["servicio"] == "wsfe" and ta["ambiente"] == "produccion"
    assert ta["cuit"] == "20000000001" and ta["emisor_id"]
    assert wsaa == [(cert, clave, "produccion", "wsfe")]
    # El servicio es un dato: sin él, lo de siempre; con mayúsculas y espacios, también.
    assert asyncio.run(nodo_offline.ta_para_el_nodo(servicio=" WSFE "))["servicio"] == "wsfe"


def test_ta_para_el_nodo_de_wscpe_usa_el_par_del_servicio_y_no_el_de_facturacion(entorno, wsaa, monkeypatch):
    from libracore.db import arca_credenciales_servicio as db_servicio

    monkeypatch.delenv("ENV", raising=False)
    fcert, fclave = _par(entorno["tmp"], "wsfe")
    db_arca.crear_arca_config("Comercio Ficticio", "20000000001", 3, fclave, fcert, ambiente="produccion")
    hcert, hclave = _par(entorno["tmp"], "cpe-homologacion")
    db_servicio.guardar_paths_de_servicio("agencia", "wscpe", "homologacion", certificado_path=hcert, clave_path=hclave)

    ta = asyncio.run(nodo_offline.ta_para_el_nodo(servicio="wscpe", empresa="agencia"))
    # Sólo hay par de homologación: se usa ése, con su ambiente (no el de arca_config, que dice producción).
    assert wsaa == [(hcert, hclave, "homologacion", "wscpe")]
    assert ta == {"token": "T-wscpe", "sign": "S", "expiracion": "2099-01-01T00:00:00", "ambiente": "homologacion",
                  "servicio": "wscpe", "cuit": None, "emisor_id": None}

    # Con el de producción cargado, manda producción.
    pcert, pclave = _par(entorno["tmp"], "cpe-produccion")
    db_servicio.guardar_paths_de_servicio("agencia", "wscpe", "produccion", certificado_path=pcert, clave_path=pclave)
    assert asyncio.run(nodo_offline.ta_para_el_nodo(servicio="wscpe", empresa="agencia"))["ambiente"] == "produccion"
    assert wsaa[-1] == (pcert, pclave, "produccion", "wscpe")
    # Y se puede pedir el ambiente.
    assert asyncio.run(nodo_offline.ta_para_el_nodo(
        servicio="wscpe", empresa="agencia", ambiente="homologacion"))["ambiente"] == "homologacion"


def test_ta_para_el_nodo_de_wscpe_pide_empresa_y_par(entorno, wsaa, monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    with pytest.raises(ValueError, match="empresa"):
        asyncio.run(nodo_offline.ta_para_el_nodo(servicio="wscpe"))
    with pytest.raises(NodoSinArca, match="wscpe"):
        asyncio.run(nodo_offline.ta_para_el_nodo(servicio="wscpe", empresa="agencia"))
    with pytest.raises(NodoSinArca):
        asyncio.run(nodo_offline.ta_para_el_nodo(servicio="wsfe"))     # sin arca_config
    assert wsaa == []      # ni se intentó autenticar


@pytest.mark.parametrize("servicio", ["wspadron", "ws_sr_padron_a13", "", "wsfecred"])
def test_un_servicio_que_no_se_presta_no_se_presta(entorno, wsaa, servicio):
    with pytest.raises(ValueError, match="no se presta"):
        asyncio.run(nodo_offline.ta_para_el_nodo(servicio=servicio, empresa="agencia"))
    assert wsaa == []


# ── La API ───────────────────────────────────────────────────────────────────


def _app(cola, **kw) -> TestClient:
    app = FastAPI()
    app.include_router(build_cola_del_nodo_router(cola=cola, usuario_actual=lambda: USUARIO, **kw))
    return TestClient(app)


def test_la_api_lista_emite_y_resuelve(producto, red, con_arca):
    cola = producto.cola()
    red.en_linea = False
    _cpe(cola, "1")
    _cpe(cola, "2")
    _doc(cola, "A-1")
    api = _app(cola)

    r = api.get("/api/nodo/cola").json()
    assert r["es_nodo"] and not r["en_linea"] and r["node_id"] == NODO
    assert r["cuentas"]["esperando"] == 3 and r["a_revisar"] == 0
    assert [p["origen_id"] for p in r["pedidos"]] == ["A-1", "2", "1"]     # los más nuevos primero
    assert [p["origen_id"] for p in api.get("/api/nodo/cola?estado=esperando&tipo=cpe").json()["pedidos"]] == ["2", "1"]
    assert api.get("/api/nodo/cola?estado=volando").status_code == 422

    sin_red = api.post("/api/nodo/cola/emitir")
    assert sin_red.status_code == 409 and sin_red.json()["detail"]["codigo"] == "sin_conexion"

    red.en_linea = True
    producto.falla = lambda pedido: (_ for _ in ()).throw(ErrorWscpe("949 mal", [(949, "mal")])) if pedido["origen_id"] == "1" else None
    hecho = api.post("/api/nodo/cola/emitir").json()
    assert len(hecho["emitidos"]) == 2 and hecho["a_revisar"][0]["origen_id"] == "1"
    revisar = api.get("/api/nodo/cola").json()
    assert revisar["a_revisar"] == 1 and revisar["cuentas"]["emitido"] == 2
    uno = next(p for p in revisar["pedidos"] if p["estado"] == "a_revisar")

    assert api.post("/api/nodo/cola/9999/descartar").status_code == 404
    assert api.post(f"/api/nodo/cola/{uno['id']}/emitido", json={"resultado": {}}).status_code == 409
    producto.falla = None
    assert api.post(f"/api/nodo/cola/{uno['id']}/reintentar", json={"datos": {"titular": "30000000019"}}
                    ).json()["estado"] == "esperando"
    assert api.post(f"/api/nodo/cola/{uno['id']}/descartar", json={"motivo": "se canceló el viaje"}
                    ).json()["estado"] == "descartado"
    assert db.get_pedido(uno["id"])["ultimo_error"] == "se canceló el viaje"
    assert api.post(f"/api/nodo/cola/{uno['id']}/descartar").status_code == 409     # ya está descartado


def test_la_api_pide_confirmar_un_incierto_y_respeta_la_guarda_de_resolver(producto, red, con_arca):
    from fastapi import HTTPException

    cola = producto.cola()
    pedido, _ = _cpe(cola, "1")
    producto.falla = EmisionIncierta("ARCA no contestó", sucursal=1, nro_orden=77, tipo_cpe=74)
    _correr(cola)

    api = _app(cola)
    r = api.post(f"/api/nodo/cola/{pedido['id']}/reintentar")
    assert r.status_code == 422 and r.json()["detail"]["codigo"] == "confirmacion_requerida"
    ok = api.post(f"/api/nodo/cola/{pedido['id']}/reintentar", json={"confirmo_que_no_salio": True})
    assert ok.status_code == 200 and ok.json()["estado"] == "esperando"

    def solo_admin():
        raise HTTPException(403, "sólo administradores")

    cerrada = _app(cola, resolver_con=solo_admin)
    assert cerrada.post(f"/api/nodo/cola/{pedido['id']}/descartar").status_code == 403
    assert cerrada.get("/api/nodo/cola").status_code == 200      # leer no pide ser administrador
    assert db.get_pedido(pedido["id"])["estado"] == "esperando"


def test_la_api_fuera_de_un_nodo_contesta_y_no_hace_nada(producto):
    cola = ColaDeArca(producto=PRODUCTO, emisores={"cpe": producto.emitir_cpe})
    api = _app(cola)
    r = api.get("/api/nodo/cola").json()
    assert r["es_nodo"] is False and r["pedidos"] == [] and r["a_revisar"] == 0
    assert api.post("/api/nodo/cola/emitir").json()["es_nodo"] is False


# ── `GET /api/nodo/conexion` de un producto con cola y sin ventas (ADR-084) ────────────────────────────────────────


def test_la_conexion_sin_puerto_de_ventas_cuenta_lo_que_espera_en_la_cola(producto, red, con_arca):
    from libracore.nodo_conexion_router import build_conexion_del_nodo_router

    cola = producto.cola()
    red.en_linea = False
    app = FastAPI()
    app.include_router(build_conexion_del_nodo_router(
        conexion=cola.conexion, usuario_actual=lambda: USUARIO, a_facturar=lambda: cola_arca.pedidos_a_emitir(cola)))
    api = TestClient(app)
    assert api.get("/api/nodo/conexion").json() == {"es_nodo": True, "en_linea": False, "node_id": NODO, "a_facturar": 0}
    _doc(cola, "A-1")
    _cpe(cola, "1")
    assert api.get("/api/nodo/conexion").json() == {"es_nodo": True, "en_linea": False, "node_id": NODO, "a_facturar": 2}
    red.en_linea = True
    _correr(cola)
    assert api.get("/api/nodo/conexion").json() == {"es_nodo": True, "en_linea": True, "node_id": NODO, "a_facturar": 0}


def test_la_conexion_sin_puerto_de_ventas_dice_lo_mismo_que_la_cola(producto, red):
    """Lo que hoy averiguan LibraCargo y LibraClub con `/api/nodo/cola?limite=1`: `es_nodo`, `en_linea` y `node_id` son
    los mismos en las dos rutas, en un nodo con y sin conexión y fuera de un nodo."""
    from libracore.nodo_conexion_router import build_conexion_del_nodo_router

    for cola in (producto.cola(), ColaDeArca(producto=PRODUCTO, emisores={"cpe": producto.emitir_cpe})):
        for en_linea in (True, False):
            red.en_linea = en_linea
            app = FastAPI()
            app.include_router(build_conexion_del_nodo_router(conexion=cola.conexion, usuario_actual=lambda: USUARIO))
            app.include_router(build_cola_del_nodo_router(cola=cola, usuario_actual=lambda: USUARIO))
            api = TestClient(app)
            conexion, de_la_cola = api.get("/api/nodo/conexion").json(), api.get("/api/nodo/cola?limite=1").json()
            assert {k: conexion[k] for k in ("es_nodo", "en_linea", "node_id")} == {
                k: de_la_cola[k] for k in ("es_nodo", "en_linea", "node_id")}


# ── Una sola forma del 202 «en cola» (ADR-084) ─────────────────────────────────────────────────────────────────────


def _pedido_en_cola(cola, tipo="documento", origen_id="A-1"):
    pedido, _ = (_doc if tipo == "documento" else _cpe)(cola, origen_id)
    return pedido


def test_el_202_de_una_factura_en_cola_tiene_una_sola_forma(producto, red):
    from libracore.cola_arca_respuesta import MENSAJE_FACTURA_EN_COLA, respuesta_en_cola

    red.en_linea = False
    cola = producto.cola()
    pedido = _pedido_en_cola(cola)
    r = respuesta_en_cola(pedido)
    assert r.status_code == 202
    assert json.loads(r.body) == {
        "en_cola": True, "codigo": "en_cola", "estado": "esperando", "pedido_id": pedido["id"],
        "mensaje": MENSAJE_FACTURA_EN_COLA, "motivo": ""}


def test_el_202_de_una_carta_de_porte_es_cpe_en_cola_y_dice_que_el_camion_no_puede_viajar(producto, red):
    from libracore.cola_arca_respuesta import respuesta_en_cola

    red.en_linea = False
    pedido = _pedido_en_cola(producto.cola(), "cpe", "1")
    cuerpo = json.loads(respuesta_en_cola(pedido).body)
    assert cuerpo["codigo"] == "cpe_en_cola" and cuerpo["en_cola"] is True and cuerpo["estado"] == "esperando"
    assert "NO hay una CPE válida" in cuerpo["mensaje"] and "no puede viajar" in cuerpo["mensaje"]


def test_el_cuerpo_unico_cubre_las_tres_formas_que_los_productos_mandaban_hasta_ahora(producto, red):
    """Cada producto leía su forma: LibraCargo `{codigo, mensaje, pedido_id, estado, motivo}`, LibraClub `{en_cola, estado,
    pedido_id, mensaje}` y GestioLibra `{en_cola, estado, pedido_id, motivo, mensaje}`. La única es la unión: cambiar a
    ella no le quita a ninguna pantalla un campo que lee."""
    from libracore.cola_arca_respuesta import cuerpo_en_cola

    red.en_linea = False
    cola = producto.cola()
    for tipo, codigo in (("documento", "en_cola"), ("cpe", "cpe_en_cola")):
        cuerpo = cuerpo_en_cola(_pedido_en_cola(cola, tipo, "7"))
        assert {"codigo", "mensaje", "pedido_id", "estado", "motivo"} <= set(cuerpo)          # LibraCargo
        assert {"en_cola", "estado", "pedido_id", "mensaje"} <= set(cuerpo)                   # LibraClub
        assert {"en_cola", "estado", "pedido_id", "motivo", "mensaje"} <= set(cuerpo)         # GestioLibra
        assert cuerpo["codigo"] == codigo
        # y lo que libra-ui reconoce (`esRespuestaEnCola`) por los dos caminos
        assert cuerpo["en_cola"] is True and cuerpo["codigo"] in ("en_cola", "cpe_en_cola")


def test_el_mensaje_lo_puede_poner_el_producto_y_el_motivo_sale_del_pedido(producto, red):
    from libracore.cola_arca_respuesta import cuerpo_en_cola

    red.en_linea = False
    cola = producto.cola()
    pedido = _pedido_en_cola(cola)
    assert cuerpo_en_cola(pedido, mensaje="La factura sale al volver la conexión.")["mensaje"] == \
        "La factura sale al volver la conexión."
    pedido["ultimo_error"] = "Sin conexión con el central."
    cuerpo = cuerpo_en_cola(pedido)
    assert cuerpo["motivo"] == "Sin conexión con el central." and cuerpo["mensaje"] != cuerpo["motivo"]
    pedido["ultimo_error"] = None
    assert cuerpo_en_cola(pedido)["motivo"] == ""


@pytest.mark.parametrize("estado", ["a_revisar", "incierto"])
def test_un_pedido_para_revisar_lleva_lo_que_dijo_arca_como_mensaje(producto, red, estado):
    """El kit muestra `mensaje` antes que `motivo` en una revisión: con el texto genérico, la persona no vería qué dijo ARCA."""
    from libracore.cola_arca_respuesta import MENSAJE_PARA_REVISAR, cuerpo_en_cola

    pedido = {"id": 4, "estado": estado, "origen_tipo": "documento", "ultimo_error": "ARCA rechazó: CUIT inválido"}
    cuerpo = cuerpo_en_cola(pedido)
    assert cuerpo["estado"] == estado and cuerpo["mensaje"] == cuerpo["motivo"] == "ARCA rechazó: CUIT inválido"
    assert cuerpo_en_cola({**pedido, "ultimo_error": ""})["mensaje"] == MENSAJE_PARA_REVISAR


@pytest.mark.parametrize("estado", ["emitido", "descartado", "volando", None])
def test_un_pedido_que_ya_no_esta_en_la_cola_no_se_contesta_como_en_cola(estado):
    from libracore.cola_arca_respuesta import cuerpo_en_cola, respuesta_en_cola

    pedido = {"id": 4, "estado": estado, "origen_tipo": "documento", "ultimo_error": ""}
    with pytest.raises(ValueError, match="no está en la cola"):
        cuerpo_en_cola(pedido)
    with pytest.raises(ValueError):
        respuesta_en_cola(pedido)


def test_un_pedido_que_se_emite_con_red_no_pasa_por_el_202(producto, red, con_arca):
    """Con conexión `encolar_y_emitir` devuelve el pedido `emitido`: el producto contesta el comprobante, y el helper se
    niega a decir «en cola»."""
    from libracore.cola_arca_respuesta import respuesta_en_cola

    cola = producto.cola()
    pedido = asyncio.run(cola_arca.encolar_y_emitir(cola, "documento", "A-9", datos={"total": 1210.0}))
    assert pedido["estado"] == "emitido"
    with pytest.raises(ValueError):
        respuesta_en_cola(pedido)


def test_el_flujo_completo_sin_red_contesta_202_desde_un_endpoint_y_con_red_ya_no(producto, red, con_arca):
    """Lo que hace el endpoint de un producto: pide el comprobante a la cola y, según cómo quedó, contesta el 202 o el 201."""
    from libracore.cola_arca_respuesta import respuesta_en_cola

    cola = producto.cola()
    app = FastAPI()

    @app.post("/facturar/{orden}")
    def facturar(orden: str):
        pedido = asyncio.run(cola_arca.encolar_y_emitir(cola, "documento", orden, datos={"total": 500.0}))
        if pedido["estado"] == "emitido":
            return JSONResponse(status_code=201, content=pedido["resultado"])
        return respuesta_en_cola(pedido)

    api = TestClient(app)
    red.en_linea = False
    r = api.post("/facturar/A-1")
    assert r.status_code == 202 and r.json()["en_cola"] is True and r.json()["estado"] == "esperando"
    assert r.json()["pedido_id"] == db.listar()[0]["id"]
    # Pedirla de nuevo no la duplica y vuelve a decir lo mismo.
    assert api.post("/facturar/A-1").json()["pedido_id"] == r.json()["pedido_id"] and len(db.listar()) == 1
    red.en_linea = True
    sale = api.post("/facturar/A-2")
    assert sale.status_code == 201 and sale.json()["cae"]


# ── La migración ─────────────────────────────────────────────────────────────

RAIZ = Path(__file__).resolve().parents[1]


def _alembic(destino: str, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=RAIZ, env={**os.environ, "DATABASE_URL": destino, "PYTHONPATH": str(RAIZ)}, capture_output=True, text=True)


def test_la_0029_crea_la_cola_en_una_base_de_antes_y_no_se_baja(tmp_path):
    import sqlite3

    destino = str(tmp_path / "antes.db")
    assert _alembic(destino, "upgrade", "0028_caja_del_nodo").returncode == 0
    # Una base de antes de la 0029 no tiene la tabla (la 0001 de hoy ya la crea con `init_core_schema`: se la sacamos).
    conn = sqlite3.connect(destino)
    conn.execute("DROP TABLE cola_arca")
    conn.commit()
    conn.close()

    assert _alembic(destino, "upgrade", "head").returncode == 0
    conn = sqlite3.connect(destino)
    try:
        columnas = {f[1] for f in conn.execute("PRAGMA table_info(cola_arca)").fetchall()}
        assert {"origen_producto", "origen_instancia", "origen_tipo", "origen_id", "estado", "datos", "fecha_ref",
                "concepto", "intentos", "ultimo_error", "resultado", "revision"} <= columnas
        indices = {f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert {"idx_cola_arca_origen", "idx_cola_arca_estado"} <= indices
        assert conn.execute("SELECT COUNT(*) FROM cola_arca").fetchone()[0] == 0     # nace vacía
    finally:
        conn.close()

    # Idempotente, y no se baja.
    assert _alembic(destino, "upgrade", "head").returncode == 0
    bajar = _alembic(destino, "downgrade", "0028_caja_del_nodo")
    assert bajar.returncode != 0 and "No se baja" in bajar.stderr
