"""Nadie encadena a mano `paths_de` + `resolve_cert_paths`.

🔴 **Este barrido nace de un defecto que cometí y no vi.** Al separar los dos
pares de credenciales (2026-09-01) arreglé el llamador del router y dejé **dos**
call sites del motor pasando el par sin el ambiente: `arca_facturacion` y
`facturas_router`, justo los del camino de emisión. El baile de dos pasos estaba
escrito cuatro veces y salió mal en dos.

**Por qué duele tanto separarlas.** `paths_de()` devuelve `("", "")` a propósito
para no entregar las credenciales reales cuando falta el par de homologación. El
rescate de `resolve_cert_paths`, si no sabe el ambiente, cae al nombre de
producción y **las repone**. El segundo paso deshace al primero, en silencio y
justo en el camino que firma comprobantes.

Un test por caso no alcanzaba: el defecto no era una llamada equivocada, era la
llamada que **todavía no existe**. Por eso el barrido pregunta *"¿alguien
encadena los dos pasos a mano?"*, y la única forma de ponerlo en verde es usar
`arca_credenciales.paths_en_disco()`.
"""

import ast
import pathlib

from libracore import arca_credenciales, config_manager
from libracore.db import arca_config as db_arca

RAIZ = pathlib.Path(__file__).resolve().parents[1] / "libracore"

#: Los que **pueden** nombrar las dos piezas sueltas, y por qué.
PERMITIDOS = {
    "arca_credenciales.py": "es la función que las encadena bien: el único lugar",
    "config_manager.py": "define el rescate",
    "db/arca_config.py": "define la elección del par",
}


def _llama_a(arbol, nombre):
    """Las líneas donde se llama a `algo.nombre(...)` o `nombre(...)`."""
    return [
        n.lineno for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == nombre
    ]


# -- El barrido -------------------------------------------------------------

def test_nadie_encadena_los_dos_pasos_a_mano():
    """🔴 El defecto que busca es el archivo que todavía no se escribió."""
    culpables, mirados = [], 0
    for f in RAIZ.rglob("*.py"):
        rel = f.relative_to(RAIZ).as_posix()
        if rel in PERMITIDOS or rel.startswith("migrations/"):
            continue
        mirados += 1
        arbol = ast.parse(f.read_text(encoding="utf-8"))
        if _llama_a(arbol, "paths_de") and _llama_a(arbol, "resolve_cert_paths"):
            culpables.append(rel)

    assert mirados >= 20, f"el barrido sólo miró {mirados} archivos: ¿cambió la raíz?"
    assert not culpables, (
        "Estos encadenan `paths_de` + `resolve_cert_paths` a mano:\n  "
        + "\n  ".join(culpables)
        + "\nUsá `arca_credenciales.paths_en_disco()`: separadas, el rescate"
          " repone el par de PRODUCCION que `paths_de` acababa de negar."
    )


def test_el_barrido_reconoce_el_encadenado_a_mano():
    """🔑 El control positivo. El de arriba recorre un AST: con el patrón mal
    escrito daría verde para siempre sin encontrar nada."""
    fuente = (
        "def leer(cfg):\n"
        "    a, b = db.paths_de(cfg)\n"
        "    return config_manager.resolve_cert_paths(a, b)\n"
    )
    arbol = ast.parse(fuente)
    assert _llama_a(arbol, "paths_de") and _llama_a(arbol, "resolve_cert_paths")


def test_el_barrido_NO_marca_a_quien_usa_la_funcion():
    """El otro control: un archivo que hace lo correcto no tiene que aparecer.
    Sin esto, un barrido que marcara todo pasaría el test de arriba."""
    fuente = (
        "def leer(cfg):\n"
        "    return arca_credenciales.paths_en_disco(cfg)\n"
    )
    arbol = ast.parse(fuente)
    assert not (_llama_a(arbol, "paths_de") and _llama_a(arbol, "resolve_cert_paths"))


def test_los_tres_call_sites_del_motor_la_usan():
    """Que el barrido no pase por vacío: si nadie llamara a `paths_en_disco`,
    "nadie encadena a mano" sería cierto y no significaría nada."""
    usan = [
        f.relative_to(RAIZ).as_posix() for f in RAIZ.rglob("*.py")
        if _llama_a(ast.parse(f.read_text(encoding="utf-8")), "paths_en_disco")
        and f.name != "arca_credenciales.py"
    ]
    assert sorted(usan) == ["arca_facturacion.py", "arca_router.py", "facturas_router.py"], usan


# -- El comportamiento ------------------------------------------------------

def _certs(tmp_path, monkeypatch, *nombres):
    d = tmp_path / "certs"
    d.mkdir()
    monkeypatch.setattr(config_manager, "CERTS_DIR", str(d))
    for n in nombres:
        (d / n).write_bytes(b"x")
    return d


def test_sin_par_de_homologacion_no_devuelve_el_de_produccion(tmp_path, monkeypatch):
    """🔴 El defecto entero, en un test. Con los dos pasos sueltos, acá salían
    las rutas del par REAL del cliente."""
    cert_prod, clave_prod = config_manager.ARCHIVOS_POR_AMBIENTE["produccion"]
    d = _certs(tmp_path, monkeypatch, cert_prod, clave_prod)

    cfg = {
        "ambiente": "homologacion",
        "certificado_path": str(d / cert_prod), "clave_path": str(d / clave_prod),
        "certificado_path_homologacion": "", "clave_path_homologacion": "",
    }
    assert arca_credenciales.paths_en_disco(cfg) == ("", "")


def test_con_su_par_cargado_lo_devuelve(tmp_path, monkeypatch):
    """El control positivo del anterior: una función que devolviera siempre
    ("", "") pasaría el test de arriba sin hacer nada."""
    cert_h, clave_h = config_manager.ARCHIVOS_POR_AMBIENTE["homologacion"]
    d = _certs(tmp_path, monkeypatch, cert_h, clave_h)

    cfg = {"ambiente": "homologacion",
           "certificado_path_homologacion": str(d / cert_h),
           "clave_path_homologacion": str(d / clave_h)}
    assert arca_credenciales.paths_en_disco(cfg) == (str(d / cert_h), str(d / clave_h))


def test_el_rescate_sigue_andando_dentro_del_ambiente(tmp_path, monkeypatch):
    """🔑 El rescate no se perdió: un path guardado que apunta a un archivo que
    se movió —una migración de volumen— sigue cayendo al nombre estándar. Pero
    al del **ambiente correcto**, que es lo único que cambió."""
    cert_h, clave_h = config_manager.ARCHIVOS_POR_AMBIENTE["homologacion"]
    d = _certs(tmp_path, monkeypatch, cert_h, clave_h)

    cfg = {"ambiente": "homologacion",
           "certificado_path_homologacion": "/volumen/viejo/que/ya/no/esta.crt",
           "clave_path_homologacion": "/volumen/viejo/que/ya/no/esta.key"}
    assert arca_credenciales.paths_en_disco(cfg) == (str(d / cert_h), str(d / clave_h))


def test_el_ambiente_explicito_gana(tmp_path, monkeypatch):
    """La pantalla pide el par del otro ambiente sin mover el selector."""
    cert_p, clave_p = config_manager.ARCHIVOS_POR_AMBIENTE["produccion"]
    d = _certs(tmp_path, monkeypatch, cert_p, clave_p)

    cfg = {"ambiente": "homologacion",
           "certificado_path": str(d / cert_p), "clave_path": str(d / clave_p)}
    assert arca_credenciales.paths_en_disco(cfg, "produccion") == (
        str(d / cert_p), str(d / clave_p))


def test_sin_config_no_revienta():
    assert arca_credenciales.paths_en_disco(None) == ("", "")
    assert arca_credenciales.paths_en_disco({}) == ("", "")


def test_normaliza_el_ambiente(tmp_path, monkeypatch):
    """Un ` Homologacion ` guardado por la pantalla tiene que encontrar su par."""
    cert_h, clave_h = config_manager.ARCHIVOS_POR_AMBIENTE["homologacion"]
    d = _certs(tmp_path, monkeypatch, cert_h, clave_h)

    cfg = {"ambiente": "  Homologacion ",
           "certificado_path_homologacion": str(d / cert_h),
           "clave_path_homologacion": str(d / clave_h)}
    assert arca_credenciales.paths_en_disco(cfg)[0] == str(d / cert_h)


def test_un_ambiente_raro_no_cae_a_produccion(tmp_path, monkeypatch):
    """Misma regla que en las dos piezas que envuelve: ante algo que no
    conocemos, nada — y sobre todo no la credencial real."""
    cert_p, clave_p = config_manager.ARCHIVOS_POR_AMBIENTE["produccion"]
    d = _certs(tmp_path, monkeypatch, cert_p, clave_p)

    cfg = {"ambiente": "testing",
           "certificado_path": str(d / cert_p), "clave_path": str(d / clave_p)}
    assert arca_credenciales.paths_en_disco(cfg) == ("", "")


def test_la_eleccion_del_par_la_hace_paths_de():
    """No hay una segunda copia de la asimetría: los nombres de columna salen
    del mapa de `db.arca_config`."""
    assert set(db_arca.COLUMNAS_POR_AMBIENTE) == {"produccion", "homologacion"}
