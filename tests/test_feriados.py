"""El catálogo de feriados nacionales.

Lo que más se prueba acá, igual que en `test_geografia.py`, es que el archivo
**esté adentro del paquete instalado** y que traiga los días que el negocio
cierra de verdad. Un catálogo que se ve completo y no tiene el feriado es peor
que no tenerlo: la agenda ofrece un turno para un día cerrado y nadie se entera
hasta que el cliente golpea la puerta.

Y hay un segundo eje, propio de este dato y no de la geografía: **el archivo
envejece**. Los tests de cobertura son la alarma de que hay que regenerarlo.
"""
import json
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracore.feriados import (
    FueraDeCobertura,
    _ARCHIVO,
    anios_cubiertos,
    build_feriados_router,
    cubre,
    es_feriado,
    feriados_de,
    generado,
)

#: Los que no se mueven nunca y todo negocio cierra. Si alguno faltara en algún
#: año, la fuente cambió de forma y el catálogo no sirve.
FIJOS = {
    "01-01": "Año nuevo",
    "03-24": "Memoria",
    "05-01": "Trabajador",
    "07-09": "Independencia",
    "12-08": "Inmaculada",
    "12-25": "Navidad",
}


def test_el_archivo_viaja_con_el_paquete():
    """🔴 El modo de falla que esto cierra es de empaquetado, no de código.

    `hatchling` incluye lo que está adentro del paquete, pero un `.json` no es
    un `.py` y nadie se entera de que falta hasta que una instancia levanta y
    la agenda deja de saber qué días cierra.
    """
    assert _ARCHIVO.exists(), f"no esta el catalogo en {_ARCHIVO}"
    crudo = json.loads(_ARCHIVO.read_text(encoding="utf-8"))
    assert crudo["fuente"].startswith("ArgentinaDatos")
    assert crudo["anios"], "el catalogo no declara ningun anio"


def test_los_feriados_fijos_estan_en_todos_los_anios():
    """El conjunto que no depende de ningún decreto."""
    for anio in anios_cubiertos():
        fechas = {f["fecha"][5:] for f in feriados_de(anio)}
        faltan = sorted(set(FIJOS) - fechas)
        assert not faltan, f"{anio} no tiene {[FIJOS[m] for m in faltan]}"


def test_hay_puentes_en_el_catalogo():
    """🔑 El test que distingue esta fuente de la alternativa.

    `date.nager.at` contesta bien y trae los 16 feriados fijos de 2026, pero
    **no trae los tres `puente`** de ese año. Para una agenda es justo lo que
    no puede faltar: el puente es el día que el negocio cierra y nadie
    recuerda. Si alguien cambiara el generador a esa fuente, esto se pone rojo.

    Se pregunta por el catálogo entero y no por un año fijo a propósito: la
    ventana de años se corre cada vez que se regenera, y un test anclado a 2026
    fallaría el día que 2026 salga del archivo, por un motivo que no es éste.
    """
    puentes = [
        f for anio in anios_cubiertos()
        for f in feriados_de(anio) if f["tipo"] == "puente"
    ]
    assert puentes, "ningun puente turistico en todo el catalogo"


def test_el_anio_en_curso_esta_cubierto():
    """⏰ La alarma de que el archivo quedó viejo.

    No es un test del código: es el recordatorio de correr
    `scripts/generar_feriados.py`. Si el año en curso no está, toda consulta de
    la agenda levanta `FueraDeCobertura` y el producto se queda sin feriados.
    """
    assert cubre(date.today().year), (
        f"el catalogo se estampo el {generado()} y ya no cubre "
        f"{date.today().year}: regenerar con scripts/generar_feriados.py"
    )


# -- la defensa principal ---------------------------------------------------

def test_fuera_de_cobertura_levanta_y_no_devuelve_none():
    """🔴 El punto entero del módulo.

    Contestar `None` para un año que no está sería indistinguible de "ese día
    no es feriado", y quien pregunta —una agenda— no tiene forma de ver la
    diferencia hasta que abre un 25 de diciembre.
    """
    lejano = max(anios_cubiertos()) + 50
    with pytest.raises(FueraDeCobertura):
        es_feriado(date(lejano, 12, 25))
    with pytest.raises(FueraDeCobertura):
        feriados_de(lejano)
    with pytest.raises(FueraDeCobertura):
        feriados_de(min(anios_cubiertos()) - 50)


def test_cubre_guarda_la_consulta():
    """El par `cubre()` + `es_feriado()` es el uso documentado."""
    assert cubre(max(anios_cubiertos())) is True
    assert cubre(max(anios_cubiertos()) + 50) is False


def test_un_dia_comun_dentro_de_la_ventana_da_none():
    """Control positivo del test de arriba: adentro de la ventana, `None`
    significa "no es feriado" y no "no sé". Sin esto, un módulo que levantara
    siempre pasaría igual."""
    assert es_feriado(date(2026, 12, 26)) is None
    assert es_feriado(date(2026, 12, 25))["nombre"] == "Navidad"


def test_no_hay_dos_feriados_el_mismo_dia():
    """Carnaval son dos días seguidos, no dos filas del mismo día. Una fecha
    repetida sería un feriado contado dos veces al importar."""
    for anio in anios_cubiertos():
        fechas = [f["fecha"] for f in feriados_de(anio)]
        assert len(fechas) == len(set(fechas)), f"fechas repetidas en {anio}"


def test_cada_feriado_cae_en_su_anio():
    """Que el agrupado por año no se haya corrido de lugar."""
    for anio in anios_cubiertos():
        for feriado in feriados_de(anio):
            assert feriado["fecha"].startswith(str(anio)), feriado


# -- el router --------------------------------------------------------------

def _cliente() -> TestClient:
    app = FastAPI()
    app.include_router(build_feriados_router())
    return TestClient(app)


def test_router_publica_la_cobertura():
    datos = _cliente().get("/api/feriados/cobertura").json()
    assert datos["anios"] == list(anios_cubiertos())
    assert datos["generado"] == generado()


def test_router_devuelve_los_feriados_de_un_anio():
    anio = max(anios_cubiertos())
    respuesta = _cliente().get(f"/api/feriados/{anio}")
    assert respuesta.status_code == 200
    assert respuesta.json() == feriados_de(anio)


def test_router_da_404_fuera_de_cobertura():
    """Y no una lista vacía, que es la forma que tiene un 200 de mentir."""
    respuesta = _cliente().get(f"/api/feriados/{max(anios_cubiertos()) + 50}")
    assert respuesta.status_code == 404
    assert "generar_feriados" in respuesta.json()["detail"]


def test_cobertura_no_se_la_come_la_ruta_dinamica():
    """`/cobertura` y `/{anio}` conviven: si la dinámica estuviera declarada
    primero, FastAPI intentaría leer "cobertura" como un entero y devolvería
    422 en vez de la cobertura."""
    assert _cliente().get("/api/feriados/cobertura").status_code == 200
