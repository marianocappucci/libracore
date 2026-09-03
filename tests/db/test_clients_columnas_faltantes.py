"""El alta y la edición de clientes contra una instancia SIN las cuatro
columnas de la revisión `0002`.

Existe por un rojo real: al bumpear a `libracore v1.32.0`, Contalibra y
Restolibra dieron `sqlite3.OperationalError: table clients has no column named
empresa` en 14 tests. `create_client()` las escribía sin preguntar, y esas
columnas **no están en `init_core_schema()`** —que quedó congelada en la `0001`—
sino sólo en la cadena de Alembic del motor, que hoy **no corre en ningún lado**.

Medido el mismo día sobre las ocho bases PostgreSQL del VPS: **siete no tienen
ninguna de las cuatro**. O sea que el problema no era de esos dos productos:
era de toda instancia que viniera de antes, y el CI de los otros no podía verlo
porque ahí la tabla nace del `CREATE TABLE`.

Lo que fijan:

1. 🔴 Que el alta **funcione** sin las columnas — que es el caso de siete de
   ocho instancias reales hoy.
2. Que cuando **sí** están, se escriban. Sin este control, el arreglo podría ser
   "no escribirlas nunca" y el test de arriba pasaría igual.
3. Lo mismo para la edición.
"""
import sqlite3

import pytest

from libracore.db import clients as mod
from libracore.db import core

TABLA_VIEJA = """
    CREATE TABLE clients (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        address       TEXT,
        cuit_dni      TEXT,
        email         TEXT,
        phone         TEXT,
        iva_condition TEXT DEFAULT '',
        activo        INTEGER DEFAULT 1,
        auto_facturar INTEGER NOT NULL DEFAULT 0,
        cc_resumen_auto INTEGER NOT NULL DEFAULT 0,
        cc_resumen_frecuencia TEXT NOT NULL DEFAULT 'mensual',
        created_at    TEXT DEFAULT (datetime('now'))
    );
"""

LAS_CUATRO = """
    ALTER TABLE clients ADD COLUMN empresa TEXT DEFAULT '';
    ALTER TABLE clients ADD COLUMN ciudad TEXT DEFAULT '';
    ALTER TABLE clients ADD COLUMN observaciones TEXT DEFAULT '';
    ALTER TABLE clients ADD COLUMN tipo_facturacion TEXT NOT NULL DEFAULT 'por_servicio';
"""


@pytest.fixture
def base(tmp_path, request):
    """Una instancia con la tabla vieja; con `las_cuatro` marca, ya migrada."""
    ruta = tmp_path / "vieja.db"
    conn = sqlite3.connect(str(ruta))
    conn.executescript(TABLA_VIEJA)
    if request.node.get_closest_marker("las_cuatro"):
        conn.executescript(LAS_CUATRO)
    conn.commit()
    conn.close()
    core.configure(db_path=str(ruta))
    return ruta


def _columnas(ruta):
    conn = sqlite3.connect(str(ruta))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(clients)")}
    finally:
        conn.close()


def test_el_alta_funciona_sin_las_cuatro_columnas(base):
    """🔴 El caso de siete de las ocho instancias reales."""
    assert "empresa" not in _columnas(base)

    client_id = mod.create_client("Ferretería Suipacha", cuit_dni="20-12345678-9")

    assert client_id
    assert mod.get_client(client_id)["name"] == "Ferretería Suipacha"


def test_la_edicion_funciona_sin_las_cuatro_columnas(base):
    client_id = mod.create_client("Antes")

    mod.update_client(client_id, name="Después", address="Suipacha 123")

    guardado = mod.get_client(client_id)
    assert guardado["name"] == "Después"
    assert guardado["address"] == "Suipacha 123"


@pytest.mark.las_cuatro
def test_cuando_estan_se_escriben(base):
    """El control del test de arriba: si el arreglo fuera "no escribirlas
    nunca", aquel pasaría igual y esto no."""
    assert "empresa" in _columnas(base)

    client_id = mod.create_client(
        "Con columnas", empresa="Suipacha SRL", ciudad="Suipacha",
        observaciones="paga a 30 días", tipo_facturacion="por_abono",
    )

    guardado = mod.get_client(client_id)
    assert guardado["empresa"] == "Suipacha SRL"
    assert guardado["ciudad"] == "Suipacha"
    assert guardado["observaciones"] == "paga a 30 días"
    assert guardado["tipo_facturacion"] == "por_abono"


@pytest.mark.las_cuatro
def test_cuando_estan_la_edicion_tambien_las_escribe(base):
    client_id = mod.create_client("Con columnas", empresa="Vieja SRL")

    mod.update_client(client_id, empresa="Nueva SRL", ciudad="Mercedes")

    guardado = mod.get_client(client_id)
    assert guardado["empresa"] == "Nueva SRL"
    assert guardado["ciudad"] == "Mercedes"


@pytest.mark.las_cuatro
def test_editar_sin_tocarlas_no_las_borra(base):
    """Un update parcial no puede vaciar lo que no se nombró."""
    client_id = mod.create_client("X", empresa="Suipacha SRL", ciudad="Suipacha")

    mod.update_client(client_id, name="Y")

    guardado = mod.get_client(client_id)
    assert guardado["name"] == "Y"
    assert guardado["empresa"] == "Suipacha SRL"
    assert guardado["ciudad"] == "Suipacha"
