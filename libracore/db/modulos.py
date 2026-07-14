"""
Módulos habilitados por plan. Extraído de database.py de Contalibra/
Restolibra (idéntico en ambos) como parte de la migración real a
libracore.db (Fase 3 de LibraCore, ver wiki/entities/libracore.md).
"""
from libracore.db.core import get_connection


def get_modulos() -> dict[str, bool]:
    """Devuelve {modulo: habilitado} para todos los módulos registrados."""
    with get_connection() as conn:
        rows = conn.execute("SELECT modulo, habilitado FROM modulos").fetchall()
    return {r["modulo"]: bool(r["habilitado"]) for r in rows}


def apply_plan(plan: str):
    """Habilita/deshabilita módulos según el plan elegido.

    El mapeo plan→módulos vive en `plans.py` en la raíz de cada producto
    (fuente de verdad compartida con el backoffice), para que lo que se
    habilita coincida con lo que se vende. Import local (no a nivel de
    módulo): `plans.py` es específico de cada producto, no vive en
    LibraCore — `import plans` resuelve al `plans.py` del producto que
    esté llamando esta función porque cada uno agrega su propia raíz de
    repo a `sys.path` al arrancar.
    """
    import plans
    activos = plans.modulos_de_plan(plan)
    with get_connection() as conn:
        rows = conn.execute("SELECT modulo FROM modulos").fetchall()
        for r in rows:
            conn.execute(
                "UPDATE modulos SET habilitado=?, plan=? WHERE modulo=?",
                (1 if r["modulo"] in activos else 0, plan, r["modulo"]),
            )
