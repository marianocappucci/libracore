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
