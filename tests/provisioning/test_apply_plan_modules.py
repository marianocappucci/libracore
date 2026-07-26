"""Tests para libracore.provisioning.apply_plan_modules — extraído
2026-07-26 de Gestiolibra/MedLibra/VentaLibra
(`plans.py::aplicar_plan_en_db`), donde el cuerpo era idéntico salvo el
nombre de la variable. Ver
wiki/analyses/auditoria-duplicacion-familia-libra.md."""
import sqlite3

import pytest

from libracore.provisioning import apply_plan_modules


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "cliente.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE modulos (modulo TEXT PRIMARY KEY, habilitado INTEGER, plan TEXT)"
    )
    con.commit()
    con.close()
    return path


def _read_modulos(db_path):
    con = sqlite3.connect(db_path)
    rows = {r[0]: (bool(r[1]), r[2]) for r in con.execute("SELECT modulo, habilitado, plan FROM modulos")}
    con.close()
    return rows


def test_applies_active_and_inactive_modules(db_path):
    apply_plan_modules(
        db_path,
        active_modules={"recordatorios"},
        all_modules={"recordatorios", "facturacion", "dashboard"},
        plan="estandar",
    )
    rows = _read_modulos(db_path)
    assert rows == {
        "recordatorios": (True, "estandar"),
        "facturacion": (False, "estandar"),
        "dashboard": (False, "estandar"),
    }


def test_is_idempotent_on_a_fresh_db(db_path):
    apply_plan_modules(db_path, active_modules={"a"}, all_modules={"a", "b"}, plan="basico")
    apply_plan_modules(db_path, active_modules={"a"}, all_modules={"a", "b"}, plan="basico")
    assert _read_modulos(db_path) == {"a": (True, "basico"), "b": (False, "basico")}


def test_applying_a_new_plan_updates_existing_rows(db_path):
    apply_plan_modules(db_path, active_modules=set(), all_modules={"a", "b"}, plan="basico")
    apply_plan_modules(db_path, active_modules={"a", "b"}, all_modules={"a", "b"}, plan="premium")
    assert _read_modulos(db_path) == {"a": (True, "premium"), "b": (True, "premium")}


def test_raises_if_modulos_table_missing(tmp_path):
    path = str(tmp_path / "sin_tabla.db")
    sqlite3.connect(path).close()
    with pytest.raises(sqlite3.OperationalError):
        apply_plan_modules(path, active_modules=set(), all_modules={"a"}, plan="basico")
