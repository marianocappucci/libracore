"""Helpers para las suites de test de los productos y motores de la familia.

Sólo se importan desde tests: nada de `libracore` los usa en runtime.

- `libracore.testing.pg_por_worker`: una base de PostgreSQL por worker de xdist.
- `libracore.testing.campos_numericos_que_aceptan_booleano`: la guardia contra `true`/`false` en
  campos numéricos de los routers (ADR-013), que cada producto corre sobre su app completa.
- `libracore.testing.cuerpos_sin_tipar`: su guardia hermana (ADR-015), informativa: lista los
  cuerpos que aceptan cualquier cosa (`dict`, `Any`, `extra="allow"`, `request.json()` a mano), que
  el humano revisa y el producto fija con un comentario por entrada.
"""

from libracore.testing.booleanos import campos_numericos_que_aceptan_booleano
from libracore.testing.sin_tipar import cuerpos_sin_tipar

__all__ = ["campos_numericos_que_aceptan_booleano", "cuerpos_sin_tipar"]
