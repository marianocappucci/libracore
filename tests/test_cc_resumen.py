"""
Resumen automático de cuenta corriente: cálculo del período, regla de
frecuencia por cliente e idempotencia del envío masivo.

El envío real de mail y la generación del PDF se sustituyen por dobles: lo
que interesa acá es a quién le toca, qué período se informa y que una segunda
corrida el mismo día no reenvíe (ver libracore/cc_resumen.py).
"""
import datetime

import pytest

from libracore import cc_resumen, config_manager
from libracore.db import clients, core, cuenta_corriente

HOY = datetime.date(2026, 8, 3)  # lunes


@pytest.fixture
def conn(tmp_path, monkeypatch, crear_schema):
    core.configure(db_path=str(tmp_path / "cc_resumen_test.db"))
    c = core.get_connection()
    crear_schema(c)
    c.commit()
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(tmp_path / "config.json"))
    yield c
    c.close()
    core._db_path = None


@pytest.fixture
def enviados(monkeypatch, tmp_path):
    """Intercepta PDF y SMTP; devuelve la lista de mails "enviados"."""
    sent = []

    def _fake_pdf(cliente, periodo, output_dir=None):
        path = tmp_path / f"resumen_{cliente['id']}.pdf"
        path.write_bytes(b"%PDF-1.4 fake")
        return str(path)

    def _fake_send(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(cc_resumen.pdf_generator, "generate_pdf_resumen_cc", _fake_pdf)
    monkeypatch.setattr(cc_resumen.email_sender, "enviar_documento", _fake_send)
    return sent


def _cfg(**over):
    base = {
        "cc_resumen_habilitado": "1",
        "cc_resumen_dia_mes": "1",
        "cc_resumen_dia_semana": "1",
        "cc_resumen_solo_con_saldo": "1",
        "email_smtp_host": "smtp.test",
        "email_smtp_user": "user@test",
        "email_smtp_password": "x",
        "empresa_nombre": "Empresa Test",
    }
    base.update(over)
    config_manager.save(base)
    return config_manager.load()


_venta_seq = 0


def _cliente_con_deuda(monto=1000.0, fecha="2026-07-20", nombre="Cliente CC", **campos):
    """Cliente con una venta cobrada a cuenta corriente (el débito más común)."""
    global _venta_seq
    _venta_seq += 1
    numero = f"V-{_venta_seq}"
    campos.setdefault("email", "cliente@test.com")
    cid = clients.create_client(nombre, **campos)
    with core.get_connection() as c:
        cur = c.execute(
            "INSERT INTO ventas (numero, fecha, cliente_id, items, total) VALUES (?,?,?,?,?)",
            (numero, fecha, cid, "[]", monto),
        )
        c.execute("INSERT INTO ventas_pagos (venta_id, medio, monto) VALUES (?,?,?)",
                  (cur.lastrowid, "cuenta_corriente", monto))
    return cid


# ── Regla de frecuencia ──────────────────────────────────────────────────────

def test_no_envia_si_el_toggle_del_cliente_esta_apagado(conn):
    cfg = _cfg()
    cliente = {"cc_resumen_auto": 0, "cc_resumen_frecuencia": "mensual",
               "cc_resumen_ultimo_envio": ""}
    assert cc_resumen.corresponde_enviar(cliente, HOY, cfg) is False


def test_mensual_envia_una_sola_vez_por_mes(conn):
    cfg = _cfg(cc_resumen_dia_mes="1")
    cliente = {"cc_resumen_auto": 1, "cc_resumen_frecuencia": "mensual",
               "cc_resumen_ultimo_envio": ""}
    assert cc_resumen.corresponde_enviar(cliente, HOY, cfg) is True

    cliente["cc_resumen_ultimo_envio"] = "2026-08-01"
    assert cc_resumen.corresponde_enviar(cliente, HOY, cfg) is False


def test_mensual_recupera_el_envio_si_se_perdio_el_dia_del_corte(conn):
    """Si el cron no corrió el día 1, el resumen igual sale el 3 — el ancla es
    el corte del mes, no la fecha exacta de hoy."""
    cfg = _cfg(cc_resumen_dia_mes="1")
    cliente = {"cc_resumen_auto": 1, "cc_resumen_frecuencia": "mensual",
               "cc_resumen_ultimo_envio": "2026-07-01"}
    assert cc_resumen.corresponde_enviar(cliente, HOY, cfg) is True


def test_semanal_espera_al_dia_de_la_semana_configurado(conn):
    cfg = _cfg(cc_resumen_dia_semana="1")  # lunes
    cliente = {"cc_resumen_auto": 1, "cc_resumen_frecuencia": "semanal",
               "cc_resumen_ultimo_envio": "2026-07-27"}  # lunes anterior
    assert cc_resumen.corresponde_enviar(cliente, HOY, cfg) is True
    # Martes: el ancla sigue siendo el lunes, que ya se envió
    cliente["cc_resumen_ultimo_envio"] = HOY.isoformat()
    assert cc_resumen.corresponde_enviar(cliente, datetime.date(2026, 8, 4), cfg) is False


def test_quincenal_saltea_una_semana(conn):
    cfg = _cfg(cc_resumen_dia_semana="1")
    cliente = {"cc_resumen_auto": 1, "cc_resumen_frecuencia": "quincenal",
               "cc_resumen_ultimo_envio": "2026-07-27"}  # hace 7 días
    assert cc_resumen.corresponde_enviar(cliente, HOY, cfg) is False
    cliente["cc_resumen_ultimo_envio"] = "2026-07-20"  # hace 14 días
    assert cc_resumen.corresponde_enviar(cliente, HOY, cfg) is True


# ── Período informado ────────────────────────────────────────────────────────

def test_periodo_arranca_el_dia_siguiente_al_ultimo_envio(conn):
    _cfg()
    cid = _cliente_con_deuda()
    clients.update_client(cid, cc_resumen_auto=1, cc_resumen_frecuencia="mensual")
    cuenta_corriente.registrar_resumen_enviado(
        cid, "2026-07-15", "2026-07-01", "2026-07-15", 0, "cliente@test.com")

    periodo = cc_resumen.calcular_periodo(clients.get_client(cid), HOY)
    assert periodo["desde"] == "2026-07-16"
    assert periodo["hasta"] == HOY.isoformat()


def test_saldo_final_del_periodo_coincide_con_el_saldo_de_la_cuenta(conn):
    """El resumen que se manda por mail no puede dar distinto que la pantalla."""
    _cfg()
    cid = _cliente_con_deuda(monto=2500.0, fecha="2026-07-20")
    cuenta_corriente.create_cc_pago(cid, 500.0, "2026-07-25", "Pago", "", "efectivo", None, None)

    periodo = cc_resumen.calcular_periodo(clients.get_client(cid), HOY)
    assert periodo["saldo_final"] == pytest.approx(cuenta_corriente.get_cc_saldo(cid))
    assert periodo["saldo_final"] == pytest.approx(2000.0)


def test_movimientos_previos_al_rango_entran_como_saldo_anterior(conn):
    _cfg()
    cid = _cliente_con_deuda(monto=800.0, fecha="2026-06-10")
    periodo = cuenta_corriente.get_cc_movimientos_periodo(cid, "2026-07-01", "2026-07-31")
    assert periodo["saldo_anterior"] == pytest.approx(800.0)
    assert periodo["movimientos"] == []
    assert periodo["saldo_final"] == pytest.approx(800.0)


# ── Envío masivo ─────────────────────────────────────────────────────────────

def test_envio_masivo_es_idempotente_en_el_dia(conn, enviados):
    _cfg(cc_resumen_dia_mes="1")
    cid = _cliente_con_deuda()
    clients.update_client(cid, cc_resumen_auto=1, cc_resumen_frecuencia="mensual")

    r1 = cc_resumen.enviar_resumenes_pendientes(hoy=HOY)
    assert len(r1["enviados"]) == 1
    assert len(enviados) == 1

    r2 = cc_resumen.enviar_resumenes_pendientes(hoy=HOY)
    assert r2["enviados"] == []
    assert len(enviados) == 1
    assert clients.get_client(cid)["cc_resumen_ultimo_envio"] == HOY.isoformat()


def test_no_envia_si_la_llave_maestra_esta_apagada(conn, enviados):
    _cfg(cc_resumen_habilitado="0")
    cid = _cliente_con_deuda()
    clients.update_client(cid, cc_resumen_auto=1)

    r = cc_resumen.enviar_resumenes_pendientes(hoy=HOY)
    assert enviados == []
    assert r["omitidos"] == [{"motivo": "deshabilitado_en_config"}]


def test_omite_clientes_sin_saldo_y_sin_email(conn, enviados):
    _cfg(cc_resumen_dia_mes="1")
    sin_saldo = clients.create_client("Sin Saldo", email="a@test.com")
    clients.update_client(sin_saldo, cc_resumen_auto=1)
    sin_email = _cliente_con_deuda(nombre="Sin Email")
    clients.update_client(sin_email, email="", cc_resumen_auto=1)

    r = cc_resumen.enviar_resumenes_pendientes(hoy=HOY)
    motivos = sorted(o["motivo"] for o in r["omitidos"])
    assert motivos == ["sin_email", "sin_saldo"]
    assert enviados == []


def test_dry_run_no_envia_ni_marca_la_base(conn, enviados):
    _cfg(cc_resumen_dia_mes="1")
    cid = _cliente_con_deuda()
    clients.update_client(cid, cc_resumen_auto=1)

    r = cc_resumen.enviar_resumenes_pendientes(hoy=HOY, dry_run=True)
    assert len(r["enviados"]) == 1
    assert enviados == []
    assert not clients.get_client(cid)["cc_resumen_ultimo_envio"]


def test_un_fallo_de_smtp_no_corta_el_resto_y_queda_registrado(conn, enviados, monkeypatch):
    _cfg(cc_resumen_dia_mes="1")
    cid = _cliente_con_deuda()
    clients.update_client(cid, cc_resumen_auto=1)

    def _boom(**kwargs):
        raise OSError("conexión rechazada")

    monkeypatch.setattr(cc_resumen.email_sender, "enviar_documento", _boom)
    r = cc_resumen.enviar_resumenes_pendientes(hoy=HOY)

    assert r["enviados"] == []
    assert r["errores"][0]["motivo"] == "error_envio"
    log = cuenta_corriente.get_resumenes_enviados(cid)
    assert log[0]["estado"] == "error"
    # Un envío fallido no adelanta el último envío: se reintenta mañana
    assert not clients.get_client(cid)["cc_resumen_ultimo_envio"]


def test_toggle_por_cliente(conn):
    cid = clients.create_client("Toggle", email="t@test.com")
    assert clients.get_client(cid)["cc_resumen_auto"] == 0
    assert clients.toggle_cc_resumen_auto(cid) is True
    assert clients.get_client(cid)["cc_resumen_auto"] == 1
    assert [c["id"] for c in clients.get_clients_cc_resumen_auto()] == [cid]
    assert clients.toggle_cc_resumen_auto(cid) is False


def test_frecuencia_invalida_se_rechaza(conn):
    cid = clients.create_client("Frecuencia", email="f@test.com")
    with pytest.raises(ValueError):
        clients.update_client(cid, cc_resumen_frecuencia="diaria")
