"""El resguardo externo: subir el backup a la nube del cliente.

Lo que fijan, en orden de lo que duele si falla:

1. 🔴 Que la **retención no borre de más**. Es lo único acá que destruye datos,
   y del lado del cliente. Nunca el más nuevo, nunca lo que no se puede fechar.
2. Que subir **verifique el producto**, no el código de salida de `rclone`: que
   el archivo esté en el destino y con el tamaño correcto.
3. Que el estado se escriba **también cuando falla** — una pantalla que no
   distingue "nunca se configuró" de "hace cuatro días que no sube" no sirve.
4. Que una instancia sin el add-on contratado se saltee sin ruido.
"""
import json
from datetime import datetime, timedelta

import pytest

from libracore.provisioning import resguardo_externo as rx


def _nombre(dt: datetime, motivo="automatico") -> str:
    return f"backup_{motivo}_{dt.strftime('%Y%m%d')}_{dt.strftime('%H%M%S')}.zip"


AHORA = datetime(2026, 8, 12, 4, 0, 0)


# ── la retención: lo único que borra ─────────────────────────────────────────

def test_conserva_los_siete_diarios():
    nombres = [_nombre(AHORA - timedelta(days=d)) for d in range(10)]

    borrar = rx.a_borrar(nombres, AHORA)

    conservados = [n for n in nombres if n not in borrar]
    for d in range(rx.GFS_DIARIOS):
        assert _nombre(AHORA - timedelta(days=d)) in conservados


def test_nunca_borra_el_mas_nuevo(monkeypatch):
    """La red de abajo: aunque las franjas quedaran en cero, la última copia se
    queda.

    🔴 **Este test hay que escribirlo con las constantes en 0.** Con los valores
    reales el más nuevo entra igual entre los 7 diarios, así que la guarda
    explícita nunca se ejercita: la primera versión de este test pasaba con la
    guarda **borrada**, por un off-by-one de `_primero_por` que devolvía un
    elemento con `cuantos=0`. Lo delató el arnés de falla forzada, no la suite.
    """
    monkeypatch.setattr(rx, "GFS_DIARIOS", 0)
    monkeypatch.setattr(rx, "GFS_SEMANALES", 0)
    monkeypatch.setattr(rx, "GFS_MENSUALES", 0)
    nombres = [_nombre(AHORA - timedelta(days=d)) for d in range(400)]

    borrar = rx.a_borrar(nombres, AHORA)

    assert _nombre(AHORA) not in borrar
    assert len(borrar) == 399, "con todas las franjas en 0 sólo sobrevive el más nuevo"


def test_con_las_franjas_reales_el_mas_nuevo_entra_por_los_diarios():
    """El control del de arriba: en operación normal no hace falta la guarda."""
    nombres = [_nombre(AHORA - timedelta(days=d)) for d in range(400)]

    assert _nombre(AHORA) not in rx.a_borrar(nombres, AHORA)


def test_no_borra_lo_que_no_puede_fechar():
    """Algo que subió una persona, o un formato futuro. Preferible pagar unos MB
    de más que borrar algo ajeno."""
    nombres = [
        _nombre(AHORA - timedelta(days=d)) for d in range(40)
    ] + ["exportacion-del-contador.zip", "backup_viejo.zip", "notas.txt"]

    borrar = rx.a_borrar(nombres, AHORA)

    assert "exportacion-del-contador.zip" not in borrar
    assert "backup_viejo.zip" not in borrar, "no matchea el patron: no se fecha, no se borra"
    assert "notas.txt" not in borrar


def test_conserva_uno_por_semana_y_uno_por_mes():
    """Un año de backups diarios tiene que dejar historia, no sólo la semana."""
    nombres = [_nombre(AHORA - timedelta(days=d)) for d in range(365)]

    borrar = rx.a_borrar(nombres, AHORA)
    conservados = [n for n in nombres if n not in borrar]

    semanas = {rx._fecha_de(n).isocalendar()[:2] for n in conservados}
    meses = {(rx._fecha_de(n).year, rx._fecha_de(n).month) for n in conservados}
    assert len(semanas) >= rx.GFS_SEMANALES
    assert len(meses) >= rx.GFS_MENSUALES
    # Y borra de verdad: con 365 no puede conservarlos a todos.
    assert len(borrar) > 300


def test_sin_nada_que_borrar_no_borra():
    assert rx.a_borrar([], AHORA) == []
    assert rx.a_borrar(["solo-uno.zip"], AHORA) == []


# ── subir: se mira el producto, no el exit code ──────────────────────────────

@pytest.fixture
def cliente(tmp_path):
    d = tmp_path / "clientes" / "cliente"
    (d / "data" / "backups").mkdir(parents=True)
    return {
        "slug": "cliente", "dir": d,
        "resguardo_externo": {"remoto": "drive_cliente:", "ruta": "libra/cliente"},
    }


def _poner_zip(cliente, dt=AHORA, contenido=b"PK\x03\x04" + b"x" * 500):
    ruta = cliente["dir"] / "data" / "backups" / _nombre(dt)
    ruta.write_bytes(contenido)
    return ruta


def _falso_rclone(monkeypatch, *, remoto_tras_copiar, registro=None):
    """Reemplaza `_rclone` por uno que simula el destino."""
    def fake(*args, binario="rclone", timeout=1800):
        if registro is not None:
            registro.append(args)
        if args[0] == "lsjson":
            return json.dumps([
                {"Name": n, "Size": s, "IsDir": False}
                for n, s in remoto_tras_copiar.items()
            ])
        return ""
    monkeypatch.setattr(rx, "_rclone", fake)


def test_sube_y_deja_el_estado_en_ok(cliente, monkeypatch):
    z = _poner_zip(cliente)
    _falso_rclone(monkeypatch, remoto_tras_copiar={z.name: z.stat().st_size})

    estado = rx.subir(cliente, cliente["dir"] / "data" / "backups",
                      ahora=AHORA, log=lambda *a: None)

    assert estado["ok"] is True
    assert estado["archivo"] == z.name
    assert estado["destino"] == "drive_cliente:libra/cliente"
    guardado = json.loads((cliente["dir"] / "data" / "backups" / rx.ESTADO).read_text())
    assert guardado["ok"] is True


def test_si_el_archivo_no_llego_al_destino_falla(cliente, monkeypatch):
    """🔴 `rclone copy` puede salir 0 y no haber dejado nada."""
    _poner_zip(cliente)
    _falso_rclone(monkeypatch, remoto_tras_copiar={})  # el destino quedo vacio

    estado = rx.subir(cliente, cliente["dir"] / "data" / "backups",
                      ahora=AHORA, log=lambda *a: None)

    assert estado["ok"] is False
    assert "no esta en el destino" in estado["error"]


def test_si_llego_truncado_falla(cliente, monkeypatch):
    z = _poner_zip(cliente)
    _falso_rclone(monkeypatch, remoto_tras_copiar={z.name: 12})

    estado = rx.subir(cliente, cliente["dir"] / "data" / "backups",
                      ahora=AHORA, log=lambda *a: None)

    assert estado["ok"] is False
    assert "bytes" in estado["error"]


def test_sin_backups_para_subir_falla_con_mensaje(cliente, monkeypatch):
    _falso_rclone(monkeypatch, remoto_tras_copiar={})

    estado = rx.subir(cliente, cliente["dir"] / "data" / "backups",
                      ahora=AHORA, log=lambda *a: None)

    assert estado["ok"] is False
    assert "no hay ningun backup" in estado["error"]


def test_el_estado_se_escribe_tambien_cuando_falla(cliente, monkeypatch):
    """Sin esto, la pantalla no distingue "nunca se configuro" de "hace cuatro
    dias que no sube"."""
    _falso_rclone(monkeypatch, remoto_tras_copiar={})

    rx.subir(cliente, cliente["dir"] / "data" / "backups",
             ahora=AHORA, log=lambda *a: None)

    guardado = json.loads((cliente["dir"] / "data" / "backups" / rx.ESTADO).read_text())
    assert guardado["ok"] is False
    assert guardado["error"]


def test_borra_en_el_destino_lo_que_sobra(cliente, monkeypatch):
    z = _poner_zip(cliente)
    viejos = {_nombre(AHORA - timedelta(days=d)): 100 for d in range(1, 40)}
    registro = []
    _falso_rclone(monkeypatch,
                  remoto_tras_copiar={z.name: z.stat().st_size, **viejos},
                  registro=registro)

    estado = rx.subir(cliente, cliente["dir"] / "data" / "backups",
                      ahora=AHORA, log=lambda *a: None)

    borrados = [a[1] for a in registro if a[0] == "deletefile"]
    assert estado["ok"] is True
    assert borrados, "con 40 copias tiene que borrar algo"
    assert not any(z.name in b for b in borrados), "nunca el que se acaba de subir"


# ── el add-on no contratado ──────────────────────────────────────────────────

def test_sin_resguardo_contratado_no_hace_nada(tmp_path):
    assert rx.destino_de({"slug": "x"}) is None


def test_resguardo_mal_configurado_avisa():
    with pytest.raises(rx.ResguardoExternoError):
        rx.destino_de({"slug": "x", "resguardo_externo": {"ruta": "sin/remoto"}})


# ── frescura ─────────────────────────────────────────────────────────────────

def test_sin_estado_no_esta_al_dia(tmp_path):
    al_dia, motivo = rx.esta_al_dia(tmp_path, ahora=AHORA)
    assert al_dia is False
    assert "nunca subio" in motivo


def test_una_copia_vieja_no_esta_al_dia(tmp_path):
    rx.escribir_estado(tmp_path, {
        "ok": True, "cuando": (AHORA - timedelta(hours=50)).isoformat(timespec="seconds"),
    })

    al_dia, motivo = rx.esta_al_dia(tmp_path, horas=36, ahora=AHORA)

    assert al_dia is False
    assert "hace mas de 36 horas" in motivo


def test_una_copia_fresca_esta_al_dia(tmp_path):
    rx.escribir_estado(tmp_path, {
        "ok": True, "cuando": (AHORA - timedelta(hours=5)).isoformat(timespec="seconds"),
    })

    al_dia, _ = rx.esta_al_dia(tmp_path, horas=36, ahora=AHORA)

    assert al_dia is True


def test_una_subida_fallida_no_esta_al_dia_aunque_sea_reciente(tmp_path):
    """El caso que importa: el cliente revoco el OAuth hace una hora."""
    rx.escribir_estado(tmp_path, {
        "ok": False, "cuando": AHORA.isoformat(timespec="seconds"),
        "error": "token expirado",
    })

    al_dia, motivo = rx.esta_al_dia(tmp_path, ahora=AHORA)

    assert al_dia is False
    assert "token expirado" in motivo
