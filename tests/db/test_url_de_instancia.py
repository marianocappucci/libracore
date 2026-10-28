"""Tests de libracore.db.url_de_instancia — el nombre normalizado de la variable
de base, con fallback a los historicos mientras se actualizan las instancias.
"""
import pytest

from libracore.db.url_de_instancia import (
    _UNA_SOLA_BASE,
    comparte_base_con_el_dominio,
    escribe_la_variable_del_core,
    nombre_normalizado,
    nombres_aceptados,
    url_de_instancia,
)


def test_el_nombre_normalizado_sale_del_prefijo():
    assert nombre_normalizado("gestiolibra") == "GESTIOLIBRA_DATABASE_URL"
    assert nombre_normalizado("gestiolibra", core=True) == "GESTIOLIBRA_LIBRACORE_DATABASE_URL"


def test_contalibra_y_restolibra_ya_estaban_normalizados():
    """La convencion no se invento: se eligio la que dos de los seis ya cumplian,
    asi que para esos dos no hay nombre historico que aceptar."""
    for p in ("contalibra", "restolibra"):
        assert nombres_aceptados(p) == (f"{p.upper()}_DATABASE_URL",)


def test_el_normalizado_gana_sobre_el_historico():
    entorno = {
        "LIBRADESK_DATABASE_URL": "postgresql://nueva/base",
        "DATABASE_URL": "postgresql://vieja/base",
    }
    assert url_de_instancia("libradesk", entorno=entorno) == "postgresql://nueva/base"


def test_el_historico_sigue_andando_mientras_no_este_el_nuevo():
    """Es lo que permite actualizar los composes de las 15 instancias sin
    ventana: la app entiende los dos nombres."""
    assert url_de_instancia("ventalibra", entorno={"VENTALIBRA_DB_PATH": "postgresql://x/y"}) \
        == "postgresql://x/y"
    assert url_de_instancia("ventalibra", core=True,
                            entorno={"VENTALIBRA_LIBRACORE_DB_PATH": "postgresql://x/core"}) \
        == "postgresql://x/core"


@pytest.mark.parametrize("prefijo", ["gestiolibra", "medlibra"])
def test_gestiolibra_y_medlibra_ya_no_aceptan_el_historico_del_core(prefijo):
    """ADR-084 (patch v1.195.1). Son de una sola base y, medido el 2026-10-25 sobre `origin/main` de los dos, ningún código, compose ni
    test nombra `<PREFIJO>_LIBRACORE_DB_PATH`. Sale de `_HISTORICOS`: el core se resuelve sólo por el nombre normalizado, y una
    instancia que todavía llevara el histórico lo vería ignorado (cae al `default`, que para estos productos es la base unida)."""
    var = f"{prefijo.upper()}_LIBRACORE_DB_PATH"
    assert nombres_aceptados(prefijo, core=True) == (f"{prefijo.upper()}_LIBRACORE_DATABASE_URL",)
    assert url_de_instancia(prefijo, core=True, entorno={var: f"postgresql://x/{prefijo}_core"}, default="el-dominio") \
        == "el-dominio"
    # el normalizado sigue mandando
    assert url_de_instancia(prefijo, core=True, entorno={f"{prefijo.upper()}_LIBRACORE_DATABASE_URL": "postgresql://x/core"}) \
        == "postgresql://x/core"


def test_ventalibra_conserva_su_historico_del_core():
    """Distinto de los dos de arriba: su `docker-compose.yml` de desarrollo y sus tests todavía ponen `VENTALIBRA_LIBRACORE_DB_PATH`
    (origin/main, 2026-10-25). Lo que se sacó de VentaLibra es que la EXIJA (ventalibra#693), no que la acepte."""
    assert nombres_aceptados("ventalibra", core=True) == ("VENTALIBRA_LIBRACORE_DATABASE_URL", "VENTALIBRA_LIBRACORE_DB_PATH")


def test_una_variable_vacia_cuenta_como_no_puesta():
    """Un `FOO=` en un compose es casi siempre una interpolacion que no ocurrio.
    Tomarlo como bueno manda a la app a conectarse a la cadena vacia y el error
    aparece lejos, sin nombrar la variable."""
    entorno = {"LIBRADESK_DATABASE_URL": "  ", "DATABASE_URL": "postgresql://buena"}
    assert url_de_instancia("libradesk", entorno=entorno) == "postgresql://buena"
    assert url_de_instancia("libradesk", entorno={"LIBRADESK_DATABASE_URL": ""},
                            default="sqlite:///x.db") == "sqlite:///x.db"


def test_sin_nada_puesto_devuelve_el_default():
    assert url_de_instancia("libradesk", entorno={}, default="sqlite:///data/libradesk.db") \
        == "sqlite:///data/libradesk.db"
    assert url_de_instancia("libradesk", entorno={}) == ""


def test_DATABASE_URL_generica_no_se_toma_en_los_que_nunca_la_usaron():
    """Contalibra nunca leyo `DATABASE_URL`. Aceptarla ahora haria que tome una
    variable generica que en un CI o en un contenedor cualquiera puede estar
    apuntando a otra base."""
    entorno = {"DATABASE_URL": "postgresql://otra/cosa"}
    assert url_de_instancia("contalibra", entorno=entorno, default="sqlite:///c.db") \
        == "sqlite:///c.db"
    assert "DATABASE_URL" not in nombres_aceptados("contalibra")


def test_el_historico_de_un_producto_no_aplica_a_otro():
    """`VENTALIBRA_DB_PATH` en el entorno no tiene que afectar a MedLibra."""
    entorno = {"VENTALIBRA_DB_PATH": "postgresql://ventalibra/x"}
    assert url_de_instancia("medlibra", entorno=entorno, default="d") == "d"


def test_requerida_falla_fuerte_y_nombra_las_variables():
    """Reemplaza a los `os.environ["DATABASE_URL"]` de las apps. Sin esto,
    cambiar un acceso por indice por esta funcion convertiria un arranque en
    falta de configuracion en una conexion a la cadena vacia."""
    with pytest.raises(RuntimeError) as e:
        url_de_instancia("medlibra", requerida=True, entorno={})
    mensaje = str(e.value)
    assert "MEDLIBRA_DATABASE_URL" in mensaje
    assert "DATABASE_URL" in mensaje  # el historico tambien, que es el que puede estar puesto


def test_requerida_no_estorba_cuando_la_variable_esta():
    assert url_de_instancia("medlibra", requerida=True,
                            entorno={"MEDLIBRA_DATABASE_URL": "postgresql://x"}) == "postgresql://x"


def test_requerida_tambien_rechaza_la_variable_vacia():
    with pytest.raises(RuntimeError):
        url_de_instancia("medlibra", requerida=True, entorno={"MEDLIBRA_DATABASE_URL": "   "})


# ── LibraCargo y LibraClub (2026-09-16) ──────────────────────────────────────

@pytest.mark.parametrize("prefijo", ["libracargo", "libraclub"])
def test_libracargo_y_libraclub_toman_el_dominio_de_DATABASE_URL(prefijo):
    """La forma real de su entorno, medida en dev y en produccion: el dominio en
    `DATABASE_URL` y ninguna `<PREFIJO>_DATABASE_URL`."""
    entorno = {"DATABASE_URL": "postgresql://h/dominio",
               f"{prefijo.upper()}_LIBRACORE_DATABASE_URL": "postgresql://h/core"}
    assert url_de_instancia(prefijo, entorno=entorno) == "postgresql://h/dominio"


@pytest.mark.parametrize("prefijo", ["libracargo", "libraclub"])
def test_DATABASE_URL_NUNCA_resuelve_el_core_de_libracargo_ni_libraclub(prefijo):
    """En estos dos `DATABASE_URL` es el DOMINIO y el core va aparte: el historico
    se cargo solo del lado del dominio."""
    entorno = {"DATABASE_URL": "postgresql://h/dominio"}
    assert url_de_instancia(prefijo, core=True, entorno=entorno, default="SIN") == "SIN"
    assert "DATABASE_URL" not in nombres_aceptados(prefijo, core=True)


def test_sumar_libracargo_y_libraclub_no_le_abre_DATABASE_URL_a_contalibra():
    """Control: la entrada es por producto."""
    for p in ("contalibra", "restolibra"):
        assert "DATABASE_URL" not in nombres_aceptados(p)


def test_los_de_una_sola_base_son_exactamente_los_medidos():
    """🔴 La lista que autoriza a `url_de_core` a caer al dominio. Medida contra
    las bases reales el 2026-09-16. Cambiarla es una decision, no un ajuste: por
    eso se fija entera y no con `in`."""
    todos = ["contalibra", "restolibra", "ventalibra", "libradesk",
             "gestiolibra", "medlibra", "libracargo", "libraclub", "librainmo", "librafarma"]
    una_sola = {p for p in todos if comparte_base_con_el_dominio(p)}
    # 2026-10-22: LibraCargo y LibraClub, unidas (ADR-078). 2026-10-25: GestioLibra y MedLibra (ADR-083): los ocho.
    # LibraInmo (ADR-087) y LibraFarma (2026-10-28) nacen así.
    assert una_sola == set(todos)
    # un prefijo desconocido NO comparte base: el default es fallar, no adivinar
    assert comparte_base_con_el_dominio("productonuevo") is False
    assert comparte_base_con_el_dominio("") is False


def test_el_alta_escribe_la_variable_del_core_solo_donde_hace_falta():
    """ADR-084. Ningún producto de `_UNA_SOLA_BASE` la recibe (VentaLibra dejó de exigirla con ventalibra#693, medido sobre su
    `origin/main` el 2026-10-25; ya no hay excepción); un producto fuera de la lista (core aparte) sí. Se fija entero."""
    assert not [p for p in _UNA_SOLA_BASE if escribe_la_variable_del_core(p)]
    for p in ("contalibra", "restolibra", "ventalibra", "libradesk", "libracargo", "libraclub", "gestiolibra", "medlibra"):
        assert escribe_la_variable_del_core(p) is False, p
    assert escribe_la_variable_del_core("productonuevo") is True
    assert escribe_la_variable_del_core("") is True
    # mayúsculas y espacios: igual que `comparte_base_con_el_dominio`
    assert escribe_la_variable_del_core(" GestioLibra ") is False
    assert escribe_la_variable_del_core(" VentaLibra ") is False
