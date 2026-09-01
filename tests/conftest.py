import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture
def crear_schema():
    """Devuelve el helper que arma el schema de una instancia REAL.

    Es fixture y no función importable porque `tests/` no es un paquete: un
    `from ..conftest import ...` desde `tests/db/` falla con "relative import
    beyond top-level package".
    """
    return _crear_schema


def _crear_schema(conn):
    """El schema que tiene una instancia REAL: `init_core_schema()` más las
    revisiones de Alembic posteriores a la baseline.

    Reemplaza a llamar `init_core_schema(conn)` pelado, que hasta el 2026-08-12
    alcanzaba porque `0001` era la única revisión y la función era todo el
    schema. Desde `0002` ya no: una base armada sólo con la función **le falta
    lo que agregaron las revisiones**, así que la suite correría contra una
    forma de la base y producción contra otra. Los 30 tests que se pusieron en
    rojo al agregar las cuatro columnas de `clients` son exactamente eso.

    Reproduce las dos mitades que producción ejecuta por separado: la app llama
    `init_core_schema()` en cada arranque, y el deploy corre `alembic upgrade
    head` (`scripts/run_migrations.sh`). Correr las dos acá es lo que mantiene
    la suite y las instancias sobre el mismo schema.

    El `upgrade` va por subproceso contra el MISMO destino que `core` ya tiene
    configurado, así que hay que llamarla después de `core.configure(...)`.
    """
    from libracore.db import core
    from libracore.db.schema import init_core_schema

    init_core_schema(conn)
    conn.commit()

    destino = core._db_path
    if destino is None:
        raise RuntimeError("crear_schema() necesita core.configure(...) hecho antes")

    resultado = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=RAIZ,
        env={**os.environ, "DATABASE_URL": destino},
        capture_output=True,
        text=True,
    )
    if resultado.returncode != 0:
        raise RuntimeError(f"alembic upgrade head falló:\n{resultado.stderr}")
    return conn


def _write_cert_key(tmp_path, name, days_valid=365, not_before_offset_days=0):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"test-{name}")]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now + datetime.timedelta(days=not_before_offset_days)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + datetime.timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )

    cert_path = tmp_path / f"{name}.crt"
    key_path = tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


def make_valid_cert_key(tmp_path):
    return _write_cert_key(tmp_path, "valid")


def make_expired_cert_key(tmp_path):
    return _write_cert_key(tmp_path, "expired", days_valid=1, not_before_offset_days=-30)


def make_mismatched_key(tmp_path):
    """Devuelve un cert válido junto con la clave de OTRO par (no coincide)."""
    cert_path, _ = _write_cert_key(tmp_path, "pairA")
    _, other_key_path = _write_cert_key(tmp_path, "pairB")
    return cert_path, other_key_path


def head_de_la_cadena() -> str:
    """La revision `head` de la cadena de LibraCore, leida de alembic.

    🔑 **Los tests que verifican una migracion afirman que la cadena llego al
    head ANTES de mirar los datos** — sin ese control, "los datos siguen ahi"
    pasa perfecto cuando la migracion no corrio. Pero escribir el numero a mano
    hace que ese control se pudra: al entrar la `0008`, los tres tests de la
    `0007` se pusieron rojos afirmando `== "0007_..."` sobre una cadena que ya
    iba mas lejos, y el rojo no decia nada sobre lo que median.

    Derivarlo de alembic mantiene el control y lo hace inmune a la revision
    siguiente.
    """
    import pathlib

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    raiz = pathlib.Path(__file__).resolve().parents[1]
    return ScriptDirectory.from_config(Config(str(raiz / "alembic.ini"))).get_current_head()
