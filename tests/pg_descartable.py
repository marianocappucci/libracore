"""Bases PostgreSQL descartables para los tests de backup y restore.

🔴 **Cada test usa bases propias, nunca la de `LIBRACORE_POSTGRES_URL`.** Desde
el 2026-09-17 el restore intercambia bases por nombre y termina las conexiones
de la que reemplaza: hacerlo sobre la base compartida del CI la renombraria y le
cortaria la sesion a cualquier otro test que la este usando.
"""
import os
import uuid

import pytest

from libracore.respaldo_postgres import (
    SUFIJO_ANTERIOR,
    SUFIJO_ANTERIOR_NUEVA,
    SUFIJO_TEMPORAL,
    con_base,
)


def url_del_servidor() -> str:
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
    return url


def conectar(url, autocommit=False):
    import psycopg

    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=autocommit)


def existe(url_servidor: str, base: str) -> bool:
    with conectar(con_base(url_servidor, "postgres"), autocommit=True) as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (base,)).fetchone() is not None


class Bases:
    """Crea bases con nombre unico y las borra —con sus temporales y anteriores—
    al terminar el test."""

    def __init__(self):
        self.servidor = url_del_servidor()
        self.nombres: list[str] = []

    def nueva(self, sufijo: str = "") -> str:
        nombre = f"lcr_{uuid.uuid4().hex[:10]}{sufijo}"
        with conectar(con_base(self.servidor, "postgres"), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{nombre}"')
        self.nombres.append(nombre)
        return con_base(self.servidor, nombre)

    def limpiar(self):
        with conectar(con_base(self.servidor, "postgres"), autocommit=True) as conn:
            for nombre in self.nombres:
                for variante in (nombre, nombre + SUFIJO_TEMPORAL, nombre + SUFIJO_ANTERIOR,
                                 nombre + SUFIJO_ANTERIOR_NUEVA):
                    conn.execute(f'DROP DATABASE IF EXISTS "{variante}" WITH (FORCE)')
