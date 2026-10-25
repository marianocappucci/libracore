"""`GET /api/nodo/conexion` sin `PuertoDeVentas` (ADR-084).

LibraCargo, LibraClub, GestioLibra y MedLibra no tienen puerto de ventas: hasta ADR-083 el cartel «sin conexión» preguntaba a
`GET /api/nodo/cola?limite=1` (o la ruta se escribía a mano). Acá se fija que el constructor genérico contesta lo mismo que el
de ventas, que no arrastra `venta_facturacion` y que el de ventas no cambió. La parte con una cola real (SQLite y
PostgreSQL) está en `test_cola_de_comprobantes.py`.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracore.nodo_conexion_router import build_conexion_del_nodo_router
from libracore.nodo_offline import FUERA_DE_NODO, ConexionDelNodo
from libracore.venta_facturacion import PuertoDeVentas
from libracore.ventas_cobro_router import build_facturacion_del_nodo_router

NODO = "nodo-consultorio-01"
USUARIO = {"id": 1, "username": "mostrador", "role": "staff"}


class Red:
    def __init__(self, nodo=NODO, en_linea=True):
        self.nodo, self.en_linea = nodo, en_linea

    def conexion(self) -> ConexionDelNodo:
        return ConexionDelNodo(nodo=lambda: self.nodo, en_linea=lambda: self.en_linea)


def _api(router) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _conexion(conexion, **kw) -> TestClient:
    return _api(build_conexion_del_nodo_router(conexion=conexion, usuario_actual=lambda: USUARIO, **kw))


def test_fuera_de_un_nodo_no_es_un_nodo_y_esta_en_linea():
    assert _conexion(FUERA_DE_NODO).get("/api/nodo/conexion").json() == {
        "es_nodo": False, "en_linea": True, "node_id": None, "a_facturar": 0}


def test_un_nodo_dice_si_ve_al_central_y_lo_consulta_en_cada_pedido():
    red = Red(en_linea=False)
    api = _conexion(red.conexion())
    assert api.get("/api/nodo/conexion").json() == {"es_nodo": True, "en_linea": False, "node_id": NODO, "a_facturar": 0}
    red.en_linea = True
    assert api.get("/api/nodo/conexion").json()["en_linea"] is True
    red.nodo = None                      # ya no es un nodo: se ve en la próxima consulta, no al montar
    assert api.get("/api/nodo/conexion").json() == {"es_nodo": False, "en_linea": True, "node_id": None, "a_facturar": 0}


def test_si_no_se_puede_saber_si_esta_en_linea_dice_que_no():
    def roto():
        raise OSError("el archivo de estado no se puede leer")

    conexion = ConexionDelNodo(nodo=lambda: NODO, en_linea=roto)
    assert _conexion(conexion).get("/api/nodo/conexion").json()["en_linea"] is False


def test_a_facturar_lo_pone_el_producto_si_tiene_algo_que_contar():
    cuenta = {"n": 3}
    api = _conexion(Red().conexion(), a_facturar=lambda: cuenta["n"])
    assert api.get("/api/nodo/conexion").json()["a_facturar"] == 3
    cuenta["n"] = 0
    assert api.get("/api/nodo/conexion").json()["a_facturar"] == 0


def test_el_prefijo_se_puede_cambiar():
    api = _conexion(Red().conexion(), prefix="/nodo")
    assert api.get("/nodo/conexion").status_code == 200
    assert api.get("/api/nodo/conexion").status_code == 404


def test_pide_sesion_con_la_dependencia_del_producto():
    def sin_sesion():
        raise HTTPException(401, "No autenticado")

    api = _api(build_conexion_del_nodo_router(conexion=Red().conexion(), usuario_actual=sin_sesion))
    assert api.get("/api/nodo/conexion").status_code == 401


def test_solo_lee_no_hay_otras_rutas():
    router = build_conexion_del_nodo_router(conexion=FUERA_DE_NODO, usuario_actual=lambda: USUARIO)
    assert [(sorted(r.methods), r.path) for r in router.routes] == [(["GET"], "/api/nodo/conexion")]


@pytest.mark.parametrize("nodo", [None, NODO])
def test_contesta_lo_mismo_que_la_ruta_del_punto_de_venta(nodo, monkeypatch):
    """Compatibilidad: quien ya monta `build_facturacion_del_nodo_router` ve el mismo cuerpo, y quien migra al genérico
    también. El `a_facturar` de ventas lo cuenta la bandeja (una base); acá se la reemplaza por una vacía."""
    from libracore import ventas_cobro_router

    monkeypatch.setattr(ventas_cobro_router, "ventas_a_facturar", lambda puerto: 0)
    red = Red(nodo=nodo, en_linea=False)
    ventas = MagicMock(spec=PuertoDeVentas)
    ventas.conexion = red.conexion()
    viejo = _api(build_facturacion_del_nodo_router(ventas=ventas, usuario_actual=lambda: USUARIO))
    nuevo = _conexion(red.conexion())
    assert viejo.get("/api/nodo/conexion").json() == nuevo.get("/api/nodo/conexion").json()


def test_el_constructor_generico_no_necesita_el_puerto_de_ventas():
    """Es la razón de ser del módulo: importarlo no arrastra `venta_facturacion` (ni, por él, la capa de ventas)."""
    codigo = (
        "import sys\n"
        "import libracore.nodo_conexion_router\n"
        "malos = sorted(m for m in sys.modules if m in ('libracore.venta_facturacion', 'libracore.ventas_cobro_router', "
        "'libracommerce'))\n"
        "assert not malos, malos\n"
    )
    r = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
