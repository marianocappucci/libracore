"""
Usuarios del sistema: alta/baja/modificación y autenticación por contraseña
(PBKDF2). Extraído de database.py de Contalibra/Restolibra (idéntico en
ambos) como parte de la migración real a libracore.db (Fase 3 de
LibraCore, ver wiki/entities/libracore.md).
"""
import hashlib
import hmac
import os
import secrets

from libracore.db.core import get_connection


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(32)
    dk   = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"pbkdf2:sha256:{salt}:{dk.hex()}"


def _verify_password(stored: str, provided: str) -> bool:
    try:
        _, algo, salt, stored_hash = stored.split(":")
        dk = hashlib.pbkdf2_hmac(algo, provided.encode(), salt.encode(), 260_000)
        return hmac.compare_digest(dk.hex(), stored_hash)
    except Exception:
        return False


# Hash señuelo, mismo costo (260k iteraciones PBKDF2) que uno real — se verifica
# contra este cuando el username no existe, para que `check_usuario_credentials`
# tarde lo mismo con usuario inexistente que con password incorrecta. Generado
# una sola vez al importar el módulo (no en cada request).
_DUMMY_PASSWORD_HASH = _hash_password(secrets.token_hex(16))


def create_usuario(username: str, nombre: str, email: str,
                   password: str, role: str = "operador") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO usuarios (username, nombre, email, password_hash, role) VALUES (?,?,?,?,?)",
            (username.strip(), nombre.strip(), email.strip(),
             _hash_password(password), role),
        )
        return cur.lastrowid


def get_usuario_by_username(username: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM usuarios WHERE username=? AND activo=1", (username,)
        ).fetchone()
        return dict(row) if row else None


def get_usuario_by_id(uid: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None


def get_all_usuarios() -> list:
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM usuarios ORDER BY role DESC, username"
        ).fetchall()]


def update_usuario(uid: int, nombre: str, email: str, role: str, activo: int):
    with get_connection() as conn:
        conn.execute(
            "UPDATE usuarios SET nombre=?, email=?, role=?, activo=? WHERE id=?",
            (nombre.strip(), email.strip(), role, activo, uid),
        )


def update_usuario_password(uid: int, new_password: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE usuarios SET password_hash=? WHERE id=?",
            (_hash_password(new_password), uid),
        )


def delete_usuario(uid: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM usuarios WHERE id=?", (uid,))


def check_usuario_credentials(username: str, password: str) -> dict | None:
    """Devuelve el usuario si las credenciales son válidas, None si no.

    Siempre corre `_verify_password` (contra un hash señuelo del mismo costo
    si el username no existe), para que el tiempo de respuesta no delate si
    un username existe — antes, un username inexistente retornaba de
    inmediato sin correr las 260k iteraciones de PBKDF2 que sí corren para
    uno real: timing attack de enumeración de usuarios."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM usuarios WHERE username=? AND activo=1", (username,)
        ).fetchone()
    user = dict(row) if row else None
    stored_hash = user["password_hash"] if user else _DUMMY_PASSWORD_HASH
    password_ok = _verify_password(stored_hash, password)
    return user if (user and password_ok) else None


def ensure_admin_user():
    """Crea el usuario admin por defecto si no existe ningún usuario."""
    if get_all_usuarios():
        return
    username = os.environ.get("ADMIN_USER", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "")
    nombre   = os.environ.get("ADMIN_NOMBRE", "Administrador")
    if not password:
        password = secrets.token_urlsafe(12)
        print(f"[WARN] ADMIN_PASSWORD no configurado. Contraseña generada: {password}")
    create_usuario(username=username, nombre=nombre, email="", password=password, role="admin")
    print(f"[INFO] Usuario admin '{username}' creado.")


def _to_json_dict(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "username": row["username"],
        "name": row["nombre"],
        "role": row["role"],
        "active": bool(row["activo"]),
    }


class UserRepository:
    """Adaptador sobre las funciones de este módulo que expone el contrato
    id/username/name/role/active (distinto del esquema real de la tabla:
    id/username/nombre/email/role/activo) — usado por las APIs JSON de la
    familia sin backoffice server-rendered propio (Gestiolibra/MedLibra/
    VentaLibra). Extraído 2026-07-26 tras confirmar que era byte-idéntico
    en los tres salvo el conjunto de roles válidos, ver
    wiki/analyses/auditoria-duplicacion-familia-libra.md."""

    def __init__(self, roles: tuple[str, ...] = ("admin", "staff")):
        self.roles = roles

    def create(self, username: str, name: str, password: str, role: str) -> dict:
        if role not in self.roles:
            raise ValueError(f"invalid role: {role!r} (expected one of {self.roles})")
        uid = create_usuario(username=username, nombre=name, email="", password=password, role=role)
        return self.get_by_id(str(uid))

    def get_by_id(self, user_id: str) -> dict | None:
        try:
            uid = int(user_id)
        except ValueError:
            return None
        row = get_usuario_by_id(uid)
        return _to_json_dict(row) if row else None

    def get_by_username(self, username: str) -> dict | None:
        row = get_usuario_by_username(username)
        return _to_json_dict(row) if row else None

    def list(self) -> list:
        return [_to_json_dict(row) for row in get_all_usuarios()]

    def update(self, user_id: str, name: str, role: str, active: bool) -> dict:
        if role not in self.roles:
            raise ValueError(f"invalid role: {role!r} (expected one of {self.roles})")
        uid = self._require_uid(user_id)
        update_usuario(uid, nombre=name, email="", role=role, activo=int(active))
        return self.get_by_id(user_id)

    def update_password(self, user_id: str, new_password: str) -> None:
        uid = self._require_uid(user_id)
        update_usuario_password(uid, new_password)

    def delete(self, user_id: str) -> None:
        uid = self._require_uid(user_id)
        delete_usuario(uid)

    def _require_uid(self, user_id: str) -> int:
        """Convierte el id de la URL a int y confirma que exista — un id no
        numérico es indistinguible de "no encontrado" para quien llama."""
        try:
            uid = int(user_id)
        except ValueError:
            raise KeyError(user_id)
        if get_usuario_by_id(uid) is None:
            raise KeyError(user_id)
        return uid

    def check_credentials(self, username: str, password: str) -> dict | None:
        row = check_usuario_credentials(username, password)
        return _to_json_dict(row) if row else None


def ensure_default_admin(repo: "UserRepository", *, env_prefix: str) -> None:
    """Crea el admin inicial si la tabla usuarios todavía está vacía.

    Fail-closed en producción: sin `<env_prefix>_ADMIN_PASSWORD` la app no
    arranca — a diferencia de `ensure_admin_user()` (usada tal cual por
    Contalibra/Restolibra), que en ese caso genera una contraseña aleatoria
    y solo loguea un warning. Ese comportamiento no se adopta acá para no
    relajar la postura de seguridad ya establecida por los verticales que
    consumen esto (Gestiolibra/MedLibra/VentaLibra)."""
    if repo.list():
        return
    username = os.environ.get(f"{env_prefix}_ADMIN_USERNAME", "admin")
    password = os.environ.get(f"{env_prefix}_ADMIN_PASSWORD", "")
    if not password:
        if os.environ.get("ENV", "production") != "development":
            raise RuntimeError(
                f"{env_prefix}_ADMIN_PASSWORD no esta seteado. No se levanta la "
                "app sin una contrasena de admin inicial (setear ENV=development "
                "para desarrollo local)."
            )
        password = "admin"
    repo.create(username=username, name="Administrador", password=password, role="admin")
