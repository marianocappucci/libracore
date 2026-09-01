"""De qué columnas sale el par de credenciales de cada ambiente.

🔴 **La asimetría es el defecto latente.** El par de producción vive en las
columnas SIN sufijo (`certificado_path`, `clave_path`) y el de homologación en
las que llevan `_homologacion`. Nombres históricos: ya existían cuando había un
solo par. Un lector que abra `cfg["clave_path"]` porque "es la clave" está
leyendo **la de producción** sin importar contra qué ambiente esté emitiendo.

De ahí que la traducción viva en un solo lugar —`paths_de()`— y que este
archivo tenga dos mitades: la que prueba esa función y **el barrido** que
verifica que nadie la esquive.
"""

import ast
import pathlib

from libracore.db import arca_config as db_arca

RAIZ = pathlib.Path(__file__).resolve().parents[1] / "libracore"

PROD = {"certificado_path": "/c/prod.crt", "clave_path": "/c/prod.key"}
HOMO = {"certificado_path_homologacion": "/c/homo.crt",
        "clave_path_homologacion": "/c/homo.key"}
LOS_DOS = {**PROD, **HOMO}


# -- La traduccion ----------------------------------------------------------

def test_el_selector_decide_cual_de_los_dos_pares_sale():
    """🔑 El control que hace que todo esto valga: la MISMA config, con los dos
    pares cargados, devuelve pares DISTINTOS según el selector. Sin esta
    afirmación, una función que devolviera siempre el de producción pasaría
    todos los tests de abajo que miran una sola fila."""
    assert db_arca.paths_de({**LOS_DOS, "ambiente": "produccion"}) == (
        "/c/prod.crt", "/c/prod.key")
    assert db_arca.paths_de({**LOS_DOS, "ambiente": "homologacion"}) == (
        "/c/homo.crt", "/c/homo.key")


def test_el_ambiente_explicito_gana_sobre_el_selector():
    """La pantalla que sube el par de homologación mientras la instancia emite
    en producción necesita pedir el otro sin cambiar el selector."""
    cfg = {**LOS_DOS, "ambiente": "produccion"}
    assert db_arca.paths_de(cfg, "homologacion") == ("/c/homo.crt", "/c/homo.key")


def test_un_ambiente_inventado_NO_cae_al_par_de_produccion():
    """🔴 El peor final posible de este cambio: un valor raro en la config y el
    sistema factura de verdad creyendo que prueba. Vacío, no fallback."""
    for raro in ("testing", "", None, "prod", "homolog"):
        assert db_arca.paths_de({**LOS_DOS, "ambiente": raro}) == ("", ""), raro


def test_sin_config_no_revienta():
    """`obtener_arca_config` devuelve None cuando la empresa no configuró ARCA."""
    assert db_arca.paths_de(None) == ("", "")
    assert db_arca.paths_de({}) == ("", "")


def test_el_par_que_falta_sale_vacio_y_no_None():
    """Quien llama hace `os.path.exists(cert)`, que con None levanta TypeError.
    El estado normal mientras se acompaña al cliente es tener sólo un par."""
    assert db_arca.paths_de({**PROD, "ambiente": "homologacion"}) == ("", "")
    assert db_arca.paths_de({"ambiente": "produccion", "clave_path": None,
                             "certificado_path": None}) == ("", "")


def test_el_ambiente_se_normaliza_igual_que_al_escribir():
    """Las dos puntas normalizan lo mismo. Si sólo lo hiciera una, un
    ` Homologacion ` guardado por la pantalla devolvería ("", "") acá y la
    instancia se quedaría sin credenciales sin que nadie tocara nada."""
    assert db_arca.paths_de({**LOS_DOS, "ambiente": "  Homologacion "}) == (
        "/c/homo.crt", "/c/homo.key")
    assert db_arca.paths_de({**LOS_DOS, "ambiente": "PRODUCCION"}) == (
        "/c/prod.crt", "/c/prod.key")


def test_el_orden_del_par_es_certificado_y_despues_clave():
    """🔑 Los dos son rutas a archivos que existen, así que invertirlos no
    revienta acá: falla recién al firmar, con un error de OpenSSL que no dice
    que se pasó el certificado como clave privada."""
    cert, clave = db_arca.paths_de({**PROD, "ambiente": "produccion"})
    assert cert.endswith(".crt") and clave.endswith(".key")


def test_los_dos_ambientes_estan_declarados():
    """Que el mapa no quede vacío o a medias por un refactor: con una sola
    entrada, el barrido de abajo y la mitad de estos tests seguirían pasando."""
    assert set(db_arca.COLUMNAS_POR_AMBIENTE) == {"produccion", "homologacion"}
    for cols in db_arca.COLUMNAS_POR_AMBIENTE.values():
        assert len(cols) == 2


# -- El barrido: nadie lee las columnas por su cuenta ------------------------

#: Los archivos que **pueden** nombrar las columnas crudas, y por qué.
PERMITIDOS = {
    "db/arca_config.py": "define el mapa: el único lugar donde vive la asimetría",
    "db/schema.py": "el DDL las crea",
    "arca_router.py": "la pantalla que las escribe nombra el destino",
}


def _accesos_directos(arbol, columnas):
    """Los `cfg["clave_path"]` y `cfg.get("clave_path")` de un AST.

    🔑 Una sola definición, usada por el barrido y por su control positivo. Con
    el patrón escrito dos veces, romper el del barrido dejaría el control en
    verde midiendo una copia que ya no se usa.
    """
    hallados = []
    for n in ast.walk(arbol):
        if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                and n.slice.value in columnas):
            hallados.append((n.lineno, n.slice.value))
        if (isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "get"
                and n.args and isinstance(n.args[0], ast.Constant)
                and n.args[0].value in columnas):
            hallados.append((n.lineno, n.args[0].value))
    return hallados


def test_ningun_lector_abre_las_columnas_del_par_directo():
    """🔴 Lo que este barrido busca no es un lector equivocado: es el lector que
    **todavía no existe**. `cfg["clave_path"]` se lee como "la clave" y es la de
    producción; quien lo escriba mientras la instancia emite contra homologación
    firma con la credencial real sin enterarse.

    Se parsea el AST y no se grepea: el nombre aparece dentro de literales SQL y
    de comentarios que explican justamente esta asimetría, y un grep los cuenta.
    """
    columnas = {c for par in db_arca.COLUMNAS_POR_AMBIENTE.values() for c in par}
    culpables, mirados = [], 0
    for f in RAIZ.rglob("*.py"):
        rel = f.relative_to(RAIZ).as_posix()
        if rel in PERMITIDOS or rel.startswith("migrations/"):
            continue
        mirados += 1
        arbol = ast.parse(f.read_text(encoding="utf-8"))
        for linea, col in _accesos_directos(arbol, columnas):
            culpables.append(f"{rel}:{linea} -> {col}")

    assert mirados >= 20, f"el barrido sólo miró {mirados} archivos: ¿cambió la raíz?"
    assert not culpables, (
        "Estos leen las columnas del par sin pasar por `paths_de()`:\n  "
        + "\n  ".join(culpables)
        + "\nLas columnas SIN sufijo son las de PRODUCCION: leerlas directo"
          " ignora contra qué ambiente se está emitiendo."
    )


def test_el_barrido_reconoce_un_acceso_directo():
    """🔑 El control positivo. El de arriba pasa recorriendo un AST: con el
    patrón mal escrito daría verde para siempre sin encontrar nada. Acá se le da
    código que SÍ accede y se comprueba que lo encuentra."""
    columnas = {c for par in db_arca.COLUMNAS_POR_AMBIENTE.values() for c in par}
    fuente = 'clave = cfg["clave_path"]\nc = cfg.get("certificado_path")'
    hallados = _accesos_directos(ast.parse(fuente), columnas)
    assert sorted(c for _, c in hallados) == ["certificado_path", "clave_path"]
