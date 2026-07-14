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
