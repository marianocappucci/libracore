"""La barra de estado del pie (ADR-075): `GET /api/estado-del-sistema` y cada uno de sus bloques.

Datos ficticios: CUIT 20-00000000-1, «Empresa de Prueba», nodo `nodo-s02`. La base es real (SQLite y PostgreSQL) para
ARCA, que lee `arca_config` y `arca_credenciales_servicio`; el resto no toca la base.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import _write_cert_key
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore import estado_del_sistema as eds
from libracore import fechas as reloj
from libracore.db import arca_config as db_arca
from libracore.db import arca_credenciales_servicio as db_servicio
from libracore.db import core
from libracore.db.schema import init_core_schema
from libracore.nodo_offline import ConexionDelNodo

ADMIN = {"id": 1, "username": "admin", "nombre": "Ana Prueba", "role": "admin"}
CAJERO = {"id": 2, "username": "caja1", "nombre": "Carlos Ficticio", "role": "cajero", "sucursal_nombre": "Centro"}
NODO = "nodo-s02"


@pytest.fixture(params=["sqlite", "postgres"])
def base(request, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("LIBRA_VERSION", raising=False)
    monkeypatch.delenv("LIBRAEDGE_ESTADO", raising=False)
    import libracore.config_manager as cm
    importlib.reload(cm)
    bases = request.getfixturevalue("bases") if request.param == "postgres" else None
    destino = str(tmp_path / "instancia.db") if bases is None else bases.nueva()
    core.configure(db_path=destino)
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()
    conn.close()
    yield tmp_path
    core._db_path = None
    core._database_url = None


def _con_certificado(tmp_path, nombre="fact", dias=365, desde=0):
    cert, clave = _write_cert_key(tmp_path, nombre, days_valid=dias, not_before_offset_days=desde)
    return cert, clave


def _arca(tmp_path, *, dias=365, desde=0):
    cert, clave = _con_certificado(tmp_path, dias=dias, desde=desde)
    db_arca.crear_arca_config("Empresa de Prueba", "20000000001", 1, clave, cert, ambiente="produccion")
    return cert


def _cliente(**opciones) -> TestClient:
    usuario = opciones.pop("usuario", ADMIN)
    app = FastAPI()
    app.include_router(eds.build_estado_del_sistema_router(usuario_actual=lambda: usuario, **opciones))
    return TestClient(app)


# ── ARCA ────────────────────────────────────────────────────────────────────


def test_sin_certificado_arca_es_null(base):
    assert eds.estado_arca() is None
    db_arca.crear_arca_config("Empresa de Prueba", "20000000001", 1, "", "", ambiente="produccion")
    assert eds.estado_arca() is None, "una configuración sin certificado en disco no factura con ARCA"


def test_certificado_vigente_es_ok(base):
    _arca(base, dias=400)
    arca = eds.estado_arca()
    assert arca["nivel"] == "ok" and arca["vencido"] is False and arca["servicio"] == "facturacion"
    assert 395 <= arca["dias_para_vencer"] <= 400 and arca["certificados"] == 1
    assert len(arca["vence"]) == 10 and arca["vence"][4] == "-", "la fecha va en ISO, la pantalla la formatea"


def test_certificado_por_vencer_es_atencion(base):
    _arca(base, dias=20)
    assert eds.estado_arca()["nivel"] == "atencion"


def test_el_umbral_es_de_30_dias(base):
    _arca(base, dias=40)
    assert eds.estado_arca()["nivel"] == "ok"
    assert eds.estado_arca(dias_aviso=45)["nivel"] == "atencion"


def test_certificado_vencido_es_error(base):
    _arca(base, dias=10, desde=-30)
    arca = eds.estado_arca()
    assert arca["nivel"] == "error" and arca["vencido"] is True and arca["dias_para_vencer"] < 0


def test_certificado_ilegible_es_error(base):
    cert = _arca(base)
    with open(cert, "w", encoding="utf-8") as archivo:
        archivo.write("esto no es un certificado")
    arca = eds.estado_arca()
    assert arca["nivel"] == "error" and arca["ilegible"] is True and arca["vence"] is None


def test_el_servicio_que_mas_apura_gana(base):
    _arca(base, dias=400)
    cert, clave = _con_certificado(base, nombre="cpe", dias=10)
    db_servicio.guardar_paths_de_servicio("Empresa de Prueba", "wscpe", "produccion",
                                          certificado_path=cert, clave_path=clave)
    arca = eds.estado_arca()
    assert arca["servicio"] == "wscpe" and arca["nivel"] == "atencion" and arca["certificados"] == 2


def test_una_demo_no_tiene_arca(base, monkeypatch):
    _arca(base, dias=10, desde=-30)
    monkeypatch.setenv("DEMO_MODE", "1")
    assert eds.estado_arca() is None


# ── Respaldo ────────────────────────────────────────────────────────────────


def _zip(carpeta, nombre, horas_atras):
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / nombre
    ruta.write_bytes(b"PK")
    cuando = time.time() - horas_atras * 3600
    os.utime(ruta, (cuando, cuando))
    return ruta


def test_sin_respaldos_es_atencion(tmp_path):
    r = eds.estado_respaldo(tmp_path / "backups")
    assert r == {"nivel": "atencion", "ultimo": None, "horas_desde_el_ultimo": None, "motivo": "sin_respaldos",
                 "nocturno": None, "externo": None}


def test_el_ultimo_respaldo_es_por_fecha_y_no_por_nombre(tmp_path):
    carpeta = tmp_path / "backups"
    _zip(carpeta, "backup_manual_20261001_100000.zip", 400)
    _zip(carpeta, "backup_automatico_20261020_030000.zip", 3)
    r = eds.estado_respaldo(carpeta)
    assert r["nivel"] == "ok" and 2.9 <= r["horas_desde_el_ultimo"] <= 3.1
    assert r["ultimo"].endswith("+00:00")


def test_respaldo_viejo_es_atencion(tmp_path):
    carpeta = tmp_path / "backups"
    _zip(carpeta, "backup_automatico_20261018_030000.zip", 50)
    assert eds.estado_respaldo(carpeta)["nivel"] == "atencion"
    assert eds.estado_respaldo(carpeta, horas=72)["nivel"] == "ok"


def _intento(carpeta, *, ok, horas_atras, error=None):
    from libracore.respaldo_nocturno import escribir_intento

    escribir_intento(carpeta, ok=ok, error=error, cuando=datetime.now(UTC) - timedelta(hours=horas_atras))


def test_respaldo_de_anoche_que_fallo_es_error_y_sin_el_texto_del_error(tmp_path):
    carpeta = tmp_path / "backups"
    _zip(carpeta, "backup_automatico_20261019_030000.zip", 27)
    _intento(carpeta, ok=False, horas_atras=3, error="pg_dump: connection to postgresql://u:clave@db/x failed")
    r = eds.estado_respaldo(carpeta)
    assert r["nivel"] == "error" and r["motivo"] == "fallo" and r["nocturno"]["fallo"] is True
    assert r["nocturno"]["ultimo_intento"].endswith("+00:00")
    assert "pg_dump" not in json.dumps(r) and "clave" not in json.dumps(r)


def test_un_fallo_anterior_al_ultimo_zip_ya_no_cuenta(tmp_path):
    """Falló a la noche y alguien hizo un respaldo a mano después: hay un ZIP más nuevo que el fallo."""
    carpeta = tmp_path / "backups"
    _intento(carpeta, ok=False, horas_atras=5)
    _zip(carpeta, "backup_manual_20261020_080000.zip", 1)
    r = eds.estado_respaldo(carpeta)
    assert r["nivel"] == "ok" and r["motivo"] is None and r["nocturno"]["fallo"] is True


def test_sin_intento_reciente_el_respaldo_viejo_es_que_no_corrio(tmp_path):
    carpeta = tmp_path / "backups"
    _zip(carpeta, "backup_automatico_20261018_030000.zip", 50)
    assert eds.estado_respaldo(carpeta)["motivo"] == "no_corrio"
    _intento(carpeta, ok=True, horas_atras=50)
    assert eds.estado_respaldo(carpeta)["motivo"] == "no_corrio"


def test_un_intento_bueno_reciente_con_zip_viejo_es_viejo(tmp_path):
    carpeta = tmp_path / "backups"
    _zip(carpeta, "backup_automatico_20261018_030000.zip", 50)
    _intento(carpeta, ok=True, horas_atras=2)
    r = eds.estado_respaldo(carpeta)
    assert (r["nivel"], r["motivo"], r["nocturno"]["fallo"]) == ("atencion", "viejo", False)


def test_un_nocturno_ilegible_se_ignora(tmp_path):
    carpeta = tmp_path / "backups"
    _zip(carpeta, "backup_automatico_20261020_030000.zip", 3)
    (carpeta / ".nocturno.json").write_text("{roto")
    r = eds.estado_respaldo(carpeta)
    assert (r["nivel"], r["motivo"], r["nocturno"]) == ("ok", None, None)


def test_copia_externa_que_fallo_es_error_y_sin_el_texto_del_error(tmp_path):
    carpeta = tmp_path / "backups"
    _zip(carpeta, "backup_automatico_20261020_030000.zip", 3)
    (carpeta / ".externo.json").write_text(json.dumps(
        {"ok": False, "error": "invalid_grant: token revocado por usuario@ejemplo.com", "cuando": "2026-10-20T03:10:00"}))
    r = eds.estado_respaldo(carpeta)
    assert r["nivel"] == "error" and r["externo"]["fallo"] is True and r["externo"]["al_dia"] is False
    assert "invalid_grant" not in json.dumps(r) and "ejemplo.com" not in json.dumps(r)


def test_copia_externa_al_dia(tmp_path):
    carpeta = tmp_path / "backups"
    _zip(carpeta, "backup_automatico_20261020_030000.zip", 3)
    (carpeta / ".externo.json").write_text(json.dumps(
        {"ok": True, "cifrado": True, "cuando": reloj.ahora().replace(tzinfo=None).isoformat(timespec="seconds")}))
    r = eds.estado_respaldo(carpeta)
    assert r["nivel"] == "ok" and r["externo"]["al_dia"] is True and r["externo"]["fallo"] is False


# ── LibraEdge ───────────────────────────────────────────────────────────────


def _estado_del_nodo(tmp_path, *, en_linea=True, pendientes=3, hace=timedelta(seconds=30)):
    ruta = tmp_path / "estado.json"
    ruta.write_text(json.dumps({
        "node_id": NODO, "en_linea": en_linea, "pendientes": pendientes, "cursor": 7,
        "ultima_sincronizacion_ok": (datetime.now(UTC) - hace).isoformat(),
        "ultimo_error": "ConnectError: https://central.ejemplo.com.ar/sync/v1/push",
    }), encoding="utf-8")
    return str(ruta)


def _conexion(tmp_path, **estado):
    from libracore.nodo_offline import en_linea_segun_estado

    return ConexionDelNodo(nodo=lambda: NODO, en_linea=en_linea_segun_estado(_estado_del_nodo(tmp_path, **estado)))


def test_fuera_de_un_nodo_libraedge_es_null():
    assert eds.estado_libraedge_del_nodo(ConexionDelNodo()) is None


def test_nodo_en_linea_con_pendientes(tmp_path):
    conexion = _conexion(tmp_path, pendientes=3)
    bloque = eds.estado_libraedge_del_nodo(conexion, a_facturar=lambda: 2, ruta_estado=str(tmp_path / "estado.json"))
    assert bloque["rol"] == "nodo" and bloque["en_linea"] is True and bloque["nivel"] == "ok"
    assert bloque["pendientes_de_subir"] == 3 and bloque["a_facturar"] == 2
    assert "ultimo_error" not in bloque and "central.ejemplo" not in json.dumps(bloque)


def test_nodo_sin_conexion_es_atencion(tmp_path, monkeypatch):
    conexion = _conexion(tmp_path, en_linea=False, pendientes=12)
    monkeypatch.setenv("LIBRAEDGE_ESTADO", str(tmp_path / "estado.json"))
    bloque = eds.estado_libraedge_del_nodo(conexion)
    assert bloque["en_linea"] is False and bloque["nivel"] == "atencion" and bloque["pendientes_de_subir"] == 12


def test_nodo_sin_archivo_de_estado_no_inventa_pendientes(tmp_path, monkeypatch):
    monkeypatch.delenv("LIBRAEDGE_ESTADO", raising=False)
    bloque = eds.estado_libraedge_del_nodo(ConexionDelNodo(nodo=lambda: NODO, en_linea=lambda: False))
    assert bloque["pendientes_de_subir"] is None and bloque["en_linea"] is False


def test_la_bandeja_que_falla_no_rompe_el_bloque(tmp_path):
    def revienta():
        raise RuntimeError("base caída")

    bloque = eds.estado_libraedge_del_nodo(_conexion(tmp_path), a_facturar=revienta)
    assert bloque["a_facturar"] == 0


def test_central_con_nodos():
    nodos = [
        {"node_id": "nodo-1-a", "activo": True, "estado": "al_dia"},
        {"node_id": "nodo-2-b", "activo": True, "estado": "sin_noticias"},
        {"node_id": "nodo-3-c", "activo": False, "estado": "baja"},
    ]
    assert eds.resumen_de_nodos_del_central(nodos) == {"rol": "central", "nodos": 2, "sin_noticias": 1,
                                                       "nivel": "atencion"}
    assert eds.resumen_de_nodos_del_central([{"activo": False, "estado": "baja"}]) is None
    assert eds.resumen_de_nodos_del_central([]) is None


# ── El router ───────────────────────────────────────────────────────────────


def test_instancia_comun_admin(base, monkeypatch):
    monkeypatch.setenv("LIBRA_VERSION", "v2026.10.20-101500")
    _arca(base, dias=20)
    _zip(base / "backups", "backup_automatico_20261020_030000.zip", 3)
    r = _cliente(backups_dir=base / "backups").get("/api/estado-del-sistema")
    assert r.status_code == 200
    datos = r.json()
    assert datos["usuario"] == {"nombre": "Ana Prueba", "rol": "admin", "sucursal": None}
    assert datos["version"]["producto"] == "v2026.10.20-101500" and datos["version"]["motor"]
    assert datos["libraedge"] is None
    assert datos["arca"]["nivel"] == "atencion" and datos["respaldo"]["nivel"] == "ok"
    assert datos["refrescar_en_segundos"] == 300


def test_un_cajero_no_ve_arca_ni_respaldo_ni_el_motor(base):
    _arca(base, dias=20)
    datos = _cliente(usuario=CAJERO, backups_dir=base / "backups").get("/api/estado-del-sistema").json()
    assert datos["arca"] is None and datos["respaldo"] is None and datos["version"]["motor"] is None
    assert datos["usuario"] == {"nombre": "Carlos Ficticio", "rol": "cajero", "sucursal": "Centro"}


def test_un_cajero_ve_el_certificado_vencido_resumido(base):
    _arca(base, dias=10, desde=-30)
    datos = _cliente(usuario=CAJERO).get("/api/estado-del-sistema").json()
    assert datos["arca"] == {"nivel": "error", "vencido": True}


def test_el_visitante_de_una_demo_no_es_admin(base):
    visitante = {**ADMIN, "demo_readonly": True}
    datos = _cliente(usuario=visitante, backups_dir=base / "backups").get("/api/estado-del-sistema").json()
    assert datos["respaldo"] is None and datos["version"]["motor"] is None


def test_es_admin_y_sucursal_del_producto(base):
    datos = _cliente(usuario=CAJERO, es_admin=lambda u: u["username"] == "caja1",
                     sucursal=lambda u: "Sucursal Norte", backups_dir=base / "backups",
                     version=lambda: "1.6.2").get("/api/estado-del-sistema").json()
    assert datos["respaldo"] is not None and datos["usuario"]["sucursal"] == "Sucursal Norte"
    assert datos["version"]["producto"] == "1.6.2"


def test_factura_con_arca_apagado(base):
    _arca(base, dias=20)
    datos = _cliente(factura_con_arca=lambda: False).get("/api/estado-del-sistema").json()
    assert datos["arca"] is None


def test_en_un_nodo(base, tmp_path, monkeypatch):
    _arca(base, dias=10, desde=-30)  # espejada del central: en el nodo no se mira
    conexion = _conexion(tmp_path, en_linea=False, pendientes=5)
    monkeypatch.setenv("LIBRAEDGE_ESTADO", str(tmp_path / "estado.json"))
    datos = _cliente(usuario=CAJERO, conexion=conexion, a_facturar=lambda: 4,
                     backups_dir=base / "backups").get("/api/estado-del-sistema").json()
    assert datos["libraedge"]["rol"] == "nodo" and datos["libraedge"]["en_linea"] is False
    assert datos["libraedge"]["pendientes_de_subir"] == 5 and datos["libraedge"]["a_facturar"] == 4
    assert datos["arca"] is None and datos["respaldo"] is None
    assert datos["refrescar_en_segundos"] == 60


def test_en_un_nodo_con_el_puerto_de_ventas(base, tmp_path):
    class Puerto:
        conexion = ConexionDelNodo(nodo=lambda: NODO, en_linea=lambda: True)
        producto = "ventalibra"

    datos = _cliente(ventas=Puerto()).get("/api/estado-del-sistema").json()
    assert datos["libraedge"]["en_linea"] is True and datos["libraedge"]["a_facturar"] == 0


def test_central_con_nodos_sólo_para_admin(base):
    nodos = lambda: [{"activo": True, "estado": "nunca"}]  # noqa: E731
    assert _cliente(nodos_del_central=nodos).get("/api/estado-del-sistema").json()["libraedge"] == {
        "rol": "central", "nodos": 1, "sin_noticias": 1, "nivel": "atencion"}
    assert _cliente(usuario=CAJERO, nodos_del_central=nodos).get("/api/estado-del-sistema").json()["libraedge"] is None


def test_un_bloque_roto_no_rompe_la_respuesta(base, monkeypatch, caplog):
    def revienta(*a, **k):
        raise RuntimeError("certificado inaccesible")

    monkeypatch.setattr(eds, "estado_arca", revienta)

    def nodos():
        raise RuntimeError("sin tablas de libraedge")

    r = _cliente(nodos_del_central=nodos, backups_dir=base / "backups").get("/api/estado-del-sistema")
    assert r.status_code == 200
    datos = r.json()
    assert datos["arca"] is None and datos["libraedge"] is None and datos["respaldo"]["nivel"] == "atencion"


def test_sin_version_es_null(base):
    assert _cliente().get("/api/estado-del-sistema").json()["version"]["producto"] is None


def test_la_respuesta_no_lleva_rutas_ni_cuit(base, monkeypatch):
    cert = _arca(base, dias=20)
    _zip(base / "backups", "backup_automatico_20261020_030000.zip", 3)
    texto = _cliente(backups_dir=base / "backups").get("/api/estado-del-sistema").text
    assert "20000000001" not in texto and str(base) not in texto and os.path.basename(cert) not in texto
