# Varios procesos por instancia (`uvicorn --workers N`)

Hasta ahora el motor suponía **un solo proceso por instancia** (`uvicorn` sin `--workers`). Desde ADR-044 el código está
listo para `WEB_CONCURRENCY` > 1. **Desde el 2026-10-17 toda la suite corre con 2** (decisión del dueño): dev por el
`.env` de cada checkout, demos y clientes por el compose de la instancia (`WEB_CONCURRENCY=2`; demos con 1 CPU, clientes
con 2), y el alta (`provisioning/nuevo_cliente.py`) ya escribe las dos cosas para una instancia nueva.
Esta página es qué se rompe con N procesos, cómo lo cubre el motor y qué le toca a cada producto.

## Qué hace el motor

| Qué se rompía con N procesos | Cómo queda | Dónde |
|---|---|---|
| La nota de crédito «una a la vez» era un `set` en memoria: un doble clic caía en dos procesos y **emitía dos notas ante ARCA** | Con `conn`, un candado de la base (`pg_try_advisory_xact_lock`) además del de memoria | `notas_de_credito.una_nota_a_la_vez(clave, conn)` / `emitir_nota_de_credito(..., conn=)`; el router de facturas del motor ya lo pasa |
| Un hook de arranque con «si no existe, crear» (sembrar el admin, `ensure_seeded`) corría en cada worker y creaba dos | Se serializa: el segundo espera al primero y ve lo que dejó | `candados.una_vez_al_arrancar(nombre)` |
| `init_core_schema()` —la creación idempotente del esquema que cada producto corre al arrancar— es una transacción larga de DDL y escrituras: dos workers juntos daban `deadlock detected` (base armada) o `duplicate key … pg_class_relname_nsp_index` (base vacía) | Se serializa con un candado de transacción **bloqueante** tomado por la propia función: el segundo espera al `commit` del primero y corre sobre lo que dejó | `candados.esperar_candado_de_transaccion` (ADR-053), llamado al principio de `db.schema.init_core_schema` |
| `config.json` se escribía con `open(..., "w")`: otro proceso lo leía vacío y `load()` devolvía los **defaults** sin avisar | Escritura atómica (temporal en el mismo directorio, `fsync`, `os.replace`) | `config_manager._escribir_atomico` |
| `respaldo_postgres._generacion` (los pools que descartan conexiones viejas tras un restore) es del proceso | Documentado: **reiniciar el contenedor** después de un restore en caliente | ver abajo |

## Candados (`libracore.candados`)

```python
from libracore.candados import candado_de_base, una_vez_al_arrancar

with candado_de_base(conn, "reposicion:42") as obtenido:     # no bloquea
    if not obtenido:
        raise Conflicto("ya hay una en curso")
    ...

with una_vez_al_arrancar("sembrar-admin"):                   # bloquea hasta que el otro termina
    if not hay_admin():
        crear_admin()
```

- `candado_de_base`: en PostgreSQL **dura hasta que termina la transacción de `conn`**, no hasta el fin del `with`; quien lo
  usa no hace `commit`/`rollback` de `conn` mientras dure lo protegido. En SQLite, un `flock` en `DATA_DIR` que dura lo que
  el `with`; con `conn=None`, un candado del proceso. `conn` puede ser la conexión de `libracore.db`, `psycopg` o una `Session`
  / `Connection` de SQLAlchemy.
- `una_vez_al_arrancar`: conexión propia, `pg_try_advisory_lock` reintentado hasta `espera` (120 s) y `TimeoutError` si no
  se consigue. Si el proceso muere con el candado, PostgreSQL lo suelta con la sesión.
- `esperar_candado_de_transaccion(conn, nombre)`: la cara **bloqueante** de `candado_de_base` (PostgreSQL, `pg_try_advisory_xact_lock`
  reintentado hasta `espera`, 120 s, y `TimeoutError`). Dura hasta el `commit`/`rollback` de `conn`; va lo primero de la
  transacción y `conn` no puede estar en `autocommit`. Devuelve `False` (y no hace nada) si `conn` no es PostgreSQL. Lo usa
  `init_core_schema()`; un producto sólo lo necesita para una transacción larga propia que dos procesos no puedan correr a la vez.
- La clave es un entero de 56 bits derivado del nombre con SHA-256 (como en `libracommerce`). Los candados asesores son **por
  base**: dos nombres iguales en dos bases no se ven.
- No reemplazan a una restricción única: evitan el trabajo duplicado (una nota ante ARCA); la restricción es la red de abajo.

## Qué le toca a cada producto

0. **Crear el esquema (`init_core_schema`)**: nada. Está serializada por dentro desde ADR-053 (libracore v1.168.0), así que el
   `configure()` de cada producto (`billing.py`, `libracore_setup.py`, `database.py`) ya es seguro con N workers: sólo hay que subir
   el pin. Lo que **no** cubre es el DDL **propio** del producto (el `Base.metadata.create_all(...)` de LibraGenda en
   MedLibra y Gestiolibra, el `init_schema()` de LibraCommerce): si corre al arrancar sobre una base vacía, va dentro de un
   `una_vez_al_arrancar`.
1. **Hooks de arranque**: envolver en `una_vez_al_arrancar("<nombre>")` lo que hoy es «si no existe, crear» (contalibra y
   restolibra `app/web/app.py`, gestiolibra/medlibra/libradesk `ensure_seeded`, libracargo/libraclub `sembrar_admin`). Tiene que
   seguir preguntando adentro del `with`.
2. **Nota de crédito**: pasar `conn=` a `emitir_nota_de_credito` con la conexión de **la transacción donde escribe la nota**, y
   no hacer `commit`/`rollback` hasta que termine. Si escribe con conexiones que abre y cierra por llamada, una conexión
   aparte abierta durante toda la emisión (como hace el router de facturas del motor).
3. **Captcha** (`libraauth`): `UsadosEnBase`.
4. **Engines**: `pool_pre_ping=True` (ver restore).
5. **Compose**: `--workers ${WEB_CONCURRENCY:-1}` y `uvicorn>=0.30` como piso.

## Restore en caliente con más de un worker

`respaldo_postgres.vigilar_pools()` descarta, al sacarlas del pool, las conexiones abiertas antes del último intercambio, pero
`_generacion` es una variable del proceso que atendió el restore: **los otros workers no se enteran**. No se resuelve con una
señal (SIGHUP no hace nada útil en uvicorn). Dos reglas:

- **Después de un restore en caliente, reiniciar el contenedor** (`docker compose restart <servicio>`); es lo que cierra el hueco.
- **Los engines del producto llevan `pool_pre_ping=True`**: el pool prueba la conexión al sacarla y descarta la muerta en lugar
  de entregarla, así que el worker que no se enteró se recupera solo en vez de dar 500 hasta el reinicio. El motor no crea
  engines de aplicación (el único, `migrations/env.py`, usa `NullPool`).

## Lo que sigue siendo por proceso (y está bien)

- Las caches `lru_cache` de `geografia` y `feriados` (datos estáticos).
- El conjunto en memoria de `una_nota_a_la_vez`: es la primera capa, no la única.
- El orden «cargar → modificar → guardar» de `config_manager` entre dos admins que editan a la vez sigue siendo
  *last write wins* (pierde una edición, no corrompe el archivo).

## Cómo se prueba

`libracore.testing.varios_procesos` levanta `uvicorn --workers 2` de verdad (`servidor()`, `en_paralelo()`, `pids_vistos()`),
y `libracore.testing.app_varios_procesos` es la app mínima. `tests/test_varios_procesos.py` (marcado `varios_procesos`, ~15 s,
necesita `LIBRACORE_POSTGRES_URL`): 20 pedidos de nota para la misma factura repartidos entre los dos procesos ⇒ **1** emitida;
el hook de arranque deja **1** fila con los dos workers vivos. Cada uno con su contraprueba (sin el candado, 2).
