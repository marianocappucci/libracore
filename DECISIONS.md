# Decisiones arquitectónicas — LibraCore

Registro ADR. Las decisiones no se borran; si dejan de aplicar, se marcan como
reemplazadas. Las fechas y el motivo salen del código y de la historia registrada
en el wiki del ecosistema (entidad `libracore` y sus bitácoras).

## ADR-001 — LibraCore es un motor común, no un producto

- Estado: aceptada
- Fecha: 2026-07-13
- Contexto: Contalibra y Restolibra compartían auth, PDF, configuración, ARCA,
  MercadoPago, provisioning y acceso a datos, cada uno con su copia.
- Decisión: extraer lo transversal a un paquete interno versionado (`libracore`)
  que los productos consumen y **componen**; LibraCore no expone una app propia.
- Consecuencias: una sola implementación probada para todos; el criterio de qué
  sube al motor y qué queda en el producto se discute caso por caso, no por
  conveniencia.

## ADR-002 — Integración por configuración inyectada, mínima huella en el consumidor

- Estado: aceptada
- Fecha: 2026-07-13
- Contexto: los productos ya tenían ~200 call sites de acceso a datos y su propio
  modelo de usuarios; reescribirlos para adoptar el motor sería inviable.
- Decisión: LibraCore se integra por configuración inyectada y callbacks, no
  reescribiendo call sites. `db.core.configure(db_path)` se llama una vez al
  arrancar y `get_connection()` sigue sin argumentos; el schema de usuarios y la
  lógica de recetas entran por callback (`configure_resolver_receta`).
- Consecuencias: un producto adopta o retira una pieza del motor sin tocar el
  resto de su código; es el principio que atraviesa todo el paquete.

## ADR-003 — Una sola capa de acceso a datos, dual SQLite/PostgreSQL

- Estado: aceptada
- Fecha: 2026-08 (capa `db/_postgres.py`)
- Contexto: la familia migró a PostgreSQL, pero el motor tiene que poder abrir
  también SQLite (bases legadas, la base de LibraEdge, el `schema_dump`).
- Decisión: mantener **una** capa de acceso (`libracore.db.*`) escrita en estilo
  `sqlite3` (placeholders `?`, `Row`, excepciones de `sqlite3`) y un wrapper
  (`db/_postgres.py`) que traduce SQL, placeholders y errores contra PostgreSQL.
- Consecuencias: los consumidores escriben SQL una vez; la capa dual no es deuda,
  es lo que permite `schema_dump` y la compatibilidad. Se paga con la
  complejidad del traductor (737 líneas).

## ADR-004 — La restricción PostgreSQL-only vive en el producto, la capacidad en el motor

- Estado: aceptada
- Fecha: 2026-08-25
- Contexto: la familia decidió PostgreSQL-only en producción/dev/tests
  (2026-08-12), pero el motor necesita seguir abriendo SQLite para
  `schema_dump`, migraciones sobre bases viejas y el nodo LibraEdge.
- Decisión: `db.core.configure()` sigue siendo **neutral** (acepta ruta SQLite o
  URL PostgreSQL); la guarda que rechaza SQLite se pone en el **arranque de cada
  producto**, no dentro del motor.
- Consecuencias: la regla "este producto no habla con otro motor" es del
  producto; el motor conserva la capacidad que necesitan las herramientas.

## ADR-005 — El alias `Conexion` y la traducción de errores por nombre, no por comportamiento

- Estado: aceptada
- Fecha: 2026-08-25
- Contexto: las funciones de `db/*` estaban anotadas `sqlite3.Connection` y contra
  PostgreSQL reciben el wrapper; leer la anotación llevó a escribir un arreglo con
  dialecto PostgreSQL que habría roto la corrida SQLite (incidente en VentaLibra).
- Decisión: introducir el alias `Conexion = sqlite3.Connection | ConnectionWrapper`
  y traducir las excepciones de psycopg a las de `sqlite3` **por nombre**
  (`_errores_como_sqlite3`).
- Consecuencias: la API tiene la misma forma en los dos motores. Pero la
  traducción **no** cubre el comportamiento transaccional: en PostgreSQL un error
  aborta la transacción y en SQLite no, así que los reintentos se auditan caso por
  caso (se encontró uno roto el 2026-08-25).

## ADR-006 — Las migraciones viajan dentro del wheel

- Estado: aceptada
- Fecha: 2026-08-25 (`v1.53.0`)
- Contexto: `migrations/` vivía en la raíz del repo, fuera de `packages`, así que
  no viajaba al contenedor: de 14 bases con schema de LibraCore, 7 no tenían
  `alembic_version` y 2 quedaron en `0001_baseline`.
- Decisión: mover `migrations/` **adentro** de `libracore/` y exponer el runner
  como console script `libracore-migrar` y como API (`from libracore.migrar
  import upgrade`), resolviendo el destino con `url_de_core`, no con
  `DATABASE_URL` a secas.
- Consecuencias: un consumidor instalado con pip aplica las migraciones sin clonar
  el repo; el deploy puede correrlas.

## ADR-007 — Un solo criterio para "esto es una URL, no un archivo"

- Estado: aceptada
- Fecha: 2026-08-09
- Contexto: el provisioning, el panel de admin y los scripts de backup recibían
  "la base" como un string que podía ser ruta o URL, y cada uno decidía por su
  cuenta — de ahí salieron el defecto del backup (trataba el nombre como ruta) y
  el del plan de módulos.
- Decisión: centralizar el criterio en `db.core.es_url_postgres()` y abrir sin
  estado global con `db.core.conectar(destino)`, del que `get_connection()`
  delega.
- Consecuencias: el código que trabaja sobre una instancia ajena (provisioning,
  backup) funciona igual contra SQLite y PostgreSQL.

## ADR-008 — El provisioning se consume por atributo; F401 se tolera en los re-exports

- Estado: aceptada
- Fecha: 2026-09-02 (unificación de ruff, E2)
- Contexto: `libracore.admin.services` hace `import panel_admin as pa` y llama
  `pa.find_client(...)`; un `ruff --fix` de F401 sobre los backoffices borraría
  esos re-exports "sin usar" y rompería la carga por atributo.
- Decisión: dejar F401 en el ignore para los re-exports del backoffice en vez de
  reescribir el patrón de consumo.
- Consecuencias: el lint no rompe el mecanismo; a cambio, esos módulos no reciben
  el chequeo de imports sin uso.

## ADR-009 — Hora de Argentina fijada en el motor y en el sidecar, no sólo con `TZ`

- Estado: aceptada
- Fecha: 2026-08-23
- Contexto: todo sistema de la familia arranca en UTC-3 fijo; pero `TZ` en la
  imagen sólo cambia el `date` del contenedor, no el `now()` del servidor
  PostgreSQL (se graba en el `initdb`, una sola vez).
- Decisión: estampar los timestamps del backend con `_ar_now()` de LibraCore y
  fijar la zona en el **sidecar** (`postgres -c timezone=...`), midiendo con
  `select now()` y no con `docker exec date`.
- Consecuencias: base y proceso quedan en la misma hora; la presentación en
  `dd-mm-aaaa` es una capa aparte.

## ADR-010 — El hook de recetas es opcional e inyectado

- Estado: aceptada
- Fecha: 2026-07 (extracción inicial)
- Contexto: Restolibra descuenta stock por receta (ingredientes) y Contalibra
  descuenta el producto vendido; `descontar_stock_venta` es común.
- Decisión: exponer `configure_resolver_receta(resolver)` — `None` = comportamiento
  simple (Contalibra); Restolibra inyecta un callable `(producto_id) -> receta`.
- Consecuencias: el motor no conoce el concepto "receta"; el vertical que lo
  necesita lo aporta, sin que el otro lo arrastre.

## ADR-011 — Cierre diario: acto registrado por sucursal, guarda en `create_turno`, sin autorización propia

- Estado: aceptada
- Fecha: 2026-09-13
- Contexto: VentaLibra y LibraClub necesitan cerrar el día operativo de una
  sucursal (todas sus cajas y turnos) con comprobante impreso, de forma
  numerada y no repetible. Las sucursales viven en la base del PRODUCTO;
  `cajas.sucursal_id` ya era un entero sin FK desde antes (ver la nota de
  `schema.py`).
- Decisión, en sus partes:
  - **Es un acto registrado**: `cierres_diarios` guarda una FOTO (por turno,
    por caja y de la sucursal) tomada al momento del cierre — reimprimir no
    vuelve a leer `caja_movimientos`, así que un movimiento anulado después
    no le cambia el comprobante ya entregado.
  - **La unicidad es por EXPRESIÓN** (`COALESCE(sucursal_id,0), fecha` y
    `..., numero`), no por columna lisa: ni SQLite ni PostgreSQL consideran
    que dos `NULL` colisionen, y una sucursal-NULL (turnos sin caja, o cajas
    sin sucursal — el caso de datos viejos, o de cualquier producto que
    todavía no terminó de asignar cajas a sucursales) es un caso real que
    tiene que poder cerrar una vez y no dos.
  - **El día operativo es el de la APERTURA del turno**, en hora Argentina
    (`_ar_now()`/`_AR_TZ`): un turno de 22:00 a 03:00 pertenece al día en que
    abrió. Se filtra con `substr(apertura,1,10)` — `apertura` es TEXT, no una
    columna de fecha, mismo criterio que `PARTE_TURNOS` de `db/logs.py`.
  - **La guarda de apertura vive en `turnos.create_turno()`**, no en cada
    producto: es el camino común de VentaLibra y LibraClub (los dos abren
    turnos llamando a esta función), así que los cubre a los dos sin que
    ninguno haya tocado su alta de turnos. Para un producto sin sucursales la
    tabla `cierres_diarios` queda vacía y la guarda es un no-op. Si la tabla
    todavía no existe (código desplegado antes de correr la migración —
    "el deploy no corre migraciones solo" es una regla ya vigente de esta
    familia), la guarda no revienta: se comporta como si nunca se hubiera
    cerrado nada, igual que antes de esta versión.
  - **La numeración correlativa por sucursal reintenta con una conexión
    nueva por intento** (mismo patrón que `facturas.crear_factura`): en
    PostgreSQL un error aborta la transacción, así que no se puede seguir
    escribiendo sobre la misma conexión después de un `IntegrityError`. Cada
    reintento repite las validaciones desde cero, así que una colisión real
    (el día ya se cerró) no se reintenta a ciegas: se convierte en el error
    correcto en el siguiente intento.
  - **El motor recibe `usuario_id` y NO autoriza.** Quién puede cerrar el día
    lo decide el producto — hoy (2026-09-13) la respuesta es "admin o
    cajero", pero con el nombre de rol que tenga cada uno. La factory de
    router (`caja_router.build_cierre_diario_router`) expone un parámetro
    `autorizar_cierre` inyectable (una dependencia de FastAPI) para el
    endpoint de cierre en particular; no hay ningún `require_admin` fijo
    adentro del motor, y sin ese parámetro el endpoint no impone ningún rol
    propio.
  - **El motor no conoce sucursales por nombre.** Los tickets y el router
    reciben `sucursal_nombre` (y, para el ticket de turno, también lo resuelve
    el producto) porque esa tabla vive del lado del dominio; `caja_nombre` sí
    lo resuelve el motor, porque `cajas` es suya.
  - **Las tres tablas nuevas NO entran a `init_core_schema()`** (congelada
    desde la `0001`): su DDL vive en `db/cierre_diario.crear_tablas()` y lo
    aplica la migración `0009_cierre_diario` — mismo criterio que toda
    revisión posterior a la baseline.
  - **El ticket de un turno se imprime EN VIVO, antes de que exista cualquier
    cierre diario** — es el caso normal: el cajero cierra su turno y lo
    imprime en el momento. `arqueo_turno_en_vivo()` calcula el mismo arqueo
    que la foto, con la MISMA función de medios y de diferencia
    (`_medios_de_turno`/`_diferencia`) — no una segunda cuenta. El endpoint
    (`GET /turno/{turno_id}/ticket`, por el id REAL del turno, no por el de
    la foto) resuelve solo cuál de los dos casos aplica: si el turno ya entró
    a una foto la usa, si no lo calcula en vivo, y devuelve 409 si el turno
    sigue abierto.
  - **`sucursal_id=None` es SIEMPRE "sin sucursal"**, en las cuatro funciones
    del módulo por igual. La primera versión de esta ADR dejaba
    `listar_cierres()` como la excepción (`None` = "todas", por calco de
    `db.caja.get_all_cajas()`) — una misma llamada sin argumentos quería decir
    cosas distintas según qué función la recibiera. "Todas" ahora es
    `listar_cierres(todas=True)`, explícito.
  - **El `except` de la tabla ausente es angosto por mensaje, no por clase
    sola.** `dia_cerrado()` atrapa `OperationalError`/`ProgrammingError` de
    `sqlite3` y sólo se los traga si el TEXTO dice "no such table" o "does
    not exist" mencionando `cierres_diarios` — porque la traducción de
    PostgreSQL (`_postgres._equivalente_sqlite3`) sube el MRO de
    `UndefinedTable` (SQLSTATE 42P01) y no aterriza en `OperationalError`
    sino en `ProgrammingError`, sin conservar el SQLSTATE original. Un
    `ProgrammingError` real por otro motivo (una conexión cerrada, por
    ejemplo) no menciona la tabla y se propaga.
- Consecuencias: los productos cablean su UI y su selección de sucursal sobre
  esta API sin haber tenido que cambiar su alta de turnos ni su modelo de
  autorización. El costo es una tabla más para auditar (`cierres_diarios_medios`,
  con tres niveles distinguidos por qué FK llevan puesta) y un caso adicional
  de "tabla que puede no existir todavía" que el motor tiene que tolerar en su
  camino más transitado.
