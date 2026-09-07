"""
Middleware de headers de seguridad para las apps de la familia.

Hay **dos** CSP porque hay dos clases de frontend, y darles la misma seria
aflojar una de las dos:

- `CSP` (default) — las apps **Jinja2 + Bootstrap**: la principal y el
  backoffice de superadmin de Contalibra y Restolibra, mas los backoffices de
  `libra-backoffice` y `libra-panel`. Necesitan `cdn.jsdelivr.net`.
- `CSP_SPA` — los productos con **SPA de React/Vite**, que no cargan **nada**
  externo: todo sale del propio origen. Medido sobre `frontend/index.html` y
  `frontend/src` de los seis el 2026-09-07: ni un `https://` a otro host.
  Darles la CSP de arriba les habilitaria un CDN que no usan, que es superficie
  regalada.

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


#: Para las SPA: mismo esqueleto, sin el CDN. `'unsafe-inline'` se conserva en
#: `style-src` —los componentes inyectan estilos en linea— pero **no** en
#: `script-src`: Vite emite el JS como archivos del propio origen, asi que la
#: SPA no necesita ejecutar scripts inline y no dejarselo hacer es justo la
#: defensa que sirve contra XSS inyectado.
CSP_SPA = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Los headers de seguridad de una app de la familia.

    `csp` permite elegir la politica; el default es la de las apps Jinja2, para
    que montar el middleware sin argumentos siga haciendo exactamente lo que
    hacia antes de que existiera `CSP_SPA`.
    """

    def __init__(self, app, csp: str = CSP):
        super().__init__(app)
        self._csp = csp

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = self._csp
        # HSTS: la app siempre se sirve detras de un proxy con SSL (Let's
        # Encrypt). Si algun dia se sirve HTTP directo esto rompería ese
        # acceso, pero hoy no existe ese caso en ningun producto.
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
