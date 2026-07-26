"""
Gating de router/endpoint por módulo habilitado según el plan comercial de
la instancia (ver `libracore.provisioning.apply_plan_modules`). Extraído
2026-07-26 de Gestiolibra/MedLibra/VentaLibra, donde era byte-idéntico
salvo comentarios — ver
wiki/analyses/auditoria-duplicacion-familia-libra.md.

No conoce ninguna tecnología de persistencia concreta: solo espera
`request.app.state.modules` con un método `is_enabled(modulo: str) -> bool`
(Gestiolibra/MedLibra usan SQLAlchemy, VentaLibra sqlite3 crudo — ambos
cumplen el mismo contrato duck-typed, sin que este módulo necesite saber
cuál).
"""
from fastapi import Depends, HTTPException, Request


def get_module_repository(request: Request):
    return request.app.state.modules


def require_module(modulo: str):
    """Dependency factory: 403 si el módulo no está habilitado para esta
    instancia (plan asignado por el provisioning)."""

    def _dependency(modules=Depends(get_module_repository)) -> None:
        if not modules.is_enabled(modulo):
            raise HTTPException(403, f"modulo '{modulo}' no incluido en el plan actual")

    return _dependency
