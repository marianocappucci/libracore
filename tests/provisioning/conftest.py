"""Fixtures compartidas de los tests de provisioning.

`cmd_actualizar` respalda cada instancia antes de tocarla (2026-09-10). El
respaldo real necesita `docker` y una instancia viva, que los tests del deploy
no tienen: sin este doble abortarían todos en el respaldo y dejarían de medir
lo que miden. El respaldo previo tiene sus propios tests en
`test_respaldo_antes_de_actualizar.py`, que lo vuelven a parchear o usan el
original.
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
