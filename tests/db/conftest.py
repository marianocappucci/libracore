"""Fixtures compartidas de los tests de `libracore.db`."""
import pytest

from libracore.db import core


@pytest.fixture
def crear_usuario():
    """Devuelve un helper que inserta una fila en `usuarios` sin pasar por
    ningun modulo de auth.

    Antes esto lo hacia `libracore.db.usuarios.create_usuario`, que **salio del
    motor el 2026-07-30**: el auth de toda la familia es ahora `libraauth` (ver
    wiki/entities/libraauth.md). La **tabla** `usuarios` sigue siendo de
    LibraCore — 12 tablas le declaran `usuario_id REFERENCES usuarios(id)` —
    asi que estos tests siguen necesitando una fila real para ejercitar esas
    FK; lo que ya no necesitan es un modulo de auth para crearla.

    Usa `core.get_connection()` igual que el modulo viejo, asi el helper sirve
    en cualquier test que ya haya configurado la base con la fixture `conn`.
    El `password_hash` es un literal a proposito: ningun test de este paquete
    verifica credenciales. Eso se prueba en la suite de libraauth.
    """
    def _crear(username, role="operador"):
        with core.get_connection() as conn:
            cur = conn.execute(
                # `TRUE` y no `1`: `usuarios.activo` es BOOLEAN desde que se
                # alineo con el modelo de libraauth, y PostgreSQL no acepta un
                # entero ahi. SQLite entiende las dos.
                "INSERT INTO usuarios (username, nombre, email, password_hash, role, activo) "
                "VALUES (?, ?, '', 'sin-hash-real--este-test-no-autentica', ?, TRUE)",
                (username, username.title(), role),
            )
            conn.commit()
            return cur.lastrowid

    return _crear
