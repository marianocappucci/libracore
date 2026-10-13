"""El registro de errores propio (ADR-045): `libracore.errores`.

Lo que se prueba, y por qué cada cosa es una propiedad y no un detalle:

- un 500 queda en el JSONL con el `ref` que ve quien lo sufre;
- los 4xx NO se registran (ruido que tapa los errores del sistema);
- nada personal llega al archivo (CUIT, mail, PEM, URL de la base, query, DETAIL de psycopg);
- la `firma` agrupa por lugar y no se parte cuando el código se mueve de línea;
- registrar nunca rompe la respuesta: un disco que no escribe no la cambia.
"""
import json
import os

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from libracore import errores
from libracore.errores import (
    build_errores_cliente_router,
    calcular_firma,
    capturar,
    construir_evento,
    instalar_registro_de_errores,
    limpiar_query,
    limpiar_texto,
)


@pytest.fixture(autouse=True)
def entorno(tmp_path, monkeypatch):
    """Un `DATA_DIR` propio por test y el cache de manejadores vacío."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LIBRA_ERRORES_DIR", raising=False)
    monkeypatch.setenv("LIBRA_INSTANCIA", "testprod-lagrace")
    monkeypatch.setenv("LIBRA_ENTORNO", "cliente")
    monkeypatch.setenv("LIBRA_VERSION", "v2026.10.12-103000")
    monkeypatch.setattr(errores, "_manejadores", {})
    monkeypatch.setattr(errores, "_producto_global", None)
    return tmp_path


def lineas(tmp_path):
    archivo = tmp_path / "errores" / "errores.jsonl"
    if not archivo.exists():
        return []
    return [json.loads(x) for x in archivo.read_text(encoding="utf-8").splitlines() if x.strip()]


class _Cuerpo(BaseModel):
    cantidad: int


def _app(**kw):
    app = FastAPI()

    @app.get("/api/clientes/{cid}")
    def boom(cid: int):
        raise ValueError("falló el cliente 20-12345678-9 (mail ana@ejemplo.com)")

    @app.get("/api/prohibido")
    def prohibido():
        raise HTTPException(403, "no")

    @app.get("/api/no-esta")
    def no_esta():
        raise HTTPException(404, "no está")

    @app.post("/api/validar")
    def validar(c: _Cuerpo):
        return {"ok": True}

    @app.get("/api/desconectado")
    def desconectado():
        raise ClientDisconnect()

    @app.get("/api/ok")
    def ok():
        return {"ok": True}

    instalar_registro_de_errores(app, "testprod", **kw)
    return app


@pytest.fixture
def cliente():
    return TestClient(_app(), raise_server_exceptions=False)


# ── El caso central ───────────────────────────────────────────────────────────

def test_una_ruta_que_lanza_da_500_con_ref_y_deja_una_linea(cliente, entorno):
    r = cliente.get("/api/clientes/77?token=abc&q=hola")

    assert r.status_code == 500
    cuerpo = r.json()
    assert cuerpo["detail"] == "Error interno"
    assert len(cuerpo["ref"]) == 8
    (ev,) = lineas(entorno)
    assert ev["ref"] == cuerpo["ref"], "el ref que ve el usuario es el de la línea"
    assert ev["producto"] == "testprod"
    assert ev["instancia"] == "testprod-lagrace"
    assert ev["entorno"] == "cliente"
    assert ev["release"] == "v2026.10.12-103000"
    assert ev["metodo"] == "GET"
    assert ev["status"] == 500
    assert ev["tipo"] == "ValueError"
    assert ev["origen"] == "servidor"
    assert ev["manejado"] is False
    assert ev["ts"].endswith("-03:00"), "UTC-3 fijo"


def test_la_ruta_es_la_plantilla_y_no_la_url_con_el_id(cliente, entorno):
    cliente.get("/api/clientes/77")
    (ev,) = lineas(entorno)
    assert ev["ruta"] == "/api/clientes/{cid}"
    assert "77" not in ev["ruta"]


def test_el_evento_trae_lo_que_debe_y_nada_mas(cliente, entorno):
    cliente.get("/api/clientes/1", headers={"Authorization": "Bearer secretisimo", "Cookie": "libra_session=xyz",
                                            "User-Agent": "Mozilla/5.0 (X11)"})
    (ev,) = lineas(entorno)
    texto = json.dumps(ev)
    assert "secretisimo" not in texto and "libra_session" not in texto and "xyz" not in texto
    assert ev["user_agent"] == "Mozilla/5.0 (X11)"
    assert set(ev) >= {"ts", "producto", "instancia", "entorno", "release", "metodo", "ruta", "status", "tipo",
                       "mensaje", "traceback", "firma", "ref", "usuario_id", "donde"}
    assert ev["donde"].endswith(":boom"), "archivo:función de la app, el dato que usa el aviso del vigía"


def test_el_traceback_no_trae_variables_locales(entorno):
    app = FastAPI()

    @app.get("/x")
    def x():
        valor_secreto_local = "contraseña-de-la-tarjeta"  # noqa: F841
        raise RuntimeError("falla")

    instalar_registro_de_errores(app, "testprod")
    TestClient(app, raise_server_exceptions=False).get("/x")
    (ev,) = lineas(entorno)
    assert "contraseña-de-la-tarjeta" not in json.dumps(ev, ensure_ascii=False)
    assert "RuntimeError: falla" in ev["traceback"]


def test_el_traceback_arranca_en_el_codigo_de_la_app_y_no_en_starlette(cliente, entorno):
    cliente.get("/api/clientes/1")
    (ev,) = lineas(entorno)
    primera = [ln for ln in ev["traceback"].splitlines() if ln.lstrip().startswith("File ")][0]
    assert "test_errores.py" in primera, primera
    assert len(json.dumps(ev)) < 6000


# ── Lo que NO se registra ─────────────────────────────────────────────────────

@pytest.mark.parametrize("metodo,ruta,esperado", [
    ("get", "/api/prohibido", 403),
    ("get", "/api/no-esta", 404),
    ("get", "/api/ruta-que-no-existe", 404),
    ("post", "/api/validar", 422),
])
def test_los_4xx_y_la_validacion_no_se_registran(cliente, entorno, metodo, ruta, esperado):
    r = getattr(cliente, metodo)(ruta, **({"json": {"cantidad": "no es un número"}} if metodo == "post" else {}))
    assert r.status_code == esperado
    assert lineas(entorno) == []


def test_un_cliente_que_se_desconecta_no_se_registra(cliente, entorno):
    cliente.get("/api/desconectado")
    assert lineas(entorno) == []


def test_un_pedido_bueno_no_deja_nada(cliente, entorno):
    assert cliente.get("/api/ok").status_code == 200
    assert not (entorno / "errores").exists()


def test_un_httpexception_que_llega_al_handler_se_responde_igual_y_no_se_registra(entorno):
    """Un middleware que levanta HTTPException la salta del `ExceptionMiddleware` y llega al handler
    de `Exception`: tiene que seguir siendo un 4xx, no volverse un 500."""
    from starlette.middleware.base import BaseHTTPMiddleware

    class Corta(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            raise HTTPException(401, "sin sesión")

    app = FastAPI()
    app.add_middleware(Corta)
    instalar_registro_de_errores(app, "testprod")
    r = TestClient(app, raise_server_exceptions=False).get("/lo-que-sea")
    assert r.status_code == 401
    assert r.json() == {"detail": "sin sesión"}
    assert lineas(entorno) == []


# ── Limpieza ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("crudo,prohibido", [
    ("CUIT 20-12345678-9 no existe", "12345678"),
    ("CUIT 20123456789 no existe", "123456789"),
    ("DNI 30.123.456", "123.456"),
    ("DNI 30123456", "30123456"),
    ("escribir a ana.perez+x@correo.com.ar", "ana.perez"),
    ("clave -----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\nabc\n-----END PRIVATE KEY----- fin", "MIIEvQ"),
    ("clave -----BEGIN CERTIFICATE-----\nMIIEvQIBADANBgkq (cortado)", "MIIEvQ"),
    ("could not connect: postgresql://libra:S3cr3t@db:5432/x", "S3cr3t"),
    ("postgresql+psycopg://u:p%40ss@h/db", "p%40ss"),
    ("DETAIL:  Key (cuit)=(20123456789) already exists.", "20123456789"),
    ("DETAIL:  Key (nombre)=(Juan Pérez) already exists.", "Juan"),
    ("DETAIL:  Failing row contains (1, Juan Pérez, 2026-10-12).", "Juan"),
    ('invalid input syntax for type integer: "Juan Pérez"', "Juan"),
    ("[SQL: INSERT INTO t VALUES (%s)] [parameters: ('Juan', 'ana@x.com')]", "Juan"),
    ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk", "eyJhbGci"),
    ("Authorization: Bearer abcdef0123456789xyz", "abcdef0123"),
    ("password=hunter2 y más", "hunter2"),
    ("SECRET_KEY: 'sup3rsecreta'", "sup3rsecreta"),
])
def test_limpieza_de_texto(crudo, prohibido):
    limpio = limpiar_texto(crudo)
    assert prohibido not in limpio, limpio


def test_la_limpieza_conserva_lo_util():
    limpio = limpiar_texto("DETAIL:  Key (cuit)=(20123456789) already exists. violates unique constraint \"clients_cuit_key\"")
    assert "Key (cuit)" in limpio
    assert "clients_cuit_key" in limpio
    assert "KeyError: 'campo'" in limpiar_texto("KeyError: 'campo'"), "un KeyError no es una clave"
    assert "línea 25" in limpiar_texto("línea 25")


def test_el_mensaje_y_el_traceback_del_evento_salen_limpios(cliente, entorno):
    cliente.get("/api/clientes/1")
    (ev,) = lineas(entorno)
    for campo in ("mensaje", "traceback"):
        assert "12345678" not in ev[campo]
        assert "ana@ejemplo.com" not in ev[campo]
    assert "[CUIT]" in ev["mensaje"] and "[email]" in ev["mensaje"]


def test_la_query_enmascara_las_claves_sensibles(cliente, entorno):
    cliente.get("/api/clientes/1?token=abc123&Password=hunter2&clave_afip=zzz&cuit=20123456789"
                "&dni=1&cbu=2&cert=3&secret=4&api_key=5&q=buscar")
    (ev,) = lineas(entorno)
    q = ev["query"]
    assert q["q"] == "buscar"
    for k in ("token", "Password", "clave_afip", "cuit", "dni", "cbu", "cert", "secret", "api_key"):
        assert q[k] == "***", k
    assert "hunter2" not in json.dumps(ev)


def test_la_query_limpia_los_valores_y_los_topa():
    q = limpiar_query({f"k{i}": "20123456789" for i in range(40)})
    assert len(q) == 20
    assert set(q.values()) == {"[CUIT/DNI]"}
    assert len(limpiar_query({"q": "x" * 5000})["q"]) <= errores.MAX_VALOR


def test_una_excepcion_encadenada_deja_las_dos_limpias(entorno):
    """`raise X from Y`: el traceback trae la causa, y la causa pasa por la misma limpieza."""
    try:
        try:
            raise KeyError("cuit 20123456789")
        except KeyError as causa:
            raise RuntimeError("no se pudo cobrar a ana@x.com") from causa
    except RuntimeError as e:
        ev = construir_evento(e)

    assert "KeyError" in ev["traceback"] and "RuntimeError" in ev["traceback"]
    assert "20123456789" not in ev["traceback"] and "ana@x.com" not in ev["traceback"]
    assert ev["tipo"] == "RuntimeError"


def test_el_traceback_se_trunca_a_8_kb(entorno):
    def recursiva(n):
        if n == 0:
            raise ValueError("fondo " + "x" * 20000)
        recursiva(n - 1)

    try:
        recursiva(40)
    except ValueError as e:
        ev = construir_evento(e)
    assert len(ev["traceback"]) <= errores.MAX_TRACEBACK + 20
    assert ev["traceback"].rstrip().endswith("x" * 50) or "[...]" in ev["traceback"]
    assert len(ev["mensaje"]) <= errores.MAX_MENSAJE


# ── La firma ──────────────────────────────────────────────────────────────────

def _lanza(fuente: str, nombre="calcular", archivo="app/facturas.py"):
    """Ejecuta `fuente` como si fuera `archivo` y devuelve la excepción que lanza `nombre()`."""
    ns: dict = {}
    exec(compile(fuente, archivo, "exec"), ns)
    try:
        ns[nombre]()
    except Exception as e:
        return e
    raise AssertionError("no lanzó")


FUENTE = "def calcular():\n    raise ZeroDivisionError('x')\n"


def test_la_firma_no_cambia_si_el_codigo_se_mueve_de_linea():
    a = _lanza(FUENTE)
    b = _lanza("\n\n\n# un comentario nuevo\n\n" + FUENTE)
    assert a.__traceback__.tb_next.tb_lineno != b.__traceback__.tb_next.tb_lineno
    assert calcular_firma("ZeroDivisionError", "/api/x", errores._frame_de_la_app(a)) == \
        calcular_firma("ZeroDivisionError", "/api/x", errores._frame_de_la_app(b))
    assert errores._frame_de_la_app(a) == "app/facturas.py:calcular"


def test_la_firma_cambia_con_el_lugar_el_tipo_o_la_ruta():
    base = construir_evento(_lanza(FUENTE))["firma"]
    assert construir_evento(_lanza(FUENTE, archivo="app/otro.py"))["firma"] != base
    assert construir_evento(_lanza(FUENTE.replace("calcular", "sumar"), nombre="sumar"))["firma"] != base
    assert construir_evento(_lanza(FUENTE.replace("ZeroDivisionError", "KeyError")))["firma"] != base
    assert calcular_firma("T", "/a", "f:g") != calcular_firma("T", "/b", "f:g")


def test_la_firma_toma_el_frame_de_la_app_y_no_el_de_la_libreria():
    """Un error de `json` llamado desde la app se firma por la línea de la app."""
    e = _lanza("import json\ndef calcular():\n    json.loads('{')\n")
    assert errores._frame_de_la_app(e) == "app/facturas.py:calcular"


def test_un_frame_de_libracore_instalado_cuenta_como_propio():
    assert not errores._es_ajeno("/app/.venv/lib/python3.12/site-packages/libracore/db/core.py")
    assert errores._es_ajeno("/app/.venv/lib/python3.12/site-packages/starlette/routing.py")
    assert errores._es_ajeno("<frozen importlib._bootstrap>")
    assert not errores._es_ajeno("/app/app/main.py")
    assert errores._archivo_relativo(
        "/app/.venv/lib/python3.12/site-packages/libracore/db/core.py") == "libracore/db/core.py"


def test_la_firma_es_la_misma_en_dos_pedidos_del_mismo_error(cliente, entorno):
    cliente.get("/api/clientes/1")
    cliente.get("/api/clientes/2")
    a, b = lineas(entorno)
    assert a["firma"] == b["firma"]
    assert a["ref"] != b["ref"]


# ── Escribir nunca rompe la respuesta ────────────────────────────────────────

def test_si_no_se_puede_escribir_la_respuesta_sale_igual_y_la_linea_va_al_stdout(entorno, monkeypatch, capsys):
    # `errores` es un ARCHIVO: no se puede crear el directorio. Falla igual de sin permisos.
    (entorno / "errores").write_text("estorbo")
    c = TestClient(_app(), raise_server_exceptions=False)

    r = c.get("/api/clientes/1")

    assert r.status_code == 500
    ref = r.json()["ref"]
    salida = capsys.readouterr().out
    ev = json.loads(salida.strip().splitlines()[-1])
    assert ev["ref"] == ref and ev["tipo"] == "ValueError"


def test_sin_permisos_de_escritura_en_el_directorio(entorno, capsys):
    if os.geteuid() == 0:
        pytest.skip("root escribe igual")
    carpeta = entorno / "errores"
    carpeta.mkdir()
    carpeta.chmod(0o500)
    try:
        r = TestClient(_app(), raise_server_exceptions=False).get("/api/clientes/1")
    finally:
        carpeta.chmod(0o700)
    assert r.status_code == 500
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ref"] == r.json()["ref"]


def test_registrar_devuelve_si_pudo_escribir(entorno):
    assert errores.registrar({"ref": "a"}) is True
    (entorno / "otro").write_text("")
    os.environ["LIBRA_ERRORES_DIR"] = str(entorno / "otro" / "no")
    try:
        assert errores.registrar({"ref": "b"}) is False
    finally:
        del os.environ["LIBRA_ERRORES_DIR"]


def test_un_evento_que_no_se_serializa_no_rompe(entorno):
    class Raro:
        def __str__(self):
            raise RuntimeError("no me pueden imprimir")

    assert errores.registrar({"ref": "c", "x": Raro()}) in (True, False)


def test_una_falla_de_la_limpieza_no_cambia_la_respuesta(entorno, monkeypatch, capsys):
    def mal(*a, **k):
        raise RuntimeError("la limpieza se rompió")

    monkeypatch.setattr(errores, "construir_evento", mal)
    r = TestClient(_app(), raise_server_exceptions=False).get("/api/clientes/1")
    assert r.status_code == 500 and r.json()["detail"] == "Error interno"
    (ev,) = lineas(entorno)
    assert ev["ref"] == r.json()["ref"] and ev["tipo"] == "ValueError"


def test_rotacion(entorno, monkeypatch):
    monkeypatch.setattr(errores, "MAX_BYTES", 2000)
    for i in range(30):
        errores.registrar({"ref": f"{i:08d}", "relleno": "x" * 200})
    nombres = sorted(p.name for p in (entorno / "errores").iterdir())
    assert nombres == ["errores.jsonl", "errores.jsonl.1", "errores.jsonl.2", "errores.jsonl.3"]
    assert errores.MAX_BYTES == 2000 and errores.COPIAS == 3


def test_si_otro_proceso_rota_el_archivo_se_reabre(entorno):
    """Con varios workers el que rota renombra el archivo: los demás no pueden seguir escribiendo en el `.1`."""
    errores.registrar({"ref": "antes"})
    archivo = entorno / "errores" / "errores.jsonl"
    archivo.rename(entorno / "errores" / "errores.jsonl.1")  # lo que hace el otro proceso
    errores.registrar({"ref": "despues"})
    assert [json.loads(x)["ref"] for x in archivo.read_text().splitlines()] == ["despues"]
    assert "antes" in (entorno / "errores" / "errores.jsonl.1").read_text()


def test_los_limites_son_los_del_diseno():
    assert (5 * 1024 * 1024, 3, 8 * 1024, 4 * 1024) == (
        errores.__dict__["MAX_BYTES"], errores.COPIAS, errores.MAX_TRACEBACK, errores.MAX_PAYLOAD_CLIENTE)


# ── Entorno y usuario ────────────────────────────────────────────────────────

@pytest.mark.parametrize("valor,esperado", [
    ("cliente", "cliente"), ("DEMO", "demo"), ("dev", "dev"), ("produccion", "desconocido"), ("", "desconocido"),
])
def test_el_entorno_faltante_o_raro_es_desconocido(monkeypatch, valor, esperado):
    monkeypatch.setenv("LIBRA_ENTORNO", valor)
    assert construir_evento(ValueError("x"))["entorno"] == esperado


def test_sin_la_variable_el_entorno_es_desconocido(monkeypatch):
    monkeypatch.delenv("LIBRA_ENTORNO")
    assert construir_evento(ValueError("x"))["entorno"] == "desconocido"


def test_el_usuario_es_solo_el_id_y_lo_resuelve_el_producto(entorno):
    app = FastAPI()

    @app.get("/x")
    def x():
        raise RuntimeError("falla")

    instalar_registro_de_errores(app, "testprod", usuario_id=lambda request: 42)
    TestClient(app, raise_server_exceptions=False).get("/x")
    assert lineas(entorno)[0]["usuario_id"] == "42"


def test_un_resolvedor_de_usuario_que_falla_no_rompe(entorno):
    app = FastAPI()

    @app.get("/x")
    def x():
        raise RuntimeError("falla")

    def malo(request):
        raise KeyError("sin sesión")

    instalar_registro_de_errores(app, "testprod", usuario_id=malo)
    r = TestClient(app, raise_server_exceptions=False).get("/x")
    assert r.status_code == 500
    assert lineas(entorno)[0]["usuario_id"] is None


def test_instalar_dos_veces_no_duplica_el_endpoint():
    app = FastAPI()
    montados = []
    original = app.include_router
    app.include_router = lambda *a, **k: (montados.append(a), original(*a, **k))[1]
    instalar_registro_de_errores(app, "a")
    instalar_registro_de_errores(app, "a")
    assert len(montados) == 1
    assert TestClient(app).post("/api/errores-cliente", json={"mensaje": "x"}).status_code == 204


# ── capturar() ───────────────────────────────────────────────────────────────

def test_capturar_registra_una_falla_manejada_y_devuelve_el_ref(entorno):
    errores._producto_global = "testprod"
    try:
        raise TimeoutError("ARCA no contesta para 20123456789")
    except TimeoutError as e:
        ref = capturar(e, tags={"servicio": "wsfe"}, factura_id=15, token="abc", cuit_emisor="20123456789")

    (ev,) = lineas(entorno)
    assert ev["ref"] == ref
    assert ev["manejado"] is True
    assert ev["tipo"] == "TimeoutError"
    assert ev["producto"] == "testprod"
    assert ev["release"] == "v2026.10.12-103000"
    assert ev["tags"] == {"servicio": "wsfe"}
    assert ev["contexto"]["factura_id"] == "15"
    assert ev["contexto"]["token"] == "***" and ev["contexto"]["cuit_emisor"] == "***"
    assert "20123456789" not in json.dumps(ev)


def test_capturar_nunca_lanza_aunque_no_se_pueda_escribir(entorno, capsys):
    (entorno / "errores").write_text("estorbo")
    ref = capturar(ValueError("x"))
    assert len(ref) == 8
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ref"] == ref


def test_capturar_con_una_excepcion_sin_traceback(entorno):
    ref = capturar(ValueError("nunca lanzada"))
    (ev,) = lineas(entorno)
    assert ev["ref"] == ref and ev["firma"]


# ── POST /api/errores-cliente ────────────────────────────────────────────────

@pytest.fixture
def web(entorno):
    app = FastAPI()
    instalar_registro_de_errores(app, "testprod")
    return TestClient(app, raise_server_exceptions=False)


PAYLOAD = {
    "tipo": "error", "mensaje": "Cannot read properties of undefined (reading 'nombre') de ana@x.com",
    "stack": "TypeError: x\n    at f (https://cliente.testprod.com.ar/assets/index-abc123.js:1:2345)",
    "pagina": "https://cliente.testprod.com.ar/clientes/1234/editar?token=zzz#frag", "release": "v2026.10.12-103000",
}


def test_el_cliente_reporta_y_queda_la_linea_con_origen_cliente(web, entorno):
    r = web.post("/api/errores-cliente", json=PAYLOAD)

    assert r.status_code == 204
    (ev,) = lineas(entorno)
    assert ev["origen"] == "cliente"
    assert ev["producto"] == "testprod" and ev["entorno"] == "cliente" and ev["instancia"] == "testprod-lagrace"
    assert ev["ruta"] == "/clientes/:id/editar", "plantilla, sin query ni fragmento ni el id"
    assert ev["release_cliente"] == "v2026.10.12-103000"
    assert ev["tipo"] == "error"
    assert "ana@x.com" not in json.dumps(ev) and "zzz" not in json.dumps(ev)
    assert ev["firma"]


def test_el_cliente_sin_sesion_alcanza(web):
    """Es público a propósito: un error de la pantalla de login también importa."""
    assert web.post("/api/errores-cliente", json=PAYLOAD).status_code == 204


def test_el_cliente_pasa_por_la_misma_limpieza(web, entorno):
    web.post("/api/errores-cliente", json={**PAYLOAD, "stack": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----",
                                           "mensaje": "postgresql://u:clave@h/db 20123456789"})
    (ev,) = lineas(entorno)
    assert "AAAA" not in json.dumps(ev) and "clave@" not in json.dumps(ev) and "20123456789" not in json.dumps(ev)


def test_se_guardan_solo_los_campos_conocidos(web, entorno):
    web.post("/api/errores-cliente", json={**PAYLOAD, "cookie": "libra_session=abc", "extra": {"a": 1}})
    (ev,) = lineas(entorno)
    assert "cookie" not in ev and "extra" not in ev and "libra_session" not in json.dumps(ev)


def test_mas_de_4_kb_es_413_y_no_registra(web, entorno):
    r = web.post("/api/errores-cliente", json={**PAYLOAD, "stack": "x" * 5000})
    assert r.status_code == 413
    assert lineas(entorno) == []


def test_mas_de_4_kb_sin_content_length_tambien(web, entorno):
    def cuerpo():
        yield b'{"stack": "' + b"x" * 5000 + b'"}'

    r = web.post("/api/errores-cliente", content=cuerpo())
    assert r.status_code == 413
    assert lineas(entorno) == []


@pytest.mark.parametrize("cuerpo", [b"no es json", b"[1, 2]", b'"hola"', b""])
def test_un_cuerpo_que_no_es_un_objeto_json_es_400(web, cuerpo, entorno):
    assert web.post("/api/errores-cliente", content=cuerpo).status_code == 400
    assert lineas(entorno) == []


def test_tope_por_ip(web, entorno):
    codigos = [web.post("/api/errores-cliente", json=PAYLOAD, headers={"X-Forwarded-For": "1.1.1.1"}).status_code
               for _ in range(errores.TOPE_POR_IP + 3)]
    assert codigos[:errores.TOPE_POR_IP] == [204] * errores.TOPE_POR_IP
    assert codigos[errores.TOPE_POR_IP:] == [429] * 3
    assert len(lineas(entorno)) == errores.TOPE_POR_IP
    # otra IP no se ve afectada
    assert web.post("/api/errores-cliente", json=PAYLOAD, headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 204


def test_la_ip_es_la_que_agrega_el_proxy_y_no_la_que_manda_el_cliente(web):
    """NPM agrega la IP real al FINAL de X-Forwarded-For; lo anterior lo escribe quien llama."""
    for i in range(errores.TOPE_POR_IP):
        assert web.post("/api/errores-cliente", json=PAYLOAD,
                        headers={"X-Forwarded-For": f"10.0.0.{i}, 3.3.3.3"}).status_code == 204
    assert web.post("/api/errores-cliente", json=PAYLOAD,
                    headers={"X-Forwarded-For": "10.9.9.9, 3.3.3.3"}).status_code == 429


def test_tope_global(entorno, monkeypatch):
    monkeypatch.setattr(errores, "TOPE_GLOBAL", 5)
    app = FastAPI()
    app.include_router(build_errores_cliente_router("testprod"))
    c = TestClient(app, raise_server_exceptions=False)
    codigos = [c.post("/api/errores-cliente", json=PAYLOAD, headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code
               for i in range(8)]
    assert codigos == [204] * 5 + [429] * 3


def test_los_topes_se_vencen_con_el_tiempo():
    t = errores._Topes()
    assert all(t.permitir("ip", ahora=100.0) for _ in range(errores.TOPE_POR_IP))
    assert not t.permitir("ip", ahora=101.0)
    assert t.permitir("ip", ahora=100.0 + errores.VENTANA_IP + 1)


def test_otro_origen_es_403(web, entorno):
    r = web.post("/api/errores-cliente", json=PAYLOAD, headers={"Origin": "https://malo.example.com"})
    assert r.status_code == 403
    assert lineas(entorno) == []


def test_el_mismo_origen_pasa_tambien_detras_del_proxy(web):
    assert web.post("/api/errores-cliente", json=PAYLOAD,
                    headers={"Origin": "https://cliente.testprod.com.ar",
                             "X-Forwarded-Host": "cliente.testprod.com.ar"}).status_code == 204
    assert web.post("/api/errores-cliente", json=PAYLOAD,
                    headers={"Origin": "http://testserver"}).status_code == 204


def test_si_no_se_puede_escribir_el_cliente_igual_recibe_204(entorno):
    (entorno / "errores").write_text("estorbo")
    app = FastAPI()
    instalar_registro_de_errores(app, "testprod")
    assert TestClient(app).post("/api/errores-cliente", json=PAYLOAD).status_code == 204


def test_get_al_endpoint_no_existe(web):
    assert web.get("/api/errores-cliente").status_code == 405


def test_sin_endpoint_cliente_no_se_monta():
    app = FastAPI()
    instalar_registro_de_errores(app, "a", endpoint_cliente=False)
    assert TestClient(app).post("/api/errores-cliente", json={"mensaje": "x"}).status_code == 404


def test_el_endpoint_convive_con_el_catch_all_de_la_spa(entorno, tmp_path):
    """Los productos montan `/{full_path:path}` (GET); el POST tiene que ganarle."""
    from libracore.spa import montar_spa

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    app = FastAPI()
    instalar_registro_de_errores(app, "testprod")
    montar_spa(app, dist)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post("/api/errores-cliente", json=PAYLOAD).status_code == 204
    assert c.get("/api/otra-cosa").status_code == 404
    assert c.get("/pantalla").status_code == 200
