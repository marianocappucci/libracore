"""
Middleware de headers de seguridad, compartido por la app principal y el
backoffice de superadmin de cada producto (Contalibra, Restolibra) — mismo
set de headers para las dos, ya que ambas cargan los mismos recursos
externos (solo cdn.jsdelivr.net para Bootstrap/Bootstrap Icons, todo lo
demas self-hosted).

CSP con 'unsafe-inline' en script-src/style-src: las plantillas usan
extensivamente onclick/onsubmit y <script> inline (docenas de paginas) —
migrar todo a nonces/archivos externos es un refactor propio, no parte de
este modulo. Igual restringe lo que si importa contra XSS reflejado/
inyectado: object-src, base-uri, frame-ancestors y que origenes externos
pueden cargarse.
"""
from starlette.middleware.base import BaseHTTPMiddleware

CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "font-src 'self' https://cdn.jsdelivr.net data:; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = CSP
        # HSTS: la app siempre se sirve detras de un proxy con SSL (Let's
        # Encrypt). Si algun dia se sirve HTTP directo esto rompería ese
        # acceso, pero hoy no existe ese caso en ningun producto.
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
