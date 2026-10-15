"""Fixtures compartidas de los tests de provisioning.

`cmd_actualizar` respalda cada instancia antes de tocarla (2026-09-10). El
respaldo real necesita `docker` y una instancia viva, que los tests del deploy
no tienen: sin este doble abortarían todos en el respaldo y dejarían de medir
lo que miden. El respaldo previo tiene sus propios tests en
`test_respaldo_antes_de_actualizar.py`, que lo vuelven a parchear o usan el
original.

Con la entrega de `data/` al uid de la app (`sin_root`) pasa lo mismo: le pregunta
a Docker con qué `USER` corre la imagen y hace `chown` a 10001, que sólo puede
hacer root. Sin este doble, los tests del deploy fallarían en un paso que no miden.
`test_sin_root.py` usa el original.
"""
import pytest

from libracore.provisioning import panel_admin as pa


@pytest.fixture(autouse=True)
def _respaldo_previo_falso(monkeypatch):
    """Devuelve la lista de slugs respaldados, para quien quiera mirarla."""
    llamadas: list[str] = []

    def _falso(slug):
        llamadas.append(slug)
        return True

    monkeypatch.setattr(pa, "_respaldo_previo", _falso)
    return llamadas


@pytest.fixture(autouse=True)
def _entrega_de_datos_falsa(monkeypatch):
    """Devuelve la lista de `(slug, ref)` a los que se les entregó `data/`."""
    llamadas: list[tuple[str, str]] = []

    def _falso(c, ref):
        llamadas.append((c["slug"], ref))
        return True

    monkeypatch.setattr(pa, "_entregar_datos_a_la_app", _falso)
    return llamadas
