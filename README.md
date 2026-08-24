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

## El vocabulario de medios de pago

Desde `v1.50.0` la lista de medios de pago de la familia vive en
`libracore.medios_pago`, y es **la unica**. Antes estaba declarada 28 veces en
11 repos y ya divergia en seis formas distintas; el inventario completo esta en
`wiki/concepts/medios-de-pago-familia-libra.md`.

Dos listas, y la diferencia es la que importa:

- **`ELEGIBLES`** — lo que se puede elegir hoy. Es lo unico que puebla un
  selector y lo unico que `validar()` acepta al escribir.
- **`HISTORICOS`** — grafias que quedaron en filas de bases reales (`tarjeta`,
  `mercado_pago`, `debito`, `credito`, `qr`, `otro`, `cuenta corriente`) y que
  hay que **saber leer**. No se ofrecen mas.

> 🔴 **Un historico no se saca nunca.** Sacarlo no borra la fila que lo tiene:
> la deja sin etiqueta, y un cierre de caja con un bucket sin nombre es peor que
> uno con un nombre viejo. Si algun dia se migran los datos, primero se migran y
> despues se saca la grafia — nunca al reves. Hay un test que se pone rojo si se
> intenta.

La API:

| | |
|---|---|
| `label(medio)` | Como se muestra. **Nunca devuelve vacio**: un medio desconocido sale tal cual, para que se pueda ver. |
| `canonico(medio)` | El elegible equivalente, para que un reporte no muestre `mercado_pago` y `mercadopago` como dos filas. |
| `validar(medio)` | El medio, o `MedioDePagoInvalido`. **Falla cerrado.** |
| `es_elegible(medio)` | Si se puede elegir hoy. Los historicos dan `False`. |
| `para_selector()` | `[{id, label}]`. Con `incluir_cuenta_corriente=False` para las pantallas que cobran. |
| `sql_es_electronico(col)` | Fragmento SQL de "se pago por QR o billetera". |

`db.caja.MEDIOS_PAGO_LABELS` sigue existiendo como alias de `ELEGIBLES`, asi que
los productos que ya lo importan heredan los medios nuevos sin tocar una linea.

> ⚠️ **Una instancia que ya existe conserva los medios de su caja.** El
> movimiento se registra igual —`create_caja_movimiento` no valida contra esa
> lista— y el cierre lo agrupa bien, pero el selector de esa caja no ofrece los
> nuevos hasta que alguien los agregue desde Cajas. Es configuracion del
> comercio, no algo que se pise solo.

## Desarrollo

```
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Migraciones de schema

El schema del core lo construye `init_core_schema()`, y desde la revision
`0001_baseline` esa funcion esta **congelada**: todo cambio de schema posterior
va como revision de Alembic, no como linea agregada ahi. El congelamiento lo
sostiene `tests/db/test_schema_congelado.py`, que compara el resultado de la
funcion contra una fixture por motor.

Las migraciones **no viajan en el wheel de pip**. La forma reproducible de
aplicarlas contra la base de un consumidor es clonar el tag que ese consumidor
pinea y correr el script:

```
LIBRACORE_REF=v1.19.0 DATABASE_URL=postgresql://user:pass@host/db \
  ./scripts/run_migrations.sh
```

`DATABASE_URL` acepta tambien la **ruta del archivo SQLite** de la instancia.
Se puede correr contra una instancia que ya existe: la baseline llama a
`init_core_schema()`, que es idempotente, asi que hace lo mismo que un arranque
de la app y ademas registra la version. Backup antes igual — es una operacion
de schema.

Alembic y SQLAlchemy **no son dependencias de runtime**: viven en el extra
`migrations`, que el script instala y que `[dev]` arrastra para los tests.

> `alembic revision --autogenerate` **no sirve en este repo**: no hay modelos
> SQLAlchemy de los que generar. Las revisiones se escriben a mano.

## Versionado

Semver via tags de Git (`vX.Y.Z`), version derivada automaticamente del tag
via `hatch-vcs` — no se edita manualmente en `pyproject.toml`. Cada producto
pinea una version exacta (`==`), nunca un rango abierto.
