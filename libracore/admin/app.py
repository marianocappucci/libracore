"""
Backoffice — panel de superadmin para gestionar clientes (alta, edición,
baja) y sus planes (asignar / upgrade / downgrade) a través de todos los
contenedores. App separada host-level, corre con:
    uvicorn admin.app:app --host 0.0.0.0 --port 8000
desde la raíz del repo del producto, con acceso al socket Docker y al
directorio clientes/.

Migrado a libracore.admin como factory configurable (Fase 4 de LibraCore,
backoffice compartido — divergencia real confirmada entre productos: solo
título de branding y si se monta `/static` para servir assets propios
(Contalibra usa Material Symbols auto-hosteado ahí, Restolibra no lo
referencia en su backoffice) — ver wiki/entities/libracore.md). Cada
producto arma su `admin/app.py` como shim de pocas líneas que llama
`create_admin_app(...)` pasando sus propios `auth`/`services`/`templates`/
`clientes_router` ya configurados — sin resolución de módulos por nombre
genérico dentro de LibraCore, todo explícito por parámetro.
"""
import os

from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from libracore.security_headers import SecurityHeadersMiddleware


def create_admin_app(product_name: str, auth, services, templates, clientes_router,
                     static_dir: str | None = None) -> FastAPI:
    app = FastAPI(title=f"{product_name} Backoffice", docs_url=None, redoc_url=None)
    app.add_middleware(SecurityHeadersMiddleware)

    if static_dir:
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    DOCS_AUTH_SECRET = os.environ.get("DOCS_AUTH_SECRET", "")

    @app.get("/login")
    def login_form(request: Request, error: str = ""):
        if auth.current_user(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": error})

    @app.post("/login")
    def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "")
        if auth.rate_limit_excedido(ip):
            return RedirectResponse("/login?error=2", status_code=303)
        if not auth.check_credentials(username, password):
            auth.registrar_intento_fallido(ip)
            return RedirectResponse("/login?error=1", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        auth.create_session_cookie(resp, username)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        auth.clear_session_cookie(resp)
        return resp

    @app.get("/health", include_in_schema=False)
    def health():
        return {"ok": True}

    @app.get("/api/clientes-publicos", include_in_schema=False)
    def clientes_publicos(request: Request):
        """Lista mínima de clientes activos para poblar el login de documentación
        en la landing. Server-to-server: requiere el secreto compartido
        DOCS_AUTH_SECRET en el header X-Internal-Auth."""
        if not DOCS_AUTH_SECRET or request.headers.get("x-internal-auth") != DOCS_AUTH_SECRET:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        clientes = [
            {"slug": c["slug"], "nombre": c["nombre"], "domain": c["domain"]}
            for c in services.listar_clientes()
            if c.get("domain") and c.get("estado") == "running"
        ]
        return JSONResponse({"clientes": clientes})

    app.include_router(clientes_router)
    return app
