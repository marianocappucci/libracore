"""Tests de libracore.db.url_de_instancia — el nombre normalizado de la variable
de base, con fallback a los historicos mientras se actualizan las instancias.
"""
import pytest

from libracore.db.url_de_instancia import (
    nombre_normalizado, nombres_aceptados, url_de_instancia,
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
    assert url_de_instancia("medlibra", core=True,
                            entorno={"MEDLIBRA_LIBRACORE_DB_PATH": "postgresql://x/core"}) \
        == "postgresql://x/core"


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
