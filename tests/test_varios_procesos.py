"""Los candados entre procesos, probados con **`uvicorn --workers 2` de verdad** contra PostgreSQL.

🔴 **Por qué existe.** Hasta ahora el motor suponía un solo proceso por instancia, y todo lo que se apoyaba en eso
(la guarda «una nota a la vez» en memoria, los hooks de arranque con «si no existe, crear») es invisible para una suite
que corre la app en un solo proceso. Estos tests levantan dos workers reales y les pegan con pedidos simultáneos:

1. `una_nota_a_la_vez` con `conn`: 20 pedidos de nota para la misma factura, repartidos entre los dos procesos ⇒
   exactamente **1** se emite (y los otros 19 reciben el 409 de «en curso»).
2. `una_vez_al_arrancar`: el hook de arranque de los dos workers deja **1** fila, no 2, y los dos workers siguen vivos.

Cada uno trae su **contraprueba**: el mismo escenario sin el candado de base tiene que producir el duplicado. Sin ella
un test verde podría estar verde porque los pedidos no se repartieron entre los procesos.

Se saltea sin `LIBRACORE_POSTGRES_URL`. Tarda unos 10 s (cada servidor son ~2 s de arranque).
"""
import time

import pytest

from libracore.testing import app_varios_procesos as banco
from libracore.testing.varios_procesos import en_paralelo, pedir, pids_vistos, servidor

pytestmark = pytest.mark.varios_procesos

APP = "libracore.testing.app_varios_procesos:app"
PEDIDOS = 20


@pytest.fixture
def url(bases):
    url = bases.nueva()
    banco.preparar_base(url)
    return url


def _pedir_notas(base, original, con_candado, cantidad=PEDIDOS):
    return en_paralelo(
        cantidad, lambda _i: pedir(f"{base}/nc/{original}?con_candado={con_candado}", metodo="POST"))


def _repartidas(respuestas) -> set[int]:
    return {cuerpo["pid"] for estado, cuerpo in respuestas if isinstance(cuerpo, dict)}


def _hasta_que_se_repartan(base, original, con_candado):
    """Corre la tanda de pedidos hasta que la atiendan **los dos** procesos (el sistema operativo reparte las
    conexiones como quiere, y una tanda que cae entera en un worker no prueba nada). Devuelve las respuestas de esa tanda."""
    for intento in range(6):
        respuestas = _pedir_notas(base, original + intento, con_candado)
        if len(_repartidas(respuestas)) == 2:
            return original + intento, respuestas
    pytest.fail("seis tandas de 20 pedidos y nunca los atendieron los dos workers")


def test_dos_workers_son_dos_procesos_vivos(url):
    with servidor(APP, workers=2, env={"PROCESOS_DB": url}) as base:
        assert len(pids_vistos(base)) == 2, "uvicorn --workers 2 no dio dos procesos distintos que contesten"


def test_con_conn_20_pedidos_repartidos_entre_dos_workers_emiten_una_sola_nota(url):
    with servidor(APP, workers=2, env={"PROCESOS_DB": url}) as base:
        original, respuestas = _hasta_que_se_repartan(base, 100, con_candado=1)

        ok = [c for e, c in respuestas if e == 200]
        en_curso = [c for e, c in respuestas if e == 409]
        assert len(ok) == 1, f"salieron {len(ok)} notas ante ARCA, tenía que ser 1: {respuestas}"
        assert len(en_curso) == PEDIDOS - 1
        assert {c["codigo"] for c in en_curso} == {"en_curso"}
        assert len(_repartidas(respuestas)) == 2, "los pedidos no se repartieron entre los dos procesos"
        # Y en la base quedó una sola, con CAE: lo que el producto cobraría.
        assert pedir(f"{base}/notas/{original}")[1]["notas"] == 1


def test_contraprueba_sin_conn_cada_proceso_emite_la_suya(url):
    """Lo que había antes: la guarda en memoria frena a los pedidos de su proceso y a ninguno de los del otro."""
    with servidor(APP, workers=2, env={"PROCESOS_DB": url}) as base:
        original, respuestas = _hasta_que_se_repartan(base, 200, con_candado=0)

        ok = [c for e, c in respuestas if e == 200]
        assert len(ok) == 2, f"sin el candado de base tenía que emitir una por proceso (2), salieron {len(ok)}"
        assert {c["pid"] for c in ok} == _repartidas(respuestas)
        assert pedir(f"{base}/notas/{original}")[1]["notas"] == 2


def test_el_arranque_con_una_vez_al_arrancar_deja_una_sola_fila_y_los_dos_workers_vivos(url):
    with servidor(APP, workers=2, env={"PROCESOS_DB": url}) as base:
        # Primero que los dos workers contesten (o sea, que los dos terminaron su arranque) y recién ahí contar: si
        # no, la cuenta podría leerse antes de que el segundo corra su hook y el 1 saldría por la razón equivocada.
        assert len(pids_vistos(base)) == 2
        assert pedir(f"{base}/semilla")[1]["filas"] == 1


def test_contraprueba_el_mismo_arranque_sin_candado_siembra_dos_veces(url):
    with servidor(APP, workers=2, env={"PROCESOS_DB": url, "PROCESOS_SIN_CANDADOS": "2"}) as base:
        # `servidor` vuelve cuando contesta el PRIMER worker, y el segundo puede estar todavía escribiendo su fila
        # (los dos pasaron la cita, pero el commit del otro llega un instante después). Se espera a que aparezca,
        # con tope: si nunca llega a 2, el duplicado no ocurrió y la contraprueba falla igual.
        limite = time.monotonic() + 10
        filas = pedir(f"{base}/semilla")[1]["filas"]
        while filas < 2 and time.monotonic() < limite:
            time.sleep(0.1)
            filas = pedir(f"{base}/semilla")[1]["filas"]
        assert filas == 2
