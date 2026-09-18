"""El cifrado de la pata externa: lo que sale del servidor va cifrado, o no sale.

Lo que fijan, en orden de lo que duele si falla:

1. 🔴 **Sin clave no hay `copy`.** Ni con el archivo ausente, ni vacio, ni corto,
   ni legible por otros. Un resguardo que no sube se ve en rojo; uno que sube
   en claro, no se ve — y eso es lo que paso hasta el 2026-09-17.
2. Que a rclone se le hable **solo** por el remoto cifrado, y que ese remoto
   envuelva el destino de siempre.
3. Que la clave no aparezca en claro en ningun argumento ni variable de entorno.
4. Que el contenido se verifique, y que un `cryptcheck` que no comparo nada no
   pase por verde.
5. Con el `rclone` real, si esta: que lo de alla no sea el ZIP, que vuelva
   identico, y que un objeto adulterado ponga la subida en rojo.
"""
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from libracore.provisioning import resguardo_externo as rx

AHORA = datetime(2026, 9, 17, 3, 20)
ZIP = "backup_automatico_20260917_031501.zip"
CLAVE = "c" * 40  # de prueba: nunca la real
CLIENTE = {"slug": "x", "resguardo_externo": {"remoto": "drive_x:", "ruta": "Resguardo"}}


def _poner_clave(tmp_path, monkeypatch, contenido=CLAVE, modo=0o600):
    ruta = tmp_path / "resguardo_cifrado.key"
    ruta.write_text(contenido)
    ruta.chmod(modo)
    monkeypatch.setenv(rx.CLAVE_ARCHIVO_ENV, str(ruta))
    return ruta


@pytest.fixture
def backups(tmp_path):
    d = tmp_path / "data" / "backups"
    d.mkdir(parents=True)
    (d / ZIP).write_bytes(b"PK\x03\x04" + b"x" * 500)
    return d


@pytest.fixture
def rclone(monkeypatch, backups):
    """Un rclone falso que registra `(args, env)` y simula el destino."""
    tam = (backups / ZIP).stat().st_size
    ctl = {"llamadas": [], "coincide": True, "remoto": {ZIP: tam}}

    def fake(*args, binario="rclone", timeout=1800, env=None):
        ctl["llamadas"].append((args, dict(env or {})))
        if args[0] == "lsjson":
            return json.dumps([
                {"Name": n, "Size": s, "IsDir": False} for n, s in ctl["remoto"].items()
            ])
        if args[0] == "cryptcheck":
            match = Path(args[args.index("--match") + 1])
            nombres = Path(args[args.index("--files-from") + 1]).read_text().splitlines()
            match.write_text("\n".join(nombres) + "\n" if ctl["coincide"] else "")
        return ""

    monkeypatch.setattr(rx, "_rclone", fake)
    monkeypatch.setattr(rx, "_obscurecer", lambda clave, binario="rclone": "OBSCURA")
    return ctl


def _subir(backups):
    return rx.subir(dict(CLIENTE), backups, ahora=AHORA, log=lambda *_: None)


# ── 1. sin clave valida, nada sale ───────────────────────────────────────────

def test_sin_archivo_de_clave_no_llama_a_rclone(tmp_path, monkeypatch, backups, rclone):
    monkeypatch.setenv(rx.CLAVE_ARCHIVO_ENV, str(tmp_path / "no-existe.key"))

    estado = _subir(backups)

    assert estado["ok"] is False
    assert "no se sube en claro" in estado["error"]
    assert rclone["llamadas"] == [], "sin clave no puede haber ni un solo rclone"
    guardado = json.loads((backups / rx.ESTADO).read_text())
    assert guardado["ok"] is False, "el rojo tiene que quedar escrito para estado-externo"


@pytest.mark.parametrize("contenido,modo,esperado", [
    (CLAVE, 0o644, "0600"),
    (CLAVE, 0o640, "0600"),
    ("corta", 0o600, "menos de"),
    ("   \n", 0o600, "menos de"),
])
def test_clave_invalida_no_llama_a_rclone(tmp_path, monkeypatch, backups, rclone,
                                           contenido, modo, esperado):
    _poner_clave(tmp_path, monkeypatch, contenido=contenido, modo=modo)

    estado = _subir(backups)

    assert estado["ok"] is False
    assert esperado in estado["error"]
    assert rclone["llamadas"] == []


def test_el_mensaje_de_error_no_trae_la_clave(tmp_path, monkeypatch, backups, rclone):
    _poner_clave(tmp_path, monkeypatch, modo=0o644)

    estado = _subir(backups)

    assert CLAVE not in estado["error"]


# ── 2. solo por el remoto cifrado ────────────────────────────────────────────

def test_solo_se_habla_por_el_remoto_cifrado(tmp_path, monkeypatch, backups, rclone):
    _poner_clave(tmp_path, monkeypatch)

    estado = _subir(backups)

    assert estado["ok"] is True, estado
    assert [a[0] for a, _ in rclone["llamadas"]] == ["copy", "lsjson", "cryptcheck"]
    copy_args = rclone["llamadas"][0][0]
    assert copy_args[2] == f"{rx.REMOTO_CIFRADO}:"
    for args, env in rclone["llamadas"]:
        assert not any(str(a).startswith("drive_x:") for a in args), (
            f"una llamada fue al destino plano, en claro: {args}"
        )
        p = f"RCLONE_CONFIG_{rx.REMOTO_CIFRADO.upper()}_"
        assert env[f"{p}TYPE"] == "crypt"
        assert env[f"{p}REMOTE"] == "drive_x:Resguardo"
        assert env[f"{p}FILENAME_ENCRYPTION"] == "off"


def test_el_estado_muestra_el_destino_legible_y_la_huella(tmp_path, monkeypatch, backups, rclone):
    _poner_clave(tmp_path, monkeypatch)

    estado = _subir(backups)

    assert estado["destino"] == "drive_x:Resguardo"
    assert estado["cifrado"] is True
    assert estado["huella_clave"] == hashlib.sha256(CLAVE.encode()).hexdigest()[:8]


# ── 3. la clave no viaja en claro ────────────────────────────────────────────

def test_la_clave_no_aparece_en_ningun_argumento_ni_entorno(tmp_path, monkeypatch, backups, rclone):
    _poner_clave(tmp_path, monkeypatch)

    _subir(backups)

    assert rclone["llamadas"], "control: tiene que haber llamadas para que el chequeo diga algo"
    for args, env in rclone["llamadas"]:
        assert not any(CLAVE in str(a) for a in args)
        assert not any(CLAVE in v for v in env.values())


# ── 4. el contenido, verificado de verdad ────────────────────────────────────

def test_un_cryptcheck_que_no_comparo_nada_falla(tmp_path, monkeypatch, backups, rclone):
    """🔴 `cryptcheck` con un filtro que no matchea sale 0 habiendo comparado cero
    archivos. El codigo de salida solo no prueba nada."""
    _poner_clave(tmp_path, monkeypatch)
    rclone["coincide"] = False

    estado = _subir(backups)

    assert estado["ok"] is False
    assert "cryptcheck" in estado["error"]


def test_la_retencion_borra_por_el_remoto_cifrado_sin_barra_de_mas(tmp_path, monkeypatch, backups, rclone):
    _poner_clave(tmp_path, monkeypatch)
    for d in range(1, 40):
        rclone["remoto"][f"backup_automatico_202608{(d % 28) + 1:02d}_0{d % 10}1501.zip"] = 10

    estado = _subir(backups)

    borrados = [a[1] for a, _ in rclone["llamadas"] if a[0] == "deletefile"]
    assert estado["ok"] is True, estado
    assert borrados, "control: con 40 copias tiene que borrar algo"
    for b in borrados:
        assert b.startswith(f"{rx.REMOTO_CIFRADO}:backup_"), b


# ── 5. con el rclone de verdad ───────────────────────────────────────────────

@pytest.mark.skipif(shutil.which("rclone") is None, reason="sin rclone en esta maquina")
def test_con_rclone_real_cifra_vuelve_identico_y_detecta_adulteracion(tmp_path, monkeypatch, backups):
    nube = tmp_path / "nube"
    nube.mkdir()
    conf = tmp_path / "rclone.conf"
    conf.write_text("[externo]\ntype = local\n")
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    # `destino_de` le saca la barra inicial a la ruta: se resuelve contra `/`.
    monkeypatch.chdir("/")
    contenido = os.urandom(200_000)
    (backups / ZIP).write_bytes(contenido)
    _poner_clave(tmp_path, monkeypatch, contenido=os.urandom(24).hex())
    cliente = {"slug": "x", "resguardo_externo": {"remoto": "externo:", "ruta": str(nube)}}

    estado = rx.subir(cliente, backups, ahora=AHORA, log=lambda *_: None)

    assert estado["ok"] is True, estado
    objetos = sorted(nube.iterdir())
    assert [o.name for o in objetos] == [ZIP + ".bin"]
    crudo = objetos[0].read_bytes()
    assert crudo.startswith(b"RCLONE\x00\x00"), "lo de alla tiene que ser un objeto cifrado"
    assert contenido[:4096] not in crudo, "el contenido no puede estar en claro"

    # Ida y vuelta: con la clave, vuelve identico.
    env = {**os.environ, **rx._entorno_cifrado(estado["destino"], rx._obscurecer(rx.leer_clave()))}
    vuelta = tmp_path / "vuelta"
    subprocess.run(["rclone", "copy", f"{rx.REMOTO_CIFRADO}:{ZIP}", str(vuelta)], check=True, env=env)
    assert (vuelta / ZIP).read_bytes() == contenido

    # 🔴 Mutar lo que custodia cryptcheck: adulterar el objeto SIN cambiar su
    # tamaño ni su fecha, para que `copy` no lo reemplace y el nombre y el tamaño
    # sigan dando bien. Solo la verificacion de contenido lo puede ver.
    info = objetos[0].stat()
    adulterado = bytearray(crudo)
    adulterado[-1] ^= 0xFF
    objetos[0].write_bytes(bytes(adulterado))
    os.utime(objetos[0], ns=(info.st_atime_ns, info.st_mtime_ns))

    estado2 = rx.subir(cliente, backups, ahora=AHORA, log=lambda *_: None)

    assert estado2["ok"] is False, "un objeto adulterado no puede quedar en verde"
    assert "cryptcheck" in estado2["error"]
