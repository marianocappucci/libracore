"""LibraCore — motor comun reutilizable para Contalibra y Restolibra.

Paquete interno versionado. Ver el plan de extraccion en el wiki del
proyecto para el orden y criterio de que vive aca vs. en cada producto.
Citalibra (turnos/reservas/agendas) es un producto futuro que consumira
libracore desde el dia uno cuando arranque, pero no forma parte del plan
de extraccion actual.
"""

try:
    from importlib.metadata import version as _version

    __version__ = _version("libracore")
except Exception:
    __version__ = "0.0.0.dev0"
