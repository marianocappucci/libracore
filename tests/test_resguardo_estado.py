"""El estado de la copia externa, que la pantalla lee.

Lo que fija: que la pantalla pueda distinguir los cuatro "no", que desde afuera
se ven igual y significan cosas muy distintas.

1. **No contratado** — no hay archivo. Para la pantalla es "no tenés el add-on",
   no una alarma. Mostrarle una alarma a quien no lo contrató es ruido.
2. **La última subida falló** — aunque sea de hace un minuto.
3. **La última subida anduvo pero es vieja** — el caso silencioso: todo se ve
   bien y hace días que no sube.
4. **El archivo está roto**.
5. **La última subida anduvo, pero sin cifrar** — desde el 2026-09-17 no es
   "al día": la clave privada de ARCA del cliente quedó legible en su nube.
"""
from datetime import datetime, timedelta

from libracore.resguardo_estado import escribir_estado, esta_al_dia, leer_estado, resumen

AHORA = datetime(2026, 8, 12, 16, 0, 0)


def test_sin_archivo_es_no_contratado_y_no_una_alarma(tmp_path):
    r = resumen(tmp_path, ahora=AHORA)

    assert r["contratado"] is False
    assert r["al_dia"] is None, "None y no False: no hay nada que alarmar"


def test_una_copia_fresca(tmp_path):
    escribir_estado(tmp_path, {
        "ok": True, "cifrado": True,
        "cuando": (AHORA - timedelta(hours=6)).isoformat(timespec="seconds"),
        "archivo": "backup_automatico_20260812_040000.zip",
        "destino": "drive_cliente:libra/cliente",
        "bytes": 3_800_000,
        "en_destino": 10,
    })

    r = resumen(tmp_path, ahora=AHORA)

    assert r["contratado"] is True
    assert r["al_dia"] is True
    assert r["detalle"]["destino"] == "drive_cliente:libra/cliente"
    assert r["detalle"]["en_destino"] == 10
    assert r["detalle"]["cifrado"] is True


def test_una_copia_fresca_que_subio_sin_cifrar_no_esta_al_dia(tmp_path):
    """🔴 El estado que dejaba el subidor hasta el 2026-09-17: ok, fresco, y con
    la clave privada de ARCA legible en la nube del cliente."""
    escribir_estado(tmp_path, {
        "ok": True,
        "cuando": (AHORA - timedelta(minutes=5)).isoformat(timespec="seconds"),
    })

    al_dia, motivo = esta_al_dia(tmp_path, ahora=AHORA)
    r = resumen(tmp_path, ahora=AHORA)

    assert al_dia is False
    assert "sin cifrar" in motivo
    assert r["detalle"]["cifrado"] is False


def test_cifrado_tiene_que_ser_true_no_solo_verdadero(tmp_path):
    """Un `"cifrado": "no"` es un string no vacío: con `if datos.get(...)` pasaría."""
    escribir_estado(tmp_path, {
        "ok": True, "cifrado": "no",
        "cuando": (AHORA - timedelta(minutes=5)).isoformat(timespec="seconds"),
    })

    al_dia, _ = esta_al_dia(tmp_path, ahora=AHORA)

    assert al_dia is False


def test_una_subida_fallida_reciente_no_esta_al_dia(tmp_path):
    """El caso que importa: el cliente revocó el acceso hace una hora."""
    escribir_estado(tmp_path, {
        "ok": False, "cuando": AHORA.isoformat(timespec="seconds"),
        "error": "rclone copy termino con codigo 1: token expired",
    })

    r = resumen(tmp_path, ahora=AHORA)

    assert r["contratado"] is True
    assert r["al_dia"] is False
    assert "token expired" in r["motivo"]


def test_una_copia_vieja_no_esta_al_dia(tmp_path):
    """El caso silencioso: la última subida anduvo, pero fue hace cuatro días."""
    escribir_estado(tmp_path, {
        "ok": True, "cifrado": True,
        "cuando": (AHORA - timedelta(days=4)).isoformat(timespec="seconds"),
    })

    al_dia, motivo = esta_al_dia(tmp_path, ahora=AHORA)

    assert al_dia is False
    assert "hace mas de 36 horas" in motivo


def test_el_limite_de_frescura_deja_pasar_una_noche_corrida(tmp_path):
    """36 y no 24: un backup que se corre unas horas no es una alarma."""
    escribir_estado(tmp_path, {
        "ok": True, "cifrado": True,
        "cuando": (AHORA - timedelta(hours=30)).isoformat(timespec="seconds"),
    })

    al_dia, _ = esta_al_dia(tmp_path, ahora=AHORA)

    assert al_dia is True


def test_un_archivo_roto_no_se_lee_como_al_dia(tmp_path):
    (tmp_path / ".externo.json").write_text("{esto no es json", encoding="utf-8")

    assert leer_estado(tmp_path) is None
    al_dia, motivo = esta_al_dia(tmp_path, ahora=AHORA)
    assert al_dia is False
    assert "ilegible" in motivo


def test_sin_fecha_valida_no_esta_al_dia(tmp_path):
    escribir_estado(tmp_path, {"ok": True, "cifrado": True})

    al_dia, motivo = esta_al_dia(tmp_path, ahora=AHORA)

    assert al_dia is False
    assert "sin fecha valida" in motivo
