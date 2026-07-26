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

from fastapi import APIRouter, Depends, Header, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pydantic import BaseModel
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


# ── Dependencias para APIs JSON puras ───────────────────────────────────────
#
# A diferencia de SessionAuth.require_auth/require_role (redirect 307 a
# /login, pensado para apps server-rendered como Contalibra/Restolibra),
# estas dependencias devuelven 401/403 con cuerpo JSON -- para verticales sin
# página de login propia (Gestiolibra/MedLibra/VentaLibra). Extraídas
# 2026-07-26 tras confirmar que `app/auth.py`/`app/routers/auth.py` eran
# byte-idénticos en los tres, ver
# wiki/analyses/auditoria-duplicacion-familia-libra.md. Esperan
# `request.app.state.session_auth` (una `SessionAuth`) y
# `request.app.state.users` (contrato UserRepository: `check_credentials`,
# `get_by_username`, ver `libracore.db.usuarios.UserRepository`).

class _LoginRequest(BaseModel):
    username: str
    password: str


class _UserOut(BaseModel):
    id: str
    username: str
    name: str
    role: str
    active: bool


class _VerifyRequest(BaseModel):
    username: str
    password: str


class _VerifyResponse(BaseModel):
    valid: bool


def json_api_get_session_auth(request: Request) -> "SessionAuth":
    return request.app.state.session_auth


def json_api_get_current_user(
    request: Request, auth: "SessionAuth" = Depends(json_api_get_session_auth),
) -> dict:
    """401 JSON (no redirect) si no hay sesión válida o el usuario está
    inactivo."""
    username = auth.get_current_user(request)
    if username is None:
        raise HTTPException(401, "not authenticated")
    users = request.app.state.users
    user = users.get_by_username(username)
    if user is None or not user["active"]:
        raise HTTPException(401, "not authenticated")
    return user


def json_api_require_role(*roles: str):
    """Dependency factory: 403 JSON a menos que el usuario logueado tenga
    uno de roles."""

    def _dependency(user: dict = Depends(json_api_get_current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, "forbidden")
        return user

    return _dependency


json_api_require_admin = json_api_require_role("admin")
json_api_require_staff = json_api_require_role("admin", "staff")


def build_json_api_auth_router() -> APIRouter:
    """Router `/auth` completo (login/logout/me/verify) para APIs JSON de
    la familia Libra sin backoffice server-rendered propio. Espera
    `request.app.state.users`/`request.app.state.session_auth` ya
    configurados al arrancar la app (ver `json_api_get_session_auth`)."""
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/login", response_model=_UserOut)
    def login(data: _LoginRequest, request: Request, response: Response):
        users = request.app.state.users
        user = users.check_credentials(data.username, data.password)
        if user is None:
            raise HTTPException(401, "invalid credentials")
        json_api_get_session_auth(request).create_session_cookie(response, user["username"])
        return user

    @router.post("/logout")
    def logout(request: Request, response: Response):
        json_api_get_session_auth(request).clear_session_cookie(response)
        return {"ok": True}

    @router.get("/me", response_model=_UserOut)
    def me(user: dict = Depends(json_api_get_current_user)):
        return user

    @router.post("/verify", response_model=_VerifyResponse)
    def verify(
        data: _VerifyRequest,
        request: Request,
        x_internal_auth: str = Header(default=""),
    ):
        """Chequeo de credenciales stateless para el login de `/docs/` de
        la landing del producto. Server-to-server (secreto compartido
        `DOCS_AUTH_SECRET`), nunca crea cookie de sesión. Falla cerrado si
        el secreto no está configurado."""
        secret = os.environ.get("DOCS_AUTH_SECRET", "")
        if not secret or not hmac.compare_digest(x_internal_auth, secret):
            raise HTTPException(401, "invalid internal auth")
        users = request.app.state.users
        user = users.check_credentials(data.username, data.password)
        return _VerifyResponse(valid=user is not None)

    return router


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
