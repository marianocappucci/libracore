"""
Construcción del objeto Jinja2Templates del backoffice, con el filtro
`moneda0` compartido. Migrado a libracore.admin como factory (Fase 4 de
LibraCore) — el directorio de templates en sí sigue viviendo en cada
producto (`admin/templates/`, forkeado por branding, nunca migra), así que
se recibe como parámetro en vez de resolverse vía `__file__` (que
apuntaría al paquete libracore instalado, no al repo del producto).
"""
from fastapi.templating import Jinja2Templates


def _moneda0(value):
    try:
        s = f"{float(value):,.0f}"
        return s.replace(",", ".")
    except (ValueError, TypeError):
        return str(value)


def create_templates(templates_dir: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=templates_dir)
    templates.env.filters["moneda0"] = _moneda0
    return templates
