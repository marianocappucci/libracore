import importlib
import json
import os


def _fresh_config_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as cm
    importlib.reload(cm)
    return cm


def test_load_returns_defaults_when_no_config_file(tmp_path, monkeypatch):
    cm = _fresh_config_manager(tmp_path, monkeypatch)
    cfg = cm.load()
    assert cfg["servicio_estado"] == "activo"
    assert cfg["empresa_nombre"] == ""


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    cm = _fresh_config_manager(tmp_path, monkeypatch)
    cm.save({"empresa_nombre": "Test SA"})
    cfg = cm.load()
    assert cfg["empresa_nombre"] == "Test SA"
    # otros defaults siguen presentes aunque no se hayan pasado explicitamente
    assert cfg["servicio_estado"] == "activo"


def test_load_with_extra_defaults_merges_correctly(tmp_path, monkeypatch):
    cm = _fresh_config_manager(tmp_path, monkeypatch)
    extra = {"cubierto_activo": "0", "cubierto_precio": "0"}
    cfg = cm.load(extra_defaults=extra)
    assert cfg["cubierto_activo"] == "0"
    assert cfg["servicio_estado"] == "activo"


def test_resolve_logo_path_empty_when_nothing_saved(tmp_path, monkeypatch):
    cm = _fresh_config_manager(tmp_path, monkeypatch)
    assert cm.resolve_logo_path({"logo_path": ""}) == ""


def test_resolve_logo_path_falls_back_to_most_recent_in_logo_dir(tmp_path, monkeypatch):
    cm = _fresh_config_manager(tmp_path, monkeypatch)
    os.makedirs(cm.LOGO_DIR, exist_ok=True)
    logo_file = os.path.join(cm.LOGO_DIR, "logo.png")
    with open(logo_file, "wb") as f:
        f.write(b"fake-png")
    resolved = cm.resolve_logo_path({"logo_path": "/nonexistent/path.png"})
    assert resolved == logo_file


def test_resolve_cert_paths_falls_back_to_standard_names(tmp_path, monkeypatch):
    cm = _fresh_config_manager(tmp_path, monkeypatch)
    os.makedirs(cm.CERTS_DIR, exist_ok=True)
    cert_file = os.path.join(cm.CERTS_DIR, "certificado.crt")
    key_file = os.path.join(cm.CERTS_DIR, "clave_privada.key")
    open(cert_file, "w").close()
    open(key_file, "w").close()
    cert, key = cm.resolve_cert_paths("/old/path/certificado.crt", "/old/path/clave.key")
    assert cert == cert_file
    assert key == key_file


def test_resolve_cert_paths_returns_original_when_no_fallback_exists(tmp_path, monkeypatch):
    cm = _fresh_config_manager(tmp_path, monkeypatch)
    cert, key = cm.resolve_cert_paths("/old/path/certificado.crt", "/old/path/clave.key")
    assert cert == "/old/path/certificado.crt"
    assert key == "/old/path/clave.key"


# ── La escritura es atómica (varios workers leen el mismo config.json) ────────

def test_save_deja_el_archivo_anterior_intacto_si_falla_a_mitad(tmp_path, monkeypatch):
    cm = _fresh_config_manager(tmp_path, monkeypatch)
    cm.save({"empresa_nombre": "Antes SA"})

    def revienta_a_mitad(datos, f, **kw):
        f.write('{"empresa_nom')
        raise OSError("disco lleno")

    monkeypatch.setattr(cm.json, "dump", revienta_a_mitad)
    try:
        cm.save({"empresa_nombre": "Despues SA"})
    except OSError:
        pass
    monkeypatch.undo()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert json.loads(open(cm.CONFIG_PATH, encoding="utf-8").read())["empresa_nombre"] == "Antes SA"
    assert os.listdir(tmp_path) == ["config.json"], "no tiene que quedar el temporal"


def test_save_conserva_los_permisos_del_archivo(tmp_path, monkeypatch):
    cm = _fresh_config_manager(tmp_path, monkeypatch)
    cm.save({"empresa_nombre": "A"})
    os.chmod(cm.CONFIG_PATH, 0o640)
    cm.save({"empresa_nombre": "B"})
    assert oct(os.stat(cm.CONFIG_PATH).st_mode & 0o777) == "0o640"


def test_un_lector_de_otro_proceso_nunca_ve_el_config_a_medio_escribir(tmp_path, monkeypatch):
    """Con varios workers hay un lector real: `load()` de otro proceso mientras éste hace `save()`.

    El `open(..., "w")` de antes trunca el archivo, y en ese instante el lector lo ve vacío o cortado: `load()`
    atrapa el error y devuelve los **defaults** (la config de fábrica, sin avisar). Acá el lector lee el archivo
    crudo mientras otro hilo lo reescribe sin parar; no puede fallar ni una vez.
    """
    import threading

    cm = _fresh_config_manager(tmp_path, monkeypatch)
    relleno = "x" * 200_000                       # grande: que el hueco entre truncar y terminar de escribir se note
    cm.save({"empresa_nombre": "inicial", "relleno": relleno})
    terminado = threading.Event()
    fallas, lecturas = [], [0]

    def escribir():
        for i in range(400):
            cm.save({"empresa_nombre": f"n{i}", "relleno": relleno})
        terminado.set()

    def leer():
        while not terminado.is_set():
            try:
                with open(cm.CONFIG_PATH, encoding="utf-8") as f:
                    json.load(f)
                lecturas[0] += 1
            except (ValueError, OSError) as e:
                fallas.append(repr(e))

    lector = threading.Thread(target=leer)
    lector.start()
    escribir()
    lector.join()
    assert fallas == []
    assert lecturas[0] > 10, "el lector casi no corrió: la prueba no dice nada"
