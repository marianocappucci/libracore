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

## Lo que este paquete NO hace: autenticacion

Desde `v1.0.0` (2026-07-30) el auth **no vive mas aca**. `libracore.auth`
(`SessionAuth`/`AdminAuth`) y `libracore.db.usuarios` (`UserRepository`,
`ensure_default_admin`, `ensure_admin_user`) se movieron a
[libraauth](https://github.com/marianocappucci/libraauth), motor propio sobre
SQLAlchemy que los 6 productos de la familia ya consumen. Es un cambio mayor:
un producto que todavia importe esos modulos no funciona con `v1.0.0`.

La **tabla** `usuarios` sigue siendo de LibraCore y no se toco: vive en
`db/schema.py` porque 12 tablas del motor declaran
`usuario_id REFERENCES usuarios(id)`, y en Contalibra/Restolibra esa tabla y
las que la referencian comparten el mismo archivo SQLite. Lo que salio es el
codigo de auth, no el schema.

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
