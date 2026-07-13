# LibraCore

Motor comun reutilizable para la familia de productos LibraCore:
[Contalibra](https://github.com/marianocappucci/contalibra) (ERP contable),
[Restolibra](https://github.com/marianocappucci/restolibra) (gestion
gastronomica) y Citalibra (turnos/reservas/agendas, futuro).

Paquete interno privado, instalado por cada producto como dependencia via
tag de Git (no hay indice PyPI propio a esta escala):

```
libracore @ git+https://github.com/marianocappucci/libracore.git@v0.1.0
```

## Estado

En extraccion progresiva desde Contalibra/Restolibra. Ver el plan completo
(fases 0-6) en el wiki del proyecto (`wiki/entities/contalibra.md` /
memoria del proyecto) para el orden y criterio de que se extrae y cuando.

## Desarrollo

```
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Versionado

Semver via tags de Git (`vX.Y.Z`), version derivada automaticamente del tag
via `hatch-vcs` — no se edita manualmente en `pyproject.toml`. Cada producto
pinea una version exacta (`==`), nunca un rango abierto.
