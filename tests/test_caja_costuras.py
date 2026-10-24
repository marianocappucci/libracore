"""Las tres costuras de la caja que pidió LibraCargo (ADR-080), en SQLite y en PostgreSQL.

1. **Corregir un movimiento** (`corregir_caja_movimiento`): en el central sí, con las reglas del alta; en un nodo no
   (409 `solo_en_el_central`); si subió de un nodo (`origen`) no; anulado no; sin gancho de modificación.
2. **Lo propio del producto viaja**: `extra` en `create_caja_movimiento` / `anular_caja_movimiento` llega a `datos`
   del gancho del nodo (y sin `extra` el `datos` es idéntico al de antes), y el gancho del central
   (`caja_del_nodo.aplicar_*_del_nodo(al_aplicar=)`) crea la fila hija con el id del motor en la misma transacción.
3. **`Decimal`**: se guarda exacto en PostgreSQL (`NUMERIC`) y en SQLite va como el `float` más cercano; `int` y `float`,
   como siempre.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracore import caja_del_nodo, nodo_offline
from libracore.caja_router import build_caja_router
from libracore.cobros import CobroNoPermitido
from libracore.db import caja as db_caja
from libracore.db import cierre_diario as cd
from libracore.db import core
from libracore.db import turnos as db_turnos
from libracore.db.schema import init_core_schema
from libracore.ganchos_de_caja import VERSION, GanchosDeCaja

DIA = "2026-10-20"
NODO = "S02-pc1"

_HIJA = """
    CREATE TABLE movimientos_hija (
        movimiento_id INTEGER PRIMARY KEY REFERENCES caja_movimientos(id),
        tercero TEXT,
        rol TEXT
    )
"""


def _url_pg() -> str:
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("LIBRACORE_POSTGRES_URL no está definida en CI — los tests contra PostgreSQL no se saltean acá")
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


@pytest.fixture(params=["sqlite", "postgres"])
def motor(request, tmp_path, monkeypatch):
    """La base de la prueba, en cada motor, con un usuario, una caja de la sucursal 3 y la tabla hija del producto."""
    monkeypatch.delenv(nodo_offline.VARIABLE_NODO, raising=False)
    if request.param == "sqlite":
        core.configure(db_path=str(tmp_path / "caja.db"))
    else:
        core.configure(_url_pg())
    conn = core.get_connection()
    if request.param == "postgres":
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    init_core_schema(conn)
    cd.crear_tablas(conn)
    conn.execute(_HIJA)
    conn.execute("INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (7,'cajero','Cajero','x','admin')")
    conn.commit()
    conn.close()
    yield request.param
    core._db_path = None
    core._database_url = None


@pytest.fixture
def caja_id(motor):
    return db_caja.create_caja_config("Mostrador", "", ["efectivo"], sucursal_id=3)


def _uno(sql: str, params=()):
    conn = core.get_connection()
    try:
        fila = conn.execute(sql, params).fetchone()
        return None if fila is None else fila[0]
    finally:
        conn.close()


def _contar(tabla: str, donde: str = "1=1") -> int:
    return _uno(f"SELECT COUNT(*) FROM {tabla} WHERE {donde}")


def _texto_del_monto(mov_id: int) -> str:
    return _uno("SELECT CAST(monto AS TEXT) FROM caja_movimientos WHERE id=?", (mov_id,))


def _fila(mov_id: int) -> dict:
    conn = core.get_connection()
    try:
        return dict(conn.execute("SELECT * FROM caja_movimientos WHERE id=?", (mov_id,)).fetchone())
    finally:
        conn.close()


class Recorder:
    """Un gancho de prueba que guarda los `datos` que recibió."""

    def __init__(self, levantar: bool = False):
        self.datos: list[dict] = []
        self.levantar = levantar

    def __call__(self, conn, datos):
        self.datos.append(datos)
        if self.levantar:
            raise RuntimeError("el outbox falló")

    @property
    def ganchos(self) -> GanchosDeCaja:
        return GanchosDeCaja(al_registrar_movimiento=self, al_anular_movimiento=self)


# ═══ 3. Decimal ═══════════════════════════════════════════════════════════


def test_un_decimal_se_guarda_exacto(motor, caja_id):
    a = db_caja.create_caja_movimiento(DIA, "ingreso", "Uno", Decimal("123456789012.34"), caja_id=caja_id)
    b = db_caja.create_caja_movimiento(DIA, "ingreso", "Dos", Decimal("0.10"), caja_id=caja_id)
    c = db_caja.create_caja_movimiento(DIA, "ingreso", "Tres", Decimal("0.20"), caja_id=caja_id)

    # Se lee como siempre: float, igual en los dos motores.
    assert _uno("SELECT monto FROM caja_movimientos WHERE id=?", (a,)) == 123456789012.34
    assert _fila(b)["monto"] == 0.1 and _fila(c)["monto"] == 0.2
    assert _texto_del_monto(a) == "123456789012.34"
    if motor == "postgres":
        # `NUMERIC` conserva el Decimal tal cual, escala incluida; con `float(monto)` quedaba `0.1` y `0.2`.
        assert (_texto_del_monto(b), _texto_del_monto(c)) == ("0.10", "0.20")
        assert _uno("SELECT CAST(SUM(monto) AS TEXT) FROM caja_movimientos WHERE id IN (?, ?)", (b, c)) == "0.30"
    else:
        # `REAL` no guarda decimales: el `float` más cercano, y la suma a dos decimales da lo que debe.
        assert _uno("SELECT ROUND(SUM(monto), 2) FROM caja_movimientos WHERE id IN (?, ?)", (b, c)) == 0.3


def test_un_decimal_de_mas_de_quince_cifras_no_pierde_nada_en_postgres(motor, caja_id):
    """`float` da 15 cifras significativas; `NUMERIC` guarda las que le lleguen (es lo que ADR-024 prometía)."""
    if motor != "postgres":
        pytest.skip("la columna de SQLite es REAL: no guarda más que un float")
    mov = db_caja.create_caja_movimiento(DIA, "ingreso", "Grande", Decimal("12345678901234.5678"), caja_id=caja_id)
    assert _texto_del_monto(mov) == "12345678901234.5678"


def test_int_y_float_se_guardan_como_siempre(motor, caja_id):
    entero = db_caja.create_caja_movimiento(DIA, "ingreso", "Entero", 100, caja_id=caja_id)
    flotante = db_caja.create_caja_movimiento(DIA, "ingreso", "Float", 0.1, caja_id=caja_id)
    assert _fila(entero)["monto"] == 100.0 and isinstance(_fila(entero)["monto"], float)
    assert _fila(flotante)["monto"] == 0.1
    # El texto es el de `float(monto)`: sin escala de más.
    assert _texto_del_monto(flotante) == "0.1"
    assert _texto_del_monto(entero) in ("100", "100.0")


@pytest.mark.parametrize("raro", [Decimal("NaN"), Decimal("Infinity")])
def test_un_decimal_que_no_es_finito_se_rechaza_y_no_deja_nada(motor, caja_id, raro):
    with pytest.raises(ValueError, match="finito"):
        db_caja.create_caja_movimiento(DIA, "ingreso", "Raro", raro, caja_id=caja_id)
    assert _contar("caja_movimientos") == 0


# ═══ 2a. `extra` en el gancho del nodo ════════════════════════════════════


def test_el_extra_viaja_en_los_datos_del_gancho_de_alta_y_de_anulacion(motor, caja_id):
    reg = Recorder()
    extra = {"tercero_id": 12, "rol": "cliente", "descripcion": "Flete a Rosario", "recibo": "0001-00000042"}
    mov = db_caja.create_caja_movimiento(
        DIA, "ingreso", "Cobranza", 1500, usuario_id=7, caja_id=caja_id, ganchos=reg.ganchos, extra=extra)
    assert reg.datos[0]["extra"] == extra
    assert reg.datos[0]["movimiento_id"] == mov

    db_caja.anular_caja_movimiento(mov, ganchos=reg.ganchos, extra={"motivo": "duplicado"})
    assert reg.datos[1]["extra"] == {"motivo": "duplicado"}
    assert reg.datos[1]["anulado"] == 1


def test_sin_extra_los_datos_son_identicos_a_los_de_antes(motor, caja_id):
    reg = Recorder()
    mov = db_caja.create_caja_movimiento(DIA, "egreso", "Retiro", 200, usuario_id=7, caja_id=caja_id,
                                         ganchos=reg.ganchos)
    db_caja.anular_caja_movimiento(mov, ganchos=reg.ganchos)
    esperado = {"version", "movimiento_id", "fecha", "tipo", "concepto", "monto", "referencia", "factura_id",
                "usuario_id", "caja_id", "sucursal_id", "medio_pago", "turno_id", "cc_pago_id", "anulado"}
    assert all(set(d) == esperado for d in reg.datos)
    assert VERSION == 1  # aditivo: un central anterior ignora la clave


def test_un_extra_vacio_no_viaja(motor, caja_id):
    reg = Recorder()
    db_caja.create_caja_movimiento(DIA, "egreso", "Retiro", 200, caja_id=caja_id, ganchos=reg.ganchos, extra={})
    assert "extra" not in reg.datos[0]


@pytest.mark.parametrize("malo", [{"importe": Decimal("1.5")}, {1: "x"}, ["no", "es", "mapeo"], {"f": float("nan")}])
def test_un_extra_que_no_es_json_se_rechaza_antes_de_escribir(motor, caja_id, malo):
    reg = Recorder()
    with pytest.raises(ValueError, match="extra"):
        db_caja.create_caja_movimiento(DIA, "ingreso", "X", 10, caja_id=caja_id, ganchos=reg.ganchos, extra=malo)
    assert _contar("caja_movimientos") == 0 and reg.datos == []


def test_el_extra_sin_ganchos_no_cambia_nada(motor, caja_id):
    mov = db_caja.create_caja_movimiento(DIA, "ingreso", "X", 10, caja_id=caja_id, extra={"a": 1})
    assert _fila(mov)["concepto"] == "X"


def test_si_el_gancho_con_extra_levanta_el_movimiento_no_queda(motor, caja_id):
    reg = Recorder(levantar=True)
    with pytest.raises(RuntimeError, match="outbox"):
        db_caja.create_caja_movimiento(DIA, "ingreso", "X", 10, caja_id=caja_id, ganchos=reg.ganchos,
                                       extra={"tercero_id": 1})
    assert _contar("caja_movimientos") == 0


# ═══ 2b. El gancho del central crea la hija en la misma transacción ═══════


def _datos_del_nodo(caja_id, **extra) -> dict:
    """Lo que subiría el nodo: los `datos` que arma el gancho, con los ids del nodo (acá, id 1)."""
    reg = Recorder()
    db_caja.create_caja_movimiento(
        DIA, "ingreso", "Cobranza", Decimal("1500.50"), usuario_id=7, caja_id=caja_id, medio_pago="efectivo",
        ganchos=reg.ganchos, extra=extra or None)
    return reg.datos[0]


def _hija_en(conn, mov_id, datos):
    extra = datos.get("extra") or {}
    conn.execute("INSERT INTO movimientos_hija (movimiento_id, tercero, rol) VALUES (?, ?, ?)",
                 (mov_id, extra.get("tercero"), extra.get("rol")))


def test_el_central_crea_la_fila_del_motor_y_la_hija_con_el_mismo_id(motor, caja_id):
    datos = _datos_del_nodo(caja_id, tercero="Pereiro", rol="cliente")
    vistas = []

    def al_aplicar(conn, mov_id, d):
        _hija_en(conn, mov_id, d)
        # Adentro de la transacción: una conexión ajena todavía no ve ni la fila ni la hija.
        vistas.append((_contar("caja_movimientos", f"origen='{NODO}'"), _contar("movimientos_hija")))

    conn = core.get_connection()
    try:
        mov_id = caja_del_nodo.aplicar_movimiento_del_nodo(conn, NODO, datos, al_aplicar=al_aplicar)
        conn.commit()
    finally:
        conn.close()

    assert vistas == [(0, 0)]
    fila = _fila(mov_id)
    assert (fila["origen"], fila["origen_id"]) == (NODO, datos["movimiento_id"])
    assert mov_id != datos["movimiento_id"]  # el id del central no es el del nodo
    assert _uno("SELECT tercero FROM movimientos_hija WHERE movimiento_id=?", (mov_id,)) == "Pereiro"
    assert fila["monto"] == 1500.5 and fila["factura_id"] is None and fila["cc_pago_id"] is None


def test_si_el_gancho_del_central_levanta_no_queda_ni_el_movimiento_ni_la_hija(motor, caja_id):
    datos = _datos_del_nodo(caja_id, tercero="Pereiro")
    antes = _contar("caja_movimientos")

    def al_aplicar(conn, mov_id, d):
        _hija_en(conn, mov_id, d)
        raise RuntimeError("la cuenta corriente falló")

    conn = core.get_connection()
    try:
        with pytest.raises(RuntimeError, match="cuenta corriente"):
            caja_del_nodo.aplicar_movimiento_del_nodo(conn, NODO, datos, al_aplicar=al_aplicar)
        conn.rollback()
    finally:
        conn.close()

    assert _contar("caja_movimientos") == antes and _contar("movimientos_hija") == 0
    # Y reintentar la aplica entera.
    conn = core.get_connection()
    try:
        mov_id = caja_del_nodo.aplicar_movimiento_del_nodo(conn, NODO, datos, al_aplicar=_hija_en)
        conn.commit()
    finally:
        conn.close()
    assert _contar("movimientos_hija", f"movimiento_id={mov_id}") == 1


def test_aplicar_dos_veces_es_una_fila_y_el_gancho_corre_una_vez(motor, caja_id):
    datos = _datos_del_nodo(caja_id, tercero="Pereiro")
    llamadas = []
    conn = core.get_connection()
    try:
        primero = caja_del_nodo.aplicar_movimiento_del_nodo(
            conn, NODO, datos, al_aplicar=lambda c, m, d: llamadas.append(m))
        segundo = caja_del_nodo.aplicar_movimiento_del_nodo(
            conn, NODO, datos, al_aplicar=lambda c, m, d: llamadas.append(m))
        conn.commit()
    finally:
        conn.close()
    assert primero == segundo and llamadas == [primero]
    assert _contar("caja_movimientos", f"origen='{NODO}'") == 1


def test_sin_gancho_del_central_se_comporta_como_hoy(motor, caja_id):
    """El central crea la fila del motor sin hija: lo de siempre."""
    datos = _datos_del_nodo(caja_id)
    conn = core.get_connection()
    try:
        mov_id = caja_del_nodo.aplicar_movimiento_del_nodo(conn, NODO, datos)
        conn.commit()
    finally:
        conn.close()
    assert _fila(mov_id)["origen"] == NODO and _contar("movimientos_hija") == 0


def test_la_anulacion_del_nodo_llama_al_gancho_con_su_extra_una_sola_vez(motor, caja_id):
    datos = _datos_del_nodo(caja_id, tercero="Pereiro")
    reg = Recorder()
    mov = db_caja.create_caja_movimiento(DIA, "ingreso", "Otro", 5, caja_id=caja_id)
    db_caja.anular_caja_movimiento(mov, ganchos=reg.ganchos, extra={"motivo": "duplicado"})
    anulacion = reg.datos[0]
    vistas = []

    conn = core.get_connection()
    try:
        mov_id = caja_del_nodo.aplicar_movimiento_del_nodo(conn, NODO, datos)
        # Una anulación con otro id de nodo: el movimiento no subió.
        with pytest.raises(caja_del_nodo.MovimientoNoSincronizado):
            caja_del_nodo.aplicar_anulacion_del_nodo(conn, NODO, anulacion)
        anulacion_del_primero = {**anulacion, "movimiento_id": datos["movimiento_id"]}
        assert caja_del_nodo.aplicar_anulacion_del_nodo(
            conn, NODO, anulacion_del_primero, al_aplicar=lambda c, m, d: vistas.append((m, d["extra"]))) is True
        assert caja_del_nodo.aplicar_anulacion_del_nodo(
            conn, NODO, anulacion_del_primero, al_aplicar=lambda c, m, d: vistas.append("de nuevo")) is False
        conn.commit()
    finally:
        conn.close()
    assert vistas == [(mov_id, {"motivo": "duplicado"})]
    assert _fila(mov_id)["anulado"] == 1


def test_lo_que_el_central_no_puede_aplicar_es_un_value_error(motor, caja_id):
    datos = _datos_del_nodo(caja_id)
    conn = core.get_connection()
    try:
        with pytest.raises(caja_del_nodo.TurnoNoSincronizado):
            caja_del_nodo.aplicar_movimiento_del_nodo(conn, NODO, {**datos, "turno_id": 99})
        with pytest.raises(caja_del_nodo.CajaNoSincronizada):
            caja_del_nodo.aplicar_movimiento_del_nodo(conn, NODO, {**datos, "caja_id": 999})
        with pytest.raises(caja_del_nodo.CajaDelNodoError, match="versión"):
            caja_del_nodo.aplicar_movimiento_del_nodo(conn, NODO, {**datos, "version": 2})
        assert issubclass(caja_del_nodo.CajaRechazadaPorElCentral, ValueError)
    finally:
        conn.close()


def test_la_referencia_que_el_central_ya_tiene_se_rechaza_en_vez_de_perder_el_movimiento(motor, caja_id):
    reg = Recorder()
    db_caja.create_caja_movimiento(DIA, "ingreso", "Mismo", 5, referencia="R-1", caja_id=caja_id)
    db_caja.create_caja_movimiento(DIA, "ingreso", "Del nodo", 5, referencia="R-2", caja_id=caja_id, ganchos=reg.ganchos)
    datos = {**reg.datos[0], "movimiento_id": 500, "referencia": "R-1"}
    conn = core.get_connection()
    try:
        with pytest.raises(caja_del_nodo.CajaRechazadaPorElCentral, match="R-1"):
            caja_del_nodo.aplicar_movimiento_del_nodo(conn, NODO, datos, al_aplicar=_hija_en)
    finally:
        conn.rollback()
        conn.close()
    assert _contar("movimientos_hija") == 0


# ═══ 1. Corregir un movimiento ═════════════════════════════════════════════


@pytest.fixture
def movimiento(motor, caja_id):
    return db_caja.create_caja_movimiento(DIA, "ingreso", "Cobranza", 1000, usuario_id=7, caja_id=caja_id,
                                          medio_pago="efectivo")


def test_corregir_en_el_central_cambia_lo_pedido_y_deja_el_resto(motor, movimiento, monkeypatch):
    libro = []
    monkeypatch.setattr(db_caja, "al_libro_de_clientes", lambda conn, movs: libro.append(list(movs)))
    fila = db_caja.corregir_caja_movimiento(
        movimiento, fecha="2026-10-21", concepto="  Cobranza corregida ", monto=Decimal("1250.75"),
        medio_pago="transferencia")
    assert (fila["fecha"], fila["concepto"], fila["monto"], fila["medio_pago"], fila["tipo"]) == (
        "2026-10-21", "Cobranza corregida", 1250.75, "transferencia", "ingreso")
    assert fila["usuario_id"] == 7 and fila["anulado"] == 0 and fila["origen"] is None
    assert _fila(movimiento) == fila
    assert libro == [[movimiento]]  # el libro de clientes se entera


def test_corregir_con_conn_es_de_quien_llama(motor, movimiento):
    conn = core.get_connection()
    try:
        db_caja.corregir_caja_movimiento(movimiento, concepto="Cambiado", conn=conn)
        conn.rollback()
    finally:
        conn.close()
    assert _fila(movimiento)["concepto"] == "Cobranza"


@pytest.mark.parametrize("cambio, mensaje", [
    ({"concepto": "   "}, "El concepto es obligatorio."),
    ({"tipo": "retiro"}, "Tipo inválido."),
    ({"monto": 0}, "El monto debe ser mayor a cero."),
    ({"monto": Decimal("-5")}, "El monto debe ser mayor a cero."),
    ({"monto": float("nan")}, "El monto debe ser mayor a cero."),
])
def test_corregir_valida_lo_mismo_que_el_alta(motor, movimiento, cambio, mensaje):
    with pytest.raises(db_caja.MovimientoInvalido) as exc:
        db_caja.corregir_caja_movimiento(movimiento, **cambio)
    assert str(exc.value) == mensaje
    assert _fila(movimiento)["monto"] == 1000.0 and _fila(movimiento)["concepto"] == "Cobranza"


def test_el_alta_del_router_dice_lo_mismo_que_la_correccion(motor, caja_id):
    app = FastAPI()
    app.include_router(build_caja_router(usuario_actual=lambda: {"id": 7}))
    cliente = TestClient(app)
    base = {"fecha": DIA, "tipo": "ingreso", "concepto": "X", "monto": 10}
    for cambio, mensaje in [({"concepto": " "}, "El concepto es obligatorio."), ({"tipo": "otro"}, "Tipo inválido."),
                            ({"monto": 0}, "El monto debe ser mayor a cero.")]:
        r = cliente.post("/api/caja", json={**base, **cambio})
        assert (r.status_code, r.json()["detail"]) == (422, mensaje)
    assert cliente.post("/api/caja", json=base).status_code == 200


def test_un_movimiento_que_no_existe_o_esta_anulado(motor, movimiento):
    with pytest.raises(db_caja.MovimientoInexistente):
        db_caja.corregir_caja_movimiento(9999, concepto="X")
    db_caja.anular_caja_movimiento(movimiento)
    with pytest.raises(db_caja.MovimientoAnulado) as exc:
        db_caja.corregir_caja_movimiento(movimiento, concepto="X")
    assert exc.value.detalle()["codigo"] == "movimiento_anulado"


def test_un_movimiento_que_subio_de_un_nodo_no_se_corrige_en_el_central(motor, caja_id):
    datos = _datos_del_nodo(caja_id)
    conn = core.get_connection()
    try:
        mov_id = caja_del_nodo.aplicar_movimiento_del_nodo(conn, NODO, datos)
        conn.commit()
    finally:
        conn.close()
    antes = _fila(mov_id)
    with pytest.raises(db_caja.MovimientoDeUnNodo) as exc:
        db_caja.corregir_caja_movimiento(mov_id, concepto="Editado en el central")
    assert exc.value.detalle()["codigo"] == "movimiento_del_nodo"
    assert _fila(mov_id) == antes


def test_en_un_nodo_no_se_corrige_se_anula_y_se_carga_de_nuevo(motor, movimiento, monkeypatch):
    monkeypatch.setenv(nodo_offline.VARIABLE_NODO, NODO)
    with pytest.raises(nodo_offline.SoloEnElCentral) as exc:
        db_caja.corregir_caja_movimiento(movimiento, concepto="Cambiado")
    assert exc.value.detalle()["codigo"] == "solo_en_el_central"
    assert "anulalo" in exc.value.detalle()["mensaje"]
    assert _fila(movimiento)["concepto"] == "Cobranza"
    # Un producto que lee su propia variable inyecta la lectura, como en `guarda_solo_en_el_central`.
    monkeypatch.delenv(nodo_offline.VARIABLE_NODO)
    with pytest.raises(nodo_offline.SoloEnElCentral):
        db_caja.corregir_caja_movimiento(movimiento, concepto="Cambiado", nodo=lambda: "otro-nodo")


def test_la_api_contesta_409_en_un_nodo_y_con_un_movimiento_del_nodo(motor, caja_id, movimiento, monkeypatch):
    """Cómo un producto traduce las excepciones: todas llevan `detalle()` (`{"codigo", "mensaje"}`)."""
    app = FastAPI()

    @app.put("/caja/{mov_id}")
    def corregir(mov_id: int, cuerpo: dict):
        try:
            return db_caja.corregir_caja_movimiento(mov_id, **cuerpo)
        except (nodo_offline.SoloEnElCentral, db_caja.CorreccionNoPermitida) as exc:
            raise HTTPException(409, exc.detalle()) from exc
        except db_caja.MovimientoInexistente as exc:
            raise HTTPException(404, str(exc)) from exc
        except db_caja.MovimientoInvalido as exc:
            raise HTTPException(422, str(exc)) from exc

    cliente = TestClient(app)
    assert cliente.put(f"/caja/{movimiento}", json={"concepto": "Nuevo"}).status_code == 200
    assert cliente.put(f"/caja/{movimiento}", json={"monto": 0}).status_code == 422
    assert cliente.put("/caja/9999", json={"concepto": "Nuevo"}).status_code == 404

    del_nodo = db_caja.create_caja_movimiento(DIA, "ingreso", "Del nodo", 5, caja_id=caja_id, origen=NODO, origen_id=3)
    r = cliente.put(f"/caja/{del_nodo}", json={"concepto": "Nuevo"})
    assert (r.status_code, r.json()["detail"]["codigo"]) == (409, "movimiento_del_nodo")

    monkeypatch.setenv(nodo_offline.VARIABLE_NODO, NODO)
    r = cliente.put(f"/caja/{movimiento}", json={"concepto": "Otra vez"})
    assert (r.status_code, r.json()["detail"]["codigo"]) == (409, "solo_en_el_central")


def _factura(total, numero=1) -> int:
    conn = core.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, items, subtotal, "
            "iva_amount, total) VALUES (11, 1, ?, ?, '20111111112', 'X', '[]', ?, 0, ?)", (numero, DIA, total, total))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_un_cobro_de_factura_no_se_corrige_para_cobrar_de_mas(motor, caja_id):
    factura = _factura(1000)
    primero = db_caja.create_caja_movimiento(DIA, "ingreso", "Cobro 1", 600, factura_id=factura, caja_id=caja_id,
                                             medio_pago="efectivo")
    db_caja.create_caja_movimiento(DIA, "ingreso", "Cobro 2", 300, factura_id=factura, caja_id=caja_id,
                                   medio_pago="efectivo")
    # Falta 100: el primero puede subir hasta 700 (su propio monto no cuenta contra el tope) y no más.
    with pytest.raises(CobroNoPermitido):
        db_caja.corregir_caja_movimiento(primero, monto=700.01)
    assert _fila(primero)["monto"] == 600.0
    assert db_caja.corregir_caja_movimiento(primero, monto=700)["monto"] == 700.0
    # Bajarlo siempre se puede, y volver a subir hasta el tope también.
    assert db_caja.corregir_caja_movimiento(primero, monto=Decimal("100.50"))["monto"] == 100.5
    assert db_caja.corregir_caja_movimiento(primero, monto=700)["monto"] == 700.0


def test_el_tope_de_cobro_no_se_aplica_a_la_marca_de_cuenta_corriente(motor, caja_id):
    factura = _factura(1000, numero=2)
    marca = db_caja.create_caja_movimiento(DIA, "ingreso", "A crédito", 1000, factura_id=factura, caja_id=caja_id,
                                           medio_pago="cuenta_corriente")
    assert db_caja.corregir_caja_movimiento(marca, monto=1500)["monto"] == 1500.0


def test_corregir_no_tiene_gancho_de_modificacion():
    """La decisión del ADR-080: ni `GanchosDeCaja` ni la función conocen una modificación."""
    import inspect

    assert "al_modificar_movimiento" not in GanchosDeCaja.__dataclass_fields__
    assert "ganchos" not in inspect.signature(db_caja.corregir_caja_movimiento).parameters


def test_el_turno_de_un_movimiento_corregido_queda_donde_estaba(motor, caja_id):
    tid = db_turnos.create_turno(7, 0, caja_id=caja_id, apertura=f"{DIA} 09:00:00")
    mov = db_caja.create_caja_movimiento(DIA, "ingreso", "Venta", 100, turno_id=tid, usuario_id=7)
    fila = db_caja.corregir_caja_movimiento(mov, monto=150)
    assert (fila["turno_id"], fila["caja_id"], fila["usuario_id"]) == (tid, caja_id, 7)


def test_corregir_otra_cosa_no_reescribe_el_monto(motor, caja_id):
    """El monto que no se toca queda como estaba: pasarlo por `float` (así lo lee el motor) perdería lo exacto."""
    if motor != "postgres":
        pytest.skip("sólo PostgreSQL guarda un monto más exacto que un float")
    mov = db_caja.create_caja_movimiento(DIA, "ingreso", "Grande", Decimal("12345678901234.5678"), caja_id=caja_id)
    db_caja.corregir_caja_movimiento(mov, concepto="Grande, corregido")
    assert _texto_del_monto(mov) == "12345678901234.5678"
    db_caja.corregir_caja_movimiento(mov, monto=Decimal("0.10"))
    assert _texto_del_monto(mov) == "0.10"
