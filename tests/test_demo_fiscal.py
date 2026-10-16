"""Una demo no muestra datos fiscales reales, y si alguien los carga, una auditoría lo dice (ADR-061).

El 2026-10-09 la demo de LibraDesk mostraba los datos fiscales de un cliente. ADR-038 cubrió `config.json`; esto fija el
resto: el emisor que sale de `arca_config`, el ambiente y las credenciales de ARCA, y `auditar_datos_fiscales`, que
devuelve lo que en una instancia de demo parece real.

Los «reales» de estos tests son inventados a propósito (`Cliente Real SRL`, CUIT con verificador válido y fuera del rango
reservado): lo que se prueba es que algo con **esa forma** se detecta, no que se conozca un dato verdadero.

Las pruebas de base corren en SQLite y en PostgreSQL (`LIBRACORE_POSTGRES_URL`).
"""
import asyncio
import importlib
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore import arca_credenciales, arca_facturacion, config_manager, emisor_del_pdf
from libracore import demo as dm
from libracore import pdf_generator as pg
from libracore.db import arca_config as db_arca
from libracore.db import arca_credenciales_servicio as db_servicio
from libracore.db import core
from libracore.db.schema import init_core_schema

REAL = {"nombre": "Cliente Real SRL", "direccion": "Calle Verdadera 1", "iibb": "123-456-7"}


def _cuit(prefijo: str, cuerpo: str) -> str:
    """Un CUIT de 11 dígitos con el verificador bien calculado (la regla pública de ARCA)."""
    base = prefijo + cuerpo
    resto = sum(int(d) * p for d, p in zip(base, (5, 4, 3, 2, 7, 6, 5, 4, 3, 2), strict=True)) % 11
    verificador = (11 - resto) % 11
    return base + str(9 if verificador == 10 else verificador)


def _con_verificador_invalido(cuit: str) -> str:
    return cuit[:10] + str((int(cuit[10]) + 1) % 10)


#: Con forma de real: verificador válido, cuerpo fuera del rango reservado.
CUIT_REAL = _cuit("30", "71000001")
#: Ficticio: verificador válido y cuerpo `99999xxx`.
CUIT_FICTICIO = _cuit("30", "99999123")
CUIT_FICTICIO_PERSONA = _cuit("27", "99999456")


@pytest.fixture(params=["sqlite", "postgres"])
def motor(request, tmp_path):
    if request.param == "postgres":
        url = os.environ.get("LIBRACORE_POSTGRES_URL")
        if not url:
            pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
        import psycopg

        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True) as c:
            c.execute("DROP SCHEMA IF EXISTS public CASCADE")
            c.execute("CREATE SCHEMA public")
        core.configure(db_path=url)
    else:
        core.configure(db_path=str(tmp_path / "demo_fiscal.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.commit()
    yield request.param
    core._db_path = None
    core._database_url = None


@pytest.fixture(autouse=True)
def _instancia(tmp_path, monkeypatch):
    """`config.json` y certificados en la carpeta del test, sin demo y sin resolvedor del producto."""
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config_manager, "CERTS_DIR", str(tmp_path / "arca_certs"))
    monkeypatch.setattr(config_manager, "_almacen", None)
    monkeypatch.setattr(pg, "FACTURAS_PDF_DIR", str(tmp_path / "facturas"))
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    config_manager.usar_empresa_demo()
    emisor_del_pdf.registrar_resolvedor(None)
    yield
    config_manager.usar_empresa_demo()
    emisor_del_pdf.registrar_resolvedor(None)


def _demo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")


def _config(tmp_path, **datos):
    (tmp_path / "config.json").write_text(json.dumps(datos), encoding="utf-8")


def _archivo(tmp_path, nombre: str) -> str:
    ruta = tmp_path / nombre
    ruta.write_text("-----BEGIN-----", encoding="utf-8")
    return str(ruta)


def _auditar(**kw):
    with core.get_connection() as conn:
        return dm.auditar_datos_fiscales(conn, **kw)


def _codigos(hallazgos) -> set[str]:
    return {h.codigo for h in hallazgos}


# ═══════════════════════════════════════════════ Qué es un documento ficticio


@pytest.mark.parametrize("valor, esperado", [
    (CUIT_FICTICIO, dm.FICTICIO),
    (CUIT_FICTICIO_PERSONA, dm.FICTICIO),
    ("30-99999900-6", dm.FICTICIO),                                  # el de EMPRESA_DEMO_POR_DEFECTO
    (CUIT_REAL, dm.PARECE_REAL),
    (_cuit("20", "30111222"), dm.PARECE_REAL),
    (_con_verificador_invalido(CUIT_REAL), dm.INVALIDO),             # no puede existir en el padrón
    (_con_verificador_invalido(CUIT_FICTICIO), dm.INVALIDO),         # en el rango, pero mal formado
    (_cuit("99", "99999123"), dm.INVALIDO),                          # prefijo que no es de CUIT
    ("", dm.VACIO), (None, dm.VACIO), ("sin cuit", dm.VACIO), ("-", dm.VACIO),
    ("99999123", dm.DNI_FICTICIO),
    ("30111222", dm.DNI),
    ("1234567", dm.DNI),
    ("12345", dm.OTRO),
    ("1234567890123", dm.OTRO),
])
def test_clasificar_documento(valor, esperado):
    assert dm.clasificar_documento(valor) == esperado


def test_el_formato_no_cambia_la_clasificacion():
    assert dm.clasificar_documento("30-99999900-6") == dm.clasificar_documento("30.99999900.6") == dm.FICTICIO
    assert dm.clasificar_documento(CUIT_REAL[:2] + "-" + CUIT_REAL[2:10] + "-" + CUIT_REAL[10]) == dm.PARECE_REAL


def test_un_producto_puede_declarar_los_que_usa():
    assert dm.clasificar_documento(CUIT_REAL, frozenset({CUIT_REAL})) == dm.FICTICIO
    assert dm.clasificar_documento(CUIT_REAL, frozenset({"20123456786"})) == dm.PARECE_REAL


def test_todo_el_rango_reservado_con_verificador_valido_es_ficticio():
    """Mil cuerpos por prefijo: el rango reservado es lo bastante grande para sembrar sin repetir."""
    cuits = [_cuit(pref, f"99999{n:03d}") for pref in ("20", "27", "30", "33") for n in range(1000)]
    assert len(set(cuits)) == 4000
    assert all(dm.clasificar_documento(c) == dm.FICTICIO for c in cuits)


def test_la_empresa_por_defecto_de_la_demo_es_ficticia():
    assert dm.es_cuit_ficticio(config_manager.EMPRESA_DEMO_POR_DEFECTO["empresa_cuit"])


def test_es_cuit_ficticio_no_acepta_un_dni():
    assert not dm.es_cuit_ficticio("99999123")
    assert not dm.es_cuit_ficticio(CUIT_REAL)
    assert not dm.es_cuit_ficticio(None)


def test_enmascarar_no_deja_el_dato_entero():
    assert dm.enmascarar("30-71000001-6").count("*") == 5
    assert CUIT_REAL[5:10] not in dm.enmascarar(CUIT_REAL)
    assert dm.enmascarar("30111222") == "30*****2"
    assert dm.enmascarar("12") == "**"


# ═══════════════════════════════════════════════ La auditoría: una demo limpia no da nada


def test_una_demo_vacia_esta_limpia(motor):
    assert _auditar() == []


def test_una_demo_sembrada_con_datos_ficticios_esta_limpia(motor, tmp_path, monkeypatch):
    """Lo que hace la semilla de un producto: empresa, clientes, proveedores y bandeja con documentos del rango reservado."""
    _demo(monkeypatch)
    _config(tmp_path, empresa_cuit=config_manager.EMPRESA_DEMO_POR_DEFECTO["empresa_cuit"],
            mp_access_token="TEST-1234-ficticio")
    with core.get_connection() as conn:
        conn.execute("INSERT INTO clients (name, cuit_dni) VALUES (?, ?)", ("Cliente Uno", CUIT_FICTICIO))
        conn.execute("INSERT INTO clients (name, cuit_dni) VALUES (?, ?)", ("Consumidor final", ""))
        conn.execute("INSERT INTO proveedores (nombre, cuit_dni) VALUES (?, ?)", ("Proveedor Uno", CUIT_FICTICIO_PERSONA))
        conn.execute(
            "INSERT INTO mp_pagos (mp_payment_id, status, monto, payer_id_number) VALUES (?,?,?,?)",
            ("demo-1", "approved", 100.0, CUIT_FICTICIO))
        conn.commit()
    assert _auditar(incluir_dudosos=True) == []


# ═══════════════════════════════════════════════ La auditoría: lo que parece real


def test_un_cuit_de_empresa_real_en_config_json(motor, tmp_path):
    _config(tmp_path, empresa_cuit=CUIT_REAL)
    [h] = _auditar()
    assert (h.codigo, h.gravedad, h.donde) == ("empresa_cuit_parece_real", dm.ALTA, "config.json empresa_cuit")


def test_el_pisado_de_la_demo_no_esconde_lo_guardado(motor, tmp_path, monkeypatch):
    """`load()` en una demo ya muestra la ficticia: la auditoría tiene que mirar lo que hay en disco, no lo que se ve."""
    _demo(monkeypatch)
    _config(tmp_path, empresa_cuit=CUIT_REAL)
    assert config_manager.load()["empresa_cuit"] != CUIT_REAL
    assert config_manager.load_sin_demo()["empresa_cuit"] == CUIT_REAL
    assert _codigos(_auditar()) == {"empresa_cuit_parece_real"}


def test_arca_configurada_en_produccion_con_su_certificado(motor, tmp_path, monkeypatch):
    cert, clave = _archivo(tmp_path, "real.crt"), _archivo(tmp_path, "real.key")
    db_arca.crear_arca_config("Cliente Real SRL", CUIT_REAL, 4, clave, cert, ambiente="produccion")
    _demo(monkeypatch)
    hallazgos = _auditar()
    assert _codigos(hallazgos) == {"empresa_cuit_parece_real", "arca_en_produccion", "arca_certificado_de_produccion"}
    assert all(h.gravedad == dm.ALTA for h in hallazgos)
    [cert_prod] = [h for h in hallazgos if h.codigo == "arca_certificado_de_produccion"]
    assert "con archivo en disco: 1" in cert_prod.detalle


def test_el_certificado_de_produccion_sin_archivo_tambien_se_avisa(motor, monkeypatch):
    db_arca.crear_arca_config("Cliente Real SRL", CUIT_FICTICIO, 1, "/no/esta.key", "/no/esta.crt", ambiente="homologacion")
    _demo(monkeypatch)
    [h] = _auditar()
    assert h.codigo == "arca_certificado_de_produccion"
    assert "con archivo en disco: 0" in h.detalle


def test_una_fila_de_arca_inactiva_tambien_cuenta(motor):
    db_arca.crear_arca_config("Vieja SA", CUIT_REAL, 1, "", "", ambiente="homologacion")
    db_arca.eliminar_arca_config("Vieja SA")
    assert "empresa_cuit_parece_real" in _codigos(_auditar())


def test_arca_ficticia_en_homologacion_y_sin_certificado_esta_limpia(motor):
    db_arca.crear_arca_config("Empresa Demo SRL", CUIT_FICTICIO, 1, "", "", ambiente="homologacion")
    assert _auditar(incluir_dudosos=True) == []


def test_un_certificado_de_homologacion_es_dudoso_no_alto(motor, tmp_path):
    db_arca.crear_arca_config("Empresa Demo SRL", CUIT_FICTICIO, 1, "", "", ambiente="homologacion")
    db_arca.actualizar_arca_config(
        "Empresa Demo SRL", certificado_path_homologacion=_archivo(tmp_path, "h.crt"),
        clave_path_homologacion=_archivo(tmp_path, "h.key"))
    assert _auditar() == []
    [h] = _auditar(incluir_dudosos=True)
    assert (h.codigo, h.gravedad) == ("arca_certificado_de_homologacion", dm.BAJA)


def test_una_credencial_de_servicio_de_produccion(motor, tmp_path):
    db_servicio.guardar_paths_de_servicio("Empresa Demo SRL", "wscpe", "produccion",
                                          certificado_path=_archivo(tmp_path, "cpe.crt"))
    db_servicio.guardar_paths_de_servicio("Empresa Demo SRL", "wscpe", "homologacion",
                                          certificado_path=_archivo(tmp_path, "cpe-h.crt"))
    [h] = _auditar()
    assert (h.codigo, h.gravedad, h.cantidad) == ("arca_servicio_de_produccion", dm.ALTA, 1)
    assert "wscpe" in h.detalle


@pytest.mark.parametrize("token, mp_ambiente_guardado, hay_hallazgo", [
    ("APP_USR-1234-real", "", True),                       # sin clasificar: no se sabe de quién es
    ("APP_USR-1234-real", "produccion", True),
    ("TEST-1234-de-prueba", "", False),                    # el prefijo es concluyente
    ("APP_USR-1234-usuario-de-prueba", "prueba", False),   # confirmada contra MercadoPago, con su huella
    ("", "", False),
])
def test_una_credencial_de_mercadopago_que_no_es_de_prueba(motor, tmp_path, token, mp_ambiente_guardado, hay_hallazgo):
    from libracore.mp_config_router import huella

    datos = {"mp_access_token": token}
    if mp_ambiente_guardado:
        datos |= {"mp_ambiente": mp_ambiente_guardado, "mp_ambiente_huella": huella(token)}
    _config(tmp_path, **datos)
    hallazgos = _auditar()
    assert _codigos(hallazgos) == ({"mp_credencial_no_es_de_prueba"} if hay_hallazgo else set())
    assert all(token not in str(h) for h in hallazgos if token)


def test_el_token_de_mercadopago_de_la_demo_viene_del_almacen_cifrado(motor):
    """El token vive en el almacén, no en el JSON: la auditoría lo ve igual."""
    class Almacen:
        def get(self, clave):
            return "APP_USR-9999-real" if clave == "mp_access_token" else ""

    config_manager.usar_almacen_de_secretos(Almacen())
    assert _codigos(_auditar()) == {"mp_credencial_no_es_de_prueba"}


def test_el_correo_cargado_es_dudoso(motor, tmp_path):
    _config(tmp_path, email_smtp_host="smtp.ejemplo.test", email_from="alguien@ejemplo.test")
    assert _auditar() == []
    [h] = _auditar(incluir_dudosos=True)
    assert (h.codigo, h.gravedad) == ("correo_cargado", dm.BAJA)
    assert "ejemplo.test" not in str(h)


# ── terceros ──


def test_un_cliente_con_cuit_de_forma_real(motor):
    with core.get_connection() as conn:
        conn.execute("INSERT INTO clients (name, cuit_dni) VALUES (?, ?)", ("Cliente Uno", CUIT_FICTICIO))
        conn.execute("INSERT INTO clients (name, cuit_dni) VALUES (?, ?)", ("Cliente Dos", CUIT_REAL))
        conn.commit()
    [h] = _auditar()
    assert (h.codigo, h.gravedad, h.donde, h.cantidad) == ("tercero_cuit_parece_real", dm.ALTA, "clients.cuit_dni", 1)
    assert "clients#2" in h.detalle


def test_el_hallazgo_no_lleva_el_dato_ni_el_nombre(motor):
    with core.get_connection() as conn:
        conn.execute("INSERT INTO clients (name, cuit_dni) VALUES (?, ?)", ("Cliente Real SRL", CUIT_REAL))
        conn.commit()
    texto = "\n".join(str(h) for h in _auditar(incluir_dudosos=True))
    assert CUIT_REAL not in texto and CUIT_REAL[2:10] not in texto
    assert "Cliente Real" not in texto
    assert dm.enmascarar(CUIT_REAL) in texto


def test_los_hallazgos_se_agrupan_por_columna(motor):
    with core.get_connection() as conn:
        for n in range(7):
            conn.execute("INSERT INTO clients (name, cuit_dni) VALUES (?, ?)", (f"C{n}", _cuit("30", f"7100{n:04d}")))
        conn.commit()
    [h] = _auditar()
    assert h.cantidad == 7
    assert "y 4 más" in h.detalle


@pytest.mark.parametrize("tabla, columna, insertar", [
    ("clients", "cuit_dni", "INSERT INTO clients (name, cuit_dni) VALUES ('x', ?)"),
    ("proveedores", "cuit_dni", "INSERT INTO proveedores (nombre, cuit_dni) VALUES ('x', ?)"),
    ("remitos", "client_cuit",
     "INSERT INTO remitos (number, date, client_name, client_cuit, items, subtotal, tax_amount, total) "
     "VALUES ('R-1', '2026-10-01', 'x', ?, '[]', 0, 0, 0)"),
    ("presupuestos", "client_cuit",
     "INSERT INTO presupuestos (number, date, valid_until, client_name, client_cuit, items, subtotal, tax_amount, total) "
     "VALUES ('P-1', '2026-10-01', '2026-11-01', 'x', ?, '[]', 0, 0, 0)"),
    ("facturas", "cliente_cuit",
     "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, items, subtotal, iva_amount, total) "
     "VALUES (11, 1, 1, '2026-10-01', ?, '[]', 0, 0, 0)"),
    ("recibos", "cliente_cuit",
     "INSERT INTO recibos (numero, fecha, cliente_razon, cliente_cuit, origen_tipo, total) "
     "VALUES (1, '2026-10-01', 'x', ?, 'venta', 0)"),
    ("comprobantes_pendientes", "cliente_cuit",
     "INSERT INTO comprobantes_pendientes (origen_producto, origen_tipo, origen_id, cliente_razon, cliente_cuit) "
     "VALUES ('p', 'remito', '1', 'x', ?)"),
    ("mp_pagos", "payer_id_number", "INSERT INTO mp_pagos (mp_payment_id, status, monto, payer_id_number) "
                                    "VALUES ('d-1', 'approved', 1, ?)"),
    ("mp_movimientos", "payer_id_number", "INSERT INTO mp_movimientos (mp_movement_id, payer_id_number) VALUES ('d-1', ?)"),
])
def test_el_motor_revisa_todas_las_columnas_de_terceros_que_conoce(motor, tabla, columna, insertar):
    with core.get_connection() as conn:
        conn.execute(insertar, (CUIT_REAL,))
        conn.commit()
    [h] = _auditar()
    assert (h.codigo, h.donde) == ("tercero_cuit_parece_real", f"{tabla}.{columna}")


def test_un_alias_de_facturacion_por_cuit_se_revisa_y_el_de_email_no(motor):
    with core.get_connection() as conn:
        cid = conn.execute("INSERT INTO clients (name, cuit_dni) VALUES ('x', ?)", (CUIT_FICTICIO,)).lastrowid
        conn.execute("INSERT INTO facturacion_alias (tipo, valor, cliente_id) VALUES ('email', 'a@b.example', ?)", (cid,))
        conn.execute("INSERT INTO facturacion_alias (tipo, valor, cliente_id) VALUES ('cuit', ?, ?)", (CUIT_REAL, cid))
        conn.commit()
    [h] = _auditar()
    assert h.donde == "facturacion_alias.valor"


def test_un_cuit_que_no_cierra_el_verificador_es_dudoso_no_alto(motor):
    """No puede existir en el padrón: no es un dato real. Pero tampoco es del rango reservado, y se dice."""
    with core.get_connection() as conn:
        conn.execute("INSERT INTO clients (name, cuit_dni) VALUES ('x', ?)", (_con_verificador_invalido(CUIT_REAL),))
        conn.execute("INSERT INTO clients (name, cuit_dni) VALUES ('y', '30111222')")
        conn.commit()
    assert _auditar() == []
    [h] = _auditar(incluir_dudosos=True)
    assert (h.codigo, h.gravedad, h.cantidad) == ("tercero_documento_sin_verificar", dm.BAJA, 2)


def test_las_columnas_propias_del_producto(motor):
    with core.get_connection() as conn:
        conn.execute("CREATE TABLE choferes_demo (id INTEGER PRIMARY KEY, dni TEXT, cuit TEXT)")
        conn.execute("INSERT INTO choferes_demo (id, dni, cuit) VALUES (1, '99999111', ?)", (CUIT_FICTICIO,))
        conn.execute("INSERT INTO choferes_demo (id, dni, cuit) VALUES (2, '24567890', ?)", (CUIT_REAL,))
        conn.commit()
    hallazgos = _auditar(terceros=[("choferes_demo", "dni"), ("choferes_demo", "cuit")], incluir_dudosos=True)
    assert {(h.codigo, h.donde) for h in hallazgos} == {
        ("tercero_documento_sin_verificar", "choferes_demo.dni"),
        ("tercero_cuit_parece_real", "choferes_demo.cuit"),
    }


def test_el_cuit_de_la_empresa_en_una_tabla_del_producto(motor):
    """LibraCargo guarda la suya en `configuracion_empresa`, fuera de `config.json` y de `arca_config`."""
    with core.get_connection() as conn:
        conn.execute("CREATE TABLE configuracion_empresa (id INTEGER PRIMARY KEY, cuit TEXT)")
        conn.execute("INSERT INTO configuracion_empresa (id, cuit) VALUES (1, ?)", (CUIT_REAL,))
        conn.commit()
    [h] = _auditar(empresa=[("configuracion_empresa", "cuit")])
    assert (h.codigo, h.donde) == ("empresa_cuit_parece_real", "configuracion_empresa.cuit")


def test_una_tabla_o_columna_declarada_que_no_existe_no_rompe_ni_aborta(motor):
    """En PostgreSQL un `SELECT` contra una tabla que no existe aborta la transacción: el resto de la auditoría moriría."""
    with core.get_connection() as conn:
        conn.execute("INSERT INTO clients (name, cuit_dni) VALUES ('x', ?)", (CUIT_REAL,))
        conn.commit()
    hallazgos = _auditar(terceros=[("no_existe", "cuit"), ("clients", "no_existe")],
                         empresa=[("tampoco_existe", "cuit")])
    assert [h.donde for h in hallazgos] == ["clients.cuit_dni"]


def test_la_lista_blanca_del_producto(motor):
    with core.get_connection() as conn:
        conn.execute("INSERT INTO clients (name, cuit_dni) VALUES ('x', ?)", (CUIT_REAL,))
        conn.commit()
    assert _auditar(ficticios=[CUIT_REAL[:2] + "-" + CUIT_REAL[2:]]) == []
    assert _codigos(_auditar(ficticios=["20123456786"])) == {"tercero_cuit_parece_real"}


@pytest.mark.parametrize("mala", [("clients; DROP TABLE clients", "cuit_dni"), ("clients", "cuit_dni --"), ("", "x")])
def test_los_nombres_declarados_tienen_que_ser_identificadores(motor, mala):
    with pytest.raises(ValueError, match="inválido"):
        _auditar(terceros=[mala])


def test_la_salida_ordena_primero_lo_alto(motor, tmp_path):
    _config(tmp_path, email_smtp_host="smtp.ejemplo.test", empresa_cuit=CUIT_REAL)
    gravedades = [h.gravedad for h in _auditar(incluir_dudosos=True)]
    assert gravedades == sorted(gravedades, key=lambda g: g != dm.ALTA)
    assert gravedades[0] == dm.ALTA and gravedades[-1] == dm.BAJA


def test_las_columnas_del_producto_pueden_estar_en_otra_base(motor, tmp_path):
    """GestioLibra, MedLibra, LibraCargo y LibraClub llevan el core aparte: `patients` no está en la base del motor."""
    import sqlite3

    producto = sqlite3.connect(str(tmp_path / "dominio.db"))
    producto.execute("CREATE TABLE terceros (id INTEGER PRIMARY KEY, cuit TEXT)")
    producto.execute("INSERT INTO terceros (id, cuit) VALUES (1, ?)", (CUIT_REAL,))
    producto.commit()
    assert _auditar(terceros=[("terceros", "cuit")]) == []   # en la base del motor no está
    [h] = _auditar(terceros=[("terceros", "cuit")], conn_producto=producto)
    assert (h.codigo, h.donde) == ("tercero_cuit_parece_real", "terceros.cuit")
    producto.close()


# ── el texto de la semilla ──


def test_un_cuit_de_forma_real_escrito_en_una_semilla():
    texto = (f'CLIENTES = [{{"nombre": "Uno", "cuit_dni": "{CUIT_REAL[:2]}-{CUIT_REAL[2:10]}-{CUIT_REAL[10]}"}},\n'
             f'  {{"nombre": "Dos", "cuit_dni": "{CUIT_FICTICIO}"}}]\n'
             f'payer = "{CUIT_REAL}"\n')
    hallazgos = dm.auditar_literales(texto, "scripts/seed_demo.py")
    assert [(h.codigo, h.donde) for h in hallazgos] == [
        ("cuit_escrito_parece_real", "scripts/seed_demo.py:1"), ("cuit_escrito_parece_real", "scripts/seed_demo.py:3")]
    assert all(h.gravedad == dm.ALTA for h in hallazgos)
    assert CUIT_REAL[2:10] not in " ".join(str(h) for h in hallazgos)


def test_el_texto_sin_cuit_de_forma_real_no_da_nada():
    texto = f'a = "{CUIT_FICTICIO}"\nb = "{_con_verificador_invalido(CUIT_REAL)}"\nc = 12345678901234\nd = "30111222"\n'
    assert dm.auditar_literales(texto) == []
    assert dm.auditar_literales(f'x = "{CUIT_REAL}"', ficticios=[CUIT_REAL]) == []


# ═══════════════════════════════════════════════ Una demo no muestra el emisor de la base (ADR-061)


def _emisor_real() -> int:
    db_arca.crear_arca_config(REAL["nombre"], CUIT_REAL, 4, "clave.key", "cert.crt", ambiente="produccion")
    return db_arca.obtener_arca_config(REAL["nombre"])["id"]


def _resolvedor_real(documento):
    return {**REAL, "iva_condition": "Responsable Inscripto", "inicio_actividades": "2001-02-03",
            "logo_bytes": b"\x89PNG real"}


def _factura(emisor_id) -> dict:
    return {"tipo": 1, "punto_venta": 1, "numero": 1, "fecha": "2026-10-06", "cliente_razon": "Consumidor",
            "cliente_cuit": "", "cliente_iva_cond": 5, "cliente_domicilio": "", "condicion_venta": "Contado",
            "items": [{"description": "Servicio", "qty": 1, "unit_price": 100.0, "subtotal": 100.0}],
            "subtotal": 100.0, "iva_amount": 21.0, "total": 121.0, "concepto": 1, "cae": "", "cae_vto": "",
            "observaciones": "", "emisor_id": emisor_id}


def test_fuera_de_una_demo_el_emisor_es_el_de_la_base(motor):
    """El contraste que hace que el test de abajo no pase por casualidad."""
    emisor_id = _emisor_real()
    emisor_del_pdf.registrar_resolvedor(_resolvedor_real)
    emp = emisor_del_pdf.emisor_para(_factura(emisor_id))
    assert (emp["nombre"], emp["cuit"], emp["direccion"]) == (REAL["nombre"], CUIT_REAL, REAL["direccion"])
    assert emp["logo_bytes"] == b"\x89PNG real"


def test_en_una_demo_el_emisor_es_la_empresa_ficticia(motor, monkeypatch):
    emisor_id = _emisor_real()
    emisor_del_pdf.registrar_resolvedor(_resolvedor_real)
    _demo(monkeypatch)
    ficticia = config_manager.empresa_demo()
    emp = emisor_del_pdf.emisor_para(_factura(emisor_id), empresa={"nombre": REAL["nombre"], "cuit": CUIT_REAL})
    assert emp["nombre"] == ficticia["empresa_nombre"]
    assert emp["cuit"] == ficticia["empresa_cuit"]
    assert emp["direccion"] == ficticia["empresa_direccion"]
    assert emp["iibb"] == ficticia["empresa_iibb"]
    assert not emp.get("logo_bytes")
    assert emp == pg._empresa()


def test_en_una_demo_el_emisor_del_producto_registrado_no_se_consulta(motor, monkeypatch):
    emisor_del_pdf.registrar_resolvedor(lambda d: pytest.fail("el resolvedor del producto no debe correr en una demo"))
    _demo(monkeypatch)
    assert emisor_del_pdf.emisor_para({"emisor_id": 12345})["nombre"] == "Empresa Demo SRL"


def test_el_pdf_de_una_demo_no_trae_nada_de_la_empresa_real(motor, tmp_path, monkeypatch):
    from pypdf import PdfReader

    emisor_id = _emisor_real()
    emisor_del_pdf.registrar_resolvedor(_resolvedor_real)
    _demo(monkeypatch)
    ruta = pg.generate_pdf_factura(_factura(emisor_id), output_dir=str(tmp_path / "pdf"))
    ruta = ruta if isinstance(ruta, str) else None
    texto = "\n".join(p.extract_text() for p in PdfReader(ruta).pages)
    assert "Empresa Demo SRL" in texto and "30-99999900-6" in texto
    for dato_real in (REAL["nombre"], REAL["direccion"], REAL["iibb"], CUIT_REAL, "2001"):
        assert dato_real not in texto
    assert b"/Subtype /Image" not in open(ruta, "rb").read()


def test_los_lectores_de_arca_no_entregan_nada_en_una_demo(motor, monkeypatch):
    emisor_id = _emisor_real()
    assert db_arca.config_del_emisor()["cuit"] == CUIT_REAL
    assert db_arca.obtener_arca_config(REAL["nombre"]) and db_arca.obtener_todas_arca_configs()
    assert db_arca.config_por_cuit(CUIT_REAL)
    _demo(monkeypatch)
    assert db_arca.config_del_emisor() is None
    assert db_arca.obtener_arca_config(REAL["nombre"]) is None
    assert db_arca.obtener_todas_arca_configs() == []
    assert db_arca.config_por_cuit(CUIT_REAL) is None
    assert db_arca.config_del_emisor_en(emisor_id) is None
    with pytest.raises(db_arca.EmisorDesconocido):
        db_arca.config_del_emisor(emisor_id)


def test_actualizar_arca_config_sigue_viendo_su_fila_en_una_demo(motor, monkeypatch):
    """Esconder lo que se lee no puede romper lo que se escribe: la semilla o un script no tienen por qué morir."""
    _emisor_real()
    _demo(monkeypatch)
    db_arca.actualizar_arca_config(REAL["nombre"], punto_venta=7)
    monkeypatch.delenv("DEMO_MODE")
    assert db_arca.obtener_arca_config(REAL["nombre"])["punto_venta"] == 7


def test_en_una_demo_ningun_certificado_se_resuelve(motor, tmp_path, monkeypatch):
    cert, clave = _archivo(tmp_path, "c.crt"), _archivo(tmp_path, "c.key")
    cfg = {"ambiente": "produccion", "certificado_path": cert, "clave_path": clave}
    assert arca_credenciales.paths_en_disco(cfg) == (cert, clave)
    db_servicio.guardar_paths_de_servicio("E", "wscpe", "produccion", certificado_path=cert, clave_path=clave)
    assert arca_credenciales.paths_en_disco_de_servicio("E", "wscpe", "produccion") == (cert, clave)
    _demo(monkeypatch)
    assert arca_credenciales.paths_en_disco(cfg) == ("", "")
    assert arca_credenciales.paths_en_disco_de_servicio("E", "wscpe", "produccion") == ("", "")


def test_una_demo_nunca_le_pide_nada_a_arca(motor, tmp_path, monkeypatch):
    """Con una fila de producción y los archivos en disco, fuera de una demo se autentica; en una demo no sale ni un pedido."""
    db_arca.crear_arca_config(REAL["nombre"], CUIT_REAL, 4, _archivo(tmp_path, "p.key"), _archivo(tmp_path, "p.crt"),
                              ambiente="produccion")
    llamadas = []

    async def autenticar(*args, **kwargs):
        llamadas.append(args)
        raise RuntimeError("sin red en los tests")

    monkeypatch.setattr(arca_facturacion.arca_wsaa, "autenticar", autenticar)

    asyncio.run(arca_facturacion.get_next_numero_with_arca(4, 11))
    assert len(llamadas) == 1   # fuera de una demo, sí

    llamadas.clear()
    _demo(monkeypatch)
    numero, ta, arca = asyncio.run(arca_facturacion.get_next_numero_with_arca(4, 11))
    assert (numero, ta, arca) == (1, None, None)
    assert llamadas == []


# ── La pantalla de ARCA ──


@pytest.fixture
def cliente_arca(tmp_path, monkeypatch, motor):
    import libracore.arca_router as ar

    importlib.reload(ar)
    app = FastAPI()
    app.include_router(ar.build_arca_router(prefix="/arca"))
    return TestClient(app)


def test_la_pantalla_de_arca_guarda_fuera_de_una_demo(cliente_arca):
    r = cliente_arca.put("/arca", json={"empresa": "default", "cuit": CUIT_FICTICIO, "punto_venta": 1,
                                   "ambiente": "homologacion"})
    assert r.status_code == 200 and r.json()["cuit"] == CUIT_FICTICIO


def test_la_pantalla_de_arca_no_guarda_ni_sube_nada_en_una_demo(cliente_arca, monkeypatch):
    _demo(monkeypatch)
    for metodo, ruta, kw in (
        ("put", "", {"json": {"empresa": "default", "cuit": CUIT_REAL, "punto_venta": 1, "ambiente": "produccion"}}),
        ("post", "/certificado", {"files": {"archivo": ("c.crt", b"x")}}),
        ("post", "/clave", {"files": {"archivo": ("c.key", b"x")}}),
        ("post", "/pedido", {"json": {"cuit": CUIT_REAL, "razon_social": "x"}}),
        ("delete", "/credenciales", {}),
        ("post", "/probar", {}),
    ):
        r = getattr(cliente_arca, metodo)("/arca" + ruta, **kw)
        assert r.status_code == 403, (metodo, ruta, r.text)
        assert "demo" in r.json()["detail"].lower()
    assert core.get_connection().execute("SELECT COUNT(*) FROM arca_config").fetchone()[0] == 0


def test_la_pantalla_de_arca_de_una_demo_se_lee_vacia_aunque_la_base_tenga_algo(cliente_arca, monkeypatch):
    _emisor_real()
    assert cliente_arca.get("/arca").json()["cuit"] == CUIT_REAL
    _demo(monkeypatch)
    assert cliente_arca.get("/arca").json() is None


# ── Registrar la empresa de la demo ──


def test_el_producto_no_puede_registrar_un_cuit_que_no_es_ficticio():
    for cuit in (CUIT_REAL, _con_verificador_invalido(CUIT_FICTICIO), "99999123", ""):
        with pytest.raises(ValueError, match="ficticio"):
            config_manager.usar_empresa_demo({"empresa_cuit": cuit})
    assert config_manager.empresa_demo()["empresa_cuit"] == config_manager.EMPRESA_DEMO_POR_DEFECTO["empresa_cuit"]


def test_el_producto_registra_un_cuit_ficticio():
    config_manager.usar_empresa_demo({"empresa_cuit": CUIT_FICTICIO_PERSONA, "empresa_nombre": "Nexo Demo SRL"})
    assert config_manager.empresa_demo()["empresa_cuit"] == CUIT_FICTICIO_PERSONA


# ═══════════════════════════════════════════════ La corrida nocturna: `python -m libracore.demo auditar`


def test_la_corrida_de_una_demo_limpia_sale_con_cero(motor, monkeypatch, capsys):
    _demo(monkeypatch)
    assert dm.main(["auditar", "--url", core._db_path]) == dm.SALIDA_LIMPIA
    assert "sin hallazgos" in capsys.readouterr().out


def test_la_corrida_con_hallazgos_sale_con_tres_y_no_imprime_el_dato(motor, monkeypatch, capsys):
    db_arca.crear_arca_config(REAL["nombre"], CUIT_REAL, 4, "", "", ambiente="produccion")
    _demo(monkeypatch)
    assert dm.main(["auditar", "--url", core._db_path]) == dm.SALIDA_HALLAZGOS
    salida = capsys.readouterr().out
    assert "ERROR" in salida and "arca_en_produccion" in salida
    assert CUIT_REAL not in salida and REAL["nombre"] not in salida


def test_la_corrida_acepta_las_columnas_del_producto(motor, monkeypatch, capsys):
    with core.get_connection() as conn:
        conn.execute("CREATE TABLE terceros (id INTEGER PRIMARY KEY, cuit TEXT)")
        conn.execute("INSERT INTO terceros (id, cuit) VALUES (1, ?)", (CUIT_REAL,))
        conn.commit()
    _demo(monkeypatch)
    url = core._db_path
    assert dm.main(["auditar", "--url", url]) == dm.SALIDA_LIMPIA
    assert dm.main(["auditar", "--url", url, "--tercero", "terceros.cuit"]) == dm.SALIDA_HALLAZGOS
    assert dm.main(["auditar", "--url", url, "--tercero", "terceros.cuit", "--ficticio", CUIT_REAL]) == dm.SALIDA_LIMPIA


def test_la_corrida_no_audita_lo_que_no_es_una_demo(motor, capsys):
    assert dm.main(["auditar", "--url", core._db_path]) == dm.SALIDA_NO_ES_DEMO
    assert "DEMO_MODE" in capsys.readouterr().err


def test_la_corrida_usa_database_url_y_pide_la_base(motor, monkeypatch, capsys):
    _demo(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", core._db_path)
    assert dm.main(["auditar"]) == dm.SALIDA_LIMPIA
    monkeypatch.delenv("DATABASE_URL")
    assert dm.main(["auditar"]) == dm.SALIDA_USO


def test_la_corrida_con_un_par_mal_escrito_es_un_error_de_uso(motor, monkeypatch, capsys):
    _demo(monkeypatch)
    assert dm.main(["auditar", "--url", core._db_path, "--tercero", "sin_punto"]) == dm.SALIDA_USO
    assert "tabla.columna" in capsys.readouterr().err


def test_la_corrida_por_producto_resuelve_las_bases_como_la_app(motor, monkeypatch, capsys):
    """`--producto contalibra` lee `CONTALIBRA_DATABASE_URL`: contalibra lleva el core en la base del dominio."""
    db_arca.crear_arca_config(REAL["nombre"], CUIT_REAL, 4, "", "", ambiente="produccion")
    _demo(monkeypatch)
    monkeypatch.setenv("CONTALIBRA_DATABASE_URL", core._db_path)
    assert dm.main(["auditar", "--producto", "contalibra"]) == dm.SALIDA_HALLAZGOS
    monkeypatch.delenv("CONTALIBRA_DATABASE_URL")
    assert dm.main(["auditar", "--producto", "contalibra"]) == dm.SALIDA_USO
    # Medlibra lleva el core aparte: sin su variable, no se cae a la del dominio.
    monkeypatch.setenv("MEDLIBRA_DATABASE_URL", core._db_path)
    assert dm.main(["auditar", "--producto", "medlibra"]) == dm.SALIDA_USO
    monkeypatch.setenv("MEDLIBRA_LIBRACORE_DATABASE_URL", core._db_path)
    assert dm.main(["auditar", "--producto", "medlibra"]) == dm.SALIDA_HALLAZGOS
