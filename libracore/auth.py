"""
Autenticación por cookie firmada, compartida por la app principal y el
backoffice de superadmin de cada producto (Contalibra, Restolibra).

`SessionAuth` cubre la sesión del usuario final (operador/admin/etc, con
roles consultados en la base del producto) y `AdminAuth` cubre el
backoffice (un único superadmin por variables de entorno, sin tabla de
roles). `SessionAuth.require_admin`/`require_role` reciben la consulta de
usuario como callback inyectado (`get_user_by_username`) en vez de importar
un `database` de un producto específico — la Fase 3 de LibraCore (motor de
datos compartido) todavía no existe como módulo, y este es el patrón a
repetir en módulos futuros que necesiten datos del producto: callback en
vez de asumir el schema.
"""
import hmac
import os
import threading
import time
from typing import Callable

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from starlette.requests import Request
from starlette.exceptions import HTTPException


def _resolve_secret_key(dev_fallback: str, missing_error: str) -> str:
    secret = os.environ.get("SECRET_KEY", "")
    if secret:
        return secret
    if os.environ.get("ENV", "production") == "development":
        return dev_fallback
    # Fail-fast: sin esto, cualquiera puede forjar una cookie de sesión con
    # itsdangerous + un secreto público conocido de antemano (hallazgo
    # cruzado desde la auditoría de Restolibra).
    raise RuntimeError(missing_error)


class SessionAuth:
    """Sesión del usuario final por cookie firmada. Cada producto instancia
    una vez, en su propio `web/auth.py`, inyectando sus consultas a
    `database`."""

    def __init__(
        self,
        *,
        dev_secret_fallback: str,
        get_user_by_username: Callable[[str], dict | None],
        check_credentials: Callable[[str, str], object],
        cookie_name: str = "cl_session",
        max_age: int = 86400 * 7,
    ):
        self.secret_key = _resolve_secret_key(
            dev_secret_fallback,
            "SECRET_KEY no está seteado. No se levanta la app sin un secreto "
            "propio por cliente (ver scripts/nuevo_cliente.py) — para "
            "desarrollo local sin uno, setear ENV=development.",
        )
        self.cookie_name = cookie_name
        self.max_age = max_age
        self._get_user_by_username = get_user_by_username
        self._check_credentials_fn = check_credentials
        self._signer = URLSafeTimedSerializer(self.secret_key)

    def create_session_cookie(self, response, username: str):
        token = self._signer.dumps(username)
        response.set_cookie(
            self.cookie_name, token, httponly=True, samesite="lax", secure=True
        )

    def clear_session_cookie(self, response):
        response.delete_cookie(self.cookie_name)

    def get_current_user(self, request: Request) -> str | None:
        token = request.cookies.get(self.cookie_name)
        if not token:
            return None
        try:
            return self._signer.loads(token, max_age=self.max_age)
        except (BadSignature, SignatureExpired):
            return None

    def require_auth(self, request: Request) -> str:
        user = self.get_current_user(request)
        if not user:
            raise HTTPException(status_code=307, headers={"Location": "/login"})
        return user

    def require_admin(self, request: Request) -> dict:
        username = self.get_current_user(request)
        if not username:
            raise HTTPException(status_code=307, headers={"Location": "/login"})
        user = self._get_user_by_username(username)
        if not user or user.get("role") != "admin":
            raise HTTPException(status_code=307, headers={"Location": "/dashboard"})
        return user

    def require_role(self, *roles: str):
        """Factory de dependencia: exige que el usuario logueado tenga uno
        de los roles indicados."""

        def _dep(request: Request) -> dict:
            username = self.get_current_user(request)
            if not username:
                raise HTTPException(status_code=307, headers={"Location": "/login"})
            user = self._get_user_by_username(username)
            if not user or user.get("role") not in roles:
                raise HTTPException(status_code=307, headers={"Location": "/dashboard"})
            return user

        return _dep

    def check_credentials(self, username: str, password: str) -> bool:
        return self._check_credentials_fn(username, password) is not None


class AdminAuth:
    """Autenticación del backoffice de superadmin: un único usuario definido
    por variables de entorno (`ADMIN_PANEL_USER`/`ADMIN_PANEL_PASSWORD`),
    sin dependencia de `database` — no hay tabla de roles, un solo
    superadmin por proceso systemd."""

    def __init__(
        self,
        *,
        dev_secret_fallback: str,
        cookie_name: str = "cladmin_session",
        max_age: int = 86400 * 3,
        login_max_intentos: int = 5,
        login_ventana_segundos: int = 15 * 60,
    ):
        self.secret_key = _resolve_secret_key(
            dev_secret_fallback,
            "SECRET_KEY no está seteado para el backoffice de superadmin. "
            "Para desarrollo local sin uno, setear ENV=development.",
        )
        self.panel_user = os.environ.get("ADMIN_PANEL_USER", "superadmin")
        self.panel_pass = os.environ.get("ADMIN_PANEL_PASSWORD", "")
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.login_max_intentos = login_max_intentos
        self.login_ventana_segundos = login_ventana_segundos
        self._signer = URLSafeTimedSerializer(self.secret_key)
        # Rate limiting de /login en memoria del propio proceso — alcanza
        # porque el backoffice corre como un único proceso systemd. Se
        # resetea si el proceso reinicia — aceptable para este panel de
        # bajo tráfico.
        self._intentos_fallidos: dict[str, list[float]] = {}
        self._intentos_lock = threading.Lock()

    def check_credentials(self, username: str, password: str) -> bool:
        if not self.panel_pass:
            # Sin contraseña configurada: se rechaza todo (fail-closed).
            return False
        return hmac.compare_digest(
            username or "", self.panel_user
        ) and hmac.compare_digest(password or "", self.panel_pass)

    def rate_limit_excedido(self, ip: str) -> bool:
        if not ip:
            return False
        ahora = time.time()
        with self._intentos_lock:
            vigentes = [
                t
                for t in self._intentos_fallidos.get(ip, [])
                if ahora - t < self.login_ventana_segundos
            ]
            self._intentos_fallidos[ip] = vigentes
            return len(vigentes) >= self.login_max_intentos

    def registrar_intento_fallido(self, ip: str):
        if not ip:
            return
        with self._intentos_lock:
            self._intentos_fallidos.setdefault(ip, []).append(time.time())

    def create_session_cookie(self, response, username: str):
        response.set_cookie(
            self.cookie_name,
            self._signer.dumps(username),
            httponly=True,
            samesite="lax",
            secure=True,
        )

    def clear_session_cookie(self, response):
        response.delete_cookie(self.cookie_name)

    def current_user(self, request: Request) -> str | None:
        token = request.cookies.get(self.cookie_name)
        if not token:
            return None
        try:
            return self._signer.loads(token, max_age=self.max_age)
        except (BadSignature, SignatureExpired):
            return None

    def require_login(self, request: Request) -> str:
        user = self.current_user(request)
        if not user:
            raise HTTPException(status_code=307, headers={"Location": "/login"})
        return user
