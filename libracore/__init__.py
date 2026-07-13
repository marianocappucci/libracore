"""LibraCore — motor comun reutilizable para Contalibra, Restolibra y Citalibra.

Paquete interno versionado. Ver el plan de extraccion (fases 0-6) en el wiki
del proyecto para el orden y criterio de que vive aca vs. en cada producto.
"""

try:
    from importlib.metadata import version as _version

    __version__ = _version("libracore")
except Exception:
    __version__ = "0.0.0.dev0"
