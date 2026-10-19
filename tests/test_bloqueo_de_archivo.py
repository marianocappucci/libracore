"""El bloqueo de archivo portable (`libracore.bloqueo_de_archivo`): excluye y es la única costura con `fcntl`."""
from __future__ import annotations

import ast
import os
from pathlib import Path

from libracore import bloqueo_de_archivo

RAIZ = Path(__file__).resolve().parent.parent / "libracore"


def _abrir(ruta: Path) -> int:
    return os.open(ruta, os.O_CREAT | os.O_RDWR, 0o600)


def test_dos_aperturas_del_mismo_archivo_se_excluyen(tmp_path):
    ruta = tmp_path / "x.lock"
    a, b = _abrir(ruta), _abrir(ruta)
    try:
        assert bloqueo_de_archivo.intentar(a) is True
        assert bloqueo_de_archivo.intentar(b) is False
        bloqueo_de_archivo.soltar(a)
        assert bloqueo_de_archivo.intentar(b) is True
        bloqueo_de_archivo.soltar(b)
    finally:
        os.close(a)
        os.close(b)


def test_ningun_modulo_del_motor_importa_fcntl_ni_otro_modulo_solo_de_posix():
    """El nodo de LibraEdge corre el motor en Windows: `import fcntl` al cargar un módulo lo dejaba sin migraciones."""
    solo_posix = {"fcntl", "pwd", "grp", "termios", "resource"}
    culpables = []
    for archivo in RAIZ.rglob("*.py"):
        if archivo.name == "bloqueo_de_archivo.py":
            continue
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            nombres = []
            if isinstance(nodo, ast.Import):
                nombres = [a.name.split(".")[0] for a in nodo.names]
            elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
                nombres = [nodo.module.split(".")[0]]
            culpables += [f"{archivo.relative_to(RAIZ)}:{nodo.lineno} {n}" for n in nombres if n in solo_posix]
    assert culpables == [], "usar libracore.bloqueo_de_archivo (o importar adentro de la función, con guarda de SO)"
