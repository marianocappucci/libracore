"""Helpers para las suites de test de los productos y motores de la familia.

Sólo se importan desde tests: nada de `libracore` los usa en runtime.

- `libracore.testing.pg_por_worker`: una base de PostgreSQL por worker de xdist.
- `libracore.testing.campos_numericos_que_aceptan_booleano`: la guardia contra `true`/`false` en
  campos numéricos de los routers (ADR-013), que cada producto corre sobre su app completa.
"""

from libracore.testing.booleanos import campos_numericos_que_aceptan_booleano

__all__ = ["campos_numericos_que_aceptan_booleano"]
