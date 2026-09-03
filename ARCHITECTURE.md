# Arquitectura — LibraCore

## Propósito y límites

LibraCore es el **motor común** de la familia Libra: el paquete Python interno
(`libracore`, `requires-python >=3.12`) del que dependen los ocho productos
verticales. No es un producto ni expone una app propia — cada vertical arma su
propia app FastAPI y **compone** las piezas de LibraCore que necesita.

Lo que vive acá es lo transversal y probado una sola vez para todos: el acceso a
la base de datos dual SQLite/PostgreSQL, la facturación electrónica ARCA (AFIP),
la integración con MercadoPago, el aprovisionamiento de instancias (panel de
admin, alta de clientes), la generación de PDFs/tickets, las migraciones del
schema común, y un puñado de routers FastAPI listos para montar. Lo que **no**
vive acá es la lógica de negocio propia de cada vertical: recetas y comandas
(Restolibra), historia clínica (MedLibra), catálogo comercial (Gestiolibra). El
criterio de qué sube al motor y qué queda en el producto es explícito y se
discute caso por caso, no por conveniencia.

El principio de diseño que atraviesa todo el paquete es **mínima huella en el
consumidor**: LibraCore se integra por configuración inyectada y callbacks, no
reescribiendo los call sites del producto. `libracore.db.core.configure()` se
llama una vez al arrancar y los ~200 call sites de cada producto siguen llamando
`get_connection()` sin argumentos; `SessionAuth` recibe un callback en vez de
asumir el schema de usuarios; el hook de recetas (`configure_resolver_receta`)
deja que Restolibra inyecte su lógica sin que Contalibra la conozca. Un producto
adopta o retira una pieza de LibraCore sin tocar el resto de su código.

## Componentes

### `libracore.db` — acceso a datos dual (30 módulos)

El subsistema más grande y el más consumido (90 sitios de import contando el
paquete; `libracore.db.core` sólo, 29). Es una capa de acceso a datos con
funciones por dominio (`ventas`, `caja`, `stock`, `cuenta_corriente`,
`facturas`, `libros_iva`, `recibos`, `remitos_presupuestos`, `tesoreria`,
`turnos`, `productos`, `listas_precio`, `clients`, `logs`, `reportes`,
`dashboard`, `resumen`…), todas escritas contra una API estilo `sqlite3`
(placeholders `?`, `Row`, excepciones de `sqlite3`).

- **`db/core.py`** es el corazón. `configure(db_path, *, timeout, extra_pragmas)`
  fija, una sola vez por proceso, contra qué base trabaja `get_connection()`.
  El `db_path` puede ser una ruta SQLite **o** una URL PostgreSQL: `core` es
  **neutral respecto del motor** a propósito. `conectar(destino, ...)` es la
  versión sin estado global —la que usan el provisioning, el panel de admin y
  los scripts de backup, que trabajan sobre una instancia que no es la suya— y
  `get_connection()` delega en ella: abrir una conexión es lógica de un solo
  lugar. `is_postgres()` y `es_url_postgres(destino)` centralizan el criterio de
  "esto es una URL, no un archivo" (antes cada script lo decidía por su cuenta, y
  de ahí salieron los defectos del backup y del plan de módulos). También viven
  acá las utilidades de fecha/hora en zona Argentina (`_ar_now`, `minutos_desde`)
  y el hook opcional `configure_resolver_receta`/`get_resolver_receta`.

  > La guarda que **rechaza** SQLite en producción **no** está en `core` — está
  > en el arranque de cada producto. Es deliberado: el motor tiene que poder
  > abrir un SQLite igual, porque de eso viven `schema_dump` y las migraciones
  > sobre bases viejas o la de LibraEdge. La regla "este producto no habla con
  > otro motor" es del producto, no del motor.

- **`db/_postgres.py`** (737 líneas) es la capa de compatibilidad que hace real
  el "estilo `sqlite3`" contra PostgreSQL. Traduce el SQL sobre la marcha:
  placeholders `?`→`%s` (`_paramstyle`, `_replace_qmarks`), `strftime`
  (`_traducir_strftime`), `round` (`_castear_round`), `%` literales
  (`_escapar_porcentajes`), FKs diferidas (`_diferir_fks_hacia_adelante`);
  expone un `Row` con acceso por nombre e índice; y **traduce las excepciones de
  psycopg a las de `sqlite3` por nombre** (`_errores_como_sqlite3`,
  `_equivalente_sqlite3`).

  > ⚠️ La traducción cubre los **nombres** de las excepciones, no el
  > comportamiento transaccional. En PostgreSQL un error **aborta la
  > transacción** y en SQLite no: un `except IntegrityError` que después sigue
  > usando la misma conexión anda en uno y muere en el otro con *"current
  > transaction is aborted"*. Es una diferencia que la capa **no** puede tapar y
  > hay que mirar caso por caso al escribir reintentos.

- **`db/schema.py`** (`init_core_schema`) crea el schema común y aplica los
  defaults con reloj en hora Argentina (`defaults_con_reloj`,
  `alters_para_hora_ar`), distinguiendo motor (`_es_postgres`).
- **`db/schema_dump.py`** vuelca el schema de una base (`volcar_schema`, con
  ramas `_volcar_sqlite`/`_volcar_postgres` y un `main()` CLI). Es la razón
  concreta por la que `core` no puede cerrarse a PostgreSQL: abre un SQLite viejo
  o el de LibraEdge en modo `solo_lectura` para compararlo.

### `libracore.migrar` + `libracore/migrations/` — migraciones del schema común

Cadena Alembic **empaquetada dentro del wheel** (`migrations/` vive adentro de
`libracore/`, no en la raíz del repo). Expuesta como el console script
`libracore-migrar` y como API (`from libracore.migrar import upgrade`). Es el
espejo de `libragenda.migrar` con una diferencia: el destino no sale de
`DATABASE_URL` a secas sino de `url_de_core(prefijo, entorno)`, que resuelve la
URL de la instancia. Los errores tipados `SinURL`/`SinAlembic` distinguen "no me
dijiste contra qué base" de "no hay cadena que aplicar".

> 🔴 Que las migraciones viajen en el wheel no es cosmético. Mientras vivieron
> fuera de `packages` no viajaban al contenedor: de 14 bases con schema de
> LibraCore, 7 no tenían `alembic_version` y 2 quedaron en `0001_baseline`. No
> reventaban sólo porque `db/clients` introspecta la tabla en cada alta y escribe
> únicamente las columnas presentes. Empaquetarlas cerró esa fuga.

### `libracore.provisioning` — aprovisionamiento de instancias

- **`provisioning/panel_admin.py`** (1645 líneas): operación de las instancias
  Docker. Recorre `clientes/*/cliente.json`, levanta/actualiza contenedores
  (`docker`/`compose`), consulta estado e imagen del contenedor
  (`container_status`, `container_image`, `container_image_id`), pinea y lee la
  imagen desplegada (`pinear_image`, `leer_image_pineada`). Es el motor del
  comando `panel_admin.py actualizar` de cada VPS.
- **`provisioning/nuevo_cliente.py`** (1145 líneas): alta de una instancia nueva
  — `slugify`, asignación de puerto libre (`used_ports`/`next_port`), build de
  imagen (`build_image`/`version_para_cliente_nuevo`), red Docker, y el alta del
  proxy en NPM (`_setup_npm_proxy`). Valida el CUIT (`cuit_valido`).
- **`provisioning/mail_cuentas.py`**, **`resguardo_externo.py`**: cuentas de
  correo y respaldo externo de la instancia.

> Estos módulos se consumen **por atributo**, no por import directo de cada
> función: `libracore.admin.services` hace `import panel_admin as pa` y llama
> `pa.find_client(...)`. Es la razón por la que un `ruff --fix` de F401 sobre los
> backoffices rompía re-exports que parecían no usados — por eso el lint deja F401
> en el ignore para estos re-exports.

### `libracore.admin` — backoffice de superadmin embebido

App FastAPI mínima (`admin/app.py`) con sus servicios (`admin/services.py`, 481
líneas) que envuelven `provisioning`: `configure(repo_root, db_filename)` fija el
contexto, y `_pa()`/`_nc()` exponen los módulos de provisioning ya cargados. Es
lo que los backoffices `admin.<producto>.com.ar` montan; el gating y el
enriquecimiento del listado de clientes (`_enrich`) viven acá.

### Facturación ARCA (AFIP) — `arca_*`

Subsistema de facturación electrónica argentina: `arca_wsaa` (autenticación
WSAA), `arca_wsfe` (comprobantes WSFE), `arca_wspadron` (padrón),
`arca_certificados`/`arca_credenciales` (certificados y credenciales por
empresa/ambiente), `arca_facturacion` (orquestación) y `arca_router`
(`build_arca_router(...)`, router FastAPI parametrizable con estado del par
cert/clave por ambiente homologación/producción). `facturas_router` monta el
alta de comprobante, cálculo de totales (`calcular_totales`), cobro y envío por
mail; `facturas_borrador`, `comprobantes_pendientes`/`comprobantes_router`,
`cc_resumen`/`resumen_router`, `recibos`, `cobros`, `pagos` completan el flujo
contable.

### MercadoPago — `mp_*`

`mp_api` (cliente), `mp_config_router`/`mp_bandeja_router`/`mp_webhook`
(configuración, bandeja y webhook como routers), `mp_sync` (sincronización) y
`mp_facturacion` (puente cobro→comprobante). El webhook (`mp_webhook`) es el más
consumido del grupo.

### Infra de instancia — `npm_api`, `respaldo`, `config_*`, `smtp_router`

- **`npm_api.py`** (`NPMClient`, `configure`, `client_from_config`): automatiza
  Nginx Proxy Manager (alta de proxy host, certificado Let's Encrypt) al crear o
  mover una instancia. `npm_setup` es el flujo de configuración inicial.
- **`respaldo.py`** / **`resguardo_estado.py`**: backup de la instancia.
- **`config_manager.py`** + **`config_router.py`**, **`smtp_router.py`**:
  configuración de la instancia y SMTP, expuestos como router.
- **`security_headers.py`**, **`modules_gate.py`** (gating de módulos por
  instancia, `db/modulos`), **`feriados.py`**, **`geografia.py`**,
  **`medios_pago.py`**, **`registro_de_clientes.py`**,
  **`db/url_de_instancia.py`** (resolución de la URL de la base por instancia, 17
  sitios).

### Generación de documentos — `pdf_generator`, `ticket_generator`

`pdf_generator` (14 sitios de import) arma los PDFs de comprobantes/recibos;
`ticket_generator` los tickets de caja/venta.

## Diseño dual SQLite/PostgreSQL

LibraCore mantiene **una sola** capa de acceso a datos que corre contra los dos
motores. No es deuda: es lo que permite que `schema_dump` lea un SQLite viejo o
la base de LibraEdge, y lo que traduce las excepciones de psycopg a las de
`sqlite3` para todos los consumidores a la vez, en un solo lugar.

La decisión de familia (2026-08-12, reforzada 2026-08-25) es **PostgreSQL en
producción, dev y tests** para los ocho productos; el rollback a SQLite dejó de
aplicar. Pero esa regla es de los **productos**: cada vertical trae en su
arranque una guarda que rechaza cualquier destino que no sea una URL PostgreSQL.
El motor sigue siendo neutral —`core.configure()` acepta ambos— precisamente para
que `schema_dump` y las migraciones sobre bases legadas sigan funcionando, y para
que LibraEdge pueda correr el producto entero con su PostgreSQL embebido. La
separación es intencional: **la restricción vive en el producto, la capacidad en
el motor.**

Al levantar un PostgreSQL para tests hay que usar la **misma imagen que
producción** del producto que consume el motor (`postgres:16-alpine` para
LibraDesk, `postgres:16` para el resto): el collation viene de la imagen y alpine
ordena por bytes.

## Puertos que consumen los productos

Medido sobre los ocho verticales (`from libracore… / import libracore…`), los
puntos de integración reales, de mayor a menor uso:

| Puerto | Sitios | Qué aporta |
|---|---:|---|
| `libracore.db` (y submódulos) | 90 | acceso a datos por dominio |
| `libracore.provisioning` | 47 | operación y alta de instancias |
| `libracore.db.core` | 29 | conexión configurable + fecha/hora AR |
| `libracore.db.url_de_instancia` | 17 | URL de la base por instancia |
| `libracore.npm_api` | 16 | automatización de NPM |
| `libracore.db.schema` | 15 | schema común |
| `libracore.pdf_generator` | 14 | PDFs de comprobantes |
| `libracore.config_router` / `respaldo` / `smtp_router` / `npm_setup` | 8 c/u | routers e infra de instancia |
| `libracore.facturas_router` / `mp_webhook` / `recibos` / `arca_router` | 6-7 | facturación, MP, ARCA |

No todos los productos consumen todo: un vertical monta el router ARCA sólo si
factura, el de MercadoPago sólo si cobra online. LibraCore es un **menú de piezas
componibles**, no un framework que impone una app.

## Versionado y distribución

Paquete versionado (`importlib.metadata`), instalado por los productos como
dependencia git pineada al tag (`git+https://…@vX.Y.Z`). Extras `dev` y
`migrations`. La automatización de bumps de motor (ver `libra-web-kit` y la
entidad `libra-bump` del wiki) detecta cuándo un producto quedó atrás del último
tag de LibraCore y abre el PR de actualización.

## Referencias

- `README.md`, `docs/` — documentación operativa del paquete.
- Wiki del ecosistema (contexto transversal, decisiones y su historia): entidad
  `libracore`, `concepts/estandares-desarrollo`, y la auditoría estructural
  `auditoria-estructural-familia-libra-2026-09`. Las decisiones de diseño que
  este documento apoya (dual-backend, PostgreSQL-only en los productos, F401 en
  re-exports, migraciones empaquetadas, guarda de motor en el producto) viven
  ahí; un `DECISIONS.md` propio en formato ADR queda pendiente si se replica el
  patrón de gestiolibra al resto de los motores.
