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
  `debito`, `credito`, `qr`, `otro`, `cuenta corriente`) y que hay que **saber
  leer**. No se ofrecen mas.

> 🔴 **Un historico no se saca sin migrar los datos primero.** Sacarlo no borra
> la fila que lo tiene: la deja sin etiqueta, y un cierre de caja con un bucket
> sin nombre es peor que uno con un nombre viejo. Hay un test que se pone rojo
> si se intenta, y ese rojo es la pregunta "¿ya migraste?".
>
> Se recorrio una vez, con `mercado_pago` (la grafia de VentaLibra) el
> 2026-08-25: medir sobre las instancias reales recorriendo TODAS las columnas
> de texto, migrar, verificar cero **despues** de desplegar, y recien ahi sacarla
> — de `HISTORICOS`, de `EQUIVALENTE_CANONICO` y de `MEDIOS_ELECTRONICOS`. El
> orden completo esta en el docstring del modulo.

La API:

| | |
|---|---|
| `label(medio)` | Como se muestra. **Nunca devuelve vacio**: un medio desconocido sale tal cual, para que se pueda ver. |
| `canonico(medio)` | El elegible equivalente, para que un reporte no muestre `tarjeta` y `tarjeta_credito` como dos filas. |
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

## Unir las dos bases de un producto

Los productos de la familia corren con **una** base (ContaLibra, RestoLibra, VentaLibra, LibraDesk, LibraCargo) o con **dos**: la
del dominio y la del core (`<producto>_core`, con el esquema de este motor). `libracore-unir-bases` pasa de dos a una (ADR-078): crea
en la base del dominio las tablas del motor, copia las filas del core **conservando los ids** y deja un informe con el que se
compara. Lo usa LibraClub; GestioLibra y MedLibra, cuando resuelvan lo que les falta (ADR-078, «Consecuencias»).

```
libracore-unir-bases --prefijo libraclub verificar --informe verificacion.json   # sólo lectura
libracore-unir-bases --prefijo libraclub unir      --informe union.json          # con la app detenida
libracore-unir-bases --prefijo libraclub revertir  --informe union.json          # la red de los primeros días
```

Las URL salen de las variables de la instancia (`LIBRACLUB_DATABASE_URL` y `LIBRACLUB_LIBRACORE_DATABASE_URL`, los nombres de
`url_de_instancia`), o de `--core`/`--dominio` (una URL, o `env:NOMBRE` para leer otra variable: una URL con contraseña en la línea
de comandos queda en el historial y en `ps`). **Nunca se imprime usuario ni contraseña**, sólo `host:puerto/base`. Sale 0 (bien),
1 (algo impide unir, o la unión se deshizo) o 2 (uso o conexión). Se corre **dentro de la imagen del producto**, que tiene
`libracore` y, si el core usa su cadena, `libracommerce`.

### Procedimiento de operación

Instancia por instancia, cada una con su OK. Nada de esto se hace desde el motor: es lo que se ejecuta en el servidor de la instancia.

1. **Antes de empezar.** El producto ya tiene su tabla de versión propia (`alembic_version_<producto>`; si su cadena todavía usa
   `alembic_version`, `verificar` lo corta), la imagen trae `libracore` ≥ v1.190.0 (y `libracommerce` si el core tiene
   `alembic_version_libracommerce`), y el core está en la cabeza (`libracore-migrar upgrade --prefijo <p>`, y `libracommerce-migrar`).
2. **Detener la app** (sólo la app; las bases siguen arriba): `docker compose stop <servicio-app>`. Sin la app no hay conexiones
   abiertas; `unir` y `revertir` cortan si las hay (`--aunque-haya-conexiones` lo saltea: sólo con una razón).
3. **Respaldar las dos bases con `pg_dump -Fc` y probar la restauración.** Un respaldo que no se restauró no es un respaldo:

   ```
   pg_dump -Fc -f dominio.dump  "$DOMINIO_URL"          # y lo mismo con el core
   createdb prueba_dominio && pg_restore -d prueba_dominio dominio.dump
   # comparar conteos de las tablas grandes contra la base viva, y borrar la de prueba
   ```
4. **Verificar**: `libracore-unir-bases --prefijo <p> verificar --informe verificacion.json`. Hasta que no diga `[OK] Se puede unir`,
   no se sigue. Los impedimentos más comunes:

   | Código | Qué quiere decir | Qué se hace |
   |---|---|---|
   | `version_repetida` | la `alembic_version` del dominio es de la cadena del producto | el producto la renombra (`alembic_version_<producto>`) y se despliega |
   | `choque_de_tablas` | una tabla del producto tiene el nombre de una del motor (`modulos`, `depositos`) | se renombra en el producto |
   | `fk_colgada` | filas del core apuntan a algo que no estará en el dominio (típico: `usuario_id` sin fila en `usuarios`) | se resuelve el dato antes; no se borra el core |
   | `usuario_distinto` | un id de usuario es otra persona en el dominio | se revisa a mano: no se mezclan usuarios |
   | `core_desactualizado` / `core_sin_version` | el core no está en la cabeza de `libracore` | `libracore-migrar upgrade --prefijo <p>` sobre el core |
   | `falta_libracommerce` | el core usa la cadena de `libracommerce` y el entorno no la tiene | correr desde la imagen del producto |
   | `destino_con_datos` | el dominio ya tiene filas en una tabla del motor que no son las semillas | alguien ya usó el dominio con el motor: se mira antes de seguir |
5. **Unir**: `libracore-unir-bases --prefijo <p> unir --informe union.json`. Guardar `union.json` junto a los respaldos. Es
   idempotente: correrlo de nuevo sobre una base ya unida no cambia nada y lo dice. Si se corta a mitad, la transacción no deja
   nada copiado y se reintenta.
6. **Cambiar el compose** (con una copia del anterior): la variable del core (`<PREFIJO>_LIBRACORE_DATABASE_URL`) apunta a la base
   **del dominio**. Cuando el producto figure en `_UNA_SOLA_BASE` (otro PR), la variable se saca.
7. **Levantar** la app y **comparar con el informe**: los conteos y las sumas de `union.json` contra las tablas del dominio, la
   pantalla de inicio sin errores, y una operación de caja de prueba en una transacción que se revierte.
8. **El core viejo se conserva intacto** (es la red de `revertir`). Se borra recién con un OK aparte, tras un ciclo de uso.

### Qué copia y qué no (ADR-078)

- **Se copian todas las tablas del core** menos las de versión de Alembic y las de auth. Las tablas del motor y de `libracommerce` se
  crean en el dominio con sus cadenas (`libracore-migrar`, `libracommerce-migrar`), y las filas se copian con `COPY`: el texto exacto,
  sin redondear `NUMERIC` ni reescribir fechas. Después, `setval` de cada secuencia a `max(id)`.
- **`usuarios`, `auth_log` y las tablas de `libraauth` no se copian: manda el dominio** (en ContaLibra y VentaLibra es una sola
  tabla, la que llegó primero). Se exige que cada `usuario_id` del core exista en el dominio y sea la misma persona (mismo
  `username`).
- **Semillas.** `cajas` («Caja Principal»), `depositos` («Depósito Principal») y `categorias_egreso` (diez) las siembra la baseline
  al crear las tablas en el dominio: si lo único que hay son esas filas, se vacían antes de copiar las del core. Cualquier otra
  fila en el destino es un dato y corta la unión.
- **Una marca en el dominio**, `libracore_union_bases` (una fila): `iniciada` con la foto de las tablas que tenía, y `unida` con
  el informe. Los `alembic revision --autogenerate` de los productos tienen que ignorarla, como ya ignoran las del motor.

### `revertir`: qué hace y qué no

Devuelve al core las filas que el dominio recibió **después** de unir (las de `id` mayor al máximo del informe), con los mismos ids,
en una transacción y ajustando las secuencias del core. Antes comprueba que el core viejo sigue **idéntico** al informe; si alguien
lo usó, se niega. Límites: lo que se *cambió o borró* en una fila vieja después de unir no vuelve; en las tablas sin `id` vuelven las
filas con clave nueva, no los cambios; las filas que apunten a un usuario creado después de unir hacen fallar la clave foránea del
core y **no se devuelve nada** (se crea antes ese usuario en el core); no deshace nada en el dominio, así que después se vuelve a
apuntar el compose a las dos bases. Es una red para los primeros días, no un modo de operar con dos bases.

## Versionado

Semver via tags de Git (`vX.Y.Z`), version derivada automaticamente del tag
via `hatch-vcs` — no se edita manualmente en `pyproject.toml`. Cada producto
pinea una version exacta (`==`), nunca un rango abierto.
