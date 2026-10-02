"""Los permisos de la clave privada de ARCA: 0600, y las ya abiertas se cierran."""
import os
import stat

import pytest
from conftest import make_valid_cert_key

from libracore import arca_certificados, arca_credenciales


def _modo(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_escribir_clave_privada_deja_0600_y_reemplaza_un_archivo_abierto(tmp_path):
    destino = tmp_path / "clave.key"
    destino.write_bytes(b"vieja")
    os.chmod(destino, 0o644)
    arca_certificados.escribir_clave_privada(str(destino), b"nueva")
    assert destino.read_bytes() == b"nueva" and _modo(destino) == 0o600
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]


def test_si_la_escritura_falla_no_queda_el_temporal_ni_se_toca_la_clave(tmp_path, monkeypatch):
    destino = tmp_path / "clave.key"
    destino.write_bytes(b"buena")

    def explota(*a, **kw):
        raise OSError("disco lleno")

    monkeypatch.setattr(os, "replace", explota)
    with pytest.raises(OSError):
        arca_certificados.escribir_clave_privada(str(destino), b"nueva")
    assert destino.read_bytes() == b"buena"
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]


def test_cerrar_permisos_cambia_sólo_lo_abierto(tmp_path):
    abierta = tmp_path / "a.key"
    abierta.write_bytes(b"x")
    os.chmod(abierta, 0o644)
    cerrada = tmp_path / "b.key"
    cerrada.write_bytes(b"x")
    os.chmod(cerrada, 0o600)
    assert arca_certificados.cerrar_permisos_de_la_clave(str(abierta)) is True
    assert arca_certificados.cerrar_permisos_de_la_clave(str(cerrada)) is False
    assert _modo(abierta) == 0o600


def test_cerrar_permisos_nunca_levanta(tmp_path):
    assert arca_certificados.cerrar_permisos_de_la_clave(str(tmp_path / "no-existe.key")) is False


def test_el_motor_cierra_la_clave_abierta_al_resolver_las_credenciales(tmp_path):
    """🔑 Es lo que corrige las instancias vivas al actualizar, sin un paso manual."""
    cert, clave = make_valid_cert_key(tmp_path)
    os.chmod(clave, 0o644)
    cfg = {"ambiente": "homologacion", "certificado_path_homologacion": cert,
           "clave_path_homologacion": clave}
    _, clave_resuelta = arca_credenciales.paths_en_disco(cfg)
    assert clave_resuelta == clave and _modo(clave) == 0o600


def test_sin_credenciales_no_hay_nada_que_cerrar():
    assert arca_credenciales.paths_en_disco(None) == ("", "")
