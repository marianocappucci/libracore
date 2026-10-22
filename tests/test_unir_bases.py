"""`libracore-unir-bases` (ADR-078): unir la base core de un producto dentro de la de su dominio.

Contra PostgreSQL de verdad y con DOS bases sintéticas que imitan a un producto de dos bases (LibraClub): el dominio con
las tablas del producto y la `usuarios`/`auth_log` de `libraauth`, y el core con el esquema del motor en su cabeza y datos
en caja, ventas, cuenta corriente, usuarios y auditoría. Se saltea sin `LIBRACORE_POSTGRES_URL`.

Lo que se fija, y por qué cada cosa:

- **Conteos, sumas, huellas, secuencias y claves foráneas iguales** después de unir: «se copió» no alcanza, se compara.
- **Idempotencia**: unir una base ya unida no cambia ni una fila y lo dice.
- **Revertir** devuelve lo nuevo, con los mismos ids, y no toca lo viejo.
- **Lo que corta**: una `usuario_id` colgada, un usuario que es otra persona, una tabla del producto con el nombre de una
  del motor, la tabla de versión repetida, un core desactualizado. Cada una **antes de tocar nada**.
- **Una unión que falla a mitad no deja nada a medias**, y se puede reintentar.
"""

import json
import uuid
from decimal import Decimal

import pytest
from pg_descartable import Bases, conectar
from psycopg import errors

from libracore import unir_bases
from libracore.respaldo_postgres import base_de, con_base
from libracore.unir_bases import Cadena

PREFIJO = "libraclub"
#: Lo que cambia de tipo al pasar `actividad_log` de la forma del motor comercial a la de `libraauth`. Las que importan son
#: `ts` y `entidad_id`; las de texto a `varchar(n)` se informan igual porque el largo puede no entrar.
CONVERSIONES = {
    "ts": "text → timestamp without time zone",
    "usuario": "text → character varying(100)",
    "accion": "text → character varying(20)",
    "entidad": "text → character varying(50)",
    "entidad_id": "integer → character varying(100)",
    "descripcion": "text → character varying(500)",
}


# --- Bases sintéticas -------------------------------------------------------------------------------------------------


def _ejecutar(url, sentencias, *, parametros=()):
    with conectar(url) as conn:
        for s in sentencias:
            conn.execute(s)
        conn.commit()


def _fila(url, consulta, params=()):
    with conectar(url) as conn:
        return conn.execute(consulta, params).fetchall()


@pytest.fixture(scope="module")
def plantilla():
    """Un core con el motor migrado y sin datos. Migrar con alembic tarda unos segundos: se hace una vez y cada test clona."""
    b = Bases()
    try:
        url = b.nueva()
        unir_bases._subir_libracore(url)
        yield url
    finally:
        b.limpiar()


def _clon(bases, plantilla):
    """Una base nueva del test, copia de la plantilla (se borra con las demás al terminar)."""
    nombre = f"lcr_{uuid.uuid4().hex[:10]}"
    with conectar(con_base(bases.servidor, "postgres"), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{nombre}" TEMPLATE "{base_de(plantilla)}"')
    bases.nombres.append(nombre)
    return con_base(bases.servidor, nombre)


def _armar_core(url, *, usuario_de_caja=2):
    """Datos de uso en un core con el motor ya migrado."""
    with conectar(url) as c:
        c.execute(
            "INSERT INTO usuarios (id, username, nombre, email, password_hash, role, activo) VALUES "
            "(1, 'admin', 'Admin', '', 'h', 'admin', TRUE), (2, 'cajero', 'Cajero', '', 'h', 'operador', TRUE)"
        )
        c.execute("INSERT INTO auth_log (evento, username, ts) VALUES ('login', 'admin', '2026-10-01 10:00:00')")
        c.execute(
            "INSERT INTO clients (id, name, cuit_dni) VALUES (1, 'Ana', '20111111112'), (2, 'Beto', '20222222223'), (40, 'Cora', '')"
        )
        c.execute("INSERT INTO cajas (id, nombre, medios_pago, activo, es_default) VALUES (2, 'Caja Salón', '[]', 1, 0)")
        c.execute("INSERT INTO categorias_egreso (nombre) VALUES ('Insumos del club')")
        c.execute(
            "INSERT INTO turnos_caja (id, usuario_id, apertura, monto_inicial) VALUES (1, 1, '2026-10-01 09:00', 1000.10)"
        )
        c.execute(
            "INSERT INTO caja_movimientos (id, fecha, tipo, concepto, monto, caja_id, turno_id, usuario_id) VALUES "
            "(1, '2026-10-01', 'ingreso', 'Cancha 1', 12500.50, 1, 1, 1), "
            "(2, '2026-10-01', 'ingreso', 'Buffet', 0.10, 2, 1, %s), "
            "(3, '2026-10-02', 'egreso', 'Pelotas', 3333.33, 1, 1, 1), "
            "(7, '2026-10-03', 'ingreso', 'Seña', 1234567.89, 2, NULL, NULL)",
            (usuario_de_caja,),
        )
        c.execute(
            "INSERT INTO ventas (id, numero, fecha, cliente_id, items, subtotal, total, usuario_id, turno_id) VALUES "
            "(1, 'V-0001', '2026-10-01', 1, '[]', 3000.00, 3500.25, 2, 1), "
            "(2, 'V-0002', '2026-10-02', NULL, '[]', 100.00, 100.00, 1, 1)"
        )
        c.execute(
            "INSERT INTO ventas_pagos (id, venta_id, medio, monto) VALUES (1, 1, 'efectivo', 3500.25), (2, 2, 'efectivo', 100.00)"
        )
        # Una contrapartida con id MENOR que el asiento que corrige, y guardada ANTES (la FK se mira al final de la sentencia):
        # ni el orden por id ni el físico sirven para copiarla.
        c.execute(
            "INSERT INTO cc_asientos (id, fecha, tercero_id, rol, concepto, debe, haber, contrapartida_de) VALUES "
            "(5, '2026-10-02', 1, 'cliente', 'Reversión', 0, 500.50, 10), "
            "(10, '2026-10-01', 1, 'cliente', 'Seña', 500.50, 0, NULL), "
            "(11, '2026-10-02', 2, 'cliente', 'Cancha', 800.00, 0, NULL)"
        )
        c.commit()


def _armar_dominio(url, *, version="alembic_version_libraclub", usuarios=((1, "admin"), (2, "cajero"), (3, "mozo"))):
    """El dominio de un producto de dos bases, antes de la unión: sus tablas, y `usuarios`/`auth_log` de libraauth."""
    _ejecutar(
        url,
        [
            "CREATE TABLE usuarios (id SERIAL PRIMARY KEY, username VARCHAR(100) NOT NULL UNIQUE, nombre VARCHAR(200) NOT NULL, "
            "email VARCHAR(200) NOT NULL, password_hash VARCHAR(200) NOT NULL, role VARCHAR(20) NOT NULL, activo BOOLEAN NOT NULL, "
            "created_at TIMESTAMP NOT NULL DEFAULT now())",
            "CREATE TABLE auth_log (id SERIAL PRIMARY KEY, ts TIMESTAMP NOT NULL DEFAULT now(), evento VARCHAR(50) NOT NULL, "
            "username VARCHAR(100) NOT NULL, ip VARCHAR(64), detalle VARCHAR(500))",
            "CREATE TABLE sucursales (id SERIAL PRIMARY KEY, nombre TEXT NOT NULL)",
            "CREATE TABLE reservas (id SERIAL PRIMARY KEY, sucursal_id INTEGER REFERENCES sucursales(id), cliente TEXT, "
            "monto NUMERIC(12, 2) NOT NULL)",
            f"CREATE TABLE {version} (version_num VARCHAR(32) PRIMARY KEY)",
            f"INSERT INTO {version} VALUES ('club_0001')",
            "INSERT INTO sucursales (nombre) VALUES ('Central')",
            "INSERT INTO reservas (sucursal_id, cliente, monto) VALUES (1, 'Ana', 9999.99), (1, 'Beto', 0.01)",
        ],
    )
    with conectar(url) as c:
        for i, u in usuarios:
            c.execute(
                "INSERT INTO usuarios (id, username, nombre, email, password_hash, role, activo) VALUES (%s, %s, %s, '', 'h', 'operador', TRUE)",
                (i, u, u.title()),
            )
        c.execute("SELECT setval('usuarios_id_seq', 10)")
        c.commit()


@pytest.fixture
def par(bases, plantilla):
    """`(core, dominio)` listos para unir. Las dos bases se borran al terminar."""
    core, dominio = _clon(bases, plantilla), bases.nueva()
    _armar_core(core)
    _armar_dominio(dominio)
    return core, dominio


def _correr(core, dominio, accion, informe=None, *extra):
    argv = ["--prefijo", PREFIJO, "--core", core, "--dominio", dominio, accion]
    if informe is not None:
        argv += ["--informe", str(informe)]
    return unir_bases.main(argv + list(extra))


def _tablas(url):
    return [r[0] for r in _fila(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")]


def _foto(url, tablas=None):
    """Una foto comparable de las tablas: resumen (conteo, sumas, huella) de cada una."""
    with conectar(url, autocommit=False) as conn:
        conn.read_only = True
        return {t: unir_bases.resumen_de(conn, t, unir_bases.columnas_de(conn, t)) for t in (tablas or _tablas(url))}


# --- verificar ----------------------------------------------------------------------------------------------------------


def test_verificar_ok_y_no_escribe_nada(par, tmp_path, capsys):
    core, dominio = par
    antes_core, antes_dominio = _foto(core), _foto(dominio)
    tablas_antes = _tablas(dominio)
    informe = tmp_path / "verificacion.json"

    assert _correr(core, dominio, "verificar", informe) == 0

    out = capsys.readouterr().out
    assert "[OK] Se puede unir" in out
    assert _tablas(dominio) == tablas_antes  # ni la marca ni una tabla del motor
    assert _foto(core) == antes_core and _foto(dominio) == antes_dominio
    datos = json.loads(informe.read_text())
    assert datos["tipo"] == "verificacion" and datos["ok"] is True
    # Lo que se copia: todo menos las tablas de auth y las de versión; el orden respeta las claves foráneas.
    copiar = datos["tablas_a_copiar"]
    assert "usuarios" not in copiar and "auth_log" not in copiar and "alembic_version" not in copiar
    assert copiar.index("clients") < copiar.index("ventas") < copiar.index("ventas_pagos")
    assert copiar.index("turnos_caja") < copiar.index("caja_movimientos")
    assert datos["no_copiadas"]["usuarios"] == {"motivo": "dominio_manda", "filas_core": 2}
    assert datos["no_copiadas"]["auth_log"]["filas_core"] == 1
    assert datos["tablas"]["caja_movimientos"]["filas"] == 4
    assert datos["dominio_previo"]["reservas"] == 2
    assert datos["cadenas"]["libracore"]["core"] == datos["cadenas"]["libracore"]["cabeza"]


def test_verificar_suma_de_dinero_exacta(par, tmp_path):
    core, dominio = par
    informe = tmp_path / "v.json"
    _correr(core, dominio, "verificar", informe)
    sumas = json.loads(informe.read_text())["tablas"]["caja_movimientos"]["sumas"]
    # 12500.50 + 0.10 + 3333.33 + 1234567.89, sin redondeo de punto flotante.
    assert Decimal(sumas["monto"]) == Decimal("1250401.82")


def test_verificar_no_pisa_un_informe_de_union(par, tmp_path):
    core, dominio = par
    informe = tmp_path / "union.json"
    informe.write_text(json.dumps({"tipo": "union"}))
    assert _correr(core, dominio, "verificar", informe) == 2
    assert json.loads(informe.read_text()) == {"tipo": "union"}


def test_misma_base_no_hay_nada_que_unir(par, tmp_path, capsys):
    core, _ = par
    assert _correr(core, core, "unir", tmp_path / "u.json") == 0
    assert "misma base" in capsys.readouterr().out
    assert not (tmp_path / "u.json").exists()


# --- lo que corta ------------------------------------------------------------------------------------------------------


def _codigos(core, dominio):
    v = unir_bases.verificar(core, dominio, PREFIJO)
    return {h.codigo for h in v.errores}, v


def test_corta_una_fk_colgada_a_usuarios(bases, plantilla):
    core, dominio = _clon(bases, plantilla), bases.nueva()
    _armar_core(core)
    _armar_dominio(dominio, usuarios=((1, "admin"), (3, "mozo")))  # el 2 (cajero) no está: caja_movimientos.usuario_id = 2

    codigos, v = _codigos(core, dominio)
    assert "fk_colgada" in codigos
    msg = next(h.mensaje for h in v.errores if h.codigo == "fk_colgada")
    assert "caja_movimientos.usuario_id" in msg and "usuarios.id" in msg and "2" in msg
    assert unir_bases.main(["--prefijo", PREFIJO, "--core", core, "--dominio", dominio, "verificar"]) == 1


def test_corta_un_usuario_que_es_otra_persona(bases, plantilla):
    core, dominio = _clon(bases, plantilla), bases.nueva()
    _armar_core(core)
    _armar_dominio(dominio, usuarios=((1, "admin"), (2, "otra-persona")))

    codigos, v = _codigos(core, dominio)
    assert codigos == {"usuario_distinto"}
    assert "otra-persona" in v.errores[0].mensaje


def test_corta_el_choque_de_una_tabla_del_producto_con_una_del_motor(par):
    core, dominio = par
    _ejecutar(dominio, ["CREATE TABLE modulos (id SERIAL PRIMARY KEY, clave TEXT)", "INSERT INTO modulos (clave) VALUES ('agenda')"])

    codigos, v = _codigos(core, dominio)
    assert codigos == {"choque_de_tablas"}
    assert "modulos" in v.errores[0].mensaje


def test_corta_la_tabla_de_version_repetida(bases, plantilla):
    core, dominio = _clon(bases, plantilla), bases.nueva()
    _armar_core(core)
    _armar_dominio(dominio, version="alembic_version")  # la cadena del producto usa el nombre de la del motor

    codigos, v = _codigos(core, dominio)
    assert codigos == {"version_repetida"}
    assert "alembic_version_libraclub" in v.errores[0].mensaje


def test_corta_un_core_desactualizado(par):
    core, dominio = par
    _ejecutar(core, ["UPDATE alembic_version SET version_num = '0027_reintegros_pendientes'"])
    codigos, _ = _codigos(core, dominio)
    assert codigos == {"core_desactualizado"}


def test_corta_un_core_sin_tabla_de_version(par):
    core, dominio = par
    _ejecutar(core, ["DROP TABLE alembic_version"])
    codigos, _ = _codigos(core, dominio)
    assert codigos == {"core_sin_version"}


def test_corta_una_cadena_que_no_sabe_crear(par):
    core, dominio = par
    _ejecutar(core, ["CREATE TABLE alembic_version_otra (version_num VARCHAR(32) PRIMARY KEY)"])
    codigos, _ = _codigos(core, dominio)
    assert codigos == {"cadena_desconocida"}


def test_corta_libracommerce_si_el_entorno_no_lo_tiene(par, monkeypatch):
    core, dominio = par
    _ejecutar(core, ["CREATE TABLE alembic_version_libracommerce (version_num VARCHAR(32) PRIMARY KEY)",
                     "INSERT INTO alembic_version_libracommerce VALUES ('0006')"])
    monkeypatch.setattr(unir_bases, "cadena_libracommerce", lambda: None)
    codigos, _ = _codigos(core, dominio)
    assert codigos == {"falta_libracommerce"}


def test_corta_un_destino_con_datos_propios(par):
    core, dominio = par
    unir_bases._subir_libracore(dominio)  # alguien ya migró el dominio a mano...
    _ejecutar(dominio, ["INSERT INTO clients (name) VALUES ('Dato del dominio')"])  # ...y la app escribió

    codigos, v = _codigos(core, dominio)
    assert codigos == {"destino_con_datos"}
    assert "clients" in v.errores[0].mensaje


def test_corta_si_la_seed_fue_tocada(par):
    core, dominio = par
    unir_bases._subir_libracore(dominio)
    _ejecutar(dominio, ["UPDATE cajas SET nombre = 'Caja del club'"])  # ya no es la semilla: es un dato
    codigos, _ = _codigos(core, dominio)
    assert codigos == {"destino_con_datos"}


def test_una_fila_del_core_sin_padre_en_el_core_tambien_corta(par):
    core, dominio = par
    with conectar(core) as c:  # una FK `NOT VALID`: existe, y deja pasar las filas que ya estaban
        c.execute("ALTER TABLE ventas_pagos DROP CONSTRAINT ventas_pagos_venta_id_fkey")
        c.execute("INSERT INTO ventas_pagos (id, venta_id, medio, monto) VALUES (99, 999, 'efectivo', 1)")
        c.execute("ALTER TABLE ventas_pagos ADD CONSTRAINT ventas_pagos_venta_id_fkey FOREIGN KEY (venta_id) "
                  "REFERENCES ventas(id) ON DELETE CASCADE NOT VALID")
        c.commit()
    codigos, v = _codigos(core, dominio)
    assert codigos == {"fk_colgada"}
    assert "ventas_pagos" in v.errores[0].mensaje


def test_una_fila_sin_padre_que_la_verificacion_no_ve_se_deshace_al_copiar(par, tmp_path, capsys):
    """Sin la FK en el core no hay cómo verlo de antemano: el dominio sí la tiene y la unión se deshace entera."""
    core, dominio = par
    _ejecutar(core, ["ALTER TABLE ventas_pagos DROP CONSTRAINT ventas_pagos_venta_id_fkey",
                     "INSERT INTO ventas_pagos (id, venta_id, medio, monto) VALUES (99, 999, 'efectivo', 1)"])
    assert unir_bases.verificar(core, dominio, PREFIJO).ok

    assert _correr(core, dominio, "unir", tmp_path / "u.json") == 1

    assert "se deshizo" in capsys.readouterr().err
    assert _fila(dominio, "SELECT count(*) FROM clients") == [(0,)]
    assert _fila(dominio, f"SELECT estado FROM {unir_bases.TABLA_MARCA}") == [("iniciada",)]


def test_las_conexiones_abiertas_son_aviso_al_verificar_e_impedimento_al_unir(par, tmp_path):
    core, dominio = par
    with conectar(dominio) as abierta:
        abierta.execute("SELECT 1")
        v = unir_bases.verificar(core, dominio, PREFIJO)
        assert v.ok and any(h.codigo == "conexiones_abiertas" for h in v.avisos)

        assert _correr(core, dominio, "unir", tmp_path / "u.json") == 1
        assert unir_bases.TABLA_MARCA not in _tablas(dominio)  # cortó antes de tocar nada
        assert _correr(core, dominio, "unir", tmp_path / "u.json", "--aunque-haya-conexiones") == 0


# --- unir --------------------------------------------------------------------------------------------------------------


def test_unir_copia_todo_y_las_dos_bases_coinciden(par, tmp_path, capsys):
    core, dominio = par
    foto_core, foto_dominio = _foto(core), _foto(dominio)
    informe = tmp_path / "union.json"

    assert _correr(core, dominio, "unir", informe) == 0
    assert "[OK] Unida" in capsys.readouterr().out

    datos = json.loads(informe.read_text())
    assert datos["tipo"] == "union" and datos["prefijo"] == PREFIJO
    en_dominio = _foto(dominio)

    # Conteos, sumas y huella de cada tabla del core: iguales en el dominio, y son los del informe.
    for t, d in datos["tablas"].items():
        assert {k: d[k] for k in ("filas", "sumas", "huella", "max_id")} == en_dominio[t], t
        assert en_dominio[t] == foto_core[t], t
    assert datos["tablas"]["caja_movimientos"]["filas"] == 4 and datos["tablas"]["clients"]["max_id"] == 40
    assert "usuarios" not in datos["tablas"] and "auth_log" not in datos["tablas"] and "cc_asientos" in datos["tablas"]

    # Lo que era del dominio no se tocó: ni sus tablas, ni `usuarios`/`auth_log`, ni su tabla de versión.
    for t in ("usuarios", "auth_log", "sucursales", "reservas", "alembic_version_libraclub"):
        assert en_dominio[t] == foto_dominio[t], t
    assert _fila(dominio, "SELECT username FROM usuarios ORDER BY id") == [("admin",), ("cajero",), ("mozo",)]
    # El core, intacto.
    assert _foto(core) == foto_core

    # Las cadenas: el motor en su cabeza dentro del dominio, y la del producto sigue donde estaba.
    assert _fila(dominio, "SELECT version_num FROM alembic_version")[0][0] == datos["cadenas"]["libracore"]["cabeza"]
    assert _fila(dominio, "SELECT version_num FROM alembic_version_libraclub") == [("club_0001",)]
    assert datos["cadenas"]["libracore"]["core"] == datos["cadenas"]["libracore"]["dominio"]

    # Las semillas del motor se reemplazaron por las filas del core, con sus ids.
    assert _fila(dominio, "SELECT id, nombre FROM cajas ORDER BY id") == [(1, "Caja Principal"), (2, "Caja Salón")]
    assert datos["tablas_vaciadas"] == ["cajas", "categorias_egreso", "depositos"]
    assert _fila(dominio, "SELECT count(*) FROM categorias_egreso") == [(11,)]

    # La autorreferencia: la contrapartida (id 5) apunta a un asiento con id mayor (10).
    assert _fila(dominio, "SELECT id, contrapartida_de FROM cc_asientos ORDER BY id") == [(5, 10), (10, None), (11, None)]

    # Secuencias: la próxima fila del dominio sigue a la última del core, no a la semilla.
    with conectar(dominio) as c:
        assert c.execute("INSERT INTO clients (name) VALUES ('Nueva') RETURNING id").fetchone()[0] == 41
        assert c.execute("INSERT INTO caja_movimientos (fecha, tipo, concepto, monto) VALUES ('2026-10-04', 'ingreso', 'x', 1) RETURNING id").fetchone()[0] == 8
        assert c.execute("INSERT INTO cajas (nombre) VALUES ('Tercera') RETURNING id").fetchone()[0] == 3
        c.rollback()
    assert datos["tablas"]["clients"]["secuencias"] == {"id": 41}

    # Claves foráneas: ninguna fila sin padre, y la validación del motor las conoce (siguen existiendo).
    with conectar(dominio) as c:
        for fk in unir_bases.claves_foraneas_de(c):
            assert unir_bases._huerfanas(c, fk) == 0, fk
        assert len(unir_bases.claves_foraneas_de(c)) > 40


def test_unir_con_el_dominio_ya_migrado_a_mano(par, tmp_path):
    """El caso de LibraCargo (2026-10-05): el dominio ya tenía el motor, con semillas, antes de unir."""
    core, dominio = par
    unir_bases._subir_libracore(dominio)
    assert _fila(dominio, "SELECT count(*) FROM cajas") == [(1,)]
    assert unir_bases.main(["--prefijo", PREFIJO, "--core", core, "--dominio", dominio, "unir", "--informe", str(tmp_path / "u.json")]) == 0
    assert _foto(dominio, ["clients", "caja_movimientos", "cajas"]) == _foto(core, ["clients", "caja_movimientos", "cajas"])
    assert _fila(dominio, "SELECT username FROM usuarios ORDER BY id") == [("admin",), ("cajero",), ("mozo",)]


def test_reunir_no_cambia_nada_y_lo_dice(par, tmp_path, capsys):
    core, dominio = par
    informe = tmp_path / "union.json"
    assert _correr(core, dominio, "unir", informe) == 0
    capsys.readouterr()
    antes = _foto(dominio)
    bytes_antes = informe.read_bytes()
    marca_antes = _fila(dominio, f"SELECT unida_en FROM {unir_bases.TABLA_MARCA}")

    assert _correr(core, dominio, "unir", informe) == 0

    assert "Ya estaba unida: no se cambió nada" in capsys.readouterr().out
    assert _foto(dominio) == antes
    assert informe.read_bytes() == bytes_antes
    assert _fila(dominio, f"SELECT unida_en FROM {unir_bases.TABLA_MARCA}") == marca_antes

    # Si se perdió el archivo, se reescribe desde la marca del dominio sin tocar la base.
    informe.unlink()
    assert _correr(core, dominio, "unir", informe) == 0
    assert json.loads(informe.read_text())["tablas"] == json.loads(bytes_antes)["tablas"]
    assert _foto(dominio) == antes


def test_una_union_que_falla_a_mitad_no_deja_nada_a_medias_y_se_reintenta(par, tmp_path, monkeypatch, capsys):
    core, dominio = par
    informe = tmp_path / "union.json"
    original = unir_bases._copiar_tabla
    llamadas = []

    def falla_en_la_tercera(origen, destino, tabla, *a, **k):
        llamadas.append(tabla)
        if len(llamadas) == 3:
            raise errors.UniqueViolation("simulada: se cayó la red")
        return original(origen, destino, tabla, *a, **k)

    monkeypatch.setattr(unir_bases, "_copiar_tabla", falla_en_la_tercera)
    assert _correr(core, dominio, "unir", informe) == 1
    assert "se deshizo" in capsys.readouterr().err
    assert not informe.exists()
    # Se deshizo todo lo del `COPY`: las semillas siguen ahí y las tablas del motor, vacías.
    assert _fila(dominio, "SELECT count(*) FROM cajas") == [(1,)]
    assert _fila(dominio, "SELECT count(*) FROM clients") == [(0,)]
    assert _fila(dominio, f"SELECT estado FROM {unir_bases.TABLA_MARCA}") == [("iniciada",)]

    monkeypatch.setattr(unir_bases, "_copiar_tabla", original)
    assert _correr(core, dominio, "unir", informe) == 0
    # La marca recuerda que `usuarios` ya era del dominio, aunque ahora haya una `usuarios` del motor en el medio.
    assert _fila(dominio, "SELECT username FROM usuarios ORDER BY id") == [("admin",), ("cajero",), ("mozo",)]
    assert _foto(dominio, ["clients", "ventas", "cc_asientos"]) == _foto(core, ["clients", "ventas", "cc_asientos"])


def test_si_el_dominio_no_tenia_usuarios_se_copian_los_del_core(bases, plantilla, tmp_path):
    core, dominio = _clon(bases, plantilla), bases.nueva()
    _armar_core(core)
    _ejecutar(dominio, ["CREATE TABLE reservas (id SERIAL PRIMARY KEY, monto NUMERIC(12, 2))"])

    assert _correr(core, dominio, "unir", tmp_path / "u.json") == 0
    assert _fila(dominio, "SELECT id, username FROM usuarios ORDER BY id") == [(1, "admin"), (2, "cajero")]
    assert _foto(dominio, ["auth_log"]) == _foto(core, ["auth_log"])


def test_una_cadena_de_libracommerce_se_crea_y_se_copia(par, tmp_path, monkeypatch):
    core, dominio = par

    def subir(url):  # lo que haría `libracommerce.migrar.upgrade`: sus tablas, su tabla de versión y la cadena numerada vieja
        _ejecutar(url, [
            "CREATE TABLE IF NOT EXISTS compras (id SERIAL PRIMARY KEY, total NUMERIC(14, 2) NOT NULL)",
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "applied_at TEXT NOT NULL DEFAULT 'ahora')",
            "INSERT INTO schema_migrations (version, name) VALUES (1, 'a'), (2, 'b') ON CONFLICT DO NOTHING",
            "CREATE TABLE IF NOT EXISTS alembic_version_libracommerce (version_num VARCHAR(32) PRIMARY KEY)",
            "DELETE FROM alembic_version_libracommerce",
            "INSERT INTO alembic_version_libracommerce VALUES ('0006_cabeza')",
        ])

    _ejecutar(core, ["CREATE TABLE compras (id SERIAL PRIMARY KEY, total NUMERIC(14, 2) NOT NULL)",
                     "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)",
                     "INSERT INTO schema_migrations VALUES (1, 'a', '2026-01-01'), (2, 'b', '2026-01-02')",
                     "INSERT INTO compras (id, total) VALUES (3, 10.50), (9, 0.05)",
                     "CREATE TABLE alembic_version_libracommerce (version_num VARCHAR(32) PRIMARY KEY)",
                     "INSERT INTO alembic_version_libracommerce VALUES ('0006_cabeza')"])
    monkeypatch.setattr(unir_bases, "cadena_libracommerce",
                        lambda: Cadena("libracommerce", "alembic_version_libracommerce", frozenset({"0006_cabeza"}), "0006_cabeza", subir))
    informe = tmp_path / "u.json"

    assert _correr(core, dominio, "unir", informe) == 0
    datos = json.loads(informe.read_text())
    assert datos["cadenas"]["libracommerce"] == {"core": "0006_cabeza", "dominio": "0006_cabeza", "cabeza": "0006_cabeza"}
    assert _fila(dominio, "SELECT id, total FROM compras ORDER BY id") == [(3, Decimal("10.50")), (9, Decimal("0.05"))]
    assert datos["tablas"]["compras"]["sumas"] == {"total": "10.55"}
    with conectar(dominio) as c:
        assert c.execute("INSERT INTO compras (total) VALUES (1) RETURNING id").fetchone()[0] == 10
        c.rollback()
    # Las tablas de versión no se copian como dato: las creó la cadena. La numerada se compara por versión.
    assert "alembic_version_libracommerce" not in datos["tablas"] and "schema_migrations" not in datos["tablas"]
    assert datos["cadenas_numeradas"] == {"schema_migrations": {"core": [1, 2], "dominio": [1, 2]}}
    assert _fila(dominio, "SELECT applied_at FROM schema_migrations ORDER BY version") == [("ahora",), ("ahora",)]


def test_corta_si_la_cadena_numerada_del_dominio_esta_atrasada(par):
    core, dominio = par
    _ejecutar(core, ["CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL)",
                     "INSERT INTO schema_migrations VALUES (1, 'a'), (2, 'b')"])
    _ejecutar(dominio, ["CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL)",
                        "INSERT INTO schema_migrations VALUES (1, 'a')"])
    codigos, v = _codigos(core, dominio)
    assert codigos == {"version_repetida"}
    assert "[2]" in v.errores[0].mensaje


def test_con_libracommerce_de_verdad(par, tmp_path):
    """Contra la cadena real de `libracommerce` (no es dependencia del motor: se saltea si el entorno no la tiene)."""
    pytest.importorskip("libracommerce.migrar")
    from libracommerce import migrar as commerce

    core, dominio = par
    commerce.upgrade(core)  # crea `actividad_log` con la forma del motor comercial; el dominio no la tiene y la crea la misma cadena
    _ejecutar(core, ["INSERT INTO actividad_log (id, ts, usuario, accion, entidad, entidad_id, descripcion) VALUES "
                     "(40, '2026-10-01 10:00:00', 'cajero', 'crear', 'venta', 12, 'Venta 12')"])
    informe = tmp_path / "u.json"

    assert _correr(core, dominio, "unir", informe) == 0

    datos = json.loads(informe.read_text())
    assert datos["cadenas"]["libracommerce"]["core"] == datos["cadenas"]["libracommerce"]["dominio"]
    assert "schema_migrations" in datos["cadenas_numeradas"] and "sales" in datos["tablas"]
    assert "actividad_log" in datos["tablas"] and datos["auditoria_agregada"] == {}  # el dominio no la tenía: se copia como es
    for t in ("sales", "sale_items", "catalog_items", "parties", "actividad_log"):
        assert _foto(core, [t]) == _foto(dominio, [t]), t


# --- actividad_log: auditoría compartida ------------------------------------------------------------------------------


#: La forma que le da `libraauth` en el dominio (`ActividadLog`, y lo que deja `0012_actividad_log` de LibraClub).
DDL_ACTIVIDAD_LIBRAAUTH = [
    "CREATE TABLE actividad_log (id SERIAL PRIMARY KEY, ts TIMESTAMP NOT NULL, usuario VARCHAR(100) NOT NULL, "
    "accion VARCHAR(20) NOT NULL, entidad VARCHAR(50) NOT NULL, entidad_id VARCHAR(100), descripcion VARCHAR(500) NOT NULL, "
    "cambios TEXT)",
    "CREATE INDEX ix_actividad_log_ts ON actividad_log (ts)",
    "CREATE INDEX ix_actividad_log_accion ON actividad_log (accion)",
    "CREATE INDEX ix_actividad_log_entidad ON actividad_log (entidad)",
]
#: La que le da `libracommerce-migrar` en el core (`db/schema.py`, tal como queda en PostgreSQL; medido con `libracommerce` 0.57.0).
DDL_ACTIVIDAD_COMMERCE = [
    "CREATE TABLE actividad_log (id BIGSERIAL PRIMARY KEY, ts TEXT NOT NULL, usuario TEXT NOT NULL DEFAULT 'Sistema', "
    "accion TEXT NOT NULL, entidad TEXT NOT NULL, entidad_id INTEGER, descripcion TEXT NOT NULL DEFAULT '', cambios TEXT)",
    "CREATE INDEX ix_actividad_log_ts ON actividad_log (ts)",
    "CREATE INDEX ix_actividad_log_accion ON actividad_log (accion)",
    "CREATE INDEX ix_actividad_log_entidad ON actividad_log (entidad)",
]


@pytest.fixture
def con_actividad(par):
    """El par de siempre, con `actividad_log` en las dos bases y con formas distintas (lo que midió LibraClub en dev y demo)."""
    core, dominio = par
    _ejecutar(dominio, DDL_ACTIVIDAD_LIBRAAUTH + [
        "INSERT INTO actividad_log (ts, usuario, accion, entidad, entidad_id, descripcion) VALUES "
        "('2026-09-01 08:00:00', 'admin', 'crear', 'reserva', '1', 'Reserva 1'), "
        "('2026-09-01 09:00:00', 'admin', 'editar', 'cancha', 'c-7', 'Cancha de texto')"])
    _ejecutar(core, DDL_ACTIVIDAD_COMMERCE + [
        "INSERT INTO actividad_log (id, ts, usuario, accion, entidad, entidad_id, descripcion, cambios) VALUES "
        "(40, '2026-10-01 10:00:00', 'cajero', 'crear', 'venta', 12, 'Venta 12', '{\"total\": 1250.5}'), "
        "(41, '2026-10-01T11:30:15', 'Sistema', 'anular', 'caja_movimiento', NULL, 'Anulado', NULL), "
        "(42, '2026-10-02', 'cajero', 'crear', 'venta', 7, 'Venta 7', NULL)"])
    return core, dominio


def _actividad(url):
    return _fila(url, "SELECT id, ts, usuario, accion, entidad, entidad_id, descripcion, cambios FROM actividad_log ORDER BY id")


def test_actividad_log_en_las_dos_bases_no_es_un_choque_y_se_informa_lo_que_se_agrega(con_actividad, tmp_path, capsys):
    core, dominio = con_actividad
    informe = tmp_path / "v.json"

    assert _correr(core, dominio, "verificar", informe) == 0

    datos = json.loads(informe.read_text())
    assert "actividad_log" not in datos["tablas_a_copiar"] and "actividad_log" not in datos["dominio_previo"]
    a = datos["auditoria"]["actividad_log"]
    assert (a["filas_core"], a["filas_dominio"]) == (3, 2)
    assert a["conversiones"] == CONVERSIONES
    assert any(h["codigo"] == "auditoria_se_agrega" for h in datos["avisos"])
    out = capsys.readouterr().out
    assert "Se agregan al dominio (auditoría, ids nuevos): actividad_log: 3 fila(s) del core a las 2 del dominio" in out
    assert _actividad(dominio)[0][0] == 1 and _fila(dominio, "SELECT count(*) FROM actividad_log") == [(2,)]  # no escribió nada


def test_unir_agrega_la_auditoria_del_core_con_los_tipos_del_dominio(con_actividad, tmp_path):
    from datetime import datetime

    core, dominio = con_actividad
    del_dominio = _actividad(dominio)
    del_core = _actividad(core)
    informe = tmp_path / "union.json"

    assert _correr(core, dominio, "unir", informe) == 0

    ahora = _actividad(dominio)
    assert ahora[:2] == del_dominio  # lo que ya había, igual y con sus ids
    nuevas = ahora[2:]
    assert [f[0] for f in nuevas] == [3, 4, 5]  # ids nuevos de la secuencia del dominio: no se conservan los del core (40, 41, 42)
    assert [f[1] for f in nuevas] == [datetime(2026, 10, 1, 10, 0, 0), datetime(2026, 10, 1, 11, 30, 15), datetime(2026, 10, 2, 0, 0)]
    assert [f[5] for f in nuevas] == ["12", None, "7"]  # el entero pasa a texto; el NULL sigue NULL
    assert [(f[2], f[3], f[4], f[6], f[7]) for f in nuevas] == [(f[2], f[3], f[4], f[6], f[7]) for f in del_core]
    assert _actividad(core) == del_core  # el core, intacto

    datos = json.loads(informe.read_text())
    assert "actividad_log" not in datos["tablas"]
    assert datos["auditoria_agregada"]["actividad_log"] == {
        "filas_core": 3, "filas_dominio_antes": 2, "filas_dominio_despues": 5, "max_id_antes": 2,
        "conversiones": CONVERSIONES,
    }
    # El resto de la unión, como siempre.
    assert _foto(core, ["clients", "caja_movimientos"]) == _foto(dominio, ["clients", "caja_movimientos"])

    # Y re-unir no agrega nada.
    assert _correr(core, dominio, "unir", informe) == 0
    assert _fila(dominio, "SELECT count(*) FROM actividad_log") == [(5,)]


def test_revertir_no_devuelve_la_auditoria(con_actividad, tmp_path, capsys):
    core, dominio = con_actividad
    informe = tmp_path / "union.json"
    assert _correr(core, dominio, "unir", informe) == 0
    _ejecutar(dominio, ["INSERT INTO actividad_log (ts, usuario, accion, entidad, descripcion) VALUES (now(), 'admin', 'crear', 'x', 'nueva')"])
    del_core = _actividad(core)
    capsys.readouterr()

    assert _correr(core, dominio, "revertir", informe) == 0

    assert "actividad_log" in capsys.readouterr().out
    assert _actividad(core) == del_core  # ni lo agregado en la unión ni lo nuevo vuelve: es auditoría, como `auth_log`


def test_corta_si_un_ts_no_se_puede_convertir(con_actividad, tmp_path):
    core, dominio = con_actividad
    _ejecutar(core, ["UPDATE actividad_log SET ts = 'ayer a la tarde' WHERE id = 41"])

    codigos, v = _codigos(core, dominio)

    assert codigos == {"conversion_fallida"}
    assert "actividad_log.ts" in v.errores[0].mensaje and "ayer a la tarde" in v.errores[0].mensaje
    antes = _tablas(dominio)
    assert _correr(core, dominio, "unir", tmp_path / "u.json") == 1
    assert _tablas(dominio) == antes  # cortó antes de tocar nada


def test_corta_si_un_valor_no_entra_en_el_largo_del_dominio(con_actividad):
    core, dominio = con_actividad
    _ejecutar(core, ["UPDATE actividad_log SET accion = 'una acción de más de veinte caracteres' WHERE id = 40"])
    codigos, v = _codigos(core, dominio)
    assert codigos == {"auditoria_no_entra"}
    assert "accion" in v.errores[0].mensaje and "character varying(20)" in v.errores[0].mensaje


def test_corta_si_algo_referencia_a_actividad_log(con_actividad):
    core, dominio = con_actividad
    _ejecutar(dominio, ["CREATE TABLE revisiones (id SERIAL PRIMARY KEY, actividad_id INTEGER REFERENCES actividad_log(id))"])
    codigos, v = _codigos(core, dominio)
    assert codigos == {"auditoria_referenciada"}
    assert "revisiones.actividad_id" in v.errores[0].mensaje


def test_corta_si_el_core_tiene_columnas_que_el_dominio_no(con_actividad):
    core, dominio = con_actividad
    _ejecutar(core, ["ALTER TABLE actividad_log ADD COLUMN sucursal_id INTEGER"])
    codigos, v = _codigos(core, dominio)
    assert codigos == {"auditoria_incompatible"} and "sucursal_id" in v.errores[0].mensaje


def test_un_ts_con_huso_avisa_y_queda_la_hora_escrita(con_actividad, tmp_path):
    from datetime import datetime

    core, dominio = con_actividad
    _ejecutar(core, ["UPDATE actividad_log SET ts = '2026-10-01T10:00:00+00:00' WHERE id = 40"])
    v = unir_bases.verificar(core, dominio, PREFIJO)
    assert v.ok and "auditoria_con_huso" in {h.codigo for h in v.avisos}
    assert _correr(core, dominio, "unir", tmp_path / "u.json") == 0
    assert _fila(dominio, "SELECT ts FROM actividad_log WHERE descripcion = 'Venta 12'") == [(datetime(2026, 10, 1, 10, 0, 0),)]


# --- revertir ----------------------------------------------------------------------------------------------------------


def _usar_el_dominio_unido(dominio):
    """Lo que hace la app después de la unión: filas nuevas con ids nuevos, y un cambio a una fila vieja."""
    with conectar(dominio) as c:
        cliente = c.execute("INSERT INTO clients (name) VALUES ('Dani') RETURNING id").fetchone()[0]
        assert cliente == 41
        c.execute("INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, usuario_id) VALUES ('2026-10-05', 'ingreso', 'Nuevo', 77.70, 1)")
        c.execute("INSERT INTO ventas (numero, fecha, cliente_id, items, total, usuario_id) VALUES ('V-0003', '2026-10-05', 41, '[]', 77.70, 2)")
        c.execute("INSERT INTO ventas_pagos (venta_id, medio, monto) VALUES (3, 'efectivo', 77.70)")
        c.execute("INSERT INTO cc_asientos (id, fecha, tercero_id, rol, concepto, debe, haber, contrapartida_de) VALUES "
                  "(13, '2026-10-05', 41, 'cliente', 'Nuevo 2', 0, 5, 12), (12, '2026-10-05', 41, 'cliente', 'Nuevo', 0, 20, NULL)")
        c.commit()


def test_revertir_devuelve_al_core_lo_nuevo_con_sus_ids(par, tmp_path, capsys):
    core, dominio = par
    informe = tmp_path / "union.json"
    assert _correr(core, dominio, "unir", informe) == 0
    foto_core_viejo = _foto(core)
    _usar_el_dominio_unido(dominio)
    capsys.readouterr()

    assert _correr(core, dominio, "revertir", informe) == 0

    out = capsys.readouterr().out
    assert "[OK] Devueltas" in out
    assert _fila(core, "SELECT id, name FROM clients WHERE id > 40") == [(41, "Dani")]
    assert _fila(core, "SELECT id, monto FROM caja_movimientos WHERE id > 7") == [(8, Decimal("77.70"))]
    assert _fila(core, "SELECT id, venta_id FROM ventas_pagos WHERE id > 2") == [(3, 3)]
    assert _fila(core, "SELECT id, contrapartida_de FROM cc_asientos WHERE id > 11 ORDER BY id") == [(12, None), (13, 12)]
    # Lo viejo del core, idéntico (con el dominio ya devuelto, ambos lados coinciden fila por fila).
    nuevas = {t: _foto(core)[t] for t in foto_core_viejo}
    for t in foto_core_viejo:
        assert nuevas[t]["filas"] >= foto_core_viejo[t]["filas"]
    for t in ("clients", "caja_movimientos", "ventas", "ventas_pagos", "cc_asientos", "cajas"):
        assert _foto(core, [t]) == _foto(dominio, [t]), t
    # La secuencia del core sigue a lo devuelto.
    with conectar(core) as c:
        assert c.execute("INSERT INTO clients (name) VALUES ('Eze') RETURNING id").fetchone()[0] == 42
        c.rollback()
    assert json.loads(informe.read_text())["reversiones"][0]["filas"] == 6

    # Y volver a revertir no devuelve nada más.
    assert _correr(core, dominio, "revertir", informe) == 0
    assert "Nada nuevo que devolver" in capsys.readouterr().out
    assert len(json.loads(informe.read_text())["reversiones"]) == 2


def test_revertir_se_niega_si_el_core_cambio(par, tmp_path, capsys):
    core, dominio = par
    informe = tmp_path / "union.json"
    assert _correr(core, dominio, "unir", informe) == 0
    _usar_el_dominio_unido(dominio)
    _ejecutar(core, ["UPDATE caja_movimientos SET monto = monto + 1 WHERE id = 1"])  # alguien siguió usando el core viejo
    capsys.readouterr()

    assert _correr(core, dominio, "revertir", informe) == 1

    assert "El core cambió desde la unión (caja_movimientos)" in capsys.readouterr().err
    assert _fila(core, "SELECT count(*) FROM clients") == [(3,)]


def test_revertir_con_filas_que_apuntan_a_un_usuario_que_el_core_no_tiene(par, tmp_path, capsys):
    """El límite documentado: un usuario creado en el dominio después de unir no existe en `usuarios` del core."""
    core, dominio = par
    informe = tmp_path / "union.json"
    assert _correr(core, dominio, "unir", informe) == 0
    _ejecutar(dominio, ["INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, usuario_id) VALUES ('2026-10-05', 'ingreso', 'x', 1, 3)"])
    foto = _foto(core)
    capsys.readouterr()

    assert _correr(core, dominio, "revertir", informe) == 1

    assert "No se devolvió nada" in capsys.readouterr().err
    assert _foto(core) == foto  # la transacción del core se deshizo entera


def test_revertir_sin_union_previa(par, tmp_path):
    core, dominio = par
    assert _correr(core, dominio, "revertir", tmp_path / "no-existe.json") == 2
    informe = tmp_path / "v.json"
    _correr(core, dominio, "verificar", informe)
    assert _correr(core, dominio, "revertir", informe) == 2  # una verificación no es una unión


# --- línea de comandos --------------------------------------------------------------------------------------------------


def test_las_url_salen_del_entorno_de_la_instancia_y_no_se_imprimen(par, tmp_path, monkeypatch, capsys):
    core, dominio = par
    monkeypatch.setenv("LIBRACLUB_LIBRACORE_DATABASE_URL", core)
    monkeypatch.setenv("LIBRACLUB_DATABASE_URL", dominio)

    assert unir_bases.main(["--prefijo", "libraclub", "verificar"]) == 0

    out = capsys.readouterr()
    assert "[OK] Se puede unir" in out.out
    assert "postgresql://" not in out.out + out.err and "@" not in out.out + out.err


def test_env_nombre_lee_otra_variable(par, monkeypatch):
    core, dominio = par
    monkeypatch.setenv("MI_CORE", core)
    monkeypatch.setenv("MI_DOMINIO", dominio)
    assert unir_bases.main(["--prefijo", "libraclub", "--core", "env:MI_CORE", "--dominio", "env:MI_DOMINIO", "verificar"]) == 0
    monkeypatch.delenv("MI_CORE")
    assert unir_bases.main(["--prefijo", "libraclub", "--core", "env:MI_CORE", "--dominio", dominio, "verificar"]) == 2


def test_faltan_urls_o_prefijo(monkeypatch, capsys):
    for var in ("LIBRACLUB_DATABASE_URL", "LIBRACLUB_LIBRACORE_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    assert unir_bases.main(["--prefijo", "libraclub", "verificar"]) == 2
    assert "LIBRACLUB_LIBRACORE_DATABASE_URL" in capsys.readouterr().err
    assert unir_bases.main(["verificar"]) == 2
    assert "--prefijo" in capsys.readouterr().err
    assert unir_bases.main(["--prefijo", "libraclub", "--core", "a.db", "--dominio", "b.db", "unir", "--informe", "x"]) == 2


def test_un_error_de_conexion_no_muestra_la_contrasena(capsys):
    url = "postgresql://usuario:SuperSecreta123@127.0.0.1:1/base"
    assert unir_bases.main(["--prefijo", "libraclub", "--core", url, "--dominio", url + "2", "verificar"]) == 2
    out = capsys.readouterr()
    assert "SuperSecreta123" not in out.out + out.err
    assert "127.0.0.1:1/base" in out.err


def test_ocultar_deja_solo_host_puerto_y_base():
    assert unir_bases.ocultar("postgresql+psycopg://u:clave@db.interna:5433/club") == "db.interna:5433/club"
    assert unir_bases.ocultar("postgres://u:clave@localhost/club") == "localhost/club"


def test_el_informe_va_antes_o_despues_del_subcomando(par, tmp_path):
    core, dominio = par
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    assert unir_bases.main(["--prefijo", PREFIJO, "--core", core, "--dominio", dominio, "--informe", str(a), "verificar"]) == 0
    assert unir_bases.main(["verificar", "--prefijo", PREFIJO, "--core", core, "--dominio", dominio, "--informe", str(b)]) == 0
    assert json.loads(a.read_text())["tablas"] == json.loads(b.read_text())["tablas"]


# --- guardas contra que el motor cambie ---------------------------------------------------------------------------------


def test_las_semillas_registradas_son_las_unicas_que_siembra_el_motor(plantilla):
    """Si una revisión nueva siembra otra tabla, `unir` la pisaría como dato: este test obliga a registrarla."""
    url = plantilla
    sembradas = {}
    with conectar(url) as c:
        for t in unir_bases.tablas_de(c):
            if t.startswith("alembic_version"):
                continue
            filas = c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            if filas:
                sembradas[t] = filas
        for t, (columna, conocidos) in unir_bases.SEMILLAS.items():
            assert {r[0] for r in c.execute(f'SELECT "{columna}" FROM "{t}"').fetchall()} == set(conocidos), t
    assert set(sembradas) == set(unir_bases.SEMILLAS), sembradas


def test_las_tablas_de_auth_que_declara_el_motor_estan_registradas(plantilla):
    with conectar(plantilla) as c:
        tablas = set(unir_bases.tablas_de(c))
    assert {"usuarios", "auth_log"} <= tablas and {"usuarios", "auth_log"} <= set(unir_bases.TABLAS_DE_AUTH)


def test_subir_el_motor_no_deja_el_estado_global_de_la_app(bases):
    from libracore.db import core as core_db

    antes = (core_db._db_path, core_db._database_url, core_db._timeout, core_db._extra_pragmas)
    unir_bases._subir_libracore(bases.nueva())
    assert (core_db._db_path, core_db._database_url, core_db._timeout, core_db._extra_pragmas) == antes
